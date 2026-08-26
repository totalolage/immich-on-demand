import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from immich_on_demand.settings import (
    Settings,
    copy_legacy_api_keys_to_default,
    has_nondefault_profile_api_keys,
    has_profile_api_keys,
    load,
    load_api_key,
    save,
    store_api_key,
)


class _SecretItem:
    def __init__(
        self,
        secret: str | bytes,
        attributes: dict[str, str],
        *,
        locked: bool = False,
    ) -> None:
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self.attributes = attributes
        self.locked = locked
        self.deleted = False

    def get_attributes(self) -> dict[str, str]:
        return self.attributes

    def is_locked(self) -> bool:
        return self.locked

    def unlock(self) -> bool:
        self.locked = False
        return True

    def get_secret(self) -> bytes:
        return self.secret

    def delete(self) -> None:
        self.deleted = True


class _SecretCollection:
    def __init__(self, items: list[_SecretItem] | None = None) -> None:
        self.items = items or []
        self.searches: list[dict[str, str]] = []
        self.created: list[tuple[str, dict[str, str], bytes, bool]] = []

    def search_items(self, attributes: dict[str, str]) -> list[_SecretItem]:
        self.searches.append(attributes)
        return [
            item
            for item in self.items
            if not item.deleted
            and all(item.attributes.get(name) == value for name, value in attributes.items())
        ]

    def create_item(
        self,
        label: str,
        attributes: dict[str, str],
        secret: bytes,
        *,
        replace: bool,
    ) -> _SecretItem:
        self.created.append((label, attributes, secret, replace))
        if replace:
            for item in self.items:
                if item.attributes == attributes:
                    item.deleted = True
        item = _SecretItem(secret, attributes)
        self.items.append(item)
        return item


class SettingsTest(unittest.TestCase):
    def test_has_profile_api_keys_accepts_only_the_complete_exact_schema(self) -> None:
        attributes = self._attributes("home")
        collection = _SecretCollection(
            [
                _SecretItem(
                    "secret-tool",
                    {
                        **attributes,
                        "xdg:schema": "org.freedesktop.Secret.Generic",
                    },
                ),
                _SecretItem("superset", {**attributes, "extra": "value"}),
                _SecretItem("other", self._attributes("work")),
            ]
        )

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            self.assertTrue(has_profile_api_keys("home"))
            self.assertFalse(has_profile_api_keys("empty"))

        self.assertEqual(
            collection.searches,
            [
                {"application": "immich-on-demand", "profile": "home"},
                {"application": "immich-on-demand", "profile": "empty"},
            ],
        )

    def test_has_profile_api_keys_fails_closed_on_attribute_read_failure(self) -> None:
        class BrokenItem(_SecretItem):
            def get_attributes(self) -> dict[str, str]:
                raise RuntimeError("backend failure")

        collection = _SecretCollection(
            [BrokenItem("secret", self._attributes("home"))]
        )
        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "inspect Profile API keys"),
        ):
            has_profile_api_keys("home")

    def test_nondefault_profile_keys_ignore_legacy_and_default_items(self) -> None:
        collection = _SecretCollection(
            [
                _SecretItem("legacy", self._legacy_attributes("read-only")),
                _SecretItem("default", self._attributes("default")),
                _SecretItem("extra", {**self._attributes("work"), "extra": "value"}),
            ]
        )
        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            self.assertFalse(has_nondefault_profile_api_keys())
            collection.items.append(
                _SecretItem(
                    "work",
                    {
                        **self._attributes("work"),
                        "xdg:schema": "org.freedesktop.Secret.Generic",
                    },
                )
            )
            self.assertTrue(has_nondefault_profile_api_keys())

    def test_copy_legacy_api_keys_to_default_creates_compared_destinations(self) -> None:
        read_canonical = self._legacy_attributes("read-only")
        read_hostname = {**read_canonical, "server": "photos.example.test"}
        mutation = self._legacy_attributes("mutation")
        legacy_items = [
            _SecretItem("read-secret", read_canonical),
            _SecretItem("read-secret", read_hostname),
            _SecretItem("mutation-secret", mutation),
            _SecretItem(
                "not-a-legacy-source",
                {**read_canonical, "profile": "other"},
            ),
        ]
        collection = _SecretCollection(legacy_items)

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            copy_legacy_api_keys_to_default(self._settings())

        self.assertEqual(
            collection.created,
            [
                (
                    "Immich On-Demand default read-only API key",
                    self._attributes("default"),
                    b"read-secret",
                    False,
                ),
                (
                    "Immich On-Demand default mutation API key",
                    self._attributes("default", "mutation"),
                    b"mutation-secret",
                    False,
                ),
            ],
        )
        self.assertTrue(all(not item.deleted for item in legacy_items))

    def test_copy_legacy_api_key_accepts_secret_tool_schema(self) -> None:
        source = _SecretItem(
            "read-secret",
            {
                **self._legacy_attributes("read-only"),
                "xdg:schema": "org.freedesktop.Secret.Generic",
            },
        )
        collection = _SecretCollection([source])

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            copy_legacy_api_keys_to_default(self._settings())

        self.assertEqual(
            collection.created,
            [
                (
                    "Immich On-Demand default read-only API key",
                    self._attributes("default"),
                    b"read-secret",
                    False,
                )
            ],
        )

    def test_copy_legacy_api_keys_preflights_an_orphan_mutation_destination(self) -> None:
        read = _SecretItem("read-secret", self._legacy_attributes("read-only"))
        orphan = _SecretItem(
            "mutation-secret", self._attributes("default", "mutation")
        )
        collection = _SecretCollection([read, orphan])

        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "could not copy legacy API keys"),
        ):
            copy_legacy_api_keys_to_default(self._settings())

        self.assertEqual(collection.created, [])
        self.assertFalse(read.deleted)
        self.assertFalse(orphan.deleted)

    def test_copy_legacy_api_keys_rejects_unusable_read_sources_before_writing(self) -> None:
        cases = (
            [],
            [
                _SecretItem("first", self._legacy_attributes("read-only")),
                _SecretItem(
                    "second",
                    {
                        **self._legacy_attributes("read-only"),
                        "server": "photos.example.test",
                    },
                ),
            ],
            [_SecretItem(b"", self._legacy_attributes("read-only"))],
            [_SecretItem(b"\xff", self._legacy_attributes("read-only"))],
        )
        for items in cases:
            with self.subTest(items=items):
                collection = _SecretCollection(items)
                with (
                    patch(
                        "immich_on_demand.settings._secret_collection",
                        return_value=collection,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError, "could not copy legacy API keys"
                    ),
                ):
                    copy_legacy_api_keys_to_default(self._settings())

                self.assertEqual(collection.created, [])
                self.assertTrue(all(not item.deleted for item in items))

    def test_copy_legacy_api_keys_ignores_hostname_source_on_a_nondefault_port(self) -> None:
        hostname = _SecretItem(
            "wrong-port-secret",
            {
                "application": "immich-on-demand",
                "server": "photos.example.test",
                "purpose": "read-only",
            },
        )
        collection = _SecretCollection([hostname])
        settings = Settings("https://photos.example.test:8443", Path("/Photos"))

        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "could not copy legacy API keys"),
        ):
            copy_legacy_api_keys_to_default(settings)

        self.assertEqual(
            collection.searches,
            [
                {
                    "application": "immich-on-demand",
                    "server": "https://photos.example.test:8443",
                    "purpose": "read-only",
                }
            ],
        )
        self.assertEqual(collection.created, [])

    def test_copy_legacy_api_keys_resumes_a_matching_read_destination(self) -> None:
        source = _SecretItem("read-secret", self._legacy_attributes("read-only"))
        destination = _SecretItem(
            "read-secret", self._attributes("default", "read-only")
        )
        collection = _SecretCollection([source, destination])

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            copy_legacy_api_keys_to_default(self._settings())

        self.assertEqual(collection.created, [])
        self.assertFalse(source.deleted)
        self.assertFalse(destination.deleted)

    def test_copy_legacy_api_keys_keeps_a_created_destination_after_read_back_failure(
        self,
    ) -> None:
        class ChangedCollection(_SecretCollection):
            def create_item(self, *args: object, **kwargs: object) -> _SecretItem:
                item = super().create_item(*args, **kwargs)
                item.secret = b"concurrent-secret"
                return item

        source = _SecretItem("read-secret", self._legacy_attributes("read-only"))
        collection = ChangedCollection([source])
        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "could not copy legacy API keys"),
        ):
            copy_legacy_api_keys_to_default(self._settings())

        self.assertEqual(len(collection.created), 1)
        self.assertEqual(collection.created[0][-1], False)
        self.assertEqual(len(collection.items), 2)
        self.assertTrue(all(not item.deleted for item in collection.items))

    def test_server_origin_is_canonical_and_ipv6_safe(self) -> None:
        cases = (
            ("https://PHOTOS.Example.TEST", "https://photos.example.test"),
            ("https://PHOTOS.Example.TEST:443", "https://photos.example.test"),
            ("https://PHOTOS.Example.TEST:8443", "https://photos.example.test:8443"),
            ("https://[2001:DB8::1]:443", "https://[2001:db8::1]"),
            ("https://[2001:DB8::1]:8443", "https://[2001:db8::1]:8443"),
        )

        for server_url, expected in cases:
            with self.subTest(server_url=server_url):
                settings = Settings(server_url, Path("/Photos"))
                self.assertEqual(settings.server_origin, expected)

    def test_load_api_key_uses_only_the_exact_profile_item(self) -> None:
        attributes = self._attributes("home")
        exact = _SecretItem(
            "home-secret",
            {
                **attributes,
                "xdg:schema": "org.freedesktop.Secret.Generic",
            },
        )
        collection = _SecretCollection(
            [
                _SecretItem("wrong-secret", {**attributes, "extra": "value"}),
                _SecretItem(
                    "wrong-schema",
                    {**attributes, "xdg:schema": "unexpected"},
                ),
                exact,
            ]
        )

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            secret = load_api_key(self._settings(), profile_id="home")

        self.assertEqual(secret, "home-secret")
        self.assertEqual(collection.searches, [attributes])

    def test_load_api_key_keeps_same_server_profiles_isolated(self) -> None:
        collection = _SecretCollection(
            [
                _SecretItem("home-secret", self._attributes("home")),
                _SecretItem("work-secret", self._attributes("work")),
            ]
        )

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            home = load_api_key(self._settings(), profile_id="home")
            work = load_api_key(self._settings(), profile_id="work")

        self.assertEqual((home, work), ("home-secret", "work-secret"))
        self.assertEqual(
            [attributes["profile"] for attributes in collection.searches],
            ["home", "work"],
        )

    def test_load_api_key_never_falls_back_to_an_unprofiled_item(self) -> None:
        legacy = _SecretItem(
            "legacy-secret",
            {
                "application": "immich-on-demand",
                "server": "https://photos.example.test",
                "purpose": "read-only",
            },
        )
        collection = _SecretCollection([legacy])

        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "found 0"),
        ):
            load_api_key(self._settings(), profile_id="default")

        self.assertFalse(legacy.deleted)
        self.assertEqual(collection.searches, [self._attributes("default")])
        self.assertEqual(collection.created, [])

    def test_load_api_key_rejects_duplicate_exact_items(self) -> None:
        attributes = self._attributes("home")
        collection = _SecretCollection(
            [_SecretItem("first", attributes), _SecretItem("second", attributes)]
        )
        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "found 2"),
        ):
            load_api_key(self._settings(), profile_id="home")

    def test_store_api_key_replaces_and_reads_back_the_exact_profile_item(self) -> None:
        attributes = self._attributes("home", "mutation")
        old = _SecretItem("old-secret", attributes)
        superset = _SecretItem("other-secret", {**attributes, "extra": "value"})
        collection = _SecretCollection([old, superset])

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            store_api_key(
                self._settings(),
                "mutation",
                "new-secret",
                profile_id="home",
            )

        self.assertEqual(
            collection.created,
            [
                (
                    "Immich On-Demand home mutation API key",
                    attributes,
                    b"new-secret",
                    True,
                )
            ],
        )
        self.assertTrue(old.deleted)
        self.assertFalse(superset.deleted)
        self.assertEqual(collection.searches, [attributes])

    def test_store_api_key_does_not_search_or_delete_legacy_items(self) -> None:
        legacy = _SecretItem(
            "legacy-secret",
            {
                "application": "immich-on-demand",
                "server": "photos.example.test",
                "purpose": "read-only",
            },
        )
        collection = _SecretCollection([legacy])

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            store_api_key(
                self._settings(),
                "read-only",
                "new-secret",
                profile_id="default",
            )

        self.assertFalse(legacy.deleted)
        self.assertEqual(collection.searches, [self._attributes("default")])

    def test_store_api_key_rejects_a_concurrent_read_back_change(self) -> None:
        class ChangedCollection(_SecretCollection):
            def create_item(self, *args: object, **kwargs: object) -> _SecretItem:
                item = super().create_item(*args, **kwargs)
                item.secret = b"concurrent-secret"
                return item

        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=ChangedCollection(),
            ),
            self.assertRaisesRegex(RuntimeError, "could not store API key"),
        ):
            store_api_key(
                self._settings(),
                "read-only",
                "requested-secret",
                profile_id="home",
            )

    def test_store_api_key_rejects_invalid_values_before_secret_service(self) -> None:
        with patch("immich_on_demand.settings._secret_collection") as collection:
            for purpose, secret in (("unknown", "secret"), ("read-only", "")):
                with self.subTest(purpose=purpose), self.assertRaises(ValueError):
                    store_api_key(
                        self._settings(),
                        purpose,
                        secret,
                        profile_id="home",
                    )
        collection.assert_not_called()

    def test_secret_service_failures_never_echo_a_key(self) -> None:
        with patch(
            "immich_on_demand.settings._secret_collection",
            side_effect=RuntimeError("backend echoed replacement-secret"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                store_api_key(
                    self._settings(),
                    "read-only",
                    "replacement-secret",
                    profile_id="home",
                )
        self.assertEqual(
            str(raised.exception),
            "could not store API key in Secret Service",
        )
        self.assertNotIn("replacement-secret", str(raised.exception))

        with patch(
            "immich_on_demand.settings._secret_collection",
            side_effect=RuntimeError("backend echoed stored-secret"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                load_api_key(self._settings(), profile_id="home")
        self.assertEqual(
            str(raised.exception),
            "could not read API key from Secret Service",
        )
        self.assertNotIn("stored-secret", str(raised.exception))

    def test_config_paths_are_required(self) -> None:
        with self.assertRaises(TypeError):
            load()  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            save(self._settings())  # type: ignore[call-arg]

    def test_config_round_trip_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            expected = Settings(
                "https://photos.example.test", Path(directory) / "Photos"
            )
            save(expected, path)

            self.assertEqual(load(path), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn("api", json.loads(path.read_text()))

    def test_load_rejects_an_unsafe_config_file(self) -> None:
        for kind in ("mode", "hard-link", "symlink", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "config.json"
                if kind == "directory":
                    path.mkdir(mode=0o700)
                else:
                    target = path if kind != "symlink" else root / "target.json"
                    self._write_config(target)
                    if kind == "mode":
                        target.chmod(0o644)
                    elif kind == "hard-link":
                        os.link(target, root / "second-link")
                    else:
                        path.symlink_to(target)

                with self.assertRaisesRegex(RuntimeError, "unsafe config file"):
                    load(path)

    def test_load_and_save_reject_an_unsafe_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir(mode=0o700)
            path = private / "config.json"
            self._write_config(path)
            private.chmod(0o755)
            for operation in (lambda: load(path), lambda: save(self._settings(), path)):
                with self.subTest(operation=operation), self.assertRaisesRegex(
                    RuntimeError, "unsafe config directory"
                ):
                    operation()

    def test_load_rejects_an_unsafe_profile_config_ancestor(self) -> None:
        for unsafe_name in ("immich-on-demand", "profiles"):
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as directory:
                application = Path(directory) / "immich-on-demand"
                registry = application / "profiles"
                profile = registry / "home"
                profile.mkdir(mode=0o700, parents=True)
                application.chmod(0o700)
                registry.chmod(0o700)
                self._write_config(profile / "config.json")
                (application if unsafe_name == "immich-on-demand" else registry).chmod(0o777)

                with self.assertRaisesRegex(RuntimeError, "unsafe config directory"):
                    load(profile / "config.json")

    def test_load_rejects_a_symlinked_config_directory_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir(mode=0o700)
            self._write_config(private / "config.json")
            alias = root / "alias"
            alias.symlink_to(private, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "unsafe config directory"):
                load(alias / "config.json")

    def test_save_refuses_an_unsafe_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path)
            path.chmod(0o644)

            with self.assertRaisesRegex(RuntimeError, "unsafe config file"):
                save(self._settings(), path)

    def test_failed_save_keeps_the_old_config_and_removes_the_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            save(self._settings(), path)
            original = path.read_bytes()

            with (
                patch(
                    "immich_on_demand.settings.json.dump",
                    side_effect=RuntimeError("write failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "write failed"),
            ):
                save(
                    Settings("https://changed.example.test", Path("/Changed")),
                    path,
                )

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(tuple(entry.name for entry in root.iterdir()), ("config.json",))

    def test_rejects_an_unsafe_server_url(self) -> None:
        for server_url in (
            "http://photos.example.test",
            "https://user:password@photos.example.test",
            "https://@photos.example.test",
            "https://photos.example.test/immich",
            "https://photos.example.test?key=value",
            "https://photos.example.test:99999",
            "https://2001:db8::1",
            "https://bad host",
        ):
            with self.subTest(server_url=server_url), self.assertRaises(ValueError):
                Settings(server_url, Path("/tmp/Photos"))

    @staticmethod
    def _settings() -> Settings:
        return Settings("https://photos.example.test", Path("/Photos"))

    @staticmethod
    def _attributes(
        profile_id: str, purpose: str = "read-only"
    ) -> dict[str, str]:
        return {
            "application": "immich-on-demand",
            "profile": profile_id,
            "server": "https://photos.example.test",
            "purpose": purpose,
        }

    @staticmethod
    def _legacy_attributes(purpose: str) -> dict[str, str]:
        return {
            "application": "immich-on-demand",
            "server": "https://photos.example.test",
            "purpose": purpose,
        }

    @staticmethod
    def _write_config(path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "mount_path": "/Photos",
                    "server_url": "https://photos.example.test",
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
