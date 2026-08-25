from __future__ import annotations

from collections.abc import Iterable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
import base64
import binascii
import hashlib
import hmac
import os
from pathlib import Path
import shutil
import stat as stat_module
import tempfile
import time
from uuid import UUID

import trio

from .immich import ImmichClient
from .model import Asset


class CacheError(RuntimeError):
    pass


class CacheIntegrityError(CacheError):
    pass


class CacheBusyError(CacheError):
    pass


class CacheCapacityError(CacheError):
    pass


@dataclass(slots=True)
class _Hydration:
    done: trio.Event
    error: BaseException | None = None


_Validation = tuple[int, int, int, int, int, str]


class ContentCache:
    """Atomic cache of complete Immich originals."""

    def __init__(
        self,
        root: Path,
        client: ImmichClient,
        *,
        max_bytes: int | None = None,
        minimum_free_bytes: int = 0,
        pinned_ids: Iterable[str] = (),
    ) -> None:
        if (max_bytes is not None and max_bytes < 0) or minimum_free_bytes < 0:
            raise ValueError("cache limits must be non-negative")
        pins = set(pinned_ids)
        for asset_id in pins:
            UUID(asset_id)
        self.root = root
        self.client = client
        self.max_bytes = max_bytes
        self.minimum_free_bytes = minimum_free_bytes
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(root)
        if not stat_module.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("cache root must be a directory owned by this user")
        os.chmod(root, 0o700)
        self._clean_stale_downloads()
        self._hydrations: dict[str, _Hydration] = {}
        self._open: dict[str, int] = {}
        self._reservations: dict[str, int] = {}
        self._written: dict[str, int] = {}
        self._validated: dict[str, _Validation] = {}
        self._pending_discards: set[str] = set()
        self._pinned = pins

    def describe(self, asset: Asset) -> dict[str, bool]:
        UUID(asset.id)
        info = self._complete_info(self.root / asset.id)
        cached = (
            info is not None
            and asset.size is not None
            and info.st_size == asset.size
            and info.st_mtime_ns == self._cache_mtime_ns(asset)
        )
        return {
            "cached": cached,
            "busy": self._busy(asset.id),
            "pinned": asset.id in self._pinned,
        }

    def pin(self, asset_id: str) -> None:
        UUID(asset_id)
        self._pinned.add(asset_id)

    def unpin(self, asset_id: str) -> None:
        UUID(asset_id)
        self._pinned.discard(asset_id)

    def acquire(self, asset_id: str) -> None:
        UUID(asset_id)
        self._open[asset_id] = self._open.get(asset_id, 0) + 1

    def release(self, asset_id: str) -> None:
        count = self._open.get(asset_id, 0)
        if not count:
            raise CacheError(f"asset {asset_id} is not open")
        if count == 1:
            del self._open[asset_id]
            if asset_id in self._pending_discards:
                self._discard(asset_id, self.root / asset_id)
        else:
            self._open[asset_id] = count - 1

    async def hydrate(self, asset: Asset) -> Path:
        hydration = self._hydrations.get(asset.id)
        if hydration is not None:
            await hydration.done.wait()
            if hydration.error is not None:
                if isinstance(hydration.error, CacheBusyError):
                    path = await self._cached_path(asset)
                    if path is not None:
                        self._touch(path, asset)
                        return path
                raise CacheError(f"shared hydration of {asset.id} failed") from hydration.error
            path = await self._cached_path(asset)
            if path is None:
                return await self.hydrate(asset)
            self._touch(path, asset)
            return path

        hydration = _Hydration(trio.Event())
        self._hydrations[asset.id] = hydration
        try:
            path = await self._cached_path(asset)
            if path is None:
                self._reserve(asset)
                # ponytail: whole-file caching is the 1.0 ceiling; add sparse ranges only
                # when Immich documents original-download range semantics.
                path = await self._download(asset)
            self._touch(path, asset)
            return path
        except BaseException as error:
            hydration.error = error
            raise
        finally:
            self._reservations.pop(asset.id, None)
            self._written.pop(asset.id, None)
            hydration.done.set()
            self._hydrations.pop(asset.id, None)

    async def read(self, asset: Asset, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise ValueError("offset and size must be non-negative")
        self.acquire(asset.id)
        try:
            path = await self.hydrate(asset)
            if self._complete_info(path) is None:
                raise CacheError(f"cached original disappeared: {asset.id}")
            async with await trio.open_file(path, "rb") as stream:
                await stream.seek(offset)
                return await stream.read(size)
        finally:
            self.release(asset.id)

    def evict(self, asset_id: str) -> bool:
        UUID(asset_id)
        if asset_id in self._pinned:
            raise CacheError(f"asset {asset_id} is pinned")
        if self._busy(asset_id):
            raise CacheBusyError(f"asset {asset_id} is open or being hydrated")
        path = self.root / asset_id
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        self._validated.pop(asset_id, None)
        return True

    def evict_to_limits(
        self,
        *,
        max_age_seconds: int,
        max_bytes: int,
        minimum_free_bytes: int,
        now_ns: int | None = None,
    ) -> list[str]:
        if min(max_age_seconds, max_bytes, minimum_free_bytes) < 0:
            raise ValueError("cache limits must be non-negative")

        now = time.time_ns() if now_ns is None else now_ns
        entries = self._complete_entries()
        total = sum(stat.st_size for _, stat in entries.values())
        free = shutil.disk_usage(self.root).free
        removed: list[str] = []

        for asset_id, (path, stat) in sorted(
            entries.items(), key=lambda item: (item[1][1].st_atime_ns, item[0])
        ):
            expired = now - stat.st_atime_ns > max_age_seconds * 1_000_000_000
            over_limit = total > max_bytes or free < minimum_free_bytes
            if not expired and not over_limit:
                continue
            if asset_id in self._pinned:
                continue
            if self._busy(asset_id):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            self._validated.pop(asset_id, None)
            total -= stat.st_size
            free += stat.st_size
            removed.append(asset_id)
        return removed

    async def _download(self, asset: Asset) -> Path:
        UUID(asset.id)
        if asset.size is None:
            raise CacheIntegrityError(f"asset {asset.id} has no expected size")

        handle, temporary_name = tempfile.mkstemp(prefix=f".{asset.id}.", dir=self.root)
        temporary = Path(temporary_name)
        destination = self.root / asset.id
        digest = hashlib.sha1(usedforsecurity=False)
        received = 0
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "wb", buffering=0) as stream:
                async with self.client.original(asset.id) as response:
                    async with aclosing(response.aiter_bytes()) as chunks:
                        async for chunk in chunks:
                            if not chunk:
                                continue
                            if received + len(chunk) > asset.size:
                                raise CacheIntegrityError(
                                    f"asset {asset.id} exceeds its expected {asset.size} bytes"
                                )
                            if (
                                shutil.disk_usage(self.root).free - len(chunk)
                                < self.minimum_free_bytes
                            ):
                                raise CacheCapacityError(
                                    f"asset {asset.id} would cross the cache free-space floor"
                                )
                            if stream.write(chunk) != len(chunk):
                                raise CacheError(f"short cache write for asset {asset.id}")
                            digest.update(chunk)
                            received += len(chunk)
                            self._written[asset.id] = received

                if received != asset.size:
                    raise CacheIntegrityError(
                        f"asset {asset.id} expected {asset.size} bytes, received {received}"
                    )
                if asset.library_id is None and not self._checksum_matches(
                    asset.checksum, digest.digest()
                ):
                    raise CacheIntegrityError(f"asset {asset.id} checksum does not match")
                stream.flush()
                os.fsync(stream.fileno())
                os.utime(stream.fileno(), ns=(time.time_ns(), self._cache_mtime_ns(asset)))
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return destination
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def _cached_path(self, asset: Asset) -> Path | None:
        UUID(asset.id)
        path = self.root / asset.id
        info = self._complete_info(path)
        if info is None:
            return None
        if asset.size is None or info.st_size != asset.size:
            self._discard(asset.id, path)
            return None
        if asset.library_id is not None:
            if info.st_mtime_ns != self._cache_mtime_ns(asset):
                self._discard(asset.id, path)
                return None
            return path

        validation = self._validation(asset, info)
        if self._validated.get(asset.id) != validation:
            try:
                actual = await trio.to_thread.run_sync(self._file_sha1, path)
                current = self._complete_info(path)
                unchanged = (
                    current is not None and self._validation(asset, current) == validation
                )
            except FileNotFoundError:
                self._validated.pop(asset.id, None)
                return None
            if not unchanged or not self._checksum_matches(asset.checksum, actual):
                self._discard(asset.id, path)
                return None
        return path

    @staticmethod
    def _checksum_matches(expected: str, actual: bytes) -> bool:
        try:
            decoded = base64.b64decode(expected, validate=True)
        except (binascii.Error, ValueError):
            return False
        return hmac.compare_digest(decoded, actual)

    @staticmethod
    def _file_sha1(path: Path) -> bytes:
        digest = hashlib.sha1(usedforsecurity=False)
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.digest()

    def _touch(self, path: Path, asset: Asset) -> None:
        info = self._complete_info(path)
        if info is None:
            raise CacheError(f"cached original disappeared: {asset.id}")
        os.utime(path, ns=(time.time_ns(), info.st_mtime_ns), follow_symlinks=False)
        current = self._complete_info(path)
        if current is None:
            raise CacheError(f"cached original disappeared: {asset.id}")
        self._validated[asset.id] = self._validation(asset, current)

    @staticmethod
    def _complete_info(path: Path) -> os.stat_result | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat_module.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(
                f"cached original must be a regular file owned by this user: {path}"
            )
        return info

    @staticmethod
    def _validation(asset: Asset, info: os.stat_result) -> _Validation:
        source = (
            f"managed:{asset.checksum}"
            if asset.library_id is None
            else f"external:{asset.updated_at}"
        )
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            source,
        )

    @staticmethod
    def _cache_mtime_ns(asset: Asset) -> int:
        if asset.library_id is None:
            return asset.modified_ns
        parsed = datetime.fromisoformat(asset.updated_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("external asset updated_at has no timezone")
        return int(parsed.timestamp() * 1_000_000_000)

    def _discard(self, asset_id: str, path: Path) -> None:
        if self._open.get(asset_id, 0) > 0:
            self._pending_discards.add(asset_id)
            raise CacheBusyError(f"asset {asset_id} changed while open")
        path.unlink(missing_ok=True)
        self._pending_discards.discard(asset_id)
        self._validated.pop(asset_id, None)

    def _busy(self, asset_id: str) -> bool:
        return asset_id in self._hydrations or self._open.get(asset_id, 0) > 0

    def _reserve(self, asset: Asset) -> None:
        if asset.size is None:
            raise CacheIntegrityError(f"asset {asset.id} has no expected size")
        if self.max_bytes is not None and asset.size > self.max_bytes:
            raise CacheCapacityError(f"asset {asset.id} exceeds cache capacity")

        reserved = sum(self._reservations.values())
        remaining = sum(
            size - self._written.get(asset_id, 0)
            for asset_id, size in self._reservations.items()
        )
        entries = self._complete_entries()
        total = sum(info.st_size for _, info in entries.values())
        free = shutil.disk_usage(self.root).free

        def fits(candidate_total: int, candidate_free: int) -> bool:
            return (
                (
                    self.max_bytes is None
                    or candidate_total + reserved + asset.size <= self.max_bytes
                )
                and candidate_free - remaining - asset.size >= self.minimum_free_bytes
            )

        evictable = [
            (asset_id, path, info)
            for asset_id, (path, info) in sorted(
                entries.items(), key=lambda item: (item[1][1].st_atime_ns, item[0])
            )
            if asset_id not in self._pinned and not self._busy(asset_id)
        ]
        reclaimable = sum(info.st_size for _, _, info in evictable)
        if not fits(total - reclaimable, free + reclaimable):
            raise CacheCapacityError(f"asset {asset.id} cannot fit within cache capacity")

        for asset_id, path, info in evictable:
            if fits(total, free):
                break
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            self._validated.pop(asset_id, None)
            total -= info.st_size
            free += info.st_size

        if not fits(total, free):
            raise CacheCapacityError(f"asset {asset.id} cannot fit within cache capacity")
        self._reservations[asset.id] = asset.size
        self._written[asset.id] = 0

    def _clean_stale_downloads(self) -> None:
        with os.scandir(self.root) as directory:
            for entry in directory:
                parts = entry.name.split(".", 2)
                if len(parts) != 3 or parts[0] or not parts[2]:
                    continue
                try:
                    UUID(parts[1])
                    info = entry.stat(follow_symlinks=False)
                except (FileNotFoundError, ValueError):
                    continue
                if stat_module.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                    Path(entry.path).unlink(missing_ok=True)

    def _complete_entries(self) -> dict[str, tuple[Path, os.stat_result]]:
        entries: dict[str, tuple[Path, os.stat_result]] = {}
        with os.scandir(self.root) as directory:
            for entry in directory:
                if entry.name.startswith("."):
                    continue
                try:
                    UUID(entry.name)
                    info = entry.stat(follow_symlinks=False)
                except (FileNotFoundError, ValueError):
                    continue
                if not stat_module.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    continue
                entries[entry.name] = (Path(entry.path), info)
        return entries
