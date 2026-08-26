from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
from uuid import UUID

import gi
import trio

from .desktop import run_action
from .settings import Settings, load, save, store_api_key

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib, Gtk


_APPLICATION_ID = "net.kalny.ImmichOnDemand"
_PROGRAM_NAME = "immich-on-demand-desktop"
_PIN_POLL_SECONDS = 0.5
_ACTION_WAIT_SECONDS = 300
_RESULT_MESSAGES = {
    "status-ok": "Service is running.",
    "status-error": "Could not query service.",
    "refresh-ok": "Refresh requested.",
    "refresh-error": "Could not request refresh.",
    "evict-ok": "Eviction requested.",
    "evict-error": "Could not request eviction.",
    "pin-error": "Could not request Pin.",
    "pin-cached": "Pinned and cached.",
    "pin-retry": "Pin saved; download needs retry.",
    "pin-cancelled": "Pin was removed by another client.",
    "pin-unknown": "Pin saved; could not confirm download.",
    "pin-timeout": "Pin saved; download is still running.",
    "unpin-ok": "Pin removed.",
    "unpin-cached": "Pin removed; cached copy retained.",
    "unpin-error": "Could not remove Pin.",
}


def _parse_action(arguments: list[str]) -> tuple[str | None, str | None]:
    if not arguments:
        return None, None
    if arguments in (["--action", "status"], ["--action", "refresh"]):
        return arguments[1], None
    if len(arguments) == 4 and arguments[0] == "--action" and arguments[2] == "--uri":
        if arguments[1] in {"evict", "pin", "unpin"}:
            return arguments[1], arguments[3]
    raise ValueError("invalid desktop action")


def _relay_result(name: str) -> None:
    if name not in _RESULT_MESSAGES:
        raise ValueError("invalid desktop result")
    try:
        subprocess.Popen(
            [_PROGRAM_NAME, "--result", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _described_pin(response: object, uri: str) -> dict[str, object] | None:
    if not isinstance(response, dict) or set(response) != {"items"}:
        return None
    items = response["items"]
    expected = {"uri", "cached", "pinned", "busy", "recoverable"}
    if not isinstance(items, list) or len(items) != 1:
        return None
    item = items[0]
    if (
        not isinstance(item, dict)
        or set(item) != expected
        or item.get("uri") != uri
        or any(type(item.get(name)) is not bool for name in expected - {"uri"})
    ):
        return None
    return item


async def _run_action_command(action: str, uri: str | None) -> int:
    try:
        result = await run_action(action, uri)
    except Exception:
        _relay_result(f"{action}-error")
        return 1

    if action == "status":
        expected = {
            "total",
            "visible",
            "missing_size",
            "trashed",
            "hidden",
            "offline",
            "mutation_enabled",
        }
        valid = (
            isinstance(result, dict)
            and set(result) == expected
            and type(result["mutation_enabled"]) is bool
            and all(
                type(result[name]) is int
                for name in expected - {"mutation_enabled"}
            )
        )
        _relay_result("status-ok" if valid else "status-error")
        return 0 if valid else 1
    if action == "refresh":
        valid = result == {"scheduled": True}
        _relay_result("refresh-ok" if valid else "refresh-error")
        return 0 if valid else 1
    if action == "evict":
        valid = (
            isinstance(result, dict)
            and set(result) == {"evicted"}
            and type(result["evicted"]) is bool
        )
        _relay_result("evict-ok" if valid else "evict-error")
        return 0 if valid else 1

    if (
        action not in {"pin", "unpin"}
        or not isinstance(result, dict)
        or set(result) != {"pinned", "cached", "busy", "scheduled"}
        or any(type(result.get(name)) is not bool for name in result)
        or uri is None
    ):
        _relay_result(f"{action}-error")
        return 1
    if action == "unpin" and result["pinned"] is not False:
        _relay_result("unpin-error")
        return 1
    if action == "unpin":
        _relay_result("unpin-cached" if result["cached"] else "unpin-ok")
        return 0
    if action == "pin" and result["pinned"] is not True:
        _relay_result("pin-error")
        return 1
    if action == "pin" and result["cached"] is True and result["busy"] is False:
        _relay_result("pin-cached")
        return 0
    with trio.move_on_after(_ACTION_WAIT_SECONDS) as wait:
        while True:
            response = None
            try:
                response = await run_action("describe", [uri])
                state = _described_pin(response, uri)
            except Exception:
                state = None
            if state is None:
                _relay_result("pin-unknown")
                return 1
            if state["busy"] is True:
                await trio.sleep(_PIN_POLL_SECONDS)
                continue
            if state["pinned"] is False:
                _relay_result("pin-cancelled")
                return 0
            if state["cached"] is True:
                _relay_result("pin-cached")
                return 0
            _relay_result("pin-retry")
            return 1
    assert wait.cancelled_caught
    _relay_result("pin-timeout")
    return 124


class _Worker:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="immich-on-demand-desktop",
        )
        self._lock = threading.Lock()
        self._pending = 0
        self._closed = False

    def submit(self, operation, callback) -> bool:
        with self._lock:
            if self._closed or self._pending >= 2:
                return False
            self._pending += 1
            future = self._executor.submit(operation)
        future.add_done_callback(
            lambda completed: self._complete(completed, callback)
        )
        return True

    def _complete(self, future: Future, callback) -> None:
        try:
            result = future.result()
            success = True
        except Exception:
            result = None
            success = False
        with self._lock:
            self._pending -= 1
            if self._closed:
                return
        GLib.idle_add(callback, success, result)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


class DesktopApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=_APPLICATION_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._worker = _Worker()
        self._window = None
        self._entries: dict[str, object] = {}
        self._remote_delete = None
        self._restore_entry = None
        self._restore_button = None
        self._message = None
        self._save_button = None

    def do_activate(self) -> None:
        if self._window is None:
            self._build_window()
            self._message.set_text("Loading settings.")
            if not self._worker.submit(load, self._finish_load):
                self._message.set_text("Desktop worker is busy.")
        self._window.present()

    def do_command_line(self, command_line) -> int:
        arguments = list(command_line.get_arguments())[1:]
        result = (
            arguments[1]
            if len(arguments) == 2
            and arguments[0] == "--result"
            and arguments[1] in _RESULT_MESSAGES
            else None
        )
        if arguments and result is None:
            self.activate()
            self._message.set_text("Invalid desktop action.")
            return 2
        self.activate()
        if result is not None:
            self._message.set_text(_RESULT_MESSAGES[result])
        return 0

    def do_shutdown(self) -> None:
        self._worker.close()
        super().do_shutdown()

    def _build_window(self) -> None:
        self._window = Adw.ApplicationWindow(
            application=self,
            title="Immich On-Demand",
            default_width=560,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(Adw.HeaderBar())
        grid = Gtk.Grid(
            column_spacing=12,
            row_spacing=8,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        content.append(grid)

        fields = (
            ("server_url", "Server URL", True),
            ("mount_path", "Mount path", True),
            ("cache_max_bytes", "Cache limit (bytes)", True),
            ("cache_max_age_seconds", "Cache maximum age (seconds)", True),
            ("minimum_free_bytes", "Minimum free space (bytes)", True),
            ("refresh_seconds", "Refresh interval (seconds)", True),
            ("read_only_key", "Read-only API key", False),
            ("mutation_key", "Mutation API key", False),
        )
        for row, (name, label, visible) in enumerate(fields):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            entry = Gtk.Entry(hexpand=True)
            entry.set_visibility(visible)
            grid.attach(entry, 1, row, 1, 1)
            self._entries[name] = entry

        self._remote_delete = Gtk.CheckButton(label="Enable remote deletion")
        grid.attach(self._remote_delete, 1, len(fields), 1, 1)
        grid.attach(
            Gtk.Label(label="Restore asset UUID", xalign=0),
            0,
            len(fields) + 1,
            1,
            1,
        )
        restore_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._restore_entry = Gtk.Entry(hexpand=True)
        restore_controls.append(self._restore_entry)
        self._restore_button = Gtk.Button(label="Restore Asset")
        self._restore_button.connect("clicked", self._restore_asset)
        restore_controls.append(self._restore_button)
        grid.attach(restore_controls, 1, len(fields) + 1, 1, 1)
        self._message = Gtk.Label(label="", xalign=0)
        grid.attach(self._message, 0, len(fields) + 2, 2, 1)
        self._save_button = Gtk.Button(label="Save Settings")
        self._save_button.connect("clicked", self._save_settings)
        grid.attach(self._save_button, 1, len(fields) + 3, 1, 1)
        self._window.set_content(content)

    def _finish_load(self, success: bool, result) -> bool:
        if not success or not isinstance(result, Settings):
            if self._message.get_text() == "Loading settings.":
                self._message.set_text("Could not load settings.")
            return False
        values = {
            "server_url": result.server_url,
            "mount_path": str(result.mount_path),
            "cache_max_bytes": str(result.cache_max_bytes),
            "cache_max_age_seconds": str(result.cache_max_age_seconds),
            "minimum_free_bytes": str(result.minimum_free_bytes),
            "refresh_seconds": str(result.refresh_seconds),
        }
        for name, value in values.items():
            self._entries[name].set_text(value)
        self._remote_delete.set_active(result.remote_delete)
        if self._message.get_text() == "Loading settings.":
            self._message.set_text("Settings loaded.")
        return False

    def _settings_from_form(self) -> Settings:
        return Settings(
            self._entries["server_url"].get_text().strip(),
            Path(self._entries["mount_path"].get_text().strip()),
            cache_max_bytes=int(self._entries["cache_max_bytes"].get_text()),
            cache_max_age_seconds=int(
                self._entries["cache_max_age_seconds"].get_text()
            ),
            minimum_free_bytes=int(
                self._entries["minimum_free_bytes"].get_text()
            ),
            refresh_seconds=int(self._entries["refresh_seconds"].get_text()),
            remote_delete=self._remote_delete.get_active(),
        )

    def _save_settings(self, _button) -> None:
        try:
            settings = self._settings_from_form()
        except (TypeError, ValueError):
            self._message.set_text("Settings are invalid.")
            return
        read_only_key = self._entries["read_only_key"].get_text()
        mutation_key = self._entries["mutation_key"].get_text()

        def persist() -> Settings:
            if read_only_key:
                store_api_key(settings, "read-only", read_only_key)
            if mutation_key:
                store_api_key(settings, "mutation", mutation_key)
            save(settings)
            return settings

        if not self._worker.submit(persist, self._finish_save):
            self._message.set_text("Desktop worker is busy.")
            return
        self._entries["read_only_key"].set_text("")
        self._entries["mutation_key"].set_text("")
        self._message.set_text("Saving settings.")

    def _finish_save(self, success: bool, _result) -> bool:
        self._message.set_text(
            "Settings saved." if success else "Could not save settings."
        )
        return False

    def _restore_asset(self, _button) -> None:
        try:
            asset_id = str(UUID(self._restore_entry.get_text().strip()))
        except ValueError:
            self._message.set_text("Restore asset UUID is invalid.")
            return

        def restore():
            return trio.run(run_action, "restore", asset_id)

        if not self._worker.submit(restore, self._finish_restore):
            self._message.set_text("Desktop worker is busy.")
            return
        self._restore_entry.set_text("")
        self._message.set_text("Requesting restore.")

    def _finish_restore(self, success: bool, result) -> bool:
        confirmed = (
            success
            and isinstance(result, dict)
            and set(result) == {"restored", "scheduled"}
            and result["restored"] is True
            and result["scheduled"] is True
        )
        self._message.set_text(
            "Restore requested." if confirmed else "Could not restore asset."
        )
        return False


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "--action":
        try:
            action, uri = _parse_action(arguments)
        except ValueError:
            return 2
        assert action is not None
        return trio.run(_run_action_command, action, uri)
    return DesktopApplication().run([_PROGRAM_NAME, *arguments])
