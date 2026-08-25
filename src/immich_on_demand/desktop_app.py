from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import sys
import threading

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


def _parse_action(arguments: list[str]) -> tuple[str | None, str | None]:
    if not arguments:
        return None, None
    if arguments in (["--action", "status"], ["--action", "refresh"]):
        return arguments[1], None
    if (
        len(arguments) == 4
        and arguments[:3] == ["--action", "evict", "--uri"]
    ):
        return "evict", arguments[3]
    raise ValueError("invalid desktop action")


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
        try:
            action, uri = _parse_action(list(command_line.get_arguments())[1:])
        except ValueError:
            self.activate()
            self._message.set_text("Invalid desktop action.")
            return 2
        self.activate()
        if action is not None:
            self._start_action(action, uri)
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
        self._message = Gtk.Label(label="", xalign=0)
        grid.attach(self._message, 0, len(fields) + 1, 2, 1)
        self._save_button = Gtk.Button(label="Save Settings")
        self._save_button.connect("clicked", self._save_settings)
        grid.attach(self._save_button, 1, len(fields) + 2, 1, 1)
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

    def _start_action(self, action: str, uri: str | None) -> None:
        def invoke():
            return trio.run(run_action, action, uri)

        callback = lambda success, result: self._finish_action(
            action, success, result
        )
        if not self._worker.submit(invoke, callback):
            self._message.set_text("Desktop worker is busy.")
            return
        self._message.set_text("Contacting service.")

    def _finish_action(self, action: str, success: bool, _result) -> bool:
        if success:
            message = {
                "status": "Service is running.",
                "refresh": "Refresh requested.",
                "evict": "Eviction requested.",
            }[action]
        else:
            message = {
                "status": "Could not query service.",
                "refresh": "Could not request refresh.",
                "evict": "Could not request eviction.",
            }[action]
        self._message.set_text(message)
        return False


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv if argv is None else [_PROGRAM_NAME, *argv]
    return DesktopApplication().run(arguments)
