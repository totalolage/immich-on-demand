from dataclasses import FrozenInstanceError
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import immich_on_demand.profiles as profiles_module
from immich_on_demand.profiles import (
    Profile,
    ProfileBusyError,
    ProfileError,
    claim_service,
    manage_profile,
    profiles,
    retire_profile,
    select_profile,
)


def _hold_claim(profile: Profile, connection) -> None:
    try:
        with claim_service(profile):
            connection.send("")
            connection.recv()
    except BaseException as error:
        connection.send(f"{type(error).__name__}: {error}")
    finally:
        connection.close()


def _hold_management(profile: Profile, connection) -> None:
    try:
        with patch(
            "immich_on_demand.settings.has_profile_api_keys",
            return_value=False,
        ):
            with manage_profile(profile):
                connection.send("")
                connection.recv()
    except BaseException as error:
        connection.send(f"{type(error).__name__}: {error}")
    finally:
        connection.close()


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
        (root / "runtime").mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_config(self, profile: Profile, mount: Path) -> None:
        profile.config.mkdir(mode=0o700, parents=True)
        os.chmod(profile.config.parent.parent, 0o700)
        os.chmod(profile.config.parent, 0o700)
        config = profile.config / "config.json"
        config.write_text(
            json.dumps(
                {
                    "server_url": "https://photos.example.test",
                    "mount_path": str(mount),
                }
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)

    def _start_holder(self, target, profile: Profile):
        parent, child = multiprocessing.get_context("fork").Pipe()
        process = multiprocessing.get_context("fork").Process(
            target=target,
            args=(profile, child),
        )
        process.start()
        child.close()
        self.assertTrue(parent.poll(5), "lock holder did not start")
        error = parent.recv()
        self.assertEqual(error, "")
        return process, parent

    def _stop_holder(self, process, connection) -> None:
        connection.send("stop")
        connection.close()
        process.join(5)
        self.assertEqual(process.exitcode, 0)

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
                with self.subTest(invalid=profile_id), self.assertRaises(ProfileError):
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
        ), self.assertRaisesRegex(ProfileError, "control socket path is too long"):
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

    def test_management_lock_is_global_and_busy_is_retryable(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            first = select_profile("first")
            second = select_profile("second")
            process, connection = self._start_holder(_hold_management, first)
            try:
                with self.assertRaises(ProfileBusyError) as caught:
                    with manage_profile(second):
                        self.fail("global management lock was not exclusive")
                self.assertEqual(caught.exception.exit_status, 75)
            finally:
                self._stop_holder(process, connection)

    def test_manage_creates_only_a_restartable_empty_config_scaffold(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("new")
            with patch(
                "immich_on_demand.settings.has_profile_api_keys",
                return_value=False,
            ):
                with manage_profile(profile, Path(self.temporary.name) / "mount"):
                    self.assertEqual(tuple(profile.config.iterdir()), ())
                    self.assertEqual(stat.S_IMODE(profile.config.stat().st_mode), 0o700)
                with manage_profile(profile, Path(self.temporary.name) / "mount"):
                    pass
            self.assertEqual(profiles(), ())

            retired = profile.config / "config.retired.json"
            retired.write_text("{}", encoding="utf-8")
            retired.chmod(0o600)
            with patch(
                "immich_on_demand.settings.has_profile_api_keys",
                return_value=False,
            ), self.assertRaisesRegex(ProfileError, "residue"):
                with manage_profile(profile, Path(self.temporary.name) / "mount"):
                    pass

            keyed = select_profile("keyed")
            with patch(
                "immich_on_demand.settings.has_profile_api_keys",
                return_value=True,
            ), self.assertRaisesRegex(ProfileError, "API key"):
                with manage_profile(keyed, Path(self.temporary.name) / "other"):
                    pass
            self.assertFalse(keyed.config.exists())

    def test_manage_rejects_equal_or_nested_mounts_before_yield(self) -> None:
        root = Path(self.temporary.name) / "mounts"
        with patch.dict(os.environ, self.environment, clear=True):
            existing = select_profile("existing")
            candidate = select_profile("candidate")
            self._write_config(existing, root / "parent")
            with patch(
                "immich_on_demand.settings.has_profile_api_keys",
                return_value=False,
            ):
                for mount in (root / "parent", root / "parent/child"):
                    with self.subTest(mount=mount), self.assertRaisesRegex(
                        ProfileError, "overlaps"
                    ):
                        with manage_profile(candidate, mount):
                            self.fail("overlapping mount reached management caller")
            self.assertFalse(candidate.config.exists())

    def test_retire_profile_preserves_local_state_and_refuses_a_mounted_path(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("home")
            mount = Path(self.temporary.name) / "mount"
            mount.mkdir()
            self._write_config(profile, mount)
            config = profile.config / "config.json"
            expected_config = config.read_bytes()
            for root in (profile.state, profile.data, profile.cache):
                root.mkdir(parents=True)
                (root / "preserved").write_bytes(root.name.encode())

            with patch("immich_on_demand.profiles.os.path.ismount", return_value=True):
                with self.assertRaisesRegex(ProfileError, "still mounted"):
                    retire_profile(profile)
            self.assertTrue(config.exists())

            with patch("immich_on_demand.profiles.os.path.ismount", return_value=False):
                retire_profile(profile)

            retired = profile.config / "config.retired.json"
            self.assertFalse(config.exists())
            self.assertEqual(retired.read_bytes(), expected_config)
            self.assertEqual(stat.S_IMODE(retired.stat().st_mode), 0o600)
            for root in (profile.state, profile.data, profile.cache):
                self.assertEqual((root / "preserved").read_bytes(), root.name.encode())
            self.assertEqual(profiles(), ())

    def test_retire_profile_rejects_a_replaced_config_directory(self) -> None:
        from immich_on_demand.settings import load

        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("home")
            mount = Path(self.temporary.name) / "mount"
            self._write_config(profile, mount)
            original_directory = profile.config.with_name("opened-home")

            def replace_directory(_path: Path):
                profile.config.rename(original_directory)
                self._write_config(profile, Path(self.temporary.name) / "other-mount")
                return load(profile.config / "config.json")

            with (
                patch("immich_on_demand.settings.load", side_effect=replace_directory),
                patch("immich_on_demand.profiles.os.path.ismount", return_value=False),
                self.assertRaisesRegex(ProfileError, "directory changed"),
            ):
                retire_profile(profile)

            self.assertTrue((original_directory / "config.json").exists())
            self.assertTrue((profile.config / "config.json").exists())
            self.assertFalse(
                (original_directory / "config.retired.json").exists()
            )

    def test_retire_profile_honors_management_profile_and_mount_locks(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            target = select_profile("target")
            other = select_profile("other")
            mount = Path(self.temporary.name) / "mount"
            self._write_config(target, mount)
            self._write_config(other, mount)

            process, connection = self._start_holder(_hold_management, other)
            try:
                with self.assertRaises(ProfileBusyError):
                    retire_profile(target)
            finally:
                self._stop_holder(process, connection)

            process, connection = self._start_holder(_hold_claim, target)
            try:
                with self.assertRaisesRegex(ProfileError, "already claimed"):
                    retire_profile(target)
            finally:
                self._stop_holder(process, connection)

            process, connection = self._start_holder(_hold_claim, other)
            try:
                with self.assertRaisesRegex(ProfileError, "mount path is already claimed"):
                    retire_profile(target)
            finally:
                self._stop_holder(process, connection)

            self.assertTrue((target.config / "config.json").exists())

    def test_retire_profile_never_replaces_a_raced_destination(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("home")
            self._write_config(profile, Path(self.temporary.name) / "mount")
            active = profile.config / "config.json"
            retired = profile.config / "config.retired.json"
            expected = active.read_bytes()
            rename = profiles_module._RENAMEAT2

            def destination_appears(*arguments) -> int:
                retired.write_bytes(b"foreign retired config")
                retired.chmod(0o600)
                return rename(*arguments)

            with (
                patch(
                    "immich_on_demand.profiles._RENAMEAT2",
                    side_effect=destination_appears,
                ),
                patch("immich_on_demand.profiles.os.path.ismount", return_value=False),
                self.assertRaisesRegex(ProfileError, "destination appeared"),
            ):
                retire_profile(profile)

            self.assertEqual(active.read_bytes(), expected)
            self.assertEqual(retired.read_bytes(), b"foreign retired config")

    def test_retire_profile_refuses_to_migrate_a_legacy_default(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("default")
            legacy = profile.config.parent.parent / "config.json"
            legacy.parent.mkdir(mode=0o700, parents=True)
            legacy.write_text(
                json.dumps(
                    {
                        "server_url": "https://photos.example.test",
                        "mount_path": str(Path(self.temporary.name) / "mount"),
                    }
                ),
                encoding="utf-8",
            )
            legacy.chmod(0o600)

            with self.assertRaisesRegex(ProfileError, "legacy config must migrate"):
                retire_profile(profile)

            self.assertTrue(legacy.exists())
            self.assertFalse(profile.config.exists())

    def test_claim_keeps_the_profile_lock_and_locks_before_config_load(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("home")
            self._write_config(profile, Path(self.temporary.name) / "mount")
            process, connection = self._start_holder(_hold_claim, profile)
            try:
                (profile.config / "config.json").unlink()
                with self.assertRaisesRegex(ProfileError, "already claimed"):
                    with claim_service(profile):
                        self.fail("config was loaded before the service lock")
            finally:
                self._stop_holder(process, connection)
            for root in (profile.state, profile.data, profile.cache):
                self.assertFalse(root.exists())
                self.assertEqual(
                    stat.S_IMODE(root.parent.parent.stat().st_mode), 0o700
                )
                self.assertEqual(stat.S_IMODE(root.parent.stat().st_mode), 0o700)

    def test_mount_claims_reject_equal_and_nested_paths_but_allow_siblings(self) -> None:
        root = Path(self.temporary.name) / "mounts"
        with patch.dict(os.environ, self.environment, clear=True):
            parent_profile = select_profile("parent")
            equal_profile = select_profile("equal")
            child_profile = select_profile("child")
            sibling_profile = select_profile("sibling")
            self._write_config(parent_profile, root / "parent")
            self._write_config(equal_profile, root / "parent")
            self._write_config(child_profile, root / "parent/child")
            self._write_config(sibling_profile, root / "sibling")

            process, connection = self._start_holder(_hold_claim, parent_profile)
            try:
                for conflict in (equal_profile, child_profile):
                    with self.subTest(conflict=conflict.id), self.assertRaisesRegex(
                        ProfileError, "mount path is already claimed"
                    ):
                        with claim_service(conflict):
                            self.fail("conflicting mount claim succeeded")
                with claim_service(sibling_profile) as settings:
                    self.assertEqual(settings.mount_path, root / "sibling")
            finally:
                self._stop_holder(process, connection)

    def test_claim_uses_the_locked_resolved_mount_after_a_symlink_changes(self) -> None:
        root = Path(self.temporary.name)
        first = root / "first"
        second = root / "second"
        (first / "mount").mkdir(parents=True)
        (second / "mount").mkdir(parents=True)
        selected = root / "selected"
        selected.symlink_to(first, target_is_directory=True)
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("home")
            self._write_config(profile, selected / "mount")
            with claim_service(profile) as settings:
                self.assertEqual(settings.mount_path, first / "mount")
                selected.unlink()
                selected.symlink_to(second, target_is_directory=True)
                self.assertEqual(settings.mount_path, first / "mount")

    @patch(
        "immich_on_demand.settings.has_nondefault_profile_api_keys",
        return_value=False,
    )
    def test_claim_migrates_exact_legacy_files_after_credentials(
        self, _no_other_keys
    ) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            profile = select_profile("default")
            mount = Path(self.temporary.name) / "mount"
            entries = (
                (profile.state, "catalog.db", b"db"),
                (profile.state, "catalog.db-wal", b"wal"),
                (profile.state, "catalog.db-shm", b"shm"),
            )
            for destination, name, content in entries:
                application = destination.parent.parent
                application.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(application, 0o700)
                source = application / name
                source.write_bytes(content)
                source.chmod(0o600)
            for destination, name, content in (
                (profile.data, "uploads", b"upload"),
                (profile.cache, "originals", b"original"),
            ):
                application = destination.parent.parent
                application.mkdir(mode=0o700, parents=True)
                os.chmod(application, 0o700)
                source = application / name
                source.mkdir(mode=0o700)
                (source / "payload").write_bytes(content)

            application = profile.config.parent.parent
            application.mkdir(mode=0o700, parents=True)
            os.chmod(application, 0o700)
            legacy = application / "config.json"
            legacy.write_text(
                json.dumps(
                    {
                        "server_url": "https://photos.example.test",
                        "mount_path": str(mount),
                    }
                ),
                encoding="utf-8",
            )
            legacy.chmod(0o600)

            with patch(
                "immich_on_demand.settings.has_nondefault_profile_api_keys",
                return_value=True,
            ), self.assertRaisesRegex(ProfileError, "another Profile API key"):
                with claim_service(profile):
                    self.fail("migration ignored another Profile credential")
            self.assertTrue(legacy.exists())

            with patch(
                "immich_on_demand.settings.copy_legacy_api_keys_to_default",
                side_effect=RuntimeError("secret failure"),
            ), self.assertRaisesRegex(ProfileError, "migrate legacy Profile"):
                with claim_service(profile):
                    self.fail("failed credential copy reached the service")

            raced_destination = profile.state / "catalog.db"

            def destination_appears_after_preflight(_settings) -> None:
                raced_destination.write_bytes(b"foreign")
                raced_destination.chmod(0o600)

            with patch(
                "immich_on_demand.settings.copy_legacy_api_keys_to_default",
                side_effect=destination_appears_after_preflight,
            ), self.assertRaisesRegex(ProfileError, "destination changed"):
                with claim_service(profile):
                    self.fail("migration overwrote a raced destination")
            self.assertEqual(raced_destination.read_bytes(), b"foreign")
            self.assertEqual(
                (profile.state.parent.parent / "catalog.db").read_bytes(), b"db"
            )
            raced_destination.unlink()

            catalog_source = profile.state.parent.parent / "catalog.db"
            catalog_source.unlink()
            raced_destination.write_bytes(b"db")
            raced_destination.chmod(0o600)

            def completed_destination_disappears(_settings) -> None:
                raced_destination.unlink()

            with patch(
                "immich_on_demand.settings.copy_legacy_api_keys_to_default",
                side_effect=completed_destination_disappears,
            ), self.assertRaisesRegex(ProfileError, "destination changed"):
                with claim_service(profile):
                    self.fail("migration published after completed state disappeared")
            self.assertFalse((profile.config / "config.json").exists())
            catalog_source.write_bytes(b"db")
            catalog_source.chmod(0o600)

            foreign = Path(self.temporary.name) / "foreign"
            foreign.write_bytes(b"foreign")

            def source_changes_after_preflight(_settings) -> None:
                catalog_source.unlink()
                catalog_source.symlink_to(foreign)

            with patch(
                "immich_on_demand.settings.copy_legacy_api_keys_to_default",
                side_effect=source_changes_after_preflight,
            ), self.assertRaisesRegex(ProfileError, "source changed|unsafe legacy"):
                with claim_service(profile):
                    self.fail("migration moved a changed source")
            self.assertTrue(catalog_source.is_symlink())
            self.assertFalse((profile.config / "config.json").exists())
            catalog_source.unlink()
            catalog_source.write_bytes(b"db")
            catalog_source.chmod(0o600)

            def credentials_copied_before_local_moves(settings) -> None:
                self.assertEqual(settings.mount_path, mount)
                self.assertTrue(legacy.exists())
                self.assertTrue((profile.state.parent.parent / "catalog.db").exists())

            with patch(
                "immich_on_demand.settings.copy_legacy_api_keys_to_default",
                side_effect=credentials_copied_before_local_moves,
            ) as copy_keys:
                with manage_profile(profile):
                    self.assertTrue((profile.config / "config.json").exists())
                with claim_service(profile) as settings:
                    self.assertEqual(settings.mount_path, mount)
            copy_keys.assert_called_once()

            self.assertFalse(legacy.exists())
            self.assertEqual((profile.config / "config.json").read_text(), json.dumps(
                {
                    "server_url": "https://photos.example.test",
                    "mount_path": str(mount),
                }
            ))
            for destination, name, content in entries:
                self.assertEqual((destination / name).read_bytes(), content)
            self.assertEqual((profile.data / "uploads/payload").read_bytes(), b"upload")
            self.assertEqual((profile.cache / "originals/payload").read_bytes(), b"original")
            for root in (profile.state, profile.data, profile.cache):
                self.assertEqual(stat.S_IMODE(root.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
