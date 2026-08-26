from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterable
from uuid import UUID

from .model import Asset, collision_name, safe_filename


ROOT_INODE = 1


def _require_owned_directory(path: Path) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("catalog state directory must be owned by this user")
    os.chmod(path, 0o700)


def _open_database(path: Path) -> int:
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _open_database(path)
    except OSError as error:
        raise PermissionError(
            "catalog database must be a regular file owned by this user"
        ) from error
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(descriptor)
        raise PermissionError("catalog database must be a regular file owned by this user")
    os.fchmod(descriptor, 0o600)
    return descriptor


def _prepare_auxiliary_files(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        auxiliary = Path(f"{path}{suffix}")
        try:
            info = os.lstat(auxiliary)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(
                "catalog auxiliary files must be regular files owned by this user"
            )
        os.chmod(auxiliary, 0o600)


def _available_name(original: str, asset_id: str, used: set[str]) -> str:
    base = safe_filename(original, asset_id)
    if base not in used:
        return base
    for ordinal in range(1, len(used) + 2):
        candidate = collision_name(base, asset_id, ordinal=ordinal)
        if candidate not in used:
            return candidate
    raise AssertionError("bounded collision search exhausted")


@dataclass(frozen=True, slots=True)
class CatalogAsset:
    asset: Asset
    inode: int
    name: str


@dataclass(frozen=True, slots=True)
class CatalogStats:
    total: int
    visible: int
    missing_size: int
    trashed: int
    hidden: int
    offline: int


class Catalog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_owned_directory(path.parent)
        descriptor = _open_database(path)
        connection: sqlite3.Connection | None = None
        try:
            _prepare_auxiliary_files(path)
            connection = sqlite3.connect(f"/proc/self/fd/{descriptor}")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            self._connection = connection
            self._database_descriptor = descriptor
            self._create_schema()
        except Exception:
            if connection is not None:
                connection.close()
            os.close(descriptor)
            raise

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            os.close(self._database_descriptor)

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                inode INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER,
                created_ns INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                checksum TEXT NOT NULL,
                visibility TEXT NOT NULL,
                is_trashed INTEGER NOT NULL,
                is_offline INTEGER NOT NULL,
                library_id TEXT
            );
            CREATE INDEX IF NOT EXISTS assets_visible_name
                ON assets(is_trashed, is_offline, visibility, name);
            CREATE TABLE IF NOT EXISTS incoming_assets (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER,
                created_ns INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                checksum TEXT NOT NULL,
                visibility TEXT NOT NULL,
                is_trashed INTEGER NOT NULL,
                is_offline INTEGER NOT NULL,
                library_id TEXT
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pins (
                asset_id TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('next_inode', 2);
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('high_water_ms', 0);
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('full_refresh_pages', 0);
            """
        )
        self._connection.commit()

    def begin_refresh(self) -> None:
        self._connection.execute("DELETE FROM incoming_assets")
        self._connection.commit()

    def stage(self, assets: Iterable[Asset]) -> None:
        self._connection.executemany(
            """
            INSERT OR REPLACE INTO incoming_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    asset.id,
                    asset.owner_id,
                    asset.original_name,
                    asset.mime_type,
                    asset.size,
                    asset.created_ns,
                    asset.modified_ns,
                    asset.updated_at,
                    asset.checksum,
                    asset.visibility,
                    asset.is_trashed,
                    asset.is_offline,
                    asset.library_id,
                )
                for asset in assets
            ),
        )
        self._connection.commit()

    def finish_refresh(
        self,
        *,
        high_water_ms: int,
        page_count: int,
    ) -> CatalogStats:
        self._validate_refresh_state(high_water_ms, page_count)
        return self._finish_staged(
            delete_missing=True,
            high_water_ms=high_water_ms,
            page_count=page_count,
        )

    def finish_incremental(self, *, high_water_ms: int) -> CatalogStats:
        self._validate_refresh_state(high_water_ms, None)
        return self._finish_staged(
            delete_missing=False,
            high_water_ms=high_water_ms,
            page_count=None,
        )

    @staticmethod
    def _validate_refresh_state(high_water_ms: int, page_count: int | None) -> None:
        if type(high_water_ms) is not int or high_water_ms < 0:
            raise ValueError("high_water_ms must be a non-negative integer")
        if page_count is not None and (type(page_count) is not int or page_count < 1):
            raise ValueError("page_count must be a positive integer")

    def _finish_staged(
        self,
        *,
        delete_missing: bool,
        high_water_ms: int,
        page_count: int | None,
    ) -> CatalogStats:
        with self._connection:
            if delete_missing:
                self._connection.execute(
                    "DELETE FROM assets WHERE id NOT IN (SELECT id FROM incoming_assets)"
                )
            existing = {
                row["id"]: (row["inode"], row["name"])
                for row in self._connection.execute("SELECT id, inode, name FROM assets")
            }
            used_names = {name for _, name in existing.values()}
            next_inode = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'next_inode'"
            ).fetchone()[0]
            rows = self._connection.execute(
                "SELECT * FROM incoming_assets ORDER BY created_ns, id"
            ).fetchall()
            for row in rows:
                identity = existing.get(row["id"])
                if identity:
                    inode, name = identity
                else:
                    inode = next_inode
                    next_inode += 1
                    name = _available_name(row["original_name"], row["id"], used_names)
                    used_names.add(name)
                self._connection.execute(
                    """
                    INSERT OR REPLACE INTO assets
                    SELECT id, ?, ?, owner_id, original_name, mime_type, size, created_ns,
                           modified_ns, updated_at, checksum, visibility, is_trashed,
                           is_offline, library_id
                      FROM incoming_assets WHERE id = ?
                    """,
                    (inode, name, row["id"]),
                )
            self._connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'next_inode'", (next_inode,)
            )
            if page_count is not None:
                self._connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'high_water_ms'",
                    (high_water_ms,),
                )
                self._connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'full_refresh_pages'",
                    (page_count,),
                )
            else:
                self._connection.execute(
                    "UPDATE metadata SET value = max(value, ?) WHERE key = 'high_water_ms'",
                    (high_water_ms,),
                )
            self._connection.execute("DELETE FROM incoming_assets")
        return self.stats()

    def refresh_state(self) -> tuple[int, int]:
        values = {
            row["key"]: row["value"]
            for row in self._connection.execute(
                "SELECT key, value FROM metadata WHERE key IN ('high_water_ms', 'full_refresh_pages')"
            )
        }
        return int(values["high_water_ms"]), int(values["full_refresh_pages"])

    def pinned_ids(self) -> frozenset[str]:
        return frozenset(
            row["asset_id"]
            for row in self._connection.execute("SELECT asset_id FROM pins")
        )

    def pin(self, asset_id: str) -> None:
        UUID(asset_id)
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO pins(asset_id) VALUES (?)", (asset_id,)
            )

    def unpin(self, asset_id: str) -> None:
        UUID(asset_id)
        with self._connection:
            self._connection.execute("DELETE FROM pins WHERE asset_id = ?", (asset_id,))

    def stats(self) -> CatalogStats:
        row = self._connection.execute(
            """
            SELECT count(*) AS total,
                   sum(size IS NOT NULL AND is_trashed = 0 AND is_offline = 0 AND visibility != 'hidden') AS visible,
                   sum(size IS NULL) AS missing_size,
                   sum(is_trashed) AS trashed,
                   sum(visibility = 'hidden') AS hidden,
                   sum(is_offline) AS offline
              FROM assets
            """
        ).fetchone()
        return CatalogStats(*(int(row[name] or 0) for name in CatalogStats.__dataclass_fields__))

    def list_visible(self) -> list[CatalogAsset]:
        return [
            self._catalog_asset(row)
            for row in self._connection.execute(
                """
                SELECT * FROM assets
                 WHERE size IS NOT NULL AND is_trashed = 0 AND is_offline = 0 AND visibility != 'hidden'
                 ORDER BY name
                """
            )
        ]

    def by_inode(self, inode: int) -> CatalogAsset | None:
        row = self._connection.execute("SELECT * FROM assets WHERE inode = ?", (inode,)).fetchone()
        return self._catalog_asset(row) if row else None

    def by_id(self, asset_id: str) -> CatalogAsset | None:
        row = self._connection.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        return self._catalog_asset(row) if row else None

    def by_name(self, name: str) -> CatalogAsset | None:
        row = self._connection.execute("SELECT * FROM assets WHERE name = ?", (name,)).fetchone()
        return self._catalog_asset(row) if row else None

    def add_uploaded(self, asset: Asset, requested_name: str) -> CatalogAsset:
        with self._connection:
            row = self._connection.execute(
                "SELECT inode, name FROM assets WHERE id = ?", (asset.id,)
            ).fetchone()
            if row:
                inode, name = row["inode"], row["name"]
            else:
                inode = self._connection.execute(
                    "SELECT value FROM metadata WHERE key = 'next_inode'"
                ).fetchone()[0]
                self._connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'next_inode'", (inode + 1,)
                )
                used_names = {
                    row["name"]
                    for row in self._connection.execute("SELECT name FROM assets")
                }
                name = _available_name(requested_name, asset.id, used_names)
            self._connection.execute(
                """
                INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.id,
                    inode,
                    name,
                    asset.owner_id,
                    asset.original_name,
                    asset.mime_type,
                    asset.size,
                    asset.created_ns,
                    asset.modified_ns,
                    asset.updated_at,
                    asset.checksum,
                    asset.visibility,
                    asset.is_trashed,
                    asset.is_offline,
                    asset.library_id,
                ),
            )
            inserted = self._connection.execute(
                "SELECT * FROM assets WHERE id = ?", (asset.id,)
            ).fetchone()
        assert inserted is not None
        return self._catalog_asset(inserted)

    def mark_trashed(self, asset_id: str) -> None:
        with self._connection:
            updated = self._connection.execute(
                "UPDATE assets SET is_trashed = 1 WHERE id = ?", (asset_id,)
            )
            if updated.rowcount != 1:
                raise KeyError(asset_id)

    def mark_restored(self, asset_id: str) -> None:
        with self._connection:
            updated = self._connection.execute(
                "UPDATE assets SET is_trashed = 0 WHERE id = ?", (asset_id,)
            )
            if updated.rowcount != 1:
                raise KeyError(asset_id)

    @staticmethod
    def _catalog_asset(row: sqlite3.Row) -> CatalogAsset:
        asset = Asset(
            id=row["id"],
            owner_id=row["owner_id"],
            original_name=row["original_name"],
            mime_type=row["mime_type"],
            size=row["size"],
            created_ns=row["created_ns"],
            modified_ns=row["modified_ns"],
            updated_at=row["updated_at"],
            checksum=row["checksum"],
            visibility=row["visibility"],
            is_trashed=bool(row["is_trashed"]),
            is_offline=bool(row["is_offline"]),
            library_id=row["library_id"],
        )
        return CatalogAsset(asset, row["inode"], row["name"])
