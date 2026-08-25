import unittest
from uuid import UUID

from immich_on_demand.model import Asset, collision_name, safe_filename


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"


class ModelTest(unittest.TestCase):
    def test_sanitizes_untrusted_filename_without_losing_extension(self) -> None:
        self.assertEqual(safe_filename("../.bad\nname.jpg", ASSET_ID), "_bad_name.jpg")
        self.assertEqual(safe_filename("..", ASSET_ID), ASSET_ID)

    def test_collision_name_contains_complete_asset_identity(self) -> None:
        name = collision_name("photo.jpg", ASSET_ID)
        self.assertEqual(name, f"photo__{ASSET_ID}.jpg")
        self.assertLessEqual(len(name.encode()), 255)

    def test_parses_asset_at_the_trust_boundary(self) -> None:
        asset = Asset.from_api(
            {
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
        )

        self.assertEqual(UUID(asset.id), UUID(ASSET_ID))
        self.assertEqual(asset.size, 123)
        self.assertTrue(asset.visible)

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
