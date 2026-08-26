from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Iterable
from urllib.parse import urlsplit
from uuid import UUID

from .model import Asset, collision_name, safe_filename


ROOT_INODE = 1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_TRUSTED_READ_PERMISSIONS = frozenset(
    {"asset.download", "asset.read", "asset.view", "user.read"}
)


def _canonical_origin(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("trusted profile origin must be a string")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("trusted profile origin must be a canonical HTTPS origin") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(ord(character) <= 32 or ord(character) == 127 for character in parsed.netloc)
    ):
        raise ValueError("trusted profile origin must be a canonical HTTPS origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}{f':{port}' if port not in {None, 443} else ''}"


def _permissions(value: frozenset[str]) -> frozenset[str]:
    if type(value) is not frozenset or not value or any(
        not isinstance(permission, str)
        or not permission
        or len(permission) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in permission
        )
        for permission in value
    ):
        raise ValueError("trusted profile read permissions must be nonempty strings")
    if value != _TRUSTED_READ_PERMISSIONS:
        raise ValueError("trusted profile read permissions must match the exact read policy")
    return value


def _fingerprint(value: str) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise ValueError("trusted profile read key must be lowercase SHA-256 hex")
    return value


def _require_owned_directory(path: Path) -> None:
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PermissionError("catalog state directory must be owned by this user")


def _open_database(path: Path) -> int:
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
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
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or getattr(info, "st_nlink", None) != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise PermissionError("catalog database must be a regular file owned by this user")
    return descriptor


def _prepare_auxiliary_files(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        auxiliary = Path(f"{path}{suffix}")
        try:
            info = os.lstat(auxiliary)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PermissionError(
                "catalog auxiliary files must be regular files owned by this user"
            )


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


@dataclass(frozen=True, slots=True)
class TrustedProfile:
    server_origin: str
    owner_id: str
    server_version: str
    read_permissions: frozenset[str]
    read_key_sha256: str
    format_version: int = 1

    def __post_init__(self) -> None:
        if type(self.format_version) is not int or self.format_version != 1:
            raise ValueError("trusted profile format version must be 1")
        object.__setattr__(
            self, "server_origin", _canonical_origin(self.server_origin)
        )
        if not isinstance(self.owner_id, str):
            raise TypeError("trusted profile owner must be a UUID string")
        try:
            owner_id = str(UUID(self.owner_id))
        except ValueError as error:
            raise ValueError("trusted profile owner must be a UUID") from error
        object.__setattr__(self, "owner_id", owner_id)
        if self.server_version != "3.0.3":
            raise ValueError("trusted profile server version must be 3.0.3")
        _permissions(self.read_permissions)
        _fingerprint(self.read_key_sha256)


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
            CREATE TABLE IF NOT EXISTS trusted_profile (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                format_version INTEGER NOT NULL,
                server_origin TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                server_version TEXT NOT NULL,
                read_permissions TEXT NOT NULL,
                read_key_sha256 TEXT NOT NULL
            );
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
        trusted_profile: TrustedProfile | None = None,
    ) -> CatalogStats:
        self._validate_refresh_state(high_water_ms, page_count)
        return self._finish_staged(
            delete_missing=True,
            high_water_ms=high_water_ms,
            page_count=page_count,
            trusted_profile=trusted_profile,
        )

    def finish_incremental(self, *, high_water_ms: int) -> CatalogStats:
        self._validate_refresh_state(high_water_ms, None)
        return self._finish_staged(
            delete_missing=False,
            high_water_ms=high_water_ms,
            page_count=None,
            trusted_profile=None,
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
        trusted_profile: TrustedProfile | None,
    ) -> CatalogStats:
        with self._connection:
            if trusted_profile is not None and self._connection.execute(
                "SELECT 1 FROM incoming_assets WHERE owner_id != ? LIMIT 1",
                (trusted_profile.owner_id,),
            ).fetchone():
                raise ValueError("trusted profile owner does not own every staged asset")
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
            if trusted_profile is not None:
                self._connection.execute(
                    """
                    INSERT OR REPLACE INTO trusted_profile VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        trusted_profile.format_version,
                        trusted_profile.server_origin,
                        trusted_profile.owner_id,
                        trusted_profile.server_version,
                        json.dumps(
                            sorted(trusted_profile.read_permissions),
                            separators=(",", ":"),
                        ),
                        trusted_profile.read_key_sha256,
                    ),
                )
            self._connection.execute("DELETE FROM incoming_assets")
        return self.stats()

    def trusted_profile(self) -> TrustedProfile | None:
        rows = self._connection.execute("SELECT * FROM trusted_profile").fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("catalog contains more than one trusted profile")
        row = rows[0]
        try:
            if type(row["singleton"]) is not int or row["singleton"] != 1:
                raise ValueError
            if type(row["read_permissions"]) is not str:
                raise ValueError
            read_values = json.loads(row["read_permissions"])
            if (
                not isinstance(read_values, list)
                or not read_values
                or any(not isinstance(value, str) for value in read_values)
                or read_values != sorted(set(read_values))
            ):
                raise ValueError
            profile = TrustedProfile(
                format_version=row["format_version"],
                server_origin=row["server_origin"],
                owner_id=row["owner_id"],
                server_version=row["server_version"],
                read_permissions=frozenset(read_values),
                read_key_sha256=row["read_key_sha256"],
            )
            stored = (
                row["server_origin"],
                row["owner_id"],
                row["server_version"],
            )
            if stored != (
                profile.server_origin,
                profile.owner_id,
                profile.server_version,
            ):
                raise ValueError
            return profile
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("catalog contains an invalid trusted profile") from error

    def require_offline_profile(self, expected: TrustedProfile) -> None:
        failure = "catalog is not trusted for offline use"
        try:
            if type(expected) is not TrustedProfile:
                raise ValueError
            quick_check = self._connection.execute("PRAGMA quick_check").fetchall()
            stored = self.trusted_profile()
            _, full_refresh_pages = self.refresh_state()
            asset_count = self._connection.execute(
                "SELECT count(*) FROM assets"
            ).fetchone()[0]
            wrong_owner = self._connection.execute(
                "SELECT 1 FROM assets WHERE owner_id != ? LIMIT 1",
                (expected.owner_id,),
            ).fetchone()
            fingerprint_matches = stored is not None and hmac.compare_digest(
                stored.read_key_sha256, expected.read_key_sha256
            )
            profile_matches = stored is not None and (
                stored.format_version,
                stored.server_origin,
                stored.owner_id,
                stored.server_version,
                stored.read_permissions,
            ) == (
                expected.format_version,
                expected.server_origin,
                expected.owner_id,
                expected.server_version,
                expected.read_permissions,
            )
            valid = (
                len(quick_check) == 1
                and len(quick_check[0]) == 1
                and quick_check[0][0] == "ok"
                and full_refresh_pages >= 1
                and asset_count >= 1
                and wrong_owner is None
                and fingerprint_matches
                and profile_matches
            )
        except Exception:
            raise ValueError(failure) from None
        if not valid:
            raise ValueError(failure) from None

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
