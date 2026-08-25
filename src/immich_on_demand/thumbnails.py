from __future__ import annotations

from hashlib import md5
from io import BytesIO
import os
from pathlib import Path
import stat
import tempfile
import warnings

from PIL import Image, ImageOps, PngImagePlugin, UnidentifiedImageError


THUMBNAIL_SIZES = {"normal": 128, "large": 256, "x-large": 512, "xx-large": 1024}
MAX_PREVIEW_BYTES = 32 * 1024**2


class ThumbnailError(ValueError):
    pass


def canonical_file_uri(path: Path) -> str:
    """Return the absolute, lexically normalized file URI used as the cache key."""
    return Path(os.path.abspath(os.fspath(path))).as_uri()


def _cache_home(cache_home: Path | None) -> Path:
    if cache_home is not None:
        return cache_home
    configured = os.environ.get("XDG_CACHE_HOME")
    return Path(configured) if configured else Path.home() / ".cache"


def _cache_name(path: Path) -> str:
    uri = canonical_file_uri(path)
    return f"{md5(uri.encode(), usedforsecurity=False).hexdigest()}.png"


def thumbnail_cache_path(path: Path, cache_home: Path | None = None, size: str = "large") -> Path:
    if size not in THUMBNAIL_SIZES:
        raise ValueError(f"unknown thumbnail size: {size}")
    return _cache_home(cache_home) / "thumbnails" / size / _cache_name(path)


def failed_thumbnail_path(path: Path, cache_home: Path | None = None) -> Path:
    return (
        _cache_home(cache_home)
        / "thumbnails"
        / "fail"
        / "gnome-thumbnail-factory"
        / _cache_name(path)
    )


def _png_metadata_matches(path: Path, expected: dict[str, str]) -> bool:
    try:
        with Image.open(path) as image:
            image.load()
            return image.format == "PNG" and all(
                image.info.get(key) == value for key, value in expected.items()
            )
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError):
        return False


def thumbnail_is_current(
    source_path: Path,
    mtime: int,
    original_size: int,
    *,
    cache_home: Path | None = None,
    size: str = "large",
) -> bool:
    expected = {
        "Thumb::URI": canonical_file_uri(source_path),
        "Thumb::MTime": str(mtime),
        "Thumb::Size": str(original_size),
    }
    return _png_metadata_matches(thumbnail_cache_path(source_path, cache_home, size), expected)


def failed_thumbnail_is_current(
    source_path: Path,
    mtime: int,
    original_size: int,
    *,
    cache_home: Path | None = None,
) -> bool:
    expected = {
        "Thumb::URI": canonical_file_uri(source_path),
        "Thumb::MTime": str(mtime),
        "Thumb::Size": str(original_size),
    }
    return _png_metadata_matches(failed_thumbnail_path(source_path, cache_home), expected)


def _metadata(path: Path, mtime: int, original_size: int) -> PngImagePlugin.PngInfo:
    if type(mtime) is not int or type(original_size) is not int or mtime < 0 or original_size < 0:
        raise ValueError("thumbnail mtime and size must be non-negative integers")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Thumb::URI", canonical_file_uri(path))
    metadata.add_text("Thumb::MTime", str(mtime))
    metadata.add_text("Thumb::Size", str(original_size))
    return metadata


def _private_cache_directory(destination: Path) -> None:
    thumbnail_root = next(parent for parent in destination.parents if parent.name == "thumbnails")
    try:
        thumbnail_root.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    info = os.lstat(thumbnail_root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(
            "thumbnail cache root must be a directory owned by this user"
        )
    current = thumbnail_root
    os.chmod(current, 0o700)
    for part in destination.parent.relative_to(thumbnail_root).parts:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(
                "thumbnail cache directory must be owned by this user"
            )
        os.chmod(current, 0o700)


def _atomic_png(
    destination: Path,
    image: Image.Image,
    metadata: PngImagePlugin.PngInfo,
    expected: dict[str, str],
) -> Path:
    _private_cache_directory(destination)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as output:
            image.save(output, format="PNG", pnginfo=metadata, interlace=False)
            output.flush()
            os.fsync(output.fileno())
            output.seek(0)
            with Image.open(output) as installed:
                installed.load()
                if (
                    installed.format != "PNG"
                    or installed.mode not in {"RGB", "RGBA"}
                    or installed.info.get("interlace")
                    or any(installed.info.get(key) != value for key, value in expected.items())
                ):
                    raise ThumbnailError("generated thumbnail failed validation")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def install_thumbnail(
    preview: bytes,
    source_path: Path,
    mtime: int,
    original_size: int,
    *,
    cache_home: Path | None = None,
    size: str = "large",
) -> Path:
    """Install an Immich preview without opening the mounted source asset."""
    if len(preview) > MAX_PREVIEW_BYTES:
        raise ThumbnailError("preview exceeds 32 MiB")
    destination = thumbnail_cache_path(source_path, cache_home, size)
    values = {
        "Thumb::URI": canonical_file_uri(source_path),
        "Thumb::MTime": str(mtime),
        "Thumb::Size": str(original_size),
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(preview)) as opened:
                opened.load()
                image = ImageOps.exif_transpose(opened).convert("RGBA")
                image.thumbnail((THUMBNAIL_SIZES[size],) * 2, Image.Resampling.LANCZOS)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError) as error:
        raise ThumbnailError("invalid or unsafe preview") from error
    return _atomic_png(destination, image, _metadata(source_path, mtime, original_size), values)


def install_failed_thumbnail(
    source_path: Path,
    mtime: int,
    original_size: int,
    *,
    cache_home: Path | None = None,
) -> Path:
    """Suppress desktop fallback thumbnailers for one mounted file URI."""
    destination = failed_thumbnail_path(source_path, cache_home)
    values = {
        "Thumb::URI": canonical_file_uri(source_path),
        "Thumb::MTime": str(mtime),
        "Thumb::Size": str(original_size),
    }
    image = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    return _atomic_png(destination, image, _metadata(source_path, mtime, original_size), values)
