import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from immich_on_demand.settings import (
    Settings,
    cache_path,
    config_path,
    load,
    load_api_key,
    runtime_path,
    save,
    state_path,
    store_api_key,
)


class _SecretItem:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.deleted = False
        self.item_path = f"/item/{id(self)}"

    def is_locked(self) -> bool:
        return False

    def get_secret(self) -> bytes:
        return self.secret.encode("utf-8")

    def delete(self) -> None:
        self.deleted = True


class _SearchCollection:
    def __init__(self, responses: dict[str, list[_SecretItem]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, str]] = []
        self.created: list[_SecretItem] = []

    def search_items(self, attributes: dict[str, str]) -> list[_SecretItem]:
        self.calls.append(attributes)
        return [
            item
            for item in self.responses.get(attributes["server"], [])
            if not item.deleted
        ]

    def create_item(
        self,
        label: str,
        attributes: dict[str, str],
        secret: bytes,
        *,
        replace: bool,
    ) -> _SecretItem:
        self.assert_create_args = (label, attributes, replace)
        if replace:
            for existing in self.responses.get(attributes["server"], []):
                existing.deleted = True
        item = _SecretItem(secret.decode("utf-8"))
        self.responses.setdefault(attributes["server"], []).append(item)
        self.created.append(item)
        return item


class SettingsTest(unittest.TestCase):
    def test_rejects_relative_xdg_paths(self) -> None:
        for variable, resolve in (
            ("XDG_CONFIG_HOME", config_path),
            ("XDG_STATE_HOME", state_path),
            ("XDG_CACHE_HOME", cache_path),
            ("XDG_RUNTIME_DIR", runtime_path),
        ):
            with (
                self.subTest(variable=variable),
                patch.dict(os.environ, {variable: "relative/path"}),
                self.assertRaisesRegex(RuntimeError, f"{variable} must be an absolute path"),
            ):
                resolve()

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

    def test_store_api_key_replaces_the_exact_secret_service_item(self) -> None:
        class Collection:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []
                self.searches: list[dict[str, str]] = []

            def create_item(self, *args: object, **kwargs: object) -> object:
                self.calls.append((*args, kwargs))
                return object()

            def search_items(self, attributes: dict[str, str]) -> list[object]:
                self.searches.append(attributes)
                return []

        collection = Collection()
        settings = Settings("https://photos.example.test", Path("/Photos"))
        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            store_api_key(settings, "mutation", "secret")

        self.assertEqual(
            collection.calls,
            [
                (
                    "Immich On-Demand mutation API key",
                    {
                        "application": "immich-on-demand",
                        "server": "https://photos.example.test",
                        "purpose": "mutation",
                    },
                    b"secret",
                    {"replace": True},
                )
            ],
        )
        self.assertEqual(
            collection.searches,
            [
                {
                    "application": "immich-on-demand",
                    "server": "photos.example.test",
                    "purpose": "mutation",
                }
            ],
        )

    def test_store_api_key_removes_the_legacy_hostname_item(self) -> None:
        legacy = _SecretItem("old-secret")
        collection = _SearchCollection({"photos.example.test": [legacy]})
        settings = Settings("https://photos.example.test", Path("/Photos"))

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            store_api_key(settings, "read-only", "new-secret")

        self.assertTrue(legacy.deleted)
        self.assertEqual(collection.created[0].secret, "new-secret")

    def test_store_api_key_rejects_invalid_values_before_secret_service(self) -> None:
        settings = Settings("https://photos.example.test", Path("/Photos"))
        with patch("immich_on_demand.settings._secret_collection") as collection:
            for purpose, secret in (("unknown", "secret"), ("read-only", "")):
                with self.subTest(purpose=purpose), self.assertRaises(ValueError):
                    store_api_key(settings, purpose, secret)
        collection.assert_not_called()

    def test_load_api_key_scopes_same_hostname_by_nondefault_port(self) -> None:
        collection = _SearchCollection(
            {
                "https://photos.example.test:8443": [_SecretItem("port-8443")],
                "https://photos.example.test:9443": [_SecretItem("port-9443")],
            }
        )

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            first = load_api_key(
                Settings("https://photos.example.test:8443", Path("/Photos"))
            )
            second = load_api_key(
                Settings("https://photos.example.test:9443", Path("/Photos"))
            )

        self.assertEqual((first, second), ("port-8443", "port-9443"))
        self.assertEqual(
            [attributes["server"] for attributes in collection.calls],
            [
                "https://photos.example.test:8443",
                "https://photos.example.test:9443",
            ],
        )

    def test_load_api_key_falls_back_to_legacy_hostname_for_default_https(self) -> None:
        legacy = _SecretItem("legacy-secret")
        collection = _SearchCollection({"photos.example.test": [legacy]})
        settings = Settings("https://PHOTOS.Example.TEST:443", Path("/Photos"))

        with patch(
            "immich_on_demand.settings._secret_collection",
            return_value=collection,
        ):
            secret = load_api_key(settings)

        self.assertEqual(secret, "legacy-secret")
        self.assertEqual(
            [attributes["server"] for attributes in collection.calls],
            [
                "https://photos.example.test",
                "photos.example.test",
                "https://photos.example.test",
            ],
        )
        self.assertTrue(legacy.deleted)
        self.assertEqual(collection.created[0].secret, "legacy-secret")

        collection.created[0].delete()
        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "found 0"),
        ):
            load_api_key(settings)

    def test_legacy_migration_failure_never_echoes_the_key(self) -> None:
        class FailingItem(_SecretItem):
            def delete(self) -> None:
                raise RuntimeError(f"backend echoed {self.secret}")

        collection = _SearchCollection(
            {"photos.example.test": [FailingItem("legacy-secret")]}
        )
        settings = Settings("https://photos.example.test", Path("/Photos"))
        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            load_api_key(settings)

        self.assertEqual(
            str(raised.exception),
            "could not migrate API key in Secret Service",
        )
        self.assertNotIn("legacy-secret", str(raised.exception))

    def test_legacy_migration_never_overwrites_a_concurrent_store(self) -> None:
        settings = Settings("https://photos.example.test", Path("/Photos"))

        class InterleavingCollection(_SearchCollection):
            def create_item(
                self,
                label: str,
                attributes: dict[str, str],
                secret: bytes,
                *,
                replace: bool,
            ) -> _SecretItem:
                if not replace and not hasattr(self, "interleaved"):
                    self.interleaved = True
                    store_api_key(settings, "read-only", "new-secret")
                return super().create_item(
                    label,
                    attributes,
                    secret,
                    replace=replace,
                )

        legacy = _SecretItem("legacy-secret")
        collection = InterleavingCollection({"photos.example.test": [legacy]})
        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "could not migrate API key"),
        ):
            load_api_key(settings)

        canonical = collection.search_items(
            {
                "application": "immich-on-demand",
                "server": "https://photos.example.test",
                "purpose": "read-only",
            }
        )
        self.assertEqual([item.secret for item in canonical], ["new-secret"])
        self.assertTrue(legacy.deleted)

    def test_load_api_key_never_uses_legacy_hostname_for_nondefault_port(self) -> None:
        collection = _SearchCollection(
            {"photos.example.test": [_SecretItem("wrong-port-secret")]}
        )
        settings = Settings("https://photos.example.test:8443", Path("/Photos"))

        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "found 0"),
        ):
            load_api_key(settings)

        self.assertEqual(
            [attributes["server"] for attributes in collection.calls],
            ["https://photos.example.test:8443"],
        )

    def test_load_api_key_does_not_fall_back_from_ambiguous_canonical_items(self) -> None:
        collection = _SearchCollection(
            {
                "https://photos.example.test": [
                    _SecretItem("first-canonical"),
                    _SecretItem("second-canonical"),
                ],
                "photos.example.test": [_SecretItem("legacy-secret")],
            }
        )
        settings = Settings("https://photos.example.test", Path("/Photos"))

        with (
            patch(
                "immich_on_demand.settings._secret_collection",
                return_value=collection,
            ),
            self.assertRaisesRegex(RuntimeError, "found 2"),
        ):
            load_api_key(settings)

        self.assertEqual(
            [attributes["server"] for attributes in collection.calls],
            ["https://photos.example.test"],
        )

    def test_secret_service_backend_errors_never_echo_a_key(self) -> None:
        settings = Settings("https://photos.example.test", Path("/Photos"))
        with patch(
            "immich_on_demand.settings._secret_collection",
            side_effect=RuntimeError("backend echoed replacement-secret"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                store_api_key(settings, "read-only", "replacement-secret")

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
                load_api_key(settings)

        self.assertEqual(
            str(raised.exception),
            "could not read API key from Secret Service",
        )
        self.assertNotIn("stored-secret", str(raised.exception))

    def test_round_trip_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            expected = Settings("https://photos.example.test", Path(directory) / "Photos")
            save(expected, path)

            self.assertEqual(load(path), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn("api", json.loads(path.read_text()))

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
