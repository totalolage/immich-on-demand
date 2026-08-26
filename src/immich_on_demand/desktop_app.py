from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading
from uuid import UUID

import gi
import trio

from .desktop import run_action
from .profiles import Profile, manage_profile, profiles, select_profile
from .settings import Settings, load, save, store_api_key

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib, Gtk


_APPLICATION_ID = "net.kalny.ImmichOnDemand"
_PROGRAM_NAME = "immich-on-demand-desktop"
_PIN_POLL_SECONDS = 0.5
_ACTION_WAIT_SECONDS = 300
_UPLOAD_STATES = frozenset(
    {"writing", "pending", "attempting", "committed", "blocked", "cancelled"}
)
_UPLOAD_ERRORS = frozenset(
    {
        "interrupted-write",
        "local-write-failed",
        "upload-unavailable",
        "upload-rejected",
        "ambiguous-response",
        "candidate-mismatch",
        "profile-mismatch",
        "payload-invalid",
        "local-state-failed",
    }
)
_RESULT_MESSAGES = {
    "status-online": "Service is online.",
    "status-offline": "Service is offline; cached files remain available.",
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


def _relay_result(profile: Profile, name: str) -> None:
    if name not in _RESULT_MESSAGES:
        raise ValueError("invalid desktop result")
    try:
        subprocess.Popen(
            [_PROGRAM_NAME, "--profile", profile.id, "--result", name],
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


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _upload_page(response: object) -> tuple[list[dict[str, object]], str | None]:
    expected = {"id", "name", "state", "size", "error", "revision"}
    if (
        not isinstance(response, dict)
        or set(response) != {"items", "next"}
        or not isinstance(response["items"], list)
        or len(response["items"]) > 32
    ):
        raise ValueError("invalid Pending uploads response")
    items = response["items"]
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != expected
            or not _canonical_uuid(item["id"])
            or not isinstance(item["name"], str)
            or item["state"] not in _UPLOAD_STATES
            or not (
                item["size"] is None
                or (type(item["size"]) is int and item["size"] >= 0)
            )
            or not (item["error"] is None or item["error"] in _UPLOAD_ERRORS)
            or type(item["revision"]) is not int
            or item["revision"] < 0
        ):
            raise ValueError("invalid Pending uploads response")
    next_id = response.get("next")
    if next_id is not None and not _canonical_uuid(next_id):
        raise ValueError("invalid Pending uploads response")
    return items, next_id


async def _fetch_uploads(profile: Profile) -> str:
    after: str | None = None
    seen: set[str] = set()
    lines: list[str] = []
    while True:
        items, next_id = _upload_page(
            await run_action(profile, "uploads", after)
        )
        lines.extend(
            " ".join(
                (
                    f"id={item['id']}",
                    f"name={json.dumps(item['name'], ensure_ascii=False)}",
                    f"state={item['state']}",
                    f"size={json.dumps(item['size'])}",
                    f"error={json.dumps(item['error'], ensure_ascii=False)}",
                    f"revision={item['revision']}",
                )
            )
            for item in items
        )
        if next_id is None:
            return "\n".join(lines)
        if next_id in seen:
            raise ValueError("invalid Pending uploads response")
        seen.add(next_id)
        after = next_id


async def _run_action_command(
    profile: Profile, action: str, uri: str | None
) -> int:
    try:
        result = await run_action(profile, action, uri)
    except Exception:
        _relay_result(profile, f"{action}-error")
        return 1

    if action == "status":
        expected = {
            "total",
            "visible",
            "missing_size",
            "trashed",
            "hidden",
            "offline",
            "pending_uploads",
            "upload_quarantined",
            "online",
            "mutation_enabled",
        }
        booleans = {"online", "mutation_enabled"}
        valid = (
            isinstance(result, dict)
            and set(result) == expected
            and all(type(result[name]) is bool for name in booleans)
            and all(type(result[name]) is int for name in expected - booleans)
        )
        _relay_result(
            profile,
            f"status-{'online' if result['online'] else 'offline'}"
            if valid
            else "status-error"
        )
        return 0 if valid else 1
    if action == "refresh":
        valid = result == {"scheduled": True}
        _relay_result(profile, "refresh-ok" if valid else "refresh-error")
        return 0 if valid else 1
    if action == "evict":
        valid = (
            isinstance(result, dict)
            and set(result) == {"evicted"}
            and type(result["evicted"]) is bool
        )
        _relay_result(profile, "evict-ok" if valid else "evict-error")
        return 0 if valid else 1

    if (
        action not in {"pin", "unpin"}
        or not isinstance(result, dict)
        or set(result) != {"pinned", "cached", "busy", "scheduled"}
        or any(type(result.get(name)) is not bool for name in result)
        or uri is None
    ):
        _relay_result(profile, f"{action}-error")
        return 1
    if action == "unpin" and result["pinned"] is not False:
        _relay_result(profile, "unpin-error")
        return 1
    if action == "unpin":
        _relay_result(
            profile, "unpin-cached" if result["cached"] else "unpin-ok"
        )
        return 0
    if action == "pin" and result["pinned"] is not True:
        _relay_result(profile, "pin-error")
        return 1
    if action == "pin" and result["cached"] is True and result["busy"] is False:
        _relay_result(profile, "pin-cached")
        return 0
    with trio.move_on_after(_ACTION_WAIT_SECONDS) as wait:
        while True:
            response = None
            try:
                response = await run_action(profile, "describe", [uri])
                state = _described_pin(response, uri)
            except Exception:
                state = None
            if state is None:
                _relay_result(profile, "pin-unknown")
                return 1
            if state["busy"] is True:
                await trio.sleep(_PIN_POLL_SECONDS)
                continue
            if state["pinned"] is False:
                _relay_result(profile, "pin-cancelled")
                return 0
            if state["cached"] is True:
                _relay_result(profile, "pin-cached")
                return 0
            _relay_result(profile, "pin-retry")
            return 1
    assert wait.cancelled_caught
    _relay_result(profile, "pin-timeout")
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
        self._profile: Profile | None = None
        self._profiles: tuple[Profile, ...] = ()
        self._profile_generation = 0
        self._profile_selector = None
        self._profile_controls: list[object] = []
        self._entries: dict[str, object] = {}
        self._remote_delete = None
        self._restore_entry = None
        self._restore_button = None
        self._uploads_label = None
        self._uploads_refresh_button = None
        self._upload_id_entry = None
        self._upload_name_entry = None
        self._upload_revision_entry = None
        self._retry_upload_button = None
        self._cancel_upload_button = None
        self._message = None
        self._save_button = None

    def do_activate(self) -> None:
        if self._window is None:
            self._build_window()
            self._message.set_text("Select a Profile.")
        self._window.present()

    def do_command_line(self, command_line) -> int:
        arguments = list(command_line.get_arguments())[1:]
        profile = None
        result = None
        try:
            if arguments:
                if len(arguments) < 2 or arguments[0] != "--profile":
                    raise ValueError
                profile = select_profile(arguments[1])
                remaining = arguments[2:]
                if remaining:
                    if (
                        len(remaining) != 2
                        or remaining[0] != "--result"
                        or remaining[1] not in _RESULT_MESSAGES
                    ):
                        raise ValueError
                    result = remaining[1]
        except (RuntimeError, ValueError):
            self.activate()
            self._message.set_text("Invalid desktop action.")
            return 2
        self.activate()
        if profile is not None:
            if (
                result is not None
                and self._profile is not None
                and self._profile != profile
            ):
                return 0
            if not self._choose_profile(profile):
                self._message.set_text("Profile is not available.")
                return 2
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

        try:
            self._profiles = profiles()
            choices = ["Select a Profile", *(profile.id for profile in self._profiles)]
        except Exception:
            self._profiles = ()
            choices = ["Could not list Profiles"]
        grid.attach(Gtk.Label(label="Profile", xalign=0), 0, 0, 1, 1)
        self._profile_selector = Gtk.DropDown(model=Gtk.StringList.new(choices))
        self._profile_selector.connect("notify::selected", self._profile_changed)
        grid.attach(self._profile_selector, 1, 0, 1, 1)

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
        for row, (name, label, visible) in enumerate(fields, start=1):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            entry = Gtk.Entry(hexpand=True)
            entry.set_visibility(visible)
            grid.attach(entry, 1, row, 1, 1)
            self._entries[name] = entry
            self._profile_controls.append(entry)

        self._remote_delete = Gtk.CheckButton(label="Enable remote deletion")
        self._profile_controls.append(self._remote_delete)
        grid.attach(self._remote_delete, 1, len(fields) + 1, 1, 1)
        grid.attach(
            Gtk.Label(label="Restore asset UUID", xalign=0),
            0,
            len(fields) + 2,
            1,
            1,
        )
        restore_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._restore_entry = Gtk.Entry(hexpand=True)
        restore_controls.append(self._restore_entry)
        self._restore_button = Gtk.Button(label="Restore Asset")
        self._restore_button.connect("clicked", self._restore_asset)
        self._profile_controls.extend((self._restore_entry, self._restore_button))
        restore_controls.append(self._restore_button)
        grid.attach(restore_controls, 1, len(fields) + 2, 1, 1)
        grid.attach(
            Gtk.Label(label="Pending uploads", xalign=0),
            0,
            len(fields) + 3,
            1,
            1,
        )
        self._uploads_refresh_button = Gtk.Button(label="Refresh")
        self._uploads_refresh_button.connect("clicked", self._refresh_uploads)
        self._profile_controls.append(self._uploads_refresh_button)
        grid.attach(self._uploads_refresh_button, 1, len(fields) + 3, 1, 1)
        self._uploads_label = Gtk.Label(
            label="Pending uploads not loaded.", xalign=0, selectable=True
        )
        grid.attach(self._uploads_label, 0, len(fields) + 4, 2, 1)
        grid.attach(
            Gtk.Label(label="Pending upload UUID", xalign=0),
            0,
            len(fields) + 5,
            1,
            1,
        )
        self._upload_id_entry = Gtk.Entry(hexpand=True)
        self._profile_controls.append(self._upload_id_entry)
        grid.attach(self._upload_id_entry, 1, len(fields) + 5, 1, 1)
        grid.attach(
            Gtk.Label(label="Current revision", xalign=0),
            0,
            len(fields) + 6,
            1,
            1,
        )
        self._upload_revision_entry = Gtk.Entry(hexpand=True)
        self._profile_controls.append(self._upload_revision_entry)
        grid.attach(self._upload_revision_entry, 1, len(fields) + 6, 1, 1)
        grid.attach(
            Gtk.Label(label="Exact name for Cancel", xalign=0),
            0,
            len(fields) + 7,
            1,
            1,
        )
        self._upload_name_entry = Gtk.Entry(hexpand=True)
        self._profile_controls.append(self._upload_name_entry)
        grid.attach(self._upload_name_entry, 1, len(fields) + 7, 1, 1)
        upload_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._retry_upload_button = Gtk.Button(label="Retry Upload")
        self._retry_upload_button.connect("clicked", self._retry_upload)
        upload_controls.append(self._retry_upload_button)
        self._cancel_upload_button = Gtk.Button(label="Cancel Upload")
        self._cancel_upload_button.connect("clicked", self._cancel_upload)
        self._profile_controls.extend(
            (self._retry_upload_button, self._cancel_upload_button)
        )
        upload_controls.append(self._cancel_upload_button)
        grid.attach(upload_controls, 1, len(fields) + 8, 1, 1)
        self._message = Gtk.Label(label="", xalign=0)
        grid.attach(self._message, 0, len(fields) + 9, 2, 1)
        self._save_button = Gtk.Button(label="Save Settings")
        self._save_button.connect("clicked", self._save_settings)
        self._profile_controls.append(self._save_button)
        grid.attach(self._save_button, 1, len(fields) + 10, 1, 1)
        self._window.set_content(content)
        self._set_profile_controls_sensitive(False)

    def _set_profile_controls_sensitive(self, sensitive: bool) -> None:
        for control in self._profile_controls:
            control.set_sensitive(sensitive)

    def _clear_profile_form(self) -> None:
        for entry in self._entries.values():
            entry.set_text("")
        self._remote_delete.set_active(False)
        self._uploads_label.set_text("Pending uploads not loaded.")
        self._clear_pending_upload_fields()

    def _profile_changed(self, selector, _parameter) -> None:
        selected = selector.get_selected()
        profile = self._profiles[selected - 1] if selected else None
        self._select_profile(profile)

    def _choose_profile(self, profile: Profile) -> bool:
        for index, candidate in enumerate(self._profiles, start=1):
            if candidate == profile:
                self._profile_selector.set_selected(index)
                return True
        return False

    def _select_profile(self, profile: Profile | None) -> None:
        self._profile_generation += 1
        generation = self._profile_generation
        self._profile = profile
        self._clear_profile_form()
        self._set_profile_controls_sensitive(False)
        if profile is None:
            self._message.set_text("Select a Profile.")
            return
        self._message.set_text(f"Loading Profile {profile.id}.")
        if not self._worker.submit(
            lambda: load(profile.config / "config.json"),
            lambda success, result: self._finish_load(
                profile, generation, success, result
            ),
        ):
            self._message.set_text("Desktop worker is busy.")

    def _capture_profile(self) -> tuple[Profile, int] | None:
        if self._profile is None:
            self._message.set_text("Select a Profile.")
            return None
        return self._profile, self._profile_generation

    def _is_current(self, profile: Profile, generation: int) -> bool:
        return self._profile == profile and self._profile_generation == generation

    def _finish_load(
        self,
        profile: Profile,
        generation: int,
        success: bool,
        result,
    ) -> bool:
        if self._profile != profile or self._profile_generation != generation:
            return False
        if not success or not isinstance(result, Settings):
            if self._message.get_text() == f"Loading Profile {profile.id}.":
                self._message.set_text(f"Could not load Profile {profile.id}.")
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
        self._set_profile_controls_sensitive(True)
        if self._message.get_text() == f"Loading Profile {profile.id}.":
            self._message.set_text(f"Profile {profile.id} loaded.")
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
        captured = self._capture_profile()
        if captured is None:
            return
        profile, generation = captured
        try:
            settings = self._settings_from_form()
        except (TypeError, ValueError):
            self._message.set_text("Settings are invalid.")
            return
        read_only_key = self._entries["read_only_key"].get_text()
        mutation_key = self._entries["mutation_key"].get_text()

        def persist() -> Settings:
            with manage_profile(profile, settings.mount_path):
                save(settings, profile.config / "config.json")
                if read_only_key:
                    store_api_key(
                        settings,
                        "read-only",
                        read_only_key,
                        profile_id=profile.id,
                    )
                if mutation_key:
                    store_api_key(
                        settings,
                        "mutation",
                        mutation_key,
                        profile_id=profile.id,
                    )
            return settings

        if not self._worker.submit(
            persist,
            lambda success, result: self._finish_save(
                profile, generation, success, result
            ),
        ):
            self._message.set_text("Desktop worker is busy.")
            return
        self._entries["read_only_key"].set_text("")
        self._entries["mutation_key"].set_text("")
        self._message.set_text("Saving settings.")

    def _finish_save(
        self,
        profile: Profile,
        generation: int,
        success: bool,
        _result,
    ) -> bool:
        if not self._is_current(profile, generation):
            return False
        self._message.set_text(
            "Settings saved." if success else "Could not save settings."
        )
        return False

    def _refresh_uploads(self, _button=None) -> None:
        captured = self._capture_profile()
        if captured is None:
            return
        self._submit_upload_refresh(*captured)

    def _submit_upload_refresh(
        self, profile: Profile, generation: int
    ) -> None:
        if not self._is_current(profile, generation):
            return
        if not self._worker.submit(
            lambda: trio.run(_fetch_uploads, profile),
            lambda success, result: self._finish_upload_refresh(
                profile, generation, success, result
            ),
        ):
            self._uploads_label.set_text("Desktop worker is busy.")
            return
        self._uploads_label.set_text("Loading Pending uploads.")

    def _finish_upload_refresh(
        self,
        profile: Profile,
        generation: int,
        success: bool,
        result,
    ) -> bool:
        if not self._is_current(profile, generation):
            return False
        self._uploads_label.set_text(
            (result or "No Pending uploads.")
            if success and isinstance(result, str)
            else "Could not load Pending uploads."
        )
        return False

    def _pending_upload_id(self) -> str:
        upload_id = self._upload_id_entry.get_text()
        if not _canonical_uuid(upload_id):
            raise ValueError
        return upload_id

    def _clear_pending_upload_fields(self) -> None:
        self._upload_id_entry.set_text("")
        self._upload_name_entry.set_text("")
        self._upload_revision_entry.set_text("")

    def _retry_upload(self, _button) -> None:
        captured = self._capture_profile()
        if captured is None:
            return
        profile, generation = captured
        try:
            upload_id = self._pending_upload_id()
        except ValueError:
            self._message.set_text("Pending upload UUID is invalid.")
            return

        if not self._worker.submit(
            lambda: trio.run(run_action, profile, "retry-upload", upload_id),
            lambda success, result: self._finish_retry_upload(
                profile, generation, success, result, upload_id
            ),
        ):
            self._message.set_text("Desktop worker is busy.")
            return
        self._message.set_text("Requesting upload retry.")

    def _finish_retry_upload(
        self,
        profile: Profile,
        generation: int,
        success: bool,
        result,
        upload_id: str,
    ) -> bool:
        if not self._is_current(profile, generation):
            return False
        accepted = (
            success
            and isinstance(result, dict)
            and set(result) == {"id", "scheduled"}
            and result["id"] == upload_id
            and result["scheduled"] is True
        )
        if not accepted:
            self._message.set_text("Could not request upload retry.")
            return False
        self._clear_pending_upload_fields()
        self._message.set_text("Upload retry requested.")
        self._submit_upload_refresh(profile, generation)
        return False

    def _cancel_upload(self, _button) -> None:
        captured = self._capture_profile()
        if captured is None:
            return
        profile, generation = captured
        try:
            upload_id = self._pending_upload_id()
        except ValueError:
            self._message.set_text("Pending upload UUID is invalid.")
            return
        revision_text = self._upload_revision_entry.get_text().strip()
        try:
            revision = int(revision_text)
        except ValueError:
            revision = -1
        if revision < 0:
            self._message.set_text("Pending upload revision is invalid.")
            return
        confirm_name = self._upload_name_entry.get_text()
        if not confirm_name.strip():
            self._message.set_text("Pending upload confirmation name is required.")
            return

        if not self._worker.submit(
            lambda: trio.run(
                run_action,
                profile,
                "cancel-upload",
                upload_id,
                revision,
                confirm_name,
            ),
            lambda success, result: self._finish_cancel_upload(
                profile, generation, success, result, upload_id
            ),
        ):
            self._message.set_text("Desktop worker is busy.")
            return
        self._message.set_text("Requesting Pending upload cancellation.")

    def _finish_cancel_upload(
        self,
        profile: Profile,
        generation: int,
        success: bool,
        result,
        upload_id: str,
    ) -> bool:
        if not self._is_current(profile, generation):
            return False
        accepted = (
            success
            and isinstance(result, dict)
            and set(result) == {"id", "cancelled"}
            and result["id"] == upload_id
            and result["cancelled"] is True
        )
        if not accepted:
            self._message.set_text("Could not cancel Pending upload.")
            return False
        self._clear_pending_upload_fields()
        self._message.set_text("Pending upload cancelled.")
        self._submit_upload_refresh(profile, generation)
        return False

    def _restore_asset(self, _button) -> None:
        captured = self._capture_profile()
        if captured is None:
            return
        profile, generation = captured
        try:
            asset_id = str(UUID(self._restore_entry.get_text().strip()))
        except ValueError:
            self._message.set_text("Restore asset UUID is invalid.")
            return

        def restore():
            return trio.run(run_action, profile, "restore", asset_id)

        if not self._worker.submit(
            restore,
            lambda success, result: self._finish_restore(
                profile, generation, success, result
            ),
        ):
            self._message.set_text("Desktop worker is busy.")
            return
        self._restore_entry.set_text("")
        self._message.set_text("Requesting restore.")

    def _finish_restore(
        self,
        profile: Profile,
        generation: int,
        success: bool,
        result,
    ) -> bool:
        if not self._is_current(profile, generation):
            return False
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
    if (
        len(arguments) >= 3
        and arguments[0] == "--profile"
        and arguments[2] == "--action"
    ):
        try:
            profile = select_profile(arguments[1])
            action, uri = _parse_action(arguments[2:])
        except (RuntimeError, ValueError):
            return 2
        assert action is not None
        return trio.run(_run_action_command, profile, action, uri)
    if "--action" in arguments:
        return 2
    return DesktopApplication().run([_PROGRAM_NAME, *arguments])
