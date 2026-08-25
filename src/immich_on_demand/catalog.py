from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from typing import Iterable

from .model import Asset, collision_name, safe_filename


ROOT_INODE = 1


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
        self._connection = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

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
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('next_inode', 2);
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

    def finish_refresh(self) -> CatalogStats:
        with self._connection:
            self._connection.execute("DELETE FROM assets WHERE id NOT IN (SELECT id FROM incoming_assets)")
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
                    name = safe_filename(row["original_name"], row["id"])
                    if name in used_names:
                        name = collision_name(name, row["id"])
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
            self._connection.execute("DELETE FROM incoming_assets")
        return self.stats()

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

    def by_name(self, name: str) -> CatalogAsset | None:
        row = self._connection.execute("SELECT * FROM assets WHERE name = ?", (name,)).fetchone()
        return self._catalog_asset(row) if row else None

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
