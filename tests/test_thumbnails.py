from io import BytesIO
from pathlib import Path
import stat
import tempfile
import unittest

from PIL import Image

from immich_on_demand.thumbnails import (
    ThumbnailError,
    canonical_file_uri,
    failed_thumbnail_path,
    install_failed_thumbnail,
    install_thumbnail,
    thumbnail_cache_path,
)


def preview(width: int = 800, height: int = 400) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "red").save(output, "JPEG")
    return output.getvalue()


class ThumbnailTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
