import importlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import sys
import tempfile
import types
import unittest
from unittest import mock


class _File:
    def __init__(self, path: str) -> None:
        self.path = PurePosixPath(path)

    @classmethod
    def new_for_path(cls, path: str):
        return cls(path)

    def equal(self, other) -> bool:
        return self.path == other.path

    def has_prefix(self, prefix) -> bool:
        return prefix.path in self.path.parents


class _FileInfo:
    def __init__(self, path: str, *, directory: bool = False) -> None:
        self.location = _File(path)
        self.directory = directory
        self.emblems: list[str] = []

    def get_location(self):
        return self.location

    def get_uri(self) -> str:
        return Path(str(self.location.path)).as_uri()

    def is_directory(self) -> bool:
        return self.directory

    def add_emblem(self, emblem: str) -> None:
        self.emblems.append(emblem)


class _MenuItem:
    def __init__(self, **properties) -> None:
        self.properties = properties
        self._callback = None
        self._callback_args = ()

    def connect(self, signal: str, callback, *args) -> None:
        if signal != "activate":
            raise AssertionError(f"unexpected signal: {signal}")
        self._callback = callback
        self._callback_args = args

    def activate(self) -> None:
        if self._callback is None:
            raise AssertionError("menu item has no callback")
        self._callback(self, *self._callback_args)


class _Subprocess:
    calls: list[tuple[list[str], int]] = []

    @classmethod
    def new(cls, argv, flags):
        cls.calls.append((list(argv), flags))
        return cls()


class _GObjectBase:
    pass


class _MenuProvider:
    pass


class _InfoProvider:
    pass


class _GLib:
    callbacks: list[tuple[object, tuple[object, ...]]] = []

    @classmethod
    def idle_add(cls, callback, *args) -> int:
        cls.callbacks.append((callback, args))
        return len(cls.callbacks)


class _OperationResult:
    COMPLETE = 0
    FAILED = 1
    IN_PROGRESS = 2


class _NautilusRuntime:
    completions: list[tuple[object, object, object, int]] = []

    @classmethod
    def complete(cls, closure, provider, handle, result) -> None:
        cls.completions.append((closure, provider, handle, result))


class _DeferredThread:
    created: list["_DeferredThread"] = []

    def __init__(self, *, target, args, daemon: bool) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.created.append(self)

    def start(self) -> None:
        self.started = True

    def run(self) -> None:
        self.target(*self.args)


def _load_extension_module():
    gi = types.ModuleType("gi")
    repository = types.ModuleType("gi.repository")
    gi.require_version = lambda _namespace, _version: None
    repository.Gio = types.SimpleNamespace(
        File=_File,
        Subprocess=_Subprocess,
        SubprocessFlags=types.SimpleNamespace(NONE=0),
    )
    repository.GObject = types.SimpleNamespace(GObject=_GObjectBase)
    repository.GLib = _GLib
    repository.Nautilus = types.SimpleNamespace(
        InfoProvider=_InfoProvider,
        MenuItem=_MenuItem,
        MenuProvider=_MenuProvider,
        OperationResult=_OperationResult,
        info_provider_update_complete_invoke=_NautilusRuntime.complete,
    )
    gi.repository = repository
    sys.modules.pop("immich_on_demand.nautilus_extension", None)
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        return importlib.import_module("immich_on_demand.nautilus_extension")


def _write_config(root: Path, mount_path: Path) -> Path:
    config_home = root / "config"
    path = config_home / "immich-on-demand" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "server_url": "https://photos.example.test",
                "mount_path": str(mount_path),
            }
        ),
        encoding="utf-8",
    )
    return config_home


def _configured_extension(root: Path):
    mount_path = root / "Immich"
    config_home = _write_config(root, mount_path)
    with mock.patch.dict(
        os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False
    ):
        module = _load_extension_module()
        return module, module.NautilusExtension(), mount_path


def _drain_deferred_work() -> None:
    next_thread = 0
    while _GLib.callbacks or next_thread < len(_DeferredThread.created):
        if _GLib.callbacks:
            callback, args = _GLib.callbacks.pop(0)
            callback(*args)
        while next_thread < len(_DeferredThread.created):
            _DeferredThread.created[next_thread].run()
            next_thread += 1


def _reset_fakes() -> None:
    _Subprocess.calls.clear()
    _GLib.callbacks.clear()
    _NautilusRuntime.completions.clear()
    _DeferredThread.created.clear()


class NautilusExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_fakes()

    def tearDown(self) -> None:
        _reset_fakes()
        sys.modules.pop("immich_on_demand.nautilus_extension", None)

    def test_mount_background_actions_launch_only_when_activated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mount_path = root / "Immich"
            config_home = _write_config(root, mount_path)
            with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False
            ):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            items = extension.get_background_items(
                _FileInfo(str(mount_path), directory=True)
            )

            self.assertEqual(
                [item.properties["label"] for item in items],
                ["Refresh Immich", "Immich On-Demand Settings"],
            )
            self.assertEqual(_Subprocess.calls, [])

            items[0].activate()
            items[1].activate()
            self.assertEqual(
                _Subprocess.calls,
                [
                    (["immich-on-demand-desktop", "--action", "refresh"], 0),
                    (["immich-on-demand-desktop"], 0),
                ],
            )

    def test_batched_description_applies_emblems_and_completes_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)

            provider = object()
            handles = [object(), object()]
            closures = [object(), object()]
            files = [
                _FileInfo(str(mount_path / "first.jpg")),
                _FileInfo(str(mount_path / "second.jpg")),
            ]
            describe = mock.AsyncMock(
                return_value={
                    "items": [
                        {
                            "uri": files[0].get_uri(),
                            "cached": True,
                            "pinned": True,
                            "busy": False,
                            "recoverable": False,
                        },
                        {
                            "uri": files[1].get_uri(),
                            "cached": False,
                            "pinned": False,
                            "busy": True,
                            "recoverable": True,
                        },
                    ]
                }
            )

            with (
                mock.patch.object(module, "run_action", describe, create=True),
                mock.patch("threading.Thread", _DeferredThread),
            ):
                for handle, closure, file in zip(handles, closures, files):
                    self.assertEqual(
                        extension.update_file_info_full(
                            provider, handle, closure, file
                        ),
                        _OperationResult.IN_PROGRESS,
                    )

                self.assertEqual(len(_GLib.callbacks), 1)
                self.assertEqual(_NautilusRuntime.completions, [])
                callback, args = _GLib.callbacks.pop(0)
                self.assertFalse(callback(*args))
                self.assertEqual(len(_DeferredThread.created), 1)
                self.assertTrue(_DeferredThread.created[0].started)
                self.assertEqual(describe.await_count, 0)

                _DeferredThread.created[0].run()
                describe.assert_awaited_once_with(
                    "describe", [file.get_uri() for file in files]
                )
                self.assertEqual(len(_GLib.callbacks), 1)

                callback, args = _GLib.callbacks.pop(0)
                self.assertFalse(callback(*args))

            self.assertEqual(
                files[0].emblems,
                ["immich-on-demand-cached", "immich-on-demand-pinned"],
            )
            self.assertEqual(
                files[1].emblems,
                ["immich-on-demand-busy", "immich-on-demand-recoverable"],
            )
            self.assertEqual(
                _NautilusRuntime.completions,
                [
                    (closures[0], provider, handles[0], _OperationResult.COMPLETE),
                    (closures[1], provider, handles[1], _OperationResult.COMPLETE),
                ],
            )

    def test_cancel_completes_once_and_removes_the_uri_from_pending_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)

            provider = object()
            handles = [object(), object()]
            closures = [object(), object()]
            files = [
                _FileInfo(str(mount_path / "cancelled.jpg")),
                _FileInfo(str(mount_path / "kept.jpg")),
            ]
            for handle, closure, file in zip(handles, closures, files):
                extension.update_file_info_full(provider, handle, closure, file)

            extension.cancel_update(provider, handles[0])

            self.assertEqual(
                _NautilusRuntime.completions,
                [(closures[0], provider, handles[0], _OperationResult.COMPLETE)],
            )
            describe = mock.AsyncMock(return_value={"items": []})
            with (
                mock.patch.object(module, "run_action", describe),
                mock.patch("threading.Thread", _DeferredThread),
            ):
                callback, args = _GLib.callbacks.pop(0)
                callback(*args)
                _DeferredThread.created[0].run()
                describe.assert_awaited_once_with("describe", [files[1].get_uri()])
                callback, args = _GLib.callbacks.pop(0)
                callback(*args)

            self.assertEqual(
                _NautilusRuntime.completions,
                [
                    (closures[0], provider, handles[0], _OperationResult.COMPLETE),
                    (closures[1], provider, handles[1], _OperationResult.COMPLETE),
                ],
            )

    def test_description_batches_are_capped_at_64_uris(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)

            provider = object()
            files = [
                _FileInfo(str(mount_path / f"asset-{index}.jpg"))
                for index in range(65)
            ]
            for file in files:
                extension.update_file_info_full(
                    provider, object(), object(), file
                )

            describe = mock.AsyncMock(return_value={"items": []})
            with (
                mock.patch.object(module, "run_action", describe),
                mock.patch("threading.Thread", _DeferredThread),
            ):
                _drain_deferred_work()

            self.assertEqual(
                [len(awaited.args[1]) for awaited in describe.await_args_list],
                [64, 1],
            )
            self.assertEqual(len(_NautilusRuntime.completions), 65)

    def test_description_batches_stay_below_the_control_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)

            provider = object()
            files = [
                _FileInfo(str(mount_path / (f"{index}-" + "x" * 1990)))
                for index in range(40)
            ]
            for file in files:
                extension.update_file_info_full(
                    provider, object(), object(), file
                )

            describe = mock.AsyncMock(return_value={"items": []})
            with (
                mock.patch.object(module, "run_action", describe),
                mock.patch("threading.Thread", _DeferredThread),
            ):
                _drain_deferred_work()

            batches = [awaited.args[1] for awaited in describe.await_args_list]
            self.assertGreater(len(batches), 1)
            for uris in batches:
                frame = json.dumps(
                    {
                        "id": (1 << 63) - 1,
                        "method": "describe",
                        "params": {"uris": uris},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                self.assertLess(len(frame), 48 * 1024)
            self.assertEqual(len(_NautilusRuntime.completions), 40)

    def test_recent_description_is_reused_without_another_control_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)

            provider = object()
            first = _FileInfo(str(mount_path / "cached.jpg"))
            uri = first.get_uri()
            describe = mock.AsyncMock(
                return_value={
                    "items": [
                        {
                            "uri": uri,
                            "cached": True,
                            "pinned": False,
                            "busy": False,
                            "recoverable": False,
                        }
                    ]
                }
            )
            with (
                mock.patch.object(module, "run_action", describe),
                mock.patch("threading.Thread", _DeferredThread),
                mock.patch.object(
                    module.time, "monotonic", side_effect=[100.0, 101.0]
                ),
            ):
                extension.update_file_info_full(provider, object(), object(), first)
                callback, args = _GLib.callbacks.pop(0)
                callback(*args)
                _DeferredThread.created[0].run()
                callback, args = _GLib.callbacks.pop(0)
                callback(*args)

                repeated = _FileInfo(str(mount_path / "cached.jpg"))
                self.assertEqual(
                    extension.update_file_info_full(
                        provider, object(), object(), repeated
                    ),
                    _OperationResult.COMPLETE,
                )

            self.assertEqual(repeated.emblems, ["immich-on-demand-cached"])
            self.assertEqual(describe.await_count, 1)
            self.assertEqual(_GLib.callbacks, [])

            expired_handle = object()
            expired_closure = object()
            with mock.patch.object(module.time, "monotonic", return_value=103.0):
                self.assertEqual(
                    extension.update_file_info_full(
                        provider,
                        expired_handle,
                        expired_closure,
                        _FileInfo(str(mount_path / "cached.jpg")),
                    ),
                    _OperationResult.IN_PROGRESS,
                )
            extension.cancel_update(provider, expired_handle)
            callback, args = _GLib.callbacks.pop(0)
            self.assertFalse(callback(*args))

    def test_worker_failure_and_inflight_cancel_both_complete_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)

            provider = object()
            handles = [object(), object()]
            closures = [object(), object()]
            files = [
                _FileInfo(str(mount_path / "cancelled.jpg")),
                _FileInfo(str(mount_path / "failed.jpg")),
            ]
            for handle, closure, file in zip(handles, closures, files):
                extension.update_file_info_full(provider, handle, closure, file)

            describe = mock.AsyncMock(side_effect=RuntimeError("private path"))
            with (
                mock.patch.object(module, "run_action", describe),
                mock.patch("threading.Thread", _DeferredThread),
            ):
                callback, args = _GLib.callbacks.pop(0)
                callback(*args)
                extension.cancel_update(provider, handles[0])
                _DeferredThread.created[0].run()
                callback, args = _GLib.callbacks.pop(0)
                callback(*args)

            self.assertEqual(files[0].emblems, [])
            self.assertEqual(files[1].emblems, [])
            self.assertEqual(
                _NautilusRuntime.completions,
                [
                    (closures[0], provider, handles[0], _OperationResult.COMPLETE),
                    (closures[1], provider, handles[1], _OperationResult.COMPLETE),
                ],
            )

    def test_worker_start_failure_completes_the_batch_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _module, extension, mount_path = _configured_extension(root)

            provider = object()
            handle = object()
            closure = object()
            extension.update_file_info_full(
                provider,
                handle,
                closure,
                _FileInfo(str(mount_path / "photo.jpg")),
            )

            with mock.patch(
                "threading.Thread", side_effect=RuntimeError("thread unavailable")
            ):
                callback, args = _GLib.callbacks.pop(0)
                self.assertFalse(callback(*args))

            self.assertEqual(
                _NautilusRuntime.completions,
                [(closure, provider, handle, _OperationResult.COMPLETE)],
            )
            self.assertEqual(_GLib.callbacks, [])

    def test_outside_mount_and_oversized_uri_complete_without_control_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)

            provider = object()
            outside = _FileInfo(str(root / "outside.jpg"))
            self.assertEqual(
                extension.update_file_info_full(
                    provider, object(), object(), outside
                ),
                _OperationResult.COMPLETE,
            )

            oversized = _FileInfo(str(mount_path / ("x" * (48 * 1024))))
            closure = object()
            handle = object()
            self.assertEqual(
                extension.update_file_info_full(
                    provider, handle, closure, oversized
                ),
                _OperationResult.IN_PROGRESS,
            )
            describe = mock.AsyncMock()
            with (
                mock.patch.object(module, "run_action", describe),
                mock.patch("threading.Thread", _DeferredThread),
            ):
                callback, args = _GLib.callbacks.pop(0)
                self.assertFalse(callback(*args))

            self.assertEqual(_DeferredThread.created, [])
            describe.assert_not_awaited()
            self.assertEqual(
                _NautilusRuntime.completions,
                [(closure, provider, handle, _OperationResult.COMPLETE)],
            )
            self.assertEqual(_GLib.callbacks, [])

    def test_one_mounted_file_can_be_evicted_without_shell_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mount_path = root / "Immich"
            asset_path = mount_path / "name with spaces.jpg"
            config_home = _write_config(root, mount_path)
            with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False
            ):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            items = extension.get_file_items([_FileInfo(str(asset_path))])

            self.assertEqual(
                [item.properties["label"] for item in items], ["Evict Local Copy"]
            )
            self.assertEqual(_Subprocess.calls, [])

            items[0].activate()
            self.assertEqual(
                _Subprocess.calls,
                [
                    (
                        [
                            "immich-on-demand-desktop",
                            "--action",
                            "evict",
                            "--uri",
                            asset_path.as_uri(),
                        ],
                        0,
                    )
                ],
            )

    def test_scope_uses_path_components_and_is_cached_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mount_path = root / "Immich"
            config_home = _write_config(root, mount_path)
            with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False
            ):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            _write_config(root, root / "Changed")
            with mock.patch.object(
                Path, "read_text", side_effect=AssertionError("configuration reread")
            ):
                self.assertEqual(
                    len(
                        extension.get_background_items(
                            _FileInfo(str(mount_path / "folder"), directory=True)
                        )
                    ),
                    2,
                )
                self.assertEqual(
                    extension.get_background_items(
                        _FileInfo(str(root / "Immich-copy"), directory=True)
                    ),
                    [],
                )
                self.assertEqual(
                    extension.get_background_items(
                        _FileInfo(str(root), directory=True)
                    ),
                    [],
                )

            self.assertEqual(_Subprocess.calls, [])

    def test_file_menu_rejects_empty_multiple_mixed_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mount_path = root / "Immich"
            config_home = _write_config(root, mount_path)
            with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False
            ):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            mounted = _FileInfo(str(mount_path / "one.jpg"))
            other_mounted = _FileInfo(str(mount_path / "two.jpg"))
            outside = _FileInfo(str(root / "outside.jpg"))
            directory_info = _FileInfo(str(mount_path / "folder"), directory=True)

            for selection in (
                [],
                [mounted, other_mounted],
                [mounted, outside],
                [outside],
                [directory_info],
            ):
                with self.subTest(count=len(selection)):
                    self.assertEqual(extension.get_file_items(selection), [])
            self.assertEqual(_Subprocess.calls, [])

    def test_missing_configuration_makes_the_provider_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(Path(directory) / "missing")},
                clear=False,
            ):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            self.assertEqual(
                extension.get_background_items(_FileInfo("/Photos", directory=True)),
                [],
            )
            self.assertEqual(
                extension.get_file_items([_FileInfo("/Photos/photo.jpg")]),
                [],
            )
            self.assertEqual(_Subprocess.calls, [])

    def test_malformed_tilde_mount_makes_the_provider_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_home = _write_config(
                root, Path("~immich-on-demand-user-does-not-exist/Immich")
            )
            with mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=False
            ):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            self.assertEqual(
                extension.get_background_items(_FileInfo("/Photos", directory=True)),
                [],
            )

    def test_import_does_not_reach_service_fuse_or_http_modules(self) -> None:
        forbidden = {
            "httpx": None,
            "pyfuse3": None,
            "immich_on_demand.filesystem": None,
            "immich_on_demand.immich": None,
            "immich_on_demand.service": None,
        }
        with mock.patch.dict(sys.modules, forbidden):
            module = _load_extension_module()
        self.assertIsNotNone(module.NautilusExtension)

    def test_packaged_script_exports_the_provider_class(self) -> None:
        module = _load_extension_module()
        script = (
            Path(__file__).parents[1]
            / "packaging"
            / "immich-on-demand-nautilus.py"
        )

        with mock.patch.dict(
            sys.modules, {"immich_on_demand.nautilus_extension": module}
        ):
            namespace = runpy.run_path(script)

        self.assertIs(namespace["NautilusExtension"], module.NautilusExtension)


if __name__ == "__main__":
    unittest.main()
