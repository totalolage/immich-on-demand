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
        self.invalidations = 0

    def get_location(self):
        return self.location

    def get_uri(self) -> str:
        return Path(str(self.location.path)).as_uri()

    def is_directory(self) -> bool:
        return self.directory

    def add_emblem(self, emblem: str) -> None:
        self.emblems.append(emblem)

    def invalidate_extension_info(self) -> None:
        self.invalidations += 1


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
    pending: list["_Subprocess"] = []

    def __init__(self) -> None:
        self.callback = None
        self.callback_args = ()

    @classmethod
    def new(cls, argv, flags):
        cls.calls.append((list(argv), flags))
        return cls()

    def wait_async(self, _cancellable, callback, *args) -> None:
        self.callback = callback
        self.callback_args = args
        self.pending.append(self)

    def wait_finish(self, _result) -> bool:
        return True

    def complete(self) -> None:
        if self.callback is None:
            raise AssertionError("subprocess has no completion callback")
        self.pending.remove(self)
        self.callback(self, object(), *self.callback_args)


class _GObjectBase:
    pass


class _MenuProvider:
    pass


class _InfoProvider:
    pass


class _GLib:
    callbacks: list[tuple[object, tuple[object, ...]]] = []
    timeouts: list[tuple[int, object, tuple[object, ...]]] = []

    @classmethod
    def idle_add(cls, callback, *args) -> int:
        cls.callbacks.append((callback, args))
        return len(cls.callbacks)

    @classmethod
    def timeout_add(cls, milliseconds: int, callback, *args) -> int:
        cls.timeouts.append((milliseconds, callback, args))
        return len(cls.timeouts)


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


def _xdg_environment(root: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
    }


def _write_config(
    root: Path, mount_path: Path, profile_id: str = "home"
) -> Path:
    application = root / "config" / "immich-on-demand"
    registry = application / "profiles"
    directory = registry / profile_id
    for path in (application, registry, directory):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    path = directory / "config.json"
    path.write_text(
        json.dumps(
            {
                "server_url": "https://photos.example.test",
                "mount_path": str(mount_path),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _configured_extension(root: Path):
    mount_path = root / "Immich"
    _write_config(root, mount_path)
    with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
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
    _Subprocess.pending.clear()
    _GLib.callbacks.clear()
    _GLib.timeouts.clear()
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
            _write_config(root, mount_path)
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            items = extension.get_background_items(
                _FileInfo(str(mount_path), directory=True)
            )

            self.assertEqual(
                [item.properties["label"] for item in items],
                [
                    "Refresh Immich",
                    "Manage Pending Uploads",
                    "Immich On-Demand Settings",
                ],
            )
            self.assertEqual(_Subprocess.calls, [])

            items[0].activate()
            items[1].activate()
            items[2].activate()
            self.assertEqual(
                _Subprocess.calls,
                [
                    (
                        [
                            "immich-on-demand-desktop",
                            "--profile",
                            "home",
                            "--action",
                            "refresh",
                        ],
                        0,
                    ),
                    (["immich-on-demand-desktop", "--profile", "home"], 0),
                    (["immich-on-demand-desktop", "--profile", "home"], 0),
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
                    extension._mounts[0][0],
                    "describe",
                    [file.get_uri() for file in files],
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

    def test_interleaved_profiles_use_separate_batches_caches_and_actions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home_mount = root / "Home"
            work_mount = root / "Work"
            _write_config(root, home_mount, "home")
            _write_config(root, work_mount, "work")
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            provider = object()
            files = [
                _FileInfo(str(home_mount / "first.jpg")),
                _FileInfo(str(work_mount / "second.jpg")),
                _FileInfo(str(home_mount / "third.jpg")),
            ]
            for file in files:
                extension.update_file_info_full(
                    provider, object(), object(), file
                )

            async def describe(profile, action, uris):
                self.assertEqual(action, "describe")
                return {
                    "items": [
                        {
                            "uri": uri,
                            "cached": True,
                            "pinned": False,
                            "busy": False,
                            "recoverable": False,
                        }
                        for uri in uris
                    ]
                }

            action = mock.AsyncMock(side_effect=describe)
            with (
                mock.patch.object(module, "run_action", action),
                mock.patch("threading.Thread", _DeferredThread),
            ):
                _drain_deferred_work()

            self.assertEqual(
                [
                    (call.args[0].id, call.args[2])
                    for call in action.await_args_list
                ],
                [
                    ("home", [files[0].get_uri(), files[2].get_uri()]),
                    ("work", [files[1].get_uri()]),
                ],
            )
            self.assertEqual(len(_DeferredThread.created), 2)
            self.assertEqual(
                set(extension._cache),
                {
                    ("home", files[0].get_uri()),
                    ("home", files[2].get_uri()),
                    ("work", files[1].get_uri()),
                },
            )

            work_items = extension.get_background_items(
                _FileInfo(str(work_mount), directory=True)
            )
            work_items[0].activate()
            self.assertEqual(
                _Subprocess.calls,
                [
                    (
                        [
                            "immich-on-demand-desktop",
                            "--profile",
                            "work",
                            "--action",
                            "refresh",
                        ],
                        0,
                    )
                ],
            )

    def test_overlapping_active_mounts_make_the_provider_inert(self) -> None:
        for work_mount in ("Home", "Home/Work", "."):
            with self.subTest(work_mount=work_mount), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "Home"
                _write_config(root, home, "home")
                _write_config(root, root / work_mount, "work")
                with mock.patch.dict(
                    os.environ, _xdg_environment(root), clear=False
                ):
                    module = _load_extension_module()
                    extension = module.NautilusExtension()

                self.assertEqual(extension._mounts, ())
                self.assertEqual(
                    extension.get_background_items(
                        _FileInfo(str(home), directory=True)
                    ),
                    [],
                )

    def test_one_unreadable_active_config_makes_the_provider_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_config(root, root / "Home", "home")
            unreadable = _write_config(root, root / "Work", "work")
            unreadable.chmod(0o644)
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            self.assertEqual(extension._mounts, ())

    def test_busy_state_invalidates_again_after_the_cache_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)
            file = _FileInfo(str(mount_path / "pending.jpg"))
            uri = file.get_uri()
            response = {
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

            extension.update_file_info_full(object(), object(), object(), file)
            callback, args = _GLib.callbacks.pop(0)
            with (
                mock.patch("threading.Thread", _DeferredThread),
                mock.patch.object(
                    module, "run_action", mock.AsyncMock(return_value=response)
                ),
            ):
                callback(*args)
                _DeferredThread.created[0].run()
            callback, args = _GLib.callbacks.pop(0)
            callback(*args)

            self.assertIn(("home", uri), extension._cache)
            self.assertEqual(file.invalidations, 0)
            self.assertEqual(len(_GLib.timeouts), 1)
            milliseconds, callback, args = _GLib.timeouts.pop(0)
            self.assertEqual(milliseconds, int(module._CACHE_SECONDS * 1000))
            self.assertFalse(callback(*args))
            self.assertNotIn(("home", uri), extension._cache)
            self.assertEqual(file.invalidations, 1)

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
                describe.assert_awaited_once_with(
                    extension._mounts[0][0],
                    "describe",
                    [files[1].get_uri()],
                )
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
                [len(awaited.args[2]) for awaited in describe.await_args_list],
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

            batches = [awaited.args[2] for awaited in describe.await_args_list]
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
            _write_config(root, mount_path)
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
                module = _load_extension_module()
                extension = module.NautilusExtension()

            items = extension.get_file_items([_FileInfo(str(asset_path))])

            self.assertEqual(
                [item.properties["label"] for item in items],
                ["Pin for Offline Use", "Evict Local Copy"],
            )
            self.assertEqual(_Subprocess.calls, [])

            items[0].activate()
            items[1].activate()
            self.assertEqual(
                _Subprocess.calls,
                [
                    (
                        [
                            "immich-on-demand-desktop",
                            "--profile",
                            "home",
                            "--action",
                            "pin",
                            "--uri",
                            asset_path.as_uri(),
                        ],
                        0,
                    ),
                    (
                        [
                            "immich-on-demand-desktop",
                            "--profile",
                            "home",
                            "--action",
                            "evict",
                            "--uri",
                            asset_path.as_uri(),
                        ],
                        0,
                    )
                ],
            )

    def test_recent_pinned_state_replaces_pin_and_evict_with_unpin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)
            asset = _FileInfo(str(mount_path / "pinned.jpg"))
            extension._cache[("home", asset.get_uri())] = (
                module.time.monotonic() + 10,
                {
                    "cached": True,
                    "pinned": True,
                    "busy": False,
                    "recoverable": False,
                },
            )

            items = extension.get_file_items([asset])

            self.assertEqual(
                [item.properties["label"] for item in items], ["Unpin"]
            )
            items[0].activate()
            self.assertIn(("home", asset.get_uri()), extension._cache)
            self.assertEqual(asset.invalidations, 0)
            _Subprocess.pending[0].complete()
            self.assertNotIn(("home", asset.get_uri()), extension._cache)
            self.assertEqual(asset.invalidations, 1)
            self.assertEqual(
                _Subprocess.calls,
                [
                    (
                        [
                            "immich-on-demand-desktop",
                            "--profile",
                            "home",
                            "--action",
                            "unpin",
                            "--uri",
                            asset.get_uri(),
                        ],
                        0,
                    )
                ],
            )

    def test_pin_launch_refreshes_from_the_daemon_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)
            asset = _FileInfo(str(mount_path / "slow-video.mp4"))
            uri = asset.get_uri()
            extension._cache[("home", uri)] = (
                module.time.monotonic() + 10,
                {
                    "cached": False,
                    "pinned": False,
                    "busy": False,
                    "recoverable": False,
                },
            )

            items = extension.get_file_items([asset])
            items[0].activate()

            self.assertIn(("home", uri), extension._cache)
            self.assertEqual(asset.invalidations, 0)
            self.assertEqual(len(_Subprocess.pending), 1)
            self.assertEqual(len(_GLib.timeouts), 1)
            _milliseconds, callback, args = _GLib.timeouts.pop(0)
            self.assertFalse(callback(*args))
            self.assertNotIn(("home", uri), extension._cache)
            self.assertEqual(asset.invalidations, 1)
            self.assertEqual(len(_Subprocess.pending), 1)

    def test_failed_pinned_download_can_be_retried_or_unpinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, extension, mount_path = _configured_extension(root)
            asset = _FileInfo(str(mount_path / "retry.jpg"))
            extension._cache[("home", asset.get_uri())] = (
                module.time.monotonic() + 10,
                {
                    "cached": False,
                    "pinned": True,
                    "busy": False,
                    "recoverable": False,
                },
            )

            items = extension.get_file_items([asset])

            self.assertEqual(
                [item.properties["label"] for item in items],
                ["Retry Pinned Download", "Unpin"],
            )
            items[0].activate()
            self.assertEqual(
                _Subprocess.calls[0][0],
                [
                    "immich-on-demand-desktop",
                    "--profile",
                    "home",
                    "--action",
                    "pin",
                    "--uri",
                    asset.get_uri(),
                ],
            )

    def test_scope_uses_path_components_and_is_cached_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mount_path = root / "Immich"
            _write_config(root, mount_path)
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
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
                    3,
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
            _write_config(root, mount_path)
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
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
            root = Path(directory)
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
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
            _write_config(
                root, Path("~immich-on-demand-user-does-not-exist/Immich")
            )
            with mock.patch.dict(os.environ, _xdg_environment(root), clear=False):
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
