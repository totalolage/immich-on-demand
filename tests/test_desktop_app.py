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

    def test_result_relay_uses_only_fixed_messages(self) -> None:
        module, idle = _load_desktop_app()
        with mock.patch.object(
            module,
            "load",
            return_value=Settings(
                "https://photos.example.test", Path("/home/user/Immich")
            ),
        ):
            application = module.DesktopApplication()
            cases = (
                ("status-ok", "Service is running."),
                ("refresh-ok", "Refresh requested."),
                ("evict-ok", "Eviction requested."),
                ("pin-cached", "Pinned and cached."),
                ("pin-retry", "Pin saved; download needs retry."),
                ("unpin-cached", "Pin removed; cached copy retained."),
            )
            for index, (name, message) in enumerate(cases):
                with self.subTest(name=name):
                    self.assertEqual(
                        application.do_command_line(
                            _CommandLine(["desktop", "--result", name])
                        ),
                        0,
                    )
                    if index == 0:
                        idle.run_next()
                    self.assertEqual(application._message.get_text(), message)

        self.assertEqual(
            application.do_command_line(
                _CommandLine(["desktop", "--result", "not-a-real-result"])
            ),
            2,
        )
        self.assertEqual(application._message.get_text(), "Invalid desktop action.")
        application.do_shutdown()

    def test_worker_bounds_pending_operations(self) -> None:
        module, idle = _load_desktop_app()
        save_started = threading.Event()
        release_save = threading.Event()
        save_calls: list[Settings] = []
        settings = Settings(
            "https://photos.example.test", Path("/home/user/Immich")
        )

        def slow_save(value: Settings) -> None:
            save_calls.append(value)
            save_started.set()
            if not release_save.wait(timeout=2):
                raise RuntimeError("test timed out")

        with (
            mock.patch.object(module, "load", return_value=settings),
            mock.patch.object(module, "save", side_effect=slow_save),
        ):
            application = module.DesktopApplication()
            application.do_activate()
            idle.run_next()
            application._save_button.click()
            self.assertTrue(save_started.wait(timeout=2))
            application._save_button.click()
            application._save_button.click()
            self.assertEqual(
                application._message.get_text(), "Desktop worker is busy."
            )
            release_save.set()
            idle.run_next()
            idle.run_next()

        self.assertEqual(save_calls, [settings, settings])
        application.do_shutdown()

    def test_pin_action_process_waits_for_terminal_hydration(self) -> None:
        module, _idle = _load_desktop_app()
        uri = "file:///home/user/Immich/photo.jpg"
        responses = iter(
            (
                {"pinned": True, "cached": False, "busy": True, "scheduled": True},
                {
                    "items": [
                        {
                            "uri": uri,
                            "cached": False,
                            "pinned": True,
                            "busy": True,
                            "recoverable": False,
                        }
                    ]
                },
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
        calls: list[tuple[str, object]] = []
        relayed: list[str] = []

        async def action(name: str, target=None):
            calls.append((name, target))
            return next(responses)

        async def no_wait(_seconds: float) -> None:
            pass

        with (
            mock.patch.object(module, "run_action", side_effect=action),
            mock.patch.object(module, "_relay_result", side_effect=relayed.append),
            mock.patch.object(module.trio, "sleep", side_effect=no_wait),
        ):
            result = module.trio.run(module._run_action_command, "pin", uri)

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [("pin", uri), ("describe", [uri]), ("describe", [uri])],
        )
        self.assertEqual(relayed, ["pin-cached"])

    def test_pin_action_reports_retryable_terminal_state(self) -> None:
        module, _idle = _load_desktop_app()
        uri = "file:///home/user/Immich/photo.jpg"
        responses = iter(
            (
                {"pinned": True, "cached": False, "busy": True, "scheduled": True},
                {
                    "items": [
                        {
                            "uri": uri,
                            "cached": False,
                            "pinned": True,
                            "busy": False,
                            "recoverable": False,
                        }
                    ]
                },
            )
        )
        relayed: list[str] = []

        async def action(_name: str, _target=None):
            return next(responses)

        with (
            mock.patch.object(module, "run_action", side_effect=action),
            mock.patch.object(module, "_relay_result", side_effect=relayed.append),
        ):
            result = module.trio.run(module._run_action_command, "pin", uri)

        self.assertEqual(result, 1)
        self.assertEqual(relayed, ["pin-retry"])

    def test_pin_action_timeout_keeps_the_durable_pin_truthful(self) -> None:
        module, _idle = _load_desktop_app()
        uri = "file:///home/user/Immich/photo.jpg"
        relayed: list[str] = []

        async def action(name: str, _target=None):
            if name == "pin":
                return {
                    "pinned": True,
                    "cached": False,
                    "busy": True,
                    "scheduled": True,
                }
            return {
                "items": [
                    {
                        "uri": uri,
                        "cached": False,
                        "pinned": True,
                        "busy": True,
                        "recoverable": False,
                    }
                ]
            }

        with (
            mock.patch.object(module, "run_action", side_effect=action),
            mock.patch.object(module, "_relay_result", side_effect=relayed.append),
            mock.patch.object(module, "_ACTION_WAIT_SECONDS", 0.01, create=True),
        ):
            result = module.trio.run(module._run_action_command, "pin", uri)

        self.assertEqual(result, 124)
        self.assertEqual(relayed, ["pin-timeout"])

    def test_unpin_action_finishes_when_the_durable_pin_is_removed(self) -> None:
        module, _idle = _load_desktop_app()
        uri = "file:///home/user/Immich/photo.jpg"
        response = {
            "pinned": False,
            "cached": True,
            "busy": True,
            "scheduled": False,
        }
        relayed: list[str] = []
        calls: list[tuple[str, object]] = []

        async def action(name: str, target=None):
            calls.append((name, target))
            return response

        with (
            mock.patch.object(module, "run_action", side_effect=action),
            mock.patch.object(module, "_relay_result", side_effect=relayed.append),
        ):
            result = module.trio.run(module._run_action_command, "unpin", uri)

        self.assertEqual(result, 0)
        self.assertEqual(calls, [("unpin", uri)])
        self.assertEqual(relayed, ["unpin-cached"])

    def test_control_failure_relays_only_a_fixed_error(self) -> None:
        module, _idle = _load_desktop_app()
        relayed: list[str] = []

        async def broken(_name: str, _target=None):
            raise RuntimeError("api-key=do-not-display")

        with (
            mock.patch.object(module, "run_action", side_effect=broken),
            mock.patch.object(module, "_relay_result", side_effect=relayed.append),
        ):
            result = module.trio.run(module._run_action_command, "refresh", None)

        self.assertEqual(result, 1)
        self.assertEqual(relayed, ["refresh-error"])

    def test_main_runs_the_unique_settings_application_without_an_action(self) -> None:
        module, _idle = _load_desktop_app()

        result = module.main([])

        self.assertEqual(result, 17)
        self.assertEqual(
            _Application.run_calls,
            [["immich-on-demand-desktop"]],
        )

    def test_main_action_bypasses_the_unique_settings_application(self) -> None:
        module, _idle = _load_desktop_app()
        run = mock.Mock(return_value=7)

        with mock.patch.object(module.trio, "run", run):
            result = module.main(["--action", "refresh"])

        self.assertEqual(result, 7)
        run.assert_called_once_with(module._run_action_command, "refresh", None)
        self.assertEqual(_Application.run_calls, [])


if __name__ == "__main__":
    unittest.main()
