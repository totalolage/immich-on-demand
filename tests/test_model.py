import signal
import unittest
from uuid import UUID

from immich_on_demand.model import Asset, collision_name, safe_filename


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"


def api_asset() -> dict[str, object]:
    return {
        "id": ASSET_ID,
        "ownerId": OWNER_ID,
        "originalFileName": "photo.jpg",
        "originalMimeType": "image/jpeg",
        "fileCreatedAt": "2026-08-25T10:00:00.000Z",
        "fileModifiedAt": "2026-08-25T11:00:00.000Z",
        "updatedAt": "2026-08-25T12:00:00.000Z",
        "checksum": "abc=",
        "visibility": "timeline",
        "isTrashed": False,
        "isOffline": False,
        "libraryId": None,
        "exifInfo": {"fileSizeInByte": 123},
    }


def raise_timeout(*_: object) -> None:
    raise TimeoutError("filename function did not return")


class ModelTest(unittest.TestCase):
    def test_sanitizes_untrusted_filename_without_losing_extension(self) -> None:
        self.assertEqual(safe_filename("../.bad\nname.jpg", ASSET_ID), "_bad_name.jpg")
        self.assertEqual(safe_filename("..", ASSET_ID), ASSET_ID)

    def test_collision_name_contains_complete_asset_identity(self) -> None:
        name = collision_name("photo.jpg", ASSET_ID)
        self.assertEqual(name, f"photo__{ASSET_ID}.jpg")
        self.assertLessEqual(len(name.encode()), 255)

    def test_collision_name_terminates_when_the_suffix_consumes_the_budget(self) -> None:
        name = "a." + "x" * 252
        previous = signal.signal(
            signal.SIGALRM,
            raise_timeout,
        )
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        try:
            result = collision_name(name, ASSET_ID)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

        self.assertLessEqual(len(result.encode("utf-8")), 255)
        self.assertIn(ASSET_ID, result)
        self.assertFalse(result.startswith("."))

    def test_safe_filename_terminates_when_an_overlong_suffix_cannot_fit(self) -> None:
        name = "a." + "x" * 255
        previous = signal.signal(
            signal.SIGALRM,
            raise_timeout,
        )
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        try:
            result = safe_filename(name, ASSET_ID)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

        self.assertLessEqual(len(result.encode("utf-8")), 255)
        self.assertEqual(safe_filename(result, ASSET_ID), result)

    def test_utf8_truncation_preserves_a_useful_extension(self) -> None:
        name = "é" * 200 + ".jpg"

        safe = safe_filename(name, ASSET_ID)
        collided = collision_name(safe, ASSET_ID)

        self.assertLessEqual(len(safe.encode("utf-8")), 255)
        self.assertLessEqual(len(collided.encode("utf-8")), 255)
        self.assertTrue(safe.endswith(".jpg"))
        self.assertTrue(collided.endswith(".jpg"))
        self.assertEqual(safe_filename("é", ASSET_ID, limit=1), ASSET_ID[0])

    def test_parses_asset_at_the_trust_boundary(self) -> None:
        asset = Asset.from_api(api_asset())

        self.assertEqual(UUID(asset.id), UUID(ASSET_ID))
        self.assertEqual(asset.size, 123)
        self.assertTrue(asset.visible)

    def test_rejects_malformed_asset_fields_without_coercion(self) -> None:
        cases = (
            ("id", 123),
            ("ownerId", 123),
            ("originalFileName", 123),
            ("originalMimeType", None),
            ("fileCreatedAt", 123),
            ("fileModifiedAt", False),
            ("updatedAt", 123),
            ("checksum", []),
            ("visibility", 1),
            ("isTrashed", 0),
            ("isOffline", "false"),
            ("libraryId", 123),
        )
        for field, malformed in cases:
            with self.subTest(field=field):
                value = api_asset()
                value[field] = malformed
                with self.assertRaises((TypeError, ValueError)):
                    Asset.from_api(value)

    def test_rejects_malformed_asset_size_without_integer_coercion(self) -> None:
        for malformed in (True, 123.0, "123", -1):
            with self.subTest(size=malformed):
                value = api_asset()
                value["exifInfo"] = {"fileSizeInByte": malformed}
                with self.assertRaises(ValueError):
                    Asset.from_api(value)

        value = api_asset()
        value["exifInfo"] = "not an object"
        with self.assertRaisesRegex(ValueError, "exifInfo"):
            Asset.from_api(value)

    def test_validates_updated_at_as_a_timezone_aware_timestamp(self) -> None:
        for malformed in ("not-a-timestamp", "2026-08-25T12:00:00"):
            with self.subTest(updated_at=malformed):
                value = api_asset()
                value["updatedAt"] = malformed
                with self.assertRaises(ValueError):
                    Asset.from_api(value)

    def test_missing_size_is_not_visible(self) -> None:
        value = {
            "id": ASSET_ID,
            "ownerId": OWNER_ID,
            "originalFileName": "photo.jpg",
            "originalMimeType": "image/jpeg",
            "fileCreatedAt": "2026-08-25T10:00:00+00:00",
            "fileModifiedAt": "2026-08-25T11:00:00+00:00",
            "updatedAt": "2026-08-25T12:00:00+00:00",
            "checksum": "abc=",
            "visibility": "timeline",
            "isTrashed": False,
            "isOffline": False,
            "libraryId": None,
            "exifInfo": None,
        }
        self.assertFalse(Asset.from_api(value).visible)
