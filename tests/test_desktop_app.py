import importlib
from pathlib import Path
import queue
import sys
import threading
import types
import unittest
from unittest import mock

from immich_on_demand.settings import Settings


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

    def get_text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

    def set_visibility(self, visible: bool) -> None:
        self.visible = visible


class _CheckButton:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self._active = False

    def get_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active


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

    def connect(self, signal: str, callback) -> None:
        if signal != "clicked":
            raise AssertionError(f"unexpected signal: {signal}")
        self._callback = callback

    def click(self) -> None:
        if self._callback is None:
            raise AssertionError("button has no callback")
        self._callback(self)


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
        Entry=_Entry,
        Grid=_Grid,
        Label=_Label,
        Orientation=types.SimpleNamespace(HORIZONTAL=1, VERTICAL=2),
    )
    gi.repository = repository
    sys.modules.pop("immich_on_demand.desktop_app", None)
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        module = importlib.import_module("immich_on_demand.desktop_app")
    return module, idle


class DesktopApplicationTests(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("immich_on_demand.desktop_app", None)

    def test_activation_loads_profile_off_thread_before_populating_form(self) -> None:
        module, idle = _load_desktop_app()
        expected = Settings(
            "https://photos.example.test",
            Path("/home/user/Immich"),
            cache_max_bytes=12_345,
            cache_max_age_seconds=67_890,
            minimum_free_bytes=2_345,
            refresh_seconds=45,
            remote_delete=True,
        )
        load_threads: list[int] = []

        def load_profile() -> Settings:
            load_threads.append(threading.get_ident())
            return expected

        application = None
        with mock.patch.object(module, "load", side_effect=load_profile):
            application = module.DesktopApplication()
            application.do_activate()
            self.assertEqual(application._window.present_count, 1)
            self.assertEqual(application._entries["server_url"].get_text(), "")
            idle.run_next()

        self.assertNotEqual(load_threads, [threading.get_ident()])
        self.assertEqual(
            {
                name: entry.get_text()
                for name, entry in application._entries.items()
            },
            {
                "server_url": "https://photos.example.test",
                "mount_path": "/home/user/Immich",
                "cache_max_bytes": "12345",
                "cache_max_age_seconds": "67890",
                "minimum_free_bytes": "2345",
                "refresh_seconds": "45",
                "read_only_key": "",
                "mutation_key": "",
            },
        )
        self.assertTrue(application._remote_delete.get_active())
        self.assertEqual(application._message.get_text(), "Settings loaded.")
        application.do_shutdown()

    def test_save_persists_profile_and_only_nonblank_keys_off_thread(self) -> None:
        module, idle = _load_desktop_app()
        original = Settings(
            "https://old.example.test",
            Path("/old/Immich"),
        )
        saved: list[tuple[Settings, int]] = []
        stored: list[tuple[Settings, str, str, int]] = []

        def save_profile(settings: Settings) -> None:
            saved.append((settings, threading.get_ident()))

        def store_key(settings: Settings, purpose: str, secret: str) -> None:
            stored.append((settings, purpose, secret, threading.get_ident()))

        with (
            mock.patch.object(module, "load", return_value=original),
            mock.patch.object(module, "save", side_effect=save_profile),
            mock.patch.object(module, "store_api_key", side_effect=store_key),
        ):
            application = module.DesktopApplication()
            application.do_activate()
            idle.run_next()
            values = {
                "server_url": "https://new.example.test",
                "mount_path": "/new/Immich",
                "cache_max_bytes": "123",
                "cache_max_age_seconds": "456",
                "minimum_free_bytes": "78",
                "refresh_seconds": "90",
                "read_only_key": "read-secret",
                "mutation_key": "mutation-secret",
            }
            for name, value in values.items():
                application._entries[name].set_text(value)
            application._remote_delete.set_active(True)

            application._save_button.click()
            self.assertEqual(
                application._entries["read_only_key"].get_text(), ""
            )
            self.assertEqual(application._entries["mutation_key"].get_text(), "")
            idle.run_next()
            application._save_button.click()
            idle.run_next()

        expected = Settings(
            "https://new.example.test",
            Path("/new/Immich"),
            cache_max_bytes=123,
            cache_max_age_seconds=456,
            minimum_free_bytes=78,
            refresh_seconds=90,
            remote_delete=True,
        )
        main_thread = threading.get_ident()
        self.assertEqual([value for value, _thread in saved], [expected, expected])
        self.assertEqual(
            [(settings, purpose, secret) for settings, purpose, secret, _ in stored],
            [
                (expected, "read-only", "read-secret"),
                (expected, "mutation", "mutation-secret"),
            ],
        )
        self.assertTrue(all(thread != main_thread for _value, thread in saved))
        self.assertTrue(all(thread != main_thread for *_value, thread in stored))
        self.assertEqual(application._message.get_text(), "Settings saved.")
        application.do_shutdown()

    def test_failed_key_store_does_not_switch_the_active_profile(self) -> None:
        module, idle = _load_desktop_app()
        current = Settings(
            "https://old.example.test",
            Path("/old/Immich"),
        )
        save_profile = mock.Mock()
        store_key = mock.Mock(side_effect=RuntimeError("secret failure"))

        with (
            mock.patch.object(module, "load", return_value=current),
            mock.patch.object(module, "save", save_profile),
            mock.patch.object(module, "store_api_key", store_key),
        ):
            application = module.DesktopApplication()
            application.do_activate()
            idle.run_next()
            application._entries["server_url"].set_text(
                "https://new.example.test"
            )
            application._entries["mount_path"].set_text("/new/Immich")
            application._entries["read_only_key"].set_text("replacement")

            application._save_button.click()
            idle.run_next()

        store_key.assert_called_once()
        save_profile.assert_not_called()
        self.assertEqual(
            application._message.get_text(), "Could not save settings."
        )
        application.do_shutdown()

    def test_command_line_actions_use_trio_worker_and_fixed_messages(self) -> None:
        module, idle = _load_desktop_app()
        action_calls: list[tuple[str, str | None, int]] = []

        async def action(name: str, uri: str | None = None):
            action_calls.append((name, uri, threading.get_ident()))
            return {"untrusted": "result must not reach the UI"}

        uri = "file:///home/user/Immich/photo.jpg"
        with (
            mock.patch.object(
                module,
                "load",
                return_value=Settings(
                    "https://photos.example.test", Path("/home/user/Immich")
                ),
            ),
            mock.patch.object(module, "run_action", side_effect=action),
        ):
            application = module.DesktopApplication()
            cases = (
                (["desktop", "--action", "status"], "Service is running."),
                (["desktop", "--action", "refresh"], "Refresh requested."),
                (
                    ["desktop", "--action", "evict", "--uri", uri],
                    "Eviction requested.",
                ),
            )
            for index, (arguments, message) in enumerate(cases):
                with self.subTest(arguments=arguments):
                    self.assertEqual(
                        application.do_command_line(_CommandLine(arguments)), 0
                    )
                    if index == 0:
                        idle.run_next()
                    idle.run_next()
                    self.assertEqual(application._message.get_text(), message)

        self.assertEqual(
            [(name, value) for name, value, _thread in action_calls],
            [("status", None), ("refresh", None), ("evict", uri)],
        )
        self.assertTrue(
            all(thread != threading.get_ident() for _name, _uri, thread in action_calls)
        )
        self.assertEqual(
            application.do_command_line(
                _CommandLine(["desktop", "--action", "evict"])
            ),
            2,
        )
        self.assertEqual(application._message.get_text(), "Invalid desktop action.")
        application.do_shutdown()

    def test_action_failure_never_displays_the_service_exception(self) -> None:
        module, idle = _load_desktop_app()

        async def broken_action(_name: str, _uri: str | None = None):
            raise RuntimeError("api-key=do-not-display")

        with (
            mock.patch.object(
                module,
                "load",
                return_value=Settings(
                    "https://photos.example.test", Path("/home/user/Immich")
                ),
            ),
            mock.patch.object(module, "run_action", side_effect=broken_action),
        ):
            application = module.DesktopApplication()
            self.assertEqual(
                application.do_command_line(
                    _CommandLine(["desktop", "--action", "status"])
                ),
                0,
            )
            idle.run_next()
            idle.run_next()

        self.assertEqual(application._message.get_text(), "Could not query service.")
        self.assertNotIn("do-not-display", application._message.get_text())
        application.do_shutdown()

    def test_worker_bounds_pending_operations(self) -> None:
        module, idle = _load_desktop_app()
        load_started = threading.Event()
        release_load = threading.Event()
        action_calls: list[str] = []

        def slow_load() -> Settings:
            load_started.set()
            if not release_load.wait(timeout=2):
                raise RuntimeError("test timed out")
            return Settings(
                "https://photos.example.test", Path("/home/user/Immich")
            )

        async def action(name: str, _uri: str | None = None):
            action_calls.append(name)
            return {}

        with (
            mock.patch.object(module, "load", side_effect=slow_load),
            mock.patch.object(module, "run_action", side_effect=action),
        ):
            application = module.DesktopApplication()
            application.do_activate()
            self.assertTrue(load_started.wait(timeout=2))
            application.do_command_line(
                _CommandLine(["desktop", "--action", "status"])
            )
            application.do_command_line(
                _CommandLine(["desktop", "--action", "refresh"])
            )
            self.assertEqual(
                application._message.get_text(), "Desktop worker is busy."
            )
            release_load.set()
            idle.run_next()
            idle.run_next()

        self.assertEqual(action_calls, ["status"])
        application.do_shutdown()

    def test_main_runs_the_unique_application_with_explicit_arguments(self) -> None:
        module, _idle = _load_desktop_app()

        result = module.main(["--action", "status"])

        self.assertEqual(result, 17)
        self.assertEqual(
            _Application.run_calls,
            [["immich-on-demand-desktop", "--action", "status"]],
        )


if __name__ == "__main__":
    unittest.main()
