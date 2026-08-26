import signal
import unittest
from dataclasses import FrozenInstanceError
from uuid import UUID

from immich_on_demand.model import (
    Album,
    Asset,
    Person,
    collision_name,
    safe_filename,
    timestamp_nanoseconds,
)


ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OWNER_ID = "87654321-4321-4321-8321-cba987654321"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"


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
        "isFavorite": True,
        "livePhotoVideoId": None,
        "localDateTime": "2026-08-25T23:59:58.123",
        "libraryId": None,
        "exifInfo": {"fileSizeInByte": 123},
    }


def raise_timeout(*_: object) -> None:
    raise TimeoutError("filename function did not return")


class ModelTest(unittest.TestCase):
    def test_album_and_person_are_immutable_value_records(self) -> None:
        album = Album(ASSET_ID, "Holiday", "2026-08-25T12:00:00Z", 3)
        person = Person(OWNER_ID, "Filip", False, None)

        self.assertEqual(
            (album.id, album.name, album.updated_at, album.asset_count),
            (ASSET_ID, "Holiday", "2026-08-25T12:00:00Z", 3),
        )
        self.assertEqual(
            (person.id, person.name, person.is_hidden, person.updated_at),
            (OWNER_ID, "Filip", False, None),
        )
        self.assertFalse(hasattr(album, "__dict__"))
        self.assertFalse(hasattr(person, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            album.name = "Changed"  # type: ignore[misc]

    def test_timestamp_conversion_preserves_exact_milliseconds(self) -> None:
        self.assertEqual(
            timestamp_nanoseconds("2026-08-25T12:00:00.123Z"),
            1_787_659_200_123_000_000,
        )

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
        self.assertEqual(asset.local_date, "2026-08-25")
        self.assertTrue(asset.is_favorite)
        self.assertEqual(asset.person_ids, ())
        self.assertTrue(asset.visible)

    def test_parses_sorted_unique_person_ids_when_people_are_present(self) -> None:
        value = api_asset()
        value["people"] = [
            {"id": OTHER_ID},
            {"id": ASSET_ID},
            {"id": OTHER_ID},
        ]

        self.assertEqual(Asset.from_api(value).person_ids, (ASSET_ID, OTHER_ID))

    def test_parses_nullable_canonical_live_photo_video_id(self) -> None:
        value = api_asset()
        value["livePhotoVideoId"] = OTHER_ID

        self.assertEqual(Asset.from_api(value).live_photo_video_id, OTHER_ID)

        value["livePhotoVideoId"] = None
        self.assertIsNone(Asset.from_api(value).live_photo_video_id)

        del value["livePhotoVideoId"]
        self.assertIsNone(Asset.from_api(value).live_photo_video_id)

    def test_rejects_malformed_or_noncanonical_live_photo_video_id(self) -> None:
        for malformed in (True, 123, "not-a-uuid", OTHER_ID.upper(), ASSET_ID):
            with self.subTest(live_photo_video_id=malformed):
                value = api_asset()
                value["livePhotoVideoId"] = malformed
                with self.assertRaises(ValueError):
                    Asset.from_api(value)

    def test_rejects_malformed_or_noncanonical_asset_people(self) -> None:
        cases: tuple[object, ...] = (
            None,
            {},
            ["not-an-object"],
            [{}],
            [{"id": True}],
            [{"id": "not-a-uuid"}],
            [{"id": ASSET_ID.upper()}],
        )
        for people in cases:
            with self.subTest(people=people):
                value = api_asset()
                value["people"] = people
                with self.assertRaises(ValueError):
                    Asset.from_api(value)

    def test_requires_exact_favorite_and_local_datetime_fields(self) -> None:
        cases = (
            ("isFavorite", None),
            ("isFavorite", 0),
            ("isFavorite", "true"),
            ("localDateTime", None),
            ("localDateTime", 123),
        )
        for field, malformed in cases:
            with self.subTest(field=field, malformed=malformed):
                value = api_asset()
                if malformed is None:
                    del value[field]
                else:
                    value[field] = malformed
                with self.assertRaises(ValueError):
                    Asset.from_api(value)

    def test_rejects_malformed_local_datetime(self) -> None:
        for malformed in (
            "not-a-timestamp",
            "2026-02-30T12:00:00",
            "2026-08-25",
        ):
            with self.subTest(local_date_time=malformed):
                value = api_asset()
                value["localDateTime"] = malformed
                with self.assertRaises(ValueError):
                    Asset.from_api(value)

    def test_hand_built_asset_defaults_view_metadata_to_unknown_and_false(self) -> None:
        value = Asset(
            ASSET_ID,
            OWNER_ID,
            "photo.jpg",
            "image/jpeg",
            123,
            1,
            2,
            "2026-08-25T12:00:00Z",
            "abc=",
            "timeline",
            False,
            False,
            None,
        )

        self.assertIsNone(value.local_date)
        self.assertFalse(value.is_favorite)
        self.assertEqual(value.person_ids, ())
        self.assertIsNone(value.live_photo_video_id)

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
            "isFavorite": False,
            "localDateTime": "2026-08-25T10:00:00",
            "libraryId": None,
            "exifInfo": None,
        }
        self.assertFalse(Asset.from_api(value).visible)
