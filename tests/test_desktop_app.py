from contextlib import contextmanager
import importlib
from pathlib import Path
import queue
import sys
import threading
import types
import unittest
from unittest import mock

from immich_on_demand.profiles import Profile
from immich_on_demand.settings import Settings


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
NEXT_ID = "87654321-4321-4321-8321-cba987654321"


def _profile(profile_id: str) -> Profile:
    root = Path("/profiles") / profile_id
    return Profile(
        profile_id,
        root / "config",
        root / "state",
        root / "data",
        root / "cache",
        root / "runtime",
    )


def _settings(name: str = "home") -> Settings:
    return Settings(
        f"https://{name}.example.test",
        Path(f"/mnt/{name}"),
        cache_max_bytes=123,
        cache_max_age_seconds=456,
        minimum_free_bytes=78,
        refresh_seconds=90,
        remote_delete=True,
    )


class _IdleQueue:
    def __init__(self) -> None:
        self.callbacks: queue.Queue[tuple[object, tuple[object, ...]]] = queue.Queue()
        self.call_threads: list[int] = []

    def idle_add(self, callback, *args):
        self.call_threads.append(threading.get_ident())
        self.callbacks.put((callback, args))
        return self.callbacks.qsize()

    def run_next(self) -> None:
        callback, args = self.callbacks.get(timeout=2)
        callback(*args)


class _Application:
    run_calls: list[list[str]] = []

    def __init__(self, **properties) -> None:
        self.properties = properties

    def activate(self) -> None:
        self.do_activate()

    def do_shutdown(self) -> None:
        pass

    def run(self, arguments: list[str]) -> int:
        self.run_calls.append(arguments)
        return 17


class _ApplicationWindow:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self.content = None
        self.present_count = 0

    def set_content(self, content) -> None:
        self.content = content

    def present(self) -> None:
        self.present_count += 1


class _Container:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self.children: list[object] = []

    def append(self, child) -> None:
        self.children.append(child)


class _Grid(_Container):
    def attach(self, child, *_position) -> None:
        self.children.append(child)


class _Entry:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self._text = ""
        self.visible = True
        self.sensitive = True

    def get_text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

    def set_visibility(self, visible: bool) -> None:
        self.visible = visible

    def set_sensitive(self, sensitive: bool) -> None:
        self.sensitive = sensitive


class _CheckButton:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self._active = False
        self.sensitive = True

    def get_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active

    def set_sensitive(self, sensitive: bool) -> None:
        self.sensitive = sensitive


class _Label:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self._text = properties.get("label", "")

    def get_text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text


class _Button:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self._callback = None
        self.sensitive = True

    def connect(self, signal: str, callback) -> None:
        if signal != "clicked":
            raise AssertionError(f"unexpected signal: {signal}")
        self._callback = callback

    def click(self) -> None:
        if self._callback is None:
            raise AssertionError("button has no callback")
        self._callback(self)

    def set_sensitive(self, sensitive: bool) -> None:
        self.sensitive = sensitive


class _StringList(list[str]):
    @classmethod
    def new(cls, values: list[str]) -> "_StringList":
        return cls(values)


class _DropDown:
    def __init__(self, **properties) -> None:
        self.model = properties["model"]
        self._selected = 0
        self._callback = None

    def connect(self, signal: str, callback) -> None:
        if signal != "notify::selected":
            raise AssertionError(f"unexpected signal: {signal}")
        self._callback = callback

    def get_selected(self) -> int:
        return self._selected

    def set_selected(self, selected: int) -> None:
        changed = selected != self._selected
        self._selected = selected
        if changed and self._callback is not None:
            self._callback(self, None)


class _CommandLine:
    def __init__(self, arguments: list[str]) -> None:
        self._arguments = arguments

    def get_arguments(self) -> list[str]:
        return self._arguments


def _load_desktop_app():
    idle = _IdleQueue()
    _Application.run_calls.clear()
    gi = types.ModuleType("gi")
    repository = types.ModuleType("gi.repository")
    gi.require_version = lambda _namespace, _version: None
    repository.Adw = types.SimpleNamespace(
        Application=_Application,
        ApplicationWindow=_ApplicationWindow,
        HeaderBar=_Container,
    )
    repository.Gio = types.SimpleNamespace(
        ApplicationFlags=types.SimpleNamespace(HANDLES_COMMAND_LINE=1)
    )
    repository.GLib = types.SimpleNamespace(idle_add=idle.idle_add)
    repository.Gtk = types.SimpleNamespace(
        Box=_Container,
        Button=_Button,
        CheckButton=_CheckButton,
        DropDown=_DropDown,
        Entry=_Entry,
        Grid=_Grid,
        Label=_Label,
        Orientation=types.SimpleNamespace(HORIZONTAL=1, VERTICAL=2),
        StringList=_StringList,
    )
    gi.repository = repository
    sys.modules.pop("immich_on_demand.desktop_app", None)
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        module = importlib.import_module("immich_on_demand.desktop_app")
    return module, idle


def _selected_application(module, idle, profile: Profile, settings: Settings):
    with (
        mock.patch.object(module, "profiles", return_value=(profile,)),
        mock.patch.object(module, "load", return_value=settings),
    ):
        application = module.DesktopApplication()
        application.do_activate()
        application._profile_selector.set_selected(1)
        idle.run_next()
    return application


class DesktopApplicationTests(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("immich_on_demand.desktop_app", None)

    def test_no_selection_performs_no_profile_operation(self) -> None:
        module, _idle = _load_desktop_app()
        profile = _profile("home")
        with (
            mock.patch.object(module, "profiles", return_value=(profile,)),
            mock.patch.object(module, "load") as load,
            mock.patch.object(module, "run_action") as run_action,
            mock.patch.object(module, "save") as save,
            mock.patch.object(module, "store_api_key") as store_api_key,
        ):
            application = module.DesktopApplication()
            application.do_activate()
            application._save_button.click()
            application._uploads_refresh_button.click()
            application._retry_upload_button.click()
            application._cancel_upload_button.click()
            application._restore_button.click()

        self.assertIsNone(application._profile)
        self.assertEqual(
            list(application._profile_selector.model),
            ["Select a Profile", "home"],
        )
        self.assertTrue(
            all(not control.sensitive for control in application._profile_controls)
        )
        self.assertEqual(application._message.get_text(), "Select a Profile.")
        load.assert_not_called()
        run_action.assert_not_called()
        save.assert_not_called()
        store_api_key.assert_not_called()
        application.do_shutdown()

    def test_selection_loads_exact_config_off_thread_and_enables_controls(self) -> None:
        module, idle = _load_desktop_app()
        profile = _profile("home")
        expected = _settings()
        calls: list[tuple[Path, int]] = []

        def load_profile(path: Path) -> Settings:
            calls.append((path, threading.get_ident()))
            return expected

        with (
            mock.patch.object(module, "profiles", return_value=(profile,)),
            mock.patch.object(module, "load", side_effect=load_profile),
        ):
            application = module.DesktopApplication()
            application.do_activate()
            application._profile_selector.set_selected(1)
            self.assertEqual(application._message.get_text(), "Loading Profile home.")
            idle.run_next()

        self.assertEqual(calls[0][0], profile.config / "config.json")
        self.assertNotEqual(calls[0][1], threading.get_ident())
        self.assertEqual(
            application._entries["server_url"].get_text(), expected.server_url
        )
        self.assertEqual(application._entries["mount_path"].get_text(), "/mnt/home")
        self.assertTrue(application._remote_delete.get_active())
        self.assertTrue(
            all(control.sensitive for control in application._profile_controls)
        )
        self.assertEqual(application._message.get_text(), "Profile home loaded.")
        application.do_shutdown()

    def test_profile_change_discards_stale_load_completion(self) -> None:
        module, idle = _load_desktop_app()
        home = _profile("home")
        work = _profile("work")

        def load_profile(path: Path) -> Settings:
            return _settings(path.parent.parent.name)

        with (
            mock.patch.object(module, "profiles", return_value=(home, work)),
            mock.patch.object(module, "load", side_effect=load_profile),
        ):
            application = module.DesktopApplication()
            application.do_activate()
            application._profile_selector.set_selected(1)
            application._profile_selector.set_selected(2)
            idle.run_next()
            self.assertEqual(application._entries["server_url"].get_text(), "")
            self.assertEqual(application._message.get_text(), "Loading Profile work.")
            idle.run_next()

        self.assertEqual(application._profile, work)
        self.assertEqual(
            application._entries["server_url"].get_text(),
            "https://work.example.test",
        )
        application._profile_selector.set_selected(0)
        self.assertIsNone(application._profile)
        self.assertEqual(application._entries["server_url"].get_text(), "")
        self.assertEqual(application._message.get_text(), "Select a Profile.")
        application.do_shutdown()

    def test_save_locks_mount_then_writes_config_before_nonblank_keys(self) -> None:
        module, idle = _load_desktop_app()
        profile = _profile("home")
        application = _selected_application(module, idle, profile, _settings())
        events: list[tuple[object, ...]] = []
        worker_threads: list[int] = []

        @contextmanager
        def manage(selected: Profile, mount_path: Path):
            events.append(("manage", selected, mount_path))
            yield selected
            events.append(("unlock", selected))

        def persist(settings: Settings, path: Path) -> None:
            worker_threads.append(threading.get_ident())
            events.append(("save", settings, path))

        def store(
            settings: Settings,
            purpose: str,
            secret: str,
            *,
            profile_id: str,
        ) -> None:
            worker_threads.append(threading.get_ident())
            events.append(("key", purpose, secret, profile_id, settings))

        application._entries["server_url"].set_text("https://new.example.test")
        application._entries["mount_path"].set_text("/mnt/new")
        application._entries["read_only_key"].set_text("read-secret")
        application._entries["mutation_key"].set_text("mutation-secret")
        with (
            mock.patch.object(module, "manage_profile", side_effect=manage),
            mock.patch.object(module, "save", side_effect=persist),
            mock.patch.object(module, "store_api_key", side_effect=store),
        ):
            application._save_button.click()
            self.assertEqual(application._entries["read_only_key"].get_text(), "")
            self.assertEqual(application._entries["mutation_key"].get_text(), "")
            idle.run_next()

        written = events[1][1]
        self.assertEqual(
            [event[0] for event in events],
            ["manage", "save", "key", "key", "unlock"],
        )
        self.assertEqual(events[0], ("manage", profile, Path("/mnt/new")))
        self.assertEqual(events[1], ("save", written, profile.config / "config.json"))
        self.assertEqual(
            [(event[1], event[2], event[3]) for event in events[2:4]],
            [
                ("read-only", "read-secret", "home"),
                ("mutation", "mutation-secret", "home"),
            ],
        )
        self.assertTrue(
            all(thread != threading.get_ident() for thread in worker_threads)
        )
        self.assertEqual(application._message.get_text(), "Settings saved.")
        application.do_shutdown()

    def test_key_failure_occurs_after_config_save_and_is_sanitized(self) -> None:
        module, idle = _load_desktop_app()
        profile = _profile("home")
        application = _selected_application(module, idle, profile, _settings())
        events: list[str] = []

        @contextmanager
        def manage(_profile: Profile, _mount_path: Path):
            yield

        def persist(_settings: Settings, _path: Path) -> None:
            events.append("save")

        def fail_store(*_args, **_kwargs) -> None:
            events.append("key")
            raise RuntimeError("api-key and private path")

        application._entries["read_only_key"].set_text("replacement")
        with (
            mock.patch.object(module, "manage_profile", side_effect=manage),
            mock.patch.object(module, "save", side_effect=persist),
            mock.patch.object(module, "store_api_key", side_effect=fail_store),
        ):
            application._save_button.click()
            idle.run_next()

        self.assertEqual(events, ["save", "key"])
        self.assertEqual(application._message.get_text(), "Could not save settings.")
        self.assertNotIn("api-key", application._message.get_text())
        application.do_shutdown()

    def test_stale_action_completion_cannot_change_new_profile(self) -> None:
        module, idle = _load_desktop_app()
        home = _profile("home")
        work = _profile("work")
        started = threading.Event()
        release = threading.Event()
        calls: list[tuple[Profile, str, object]] = []

        def load_profile(path: Path) -> Settings:
            return _settings(path.parent.parent.name)

        async def restore(profile: Profile, action: str, target=None):
            calls.append((profile, action, target))
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test timed out")
            return {"restored": True, "scheduled": True}

        with (
            mock.patch.object(module, "profiles", return_value=(home, work)),
            mock.patch.object(module, "load", side_effect=load_profile),
            mock.patch.object(module, "run_action", side_effect=restore),
        ):
            application = module.DesktopApplication()
            application.do_activate()
            application._profile_selector.set_selected(1)
            idle.run_next()
            application._restore_entry.set_text(ASSET_ID)
            application._restore_button.click()
            self.assertTrue(started.wait(timeout=2))
            application._profile_selector.set_selected(2)
            release.set()
            idle.run_next()
            self.assertEqual(application._message.get_text(), "Loading Profile work.")
            idle.run_next()

        self.assertEqual(calls, [(home, "restore", ASSET_ID)])
        self.assertEqual(application._profile, work)
        self.assertEqual(application._message.get_text(), "Profile work loaded.")
        application.do_shutdown()

    def test_pending_upload_pagination_captures_one_profile(self) -> None:
        module, _idle = _load_desktop_app()
        profile = _profile("home")
        calls: list[tuple[Profile, str, object]] = []
        pages = iter(
            (
                {
                    "items": [
                        {
                            "id": ASSET_ID,
                            "name": "First image.jpg",
                            "state": "blocked",
                            "size": None,
                            "error": "interrupted-write",
                            "revision": 2,
                        }
                    ],
                    "next": NEXT_ID,
                },
                {"items": [], "next": None},
            )
        )

        async def action(selected: Profile, name: str, target=None):
            calls.append((selected, name, target))
            return next(pages)

        with mock.patch.object(module, "run_action", side_effect=action):
            result = module.trio.run(module._fetch_uploads, profile)

        self.assertEqual(
            calls,
            [(profile, "uploads", None), (profile, "uploads", NEXT_ID)],
        )
        self.assertIn(f"id={ASSET_ID}", result)

    def test_retry_cancel_and_restore_keep_the_selected_profile(self) -> None:
        module, idle = _load_desktop_app()
        profile = _profile("home")
        application = _selected_application(module, idle, profile, _settings())
        calls: list[tuple[object, ...]] = []
        responses = iter(
            (
                {"id": ASSET_ID, "scheduled": True},
                {"items": [], "next": None},
                {"id": ASSET_ID, "cancelled": True},
                {"items": [], "next": None},
                {"restored": True, "scheduled": True},
            )
        )

        async def action(*arguments):
            calls.append(arguments)
            return next(responses)

        with mock.patch.object(module, "run_action", side_effect=action):
            application._upload_id_entry.set_text(ASSET_ID)
            application._retry_upload_button.click()
            idle.run_next()
            idle.run_next()

            application._upload_id_entry.set_text(ASSET_ID)
            application._upload_revision_entry.set_text("7")
            application._upload_name_entry.set_text("Exact name.jpg")
            application._cancel_upload_button.click()
            idle.run_next()
            idle.run_next()

            application._restore_entry.set_text(ASSET_ID.upper())
            application._restore_button.click()
            idle.run_next()

        self.assertEqual(
            calls,
            [
                (profile, "retry-upload", ASSET_ID),
                (profile, "uploads", None),
                (profile, "cancel-upload", ASSET_ID, 7, "Exact name.jpg"),
                (profile, "uploads", None),
                (profile, "restore", ASSET_ID),
            ],
        )
        self.assertEqual(application._message.get_text(), "Restore requested.")
        application.do_shutdown()

    def test_result_relay_argv_includes_profile_and_fixed_result(self) -> None:
        module, _idle = _load_desktop_app()
        profile = _profile("home")
        with mock.patch.object(module.subprocess, "Popen") as popen:
            module._relay_result(profile, "refresh-ok")

        popen.assert_called_once_with(
            [
                "immich-on-demand-desktop",
                "--profile",
                "home",
                "--result",
                "refresh-ok",
            ],
            stdin=module.subprocess.DEVNULL,
            stdout=module.subprocess.DEVNULL,
            stderr=module.subprocess.DEVNULL,
            start_new_session=True,
        )
        with self.assertRaises(ValueError):
            module._relay_result(profile, "not-a-result")

    def test_action_command_captures_profile_for_control_and_relay(self) -> None:
        module, _idle = _load_desktop_app()
        profile = _profile("home")
        response = {
            "online": True,
            "total": 7,
            "visible": 6,
            "missing_size": 1,
            "trashed": 0,
            "hidden": 0,
            "offline": 0,
            "pending_uploads": 2,
            "upload_quarantined": 1,
            "mutation_enabled": False,
        }
        calls: list[tuple[Profile, str, object]] = []
        relayed: list[tuple[Profile, str]] = []

        async def action(selected: Profile, name: str, target=None):
            calls.append((selected, name, target))
            return response

        with (
            mock.patch.object(module, "run_action", side_effect=action),
            mock.patch.object(
                module,
                "_relay_result",
                side_effect=lambda selected, name: relayed.append((selected, name)),
            ),
        ):
            result = module.trio.run(
                module._run_action_command, profile, "status", None
            )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [(profile, "status", None)])
        self.assertEqual(relayed, [(profile, "status-online")])

    def test_pin_polling_keeps_the_selected_profile(self) -> None:
        module, _idle = _load_desktop_app()
        profile = _profile("home")
        uri = "file:///mnt/home/photo.jpg"
        responses = iter(
            (
                {"pinned": True, "cached": False, "busy": True, "scheduled": True},
                {
                    "items": [
                        {
                            "uri": uri,
                            "cached": True,
                            "pinned": True,
                            "busy": False,
                            "recoverable": False,
                        }
                    ]
                },
            )
        )
        calls: list[tuple[Profile, str, object]] = []
        relayed: list[tuple[Profile, str]] = []

        async def action(selected: Profile, name: str, target=None):
            calls.append((selected, name, target))
            return next(responses)

        with (
            mock.patch.object(module, "run_action", side_effect=action),
            mock.patch.object(
                module,
                "_relay_result",
                side_effect=lambda selected, name: relayed.append((selected, name)),
            ),
        ):
            result = module.trio.run(
                module._run_action_command, profile, "pin", uri
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [(profile, "pin", uri), (profile, "describe", [uri])],
        )
        self.assertEqual(relayed, [(profile, "pin-cached")])

    def test_command_line_selects_profile_before_showing_result(self) -> None:
        module, idle = _load_desktop_app()
        home = _profile("home")
        with (
            mock.patch.object(module, "profiles", return_value=(home,)),
            mock.patch.object(module, "select_profile", return_value=home) as select,
            mock.patch.object(module, "load", return_value=_settings()),
        ):
            application = module.DesktopApplication()
            result = application.do_command_line(
                _CommandLine(
                    ["desktop", "--profile", "home", "--result", "refresh-ok"]
                )
            )
            self.assertEqual(application._message.get_text(), "Refresh requested.")
            idle.run_next()

        self.assertEqual(result, 0)
        select.assert_called_once_with("home")
        self.assertEqual(application._profile, home)
        self.assertEqual(application._message.get_text(), "Refresh requested.")
        application.do_shutdown()

    def test_stale_relay_does_not_switch_the_visible_profile(self) -> None:
        module, idle = _load_desktop_app()
        home = _profile("home")
        work = _profile("work")

        def load_profile(path: Path) -> Settings:
            return _settings(path.parent.parent.name)

        with (
            mock.patch.object(module, "profiles", return_value=(home, work)),
            mock.patch.object(module, "select_profile", return_value=home),
            mock.patch.object(module, "load", side_effect=load_profile) as load,
        ):
            application = module.DesktopApplication()
            application.do_activate()
            application._profile_selector.set_selected(2)
            idle.run_next()
            self.assertEqual(application._message.get_text(), "Profile work loaded.")

            result = application.do_command_line(
                _CommandLine(
                    ["desktop", "--profile", "home", "--result", "refresh-ok"]
                )
            )

        self.assertEqual(result, 0)
        self.assertEqual(application._profile, work)
        self.assertEqual(application._message.get_text(), "Profile work loaded.")
        self.assertEqual(load.call_count, 1)
        application.do_shutdown()

    def test_command_line_rejects_missing_or_unlisted_profile(self) -> None:
        module, _idle = _load_desktop_app()
        home = _profile("home")
        work = _profile("work")
        with (
            mock.patch.object(module, "profiles", return_value=(home,)),
            mock.patch.object(module, "select_profile", return_value=work),
        ):
            application = module.DesktopApplication()
            self.assertEqual(
                application.do_command_line(
                    _CommandLine(
                        ["desktop", "--profile", "work", "--result", "refresh-ok"]
                    )
                ),
                2,
            )
            self.assertEqual(
                application._message.get_text(), "Profile is not available."
            )
            self.assertEqual(
                application.do_command_line(
                    _CommandLine(["desktop", "--result", "refresh-ok"])
                ),
                2,
            )

        self.assertEqual(application._message.get_text(), "Invalid desktop action.")
        application.do_shutdown()

    def test_worker_bounds_pending_operations(self) -> None:
        module, idle = _load_desktop_app()
        started = threading.Event()
        release = threading.Event()
        completed: list[tuple[bool, object]] = []

        def blocked() -> str:
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test timed out")
            return "first"

        worker = module._Worker()
        self.assertTrue(
            worker.submit(blocked, lambda *result: completed.append(result))
        )
        self.assertTrue(started.wait(timeout=2))
        self.assertTrue(
            worker.submit(lambda: "second", lambda *result: completed.append(result))
        )
        self.assertFalse(
            worker.submit(lambda: "third", lambda *result: completed.append(result))
        )
        release.set()
        idle.run_next()
        idle.run_next()
        self.assertEqual(completed, [(True, "first"), (True, "second")])
        worker.close()

    def test_main_action_selects_profile_before_running_action(self) -> None:
        module, _idle = _load_desktop_app()
        profile = _profile("home")
        events: list[str] = []

        def select(profile_id: str) -> Profile:
            self.assertEqual(profile_id, "home")
            events.append("select")
            return profile

        def run(*arguments) -> int:
            events.append("run")
            self.assertEqual(
                arguments,
                (module._run_action_command, profile, "refresh", None),
            )
            return 7

        with (
            mock.patch.object(module, "select_profile", side_effect=select),
            mock.patch.object(module.trio, "run", side_effect=run),
        ):
            result = module.main(["--profile", "home", "--action", "refresh"])

        self.assertEqual(result, 7)
        self.assertEqual(events, ["select", "run"])
        self.assertEqual(_Application.run_calls, [])
        self.assertEqual(module.main(["--action", "refresh"]), 2)

    def test_main_keeps_one_application_without_an_action(self) -> None:
        module, _idle = _load_desktop_app()

        self.assertEqual(module.main([]), 17)
        self.assertEqual(_Application.run_calls, [["immich-on-demand-desktop"]])


if __name__ == "__main__":
    unittest.main()
