from io import BytesIO
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from immich_on_demand.thumbnails import (
    ThumbnailError,
    canonical_file_uri,
    failed_thumbnail_is_current,
    failed_thumbnail_path,
    install_failed_thumbnail,
    install_thumbnail,
    prepare_thumbnail_cache,
    thumbnail_cache_path,
    thumbnail_is_current,
)


def preview(width: int = 800, height: int = 400) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "red").save(output, "JPEG")
    return output.getvalue()


class ThumbnailTest(unittest.TestCase):
    def test_prepares_failure_before_removing_a_stale_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            source = root / "photo.jpg"
            stale = install_thumbnail(
                preview(), source, 1, 1, cache_home=cache, size="xx-large"
            )
            real_unlink = Path.unlink

            def assert_suppressed(path: Path, *args: object, **kwargs: object) -> None:
                if path == stale:
                    self.assertTrue(
                        failed_thumbnail_is_current(
                            source, 2, 1, cache_home=cache
                        )
                    )
                real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

            with patch.object(Path, "unlink", assert_suppressed):
                retained = prepare_thumbnail_cache(source, 2, 1, cache_home=cache)

            self.assertFalse(retained)
            self.assertFalse(stale.exists())

    def test_derives_canonical_uri_and_standard_cache_path(self) -> None:
        path = Path("/home/alice/Photos/../Photos/example one.jpg")
        uri = "file:///home/alice/Photos/example%20one.jpg"

        self.assertEqual(canonical_file_uri(path), uri)
        self.assertEqual(
            thumbnail_cache_path(path, Path("/cache")),
            Path("/cache/thumbnails/large/9374a91bb637576c4923dc06205febd2.png"),
        )

    def test_installs_bounded_private_thumbnail_with_exact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            source = Path(directory) / "missing source.jpg"
            result = install_thumbnail(preview(), source, 1_777_777_777, 987_654, cache_home=cache)

            self.assertEqual(result, thumbnail_cache_path(source, cache))
            self.assertFalse(source.exists())
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((cache / "thumbnails").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(result.parent.stat().st_mode), 0o700)
            self.assertEqual(list(result.parent.glob(f".{result.name}.*")), [])
            encoded = result.read_bytes()
            for key in (b"Thumb::URI", b"Thumb::MTime", b"Thumb::Size"):
                self.assertIn(b"tEXt" + key + b"\0", encoded)
            with Image.open(result) as installed:
                installed.load()
                self.assertEqual(installed.format, "PNG")
                self.assertEqual(installed.mode, "RGBA")
                self.assertEqual(installed.size, (256, 128))
                self.assertEqual(installed.info["Thumb::URI"], canonical_file_uri(source))
                self.assertEqual(installed.info["Thumb::MTime"], "1777777777")
                self.assertEqual(installed.info["Thumb::Size"], "987654")
                self.assertNotIn("interlace", installed.info)

    def test_rejects_invalid_and_decompression_bomb_previews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            source = Path(directory) / "missing.jpg"
            with self.assertRaises(ThumbnailError):
                install_thumbnail(b"not an image", source, 1, 1, cache_home=cache)

            bomb = preview(20, 20)
            previous_limit = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = 100
            try:
                with self.assertRaises(ThumbnailError):
                    install_thumbnail(bomb, source, 1, 1, cache_home=cache)
            finally:
                Image.MAX_IMAGE_PIXELS = previous_limit

            self.assertFalse(thumbnail_cache_path(source, cache).exists())

    def test_installs_private_per_uri_failure_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            source = Path(directory) / "unsupported.raw"
            result = install_failed_thumbnail(source, 42, 1234, cache_home=cache)

            self.assertEqual(result, failed_thumbnail_path(source, cache))
            self.assertEqual(result.parent.relative_to(cache), Path("thumbnails/fail/gnome-thumbnail-factory"))
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
            for parent in (result.parent, result.parent.parent, result.parent.parent.parent):
                self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
            with Image.open(result) as failed:
                failed.load()
                self.assertEqual(failed.size, (1, 1))
                self.assertEqual(failed.info["Thumb::URI"], canonical_file_uri(source))
                self.assertEqual(failed.info["Thumb::MTime"], "42")
                self.assertEqual(failed.info["Thumb::Size"], "1234")

            self.assertTrue(failed_thumbnail_is_current(source, 42, 1234, cache_home=cache))
            self.assertFalse(failed_thumbnail_is_current(source, 43, 1234, cache_home=cache))

    def test_rejects_a_symlinked_thumbnail_root_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            target = root / "target"
            target.mkdir(mode=0o755)
            (cache / "thumbnails").symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(PermissionError, "thumbnail cache root"):
                install_failed_thumbnail(root / "photo.jpg", 1, 1, cache_home=cache)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertEqual(list(target.iterdir()), [])

    def test_rejects_a_non_directory_thumbnail_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            (cache / "thumbnails").write_text("keep")

            with self.assertRaisesRegex(PermissionError, "thumbnail cache root"):
                install_failed_thumbnail(root / "photo.jpg", 1, 1, cache_home=cache)

    def test_rejects_a_thumbnail_root_not_owned_by_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            thumbnail_root = cache / "thumbnails"
            thumbnail_root.mkdir(parents=True)

            with patch(
                "immich_on_demand.thumbnails.os.getuid",
                return_value=thumbnail_root.stat().st_uid + 1,
            ), self.assertRaisesRegex(PermissionError, "thumbnail cache root"):
                install_failed_thumbnail(root / "photo.jpg", 1, 1, cache_home=cache)

    def test_reconciliation_rejects_a_symlinked_size_directory_without_touching_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            thumbnail_root = cache / "thumbnails"
            thumbnail_root.mkdir(parents=True)
            target = root / "target"
            target.mkdir()
            (thumbnail_root / "xx-large").symlink_to(target, target_is_directory=True)
            source = root / "photo.jpg"
            target_entry = target / thumbnail_cache_path(
                source, cache, "xx-large"
            ).name
            target_entry.write_bytes(b"keep")

            with self.assertRaisesRegex(PermissionError, "thumbnail cache directory"):
                prepare_thumbnail_cache(source, 1, 1, cache_home=cache)

            self.assertEqual(target_entry.read_bytes(), b"keep")

    def test_reconciliation_refuses_a_foreign_owned_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            source = root / "photo.jpg"
            cached = install_thumbnail(
                preview(), source, 1, 1, cache_home=cache, size="xx-large"
            )
            real_lstat = os.lstat

            def foreign_entry(path: Path) -> os.stat_result:
                info = real_lstat(path)
                if Path(path) == cached:
                    values = list(info)
                    values[4] = info.st_uid + 1
                    return os.stat_result(values)
                return info

            with patch(
                "immich_on_demand.thumbnails.os.lstat", side_effect=foreign_entry
            ), self.assertRaisesRegex(PermissionError, "thumbnail cache entry"):
                prepare_thumbnail_cache(source, 1, 1, cache_home=cache)

            self.assertTrue(cached.exists())

    def test_reconciliation_tolerates_cleanup_during_entry_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            source = root / "photo.jpg"
            cached = install_thumbnail(
                preview(), source, 1, 1, cache_home=cache, size="xx-large"
            )
            real_lstat = os.lstat
            candidate_checks = 0

            def disappear(path: Path) -> os.stat_result:
                nonlocal candidate_checks
                if Path(path) == cached:
                    candidate_checks += 1
                    if candidate_checks == 2:
                        cached.unlink()
                return real_lstat(path)

            with patch("immich_on_demand.thumbnails.os.lstat", side_effect=disappear):
                self.assertFalse(
                    prepare_thumbnail_cache(source, 1, 1, cache_home=cache)
                )

    def test_reconciliation_tolerates_cleanup_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            source = root / "photo.jpg"
            cached = install_thumbnail(
                preview(), source, 1, 1, cache_home=cache, size="xx-large"
            )
            real_unlink = Path.unlink

            def disappear(path: Path, *args: object, **kwargs: object) -> None:
                if path == cached:
                    real_unlink(path)
                real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

            with patch.object(Path, "unlink", disappear):
                self.assertFalse(
                    prepare_thumbnail_cache(source, 1, 1, cache_home=cache)
                )

    def test_recognizes_only_a_matching_cached_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            source = Path(directory) / "photo.jpg"
            installed = install_thumbnail(preview(), source, 42, 1234, cache_home=cache)

            self.assertTrue(thumbnail_is_current(source, 42, 1234, cache_home=cache))
            self.assertFalse(thumbnail_is_current(source, 43, 1234, cache_home=cache))
            installed.write_bytes(b"broken")
            self.assertFalse(thumbnail_is_current(source, 42, 1234, cache_home=cache))


if __name__ == "__main__":
    unittest.main()
