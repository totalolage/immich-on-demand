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

    def get_location(self):
        return self.location

    def get_uri(self) -> str:
        return Path(str(self.location.path)).as_uri()

    def is_directory(self) -> bool:
        return self.directory


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
    repository.Nautilus = types.SimpleNamespace(
        MenuItem=_MenuItem,
        MenuProvider=_MenuProvider,
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


class NautilusExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        _Subprocess.calls.clear()

    def tearDown(self) -> None:
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
