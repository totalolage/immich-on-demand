import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from immich_on_demand.settings import (
    Settings,
    load,
    load_api_key,
    save,
    store_api_key,
)


class SettingsTest(unittest.TestCase):
    def test_store_api_key_replaces_the_exact_secret_service_item(self) -> None:
        class Collection:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def create_item(self, *args: object, **kwargs: object) -> object:
                self.calls.append((*args, kwargs))
                return object()

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
                        "server": "photos.example.test",
                        "purpose": "mutation",
                    },
                    b"secret",
                    {"replace": True},
                )
            ],
        )

    def test_store_api_key_rejects_invalid_values_before_secret_service(self) -> None:
        settings = Settings("https://photos.example.test", Path("/Photos"))
        with patch("immich_on_demand.settings._secret_collection") as collection:
            for purpose, secret in (("unknown", "secret"), ("read-only", "")):
                with self.subTest(purpose=purpose), self.assertRaises(ValueError):
                    store_api_key(settings, purpose, secret)
        collection.assert_not_called()

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
        ):
            with self.subTest(server_url=server_url), self.assertRaises(ValueError):
                Settings(server_url, Path("/tmp/Photos"))
