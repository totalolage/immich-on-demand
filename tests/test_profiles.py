from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from immich_on_demand.profiles import Profile, profiles, select_profile


class ProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_select_profile_validates_an_immutable_id_and_derives_all_roots(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("home-2")

        self.assertEqual(
            profile,
            Profile(
                id="home-2",
                config=Path(self.environment["XDG_CONFIG_HOME"])
                / "immich-on-demand/profiles/home-2",
                state=Path(self.environment["XDG_STATE_HOME"])
                / "immich-on-demand/profiles/home-2",
                data=Path(self.environment["XDG_DATA_HOME"])
                / "immich-on-demand/profiles/home-2",
                cache=Path(self.environment["XDG_CACHE_HOME"])
                / "immich-on-demand/profiles/home-2",
                runtime=Path(self.environment["XDG_RUNTIME_DIR"])
                / "immich-on-demand/profiles/home-2",
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            profile.id = "other"  # type: ignore[misc]
        self.assertFalse(hasattr(profile, "__dict__"))

        valid = ("a", "0", "a-b", "a" * 32)
        invalid = (
            "",
            "A",
            "a_b",
            "-a",
            "a-",
            "a" * 33,
            " a",
            "a ",
            "café",
            "a/b",
            1,
        )
        with patch.dict(os.environ, self.environment, clear=True):
            for profile_id in valid:
                with self.subTest(valid=profile_id):
                    self.assertEqual(select_profile(profile_id).id, profile_id)
            for profile_id in invalid:
                with self.subTest(invalid=profile_id), self.assertRaises(ValueError):
                    select_profile(profile_id)  # type: ignore[arg-type]

    def test_select_profile_rejects_a_control_socket_that_cannot_be_bound(self) -> None:
        suffix = "/immich-on-demand/profiles/a/control.sock"
        allowed_base = "/" + "r" * (107 - 1 - len(os.fsencode(suffix)))
        environment = {**self.environment, "XDG_RUNTIME_DIR": allowed_base}

        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                len(os.fsencode(select_profile("a").runtime / "control.sock")),
                107,
            )
        with patch.dict(
            os.environ,
            {**environment, "XDG_RUNTIME_DIR": allowed_base + "r"},
            clear=True,
        ), self.assertRaisesRegex(ValueError, "control socket path is too long"):
            select_profile("a")

    def test_profiles_lists_only_active_strict_profile_directories(self) -> None:
        root = (
            Path(self.environment["XDG_CONFIG_HOME"])
            / "immich-on-demand/profiles"
        )
        root.mkdir(mode=0o700, parents=True)
        os.chmod(root.parent, 0o700)
        for profile_id in ("zeta", "alpha"):
            directory = root / profile_id
            directory.mkdir(mode=0o700)
            config = directory / "config.json"
            config.write_text("not parsed", encoding="utf-8")
            config.chmod(0o600)
        (root / "empty-scaffold").mkdir(mode=0o700)
        retired = root / "retired"
        retired.mkdir(mode=0o700)
        retired_config = retired / "config.retired.json"
        retired_config.write_text("not parsed", encoding="utf-8")
        retired_config.chmod(0o600)
        (root / "Invalid").mkdir(mode=0o700)
        (root / "README").write_text("ignored", encoding="utf-8")

        with patch.dict(os.environ, self.environment, clear=True):
            discovered = profiles()

        self.assertEqual(tuple(profile.id for profile in discovered), ("alpha", "zeta"))

    def test_profiles_refuses_an_unsafe_active_profile(self) -> None:
        root = (
            Path(self.environment["XDG_CONFIG_HOME"])
            / "immich-on-demand/profiles"
        )
        directory = root / "home"
        directory.mkdir(mode=0o700, parents=True)
        os.chmod(root, 0o700)
        os.chmod(root.parent, 0o700)
        config = directory / "config.json"
        config.write_text("{}", encoding="utf-8")
        config.chmod(0o644)

        with patch.dict(os.environ, self.environment, clear=True), self.assertRaisesRegex(
            RuntimeError, "unsafe Profile config"
        ):
            profiles()

    def test_legacy_config_is_the_only_discovered_and_selectable_profile(self) -> None:
        application = Path(self.environment["XDG_CONFIG_HOME"]) / "immich-on-demand"
        profiled = application / "profiles/home"
        profiled.mkdir(mode=0o700, parents=True)
        os.chmod(application, 0o700)
        os.chmod(profiled.parent, 0o700)
        config = profiled / "config.json"
        config.write_text("{}", encoding="utf-8")
        config.chmod(0o600)
        legacy = application / "config.json"
        legacy.write_text("migration candidate", encoding="utf-8")

        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(tuple(profile.id for profile in profiles()), ("default",))
            self.assertEqual(select_profile("default").id, "default")
            with self.assertRaisesRegex(RuntimeError, "legacy config"):
                select_profile("home")


if __name__ == "__main__":
    unittest.main()
