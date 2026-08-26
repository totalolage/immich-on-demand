from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Iterable
from urllib.parse import urlsplit
from uuid import UUID

from .model import Album, Asset, Person, collision_name, safe_filename


ROOT_INODE = 1
_NAMESPACE_FORMAT = 1
_FIXED_DIRECTORIES = (
    ("view:all", "All", True),
    ("view:albums", "Albums", False),
    ("view:favorites", "Favorites", False),
    ("view:people", "People", False),
    ("view:date", "by Date", False),
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_CORE_READ_PERMISSIONS = frozenset(
    {"asset.download", "asset.read", "asset.view", "user.read"}
)
_RICH_READ_PERMISSIONS = _CORE_READ_PERMISSIONS | {"album.read", "person.read"}
_READ_PERMISSION_POLICIES = {
    1: _CORE_READ_PERMISSIONS,
    2: _RICH_READ_PERMISSIONS,
}


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


def _permissions(value: frozenset[str], format_version: int) -> frozenset[str]:
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
    if value != _READ_PERMISSION_POLICIES[format_version]:
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
class CatalogDirectory:
    inode: int
    nlink: int
    mutation_root: bool


@dataclass(frozen=True, slots=True)
class CatalogFile:
    asset: Asset
    inode: int
    name: str
    nlink: int


CatalogNode = CatalogDirectory | CatalogFile


@dataclass(frozen=True, slots=True)
class CatalogDirent:
    name: str
    node: CatalogNode


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
        if (
            type(self.format_version) is not int
            or self.format_version not in _READ_PERMISSION_POLICIES
        ):
            raise ValueError("trusted profile format version must be 1 or 2")
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
        _permissions(self.read_permissions, self.format_version)
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
                library_id TEXT,
                local_date TEXT,
                is_favorite INTEGER NOT NULL DEFAULT 0,
                live_photo_video_id TEXT
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
                library_id TEXT,
                local_date TEXT,
                is_favorite INTEGER NOT NULL DEFAULT 0,
                live_photo_video_id TEXT
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
            CREATE TABLE IF NOT EXISTS namespace_directories (
                identity TEXT PRIMARY KEY,
                inode INTEGER NOT NULL UNIQUE,
                parent_inode INTEGER NOT NULL,
                name TEXT NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                mutation_root INTEGER NOT NULL CHECK (mutation_root IN (0, 1)),
                UNIQUE(parent_inode, name)
            );
            CREATE TABLE IF NOT EXISTS namespace_links (
                directory_inode INTEGER NOT NULL REFERENCES namespace_directories(inode),
                asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                PRIMARY KEY(directory_inode, asset_id)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS namespace_links_asset
                ON namespace_links(asset_id);
            CREATE TABLE IF NOT EXISTS namespace_memberships (
                directory_inode INTEGER NOT NULL REFERENCES namespace_directories(inode),
                asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                PRIMARY KEY(directory_inode, asset_id)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS namespace_memberships_asset
                ON namespace_memberships(asset_id);
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('next_inode', 2);
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('high_water_ms', 0);
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('full_refresh_pages', 0);
            """
        )
        self._ensure_column("assets", "local_date", "TEXT")
        self._ensure_column(
            "assets", "is_favorite", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column("assets", "live_photo_video_id", "TEXT")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS assets_live_photo_video "
            "ON assets(live_photo_video_id)"
        )
        self._ensure_column("incoming_assets", "local_date", "TEXT")
        self._ensure_column(
            "incoming_assets", "is_favorite", "INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column("incoming_assets", "live_photo_video_id", "TEXT")
        version = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'namespace_format'"
        ).fetchone()
        if version is None:
            with self._connection:
                self._ensure_fixed_directories()
                self._replace_namespace()
                self._connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('namespace_format', ?)",
                    (_NAMESPACE_FORMAT,),
                )
        elif type(version[0]) is not int or version[0] != _NAMESPACE_FORMAT:
            raise ValueError("catalog namespace format is unsupported")

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def _ensure_fixed_directories(self) -> None:
        for identity, name, mutation_root in _FIXED_DIRECTORIES:
            self._ensure_directory(
                identity,
                ROOT_INODE,
                name,
                mutation_root=mutation_root,
            )

    def begin_refresh(self) -> None:
        self._connection.execute("DELETE FROM incoming_assets")
        self._connection.commit()

    def stage(self, assets: Iterable[Asset]) -> None:
        self._connection.executemany(
            """
            INSERT OR REPLACE INTO incoming_assets (
                id, owner_id, original_name, mime_type, size, created_ns,
                modified_ns, updated_at, checksum, visibility, is_trashed,
                is_offline, library_id, local_date, is_favorite,
                live_photo_video_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    asset.local_date,
                    int(asset.is_favorite),
                    asset.live_photo_video_id,
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
        if trusted_profile is not None and (
            type(trusted_profile) is not TrustedProfile
            or trusted_profile.format_version != 1
        ):
            raise ValueError("asset refresh can publish only a version 1 profile")
        current_profile = self.trusted_profile()
        if (
            trusted_profile is not None
            and current_profile is not None
            and current_profile.format_version > trusted_profile.format_version
        ):
            raise ValueError("trusted profile downgrade is not allowed")
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
            changed_ids = [row["id"] for row in rows]
            changed_relation_ids = {
                row["live_photo_video_id"]
                for row in rows
                if row["live_photo_video_id"] is not None
            }
            if not delete_missing:
                changed_relation_ids.update(
                    row["live_photo_video_id"]
                    for row in self._connection.execute(
                        """
                        SELECT assets.live_photo_video_id
                          FROM assets
                          JOIN incoming_assets ON incoming_assets.id = assets.id
                         WHERE assets.live_photo_video_id IS NOT NULL
                        """
                    )
                )
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
                    INSERT INTO assets (
                        id, inode, name, owner_id, original_name, mime_type, size,
                        created_ns, modified_ns, updated_at, checksum, visibility,
                        is_trashed, is_offline, library_id, local_date, is_favorite,
                        live_photo_video_id
                    )
                    SELECT id, ?, ?, owner_id, original_name, mime_type, size,
                           created_ns, modified_ns, updated_at, checksum, visibility,
                           is_trashed, is_offline, library_id, local_date, is_favorite,
                           live_photo_video_id
                      FROM incoming_assets WHERE id = ?
                    ON CONFLICT(id) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        original_name = excluded.original_name,
                        mime_type = excluded.mime_type,
                        size = excluded.size,
                        created_ns = excluded.created_ns,
                        modified_ns = excluded.modified_ns,
                        updated_at = excluded.updated_at,
                        checksum = excluded.checksum,
                        visibility = excluded.visibility,
                        is_trashed = excluded.is_trashed,
                        is_offline = excluded.is_offline,
                        library_id = excluded.library_id,
                        local_date = excluded.local_date,
                        is_favorite = excluded.is_favorite,
                        live_photo_video_id = excluded.live_photo_video_id
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
                self._store_trusted_profile(trusted_profile)
            if delete_missing:
                self._replace_namespace()
            else:
                for asset_id in {*changed_ids, *changed_relation_ids}:
                    self._project_asset(asset_id)
                self._refresh_date_activity()
            if not self._live_photo_relationships_are_valid():
                raise ValueError("catalog contains an invalid Live Photo relationship")
            self._connection.execute("DELETE FROM incoming_assets")
        return self.stats()

    def _store_trusted_profile(self, profile: TrustedProfile) -> None:
        self._connection.execute(
            """
            INSERT OR REPLACE INTO trusted_profile VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                profile.format_version,
                profile.server_origin,
                profile.owner_id,
                profile.server_version,
                json.dumps(
                    sorted(profile.read_permissions),
                    separators=(",", ":"),
                ),
                profile.read_key_sha256,
            ),
        )

    def _next_inode(self) -> int:
        inode = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'next_inode'"
        ).fetchone()[0]
        self._connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'next_inode'", (inode + 1,)
        )
        return inode

    def _ensure_directory(
        self,
        identity: str,
        parent_inode: int,
        name: str,
        *,
        mutation_root: bool = False,
    ) -> int:
        row = self._connection.execute(
            "SELECT * FROM namespace_directories WHERE identity = ?", (identity,)
        ).fetchone()
        if row is not None:
            if (
                row["parent_inode"] != parent_inode
                or row["name"] != name
                or bool(row["mutation_root"]) != mutation_root
            ):
                raise ValueError("catalog namespace directory identity changed")
            self._connection.execute(
                "UPDATE namespace_directories SET active = 1 WHERE identity = ?",
                (identity,),
            )
            return row["inode"]
        inode = self._next_inode()
        self._connection.execute(
            "INSERT INTO namespace_directories VALUES (?, ?, ?, ?, 1, ?)",
            (identity, inode, parent_inode, name, int(mutation_root)),
        )
        return inode

    @staticmethod
    def _canonical_id(value: object, description: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{description} must be a canonical UUID")
        try:
            canonical = str(UUID(value))
        except ValueError as error:
            raise ValueError(f"{description} must be a canonical UUID") from error
        if value != canonical:
            raise ValueError(f"{description} must be a canonical UUID")
        return value

    def _activate_collection_directory(
        self,
        identity: str,
        stable_id: str,
        parent_inode: int,
        label: str,
        used_names: set[str],
    ) -> int:
        existing = self._connection.execute(
            "SELECT * FROM namespace_directories WHERE identity = ?", (identity,)
        ).fetchone()
        if existing is not None:
            return self._ensure_directory(
                identity,
                parent_inode,
                existing["name"],
            )
        requested = (
            collision_name("Unnamed", stable_id)
            if label == ""
            else safe_filename(label.replace("/", "_"), stable_id)
        )
        name = _available_name(requested, stable_id, used_names)
        used_names.add(name)
        return self._ensure_directory(identity, parent_inode, name)

    def replace_album_people(
        self,
        *,
        albums: Iterable[Album],
        album_memberships: Iterable[tuple[str, str]],
        people: Iterable[Person],
        person_memberships: Iterable[tuple[str, str]],
        trusted_profile: TrustedProfile | None = None,
    ) -> None:
        album_values = tuple(albums)
        people_values = tuple(people)
        album_ids: set[str] = set()
        person_ids: set[str] = set()
        for value in album_values:
            if type(value) is not Album or not isinstance(value.name, str):
                raise ValueError("albums must contain valid Album values")
            album_id = self._canonical_id(value.id, "album id")
            if album_id in album_ids:
                raise ValueError("album ids must be unique")
            album_ids.add(album_id)
        for value in people_values:
            if (
                type(value) is not Person
                or not isinstance(value.name, str)
                or type(value.is_hidden) is not bool
                or value.is_hidden
            ):
                raise ValueError("people must contain visible Person values")
            person_id = self._canonical_id(value.id, "person id")
            if person_id in person_ids:
                raise ValueError("person ids must be unique")
            person_ids.add(person_id)

        def memberships(
            values: Iterable[tuple[str, str]],
            collection_ids: set[str],
            description: str,
        ) -> tuple[tuple[str, str], ...]:
            result: set[tuple[str, str]] = set()
            for value in values:
                if type(value) is not tuple or len(value) != 2:
                    raise ValueError(f"{description} memberships must be pairs")
                collection_id = self._canonical_id(
                    value[0], f"{description} membership collection id"
                )
                asset_id = self._canonical_id(
                    value[1], f"{description} membership asset id"
                )
                if collection_id not in collection_ids:
                    raise ValueError(
                        f"{description} membership references an unknown collection"
                    )
                pair = (collection_id, asset_id)
                if pair in result:
                    raise ValueError(f"{description} memberships must be unique")
                result.add(pair)
            return tuple(sorted(result))

        album_pairs = memberships(album_memberships, album_ids, "album")
        person_pairs = memberships(person_memberships, person_ids, "person")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            profile = (
                trusted_profile
                if trusted_profile is not None
                else self.trusted_profile()
            )
            if type(profile) is not TrustedProfile or profile.format_version != 2:
                raise ValueError("rich collection replacement requires a version 2 profile")
            for asset_id in sorted(
                {pair[1] for pair in album_pairs} | {pair[1] for pair in person_pairs}
            ):
                if self._connection.execute(
                    """
                    SELECT 1 FROM assets
                     WHERE id = ? AND owner_id = ? AND size IS NOT NULL
                       AND is_trashed = 0 AND is_offline = 0
                       AND visibility != 'hidden'
                    """,
                    (asset_id, profile.owner_id),
                ).fetchone() is None:
                    raise ValueError(
                        "collection membership requires a current visible owned asset"
                    )

            self._connection.execute(
                """
                UPDATE namespace_directories SET active = 0
                 WHERE identity LIKE 'album:%' OR identity LIKE 'person:%'
                """
            )
            self._connection.execute("DELETE FROM namespace_memberships")
            album_parent = self._directory_inode("view:albums")
            people_parent = self._directory_inode("view:people")
            album_names = {
                row["name"]
                for row in self._connection.execute(
                    "SELECT name FROM namespace_directories WHERE parent_inode = ?",
                    (album_parent,),
                )
            }
            people_names = {
                row["name"]
                for row in self._connection.execute(
                    "SELECT name FROM namespace_directories WHERE parent_inode = ?",
                    (people_parent,),
                )
            }
            album_directories = {
                value.id: self._activate_collection_directory(
                    f"album:{value.id}",
                    value.id,
                    album_parent,
                    value.name,
                    album_names,
                )
                for value in sorted(album_values, key=lambda item: item.id)
            }
            person_directories = {
                value.id: self._activate_collection_directory(
                    f"person:{value.id}",
                    value.id,
                    people_parent,
                    value.name,
                    people_names,
                )
                for value in sorted(people_values, key=lambda item: item.id)
            }
            self._connection.executemany(
                "INSERT INTO namespace_memberships VALUES (?, ?)",
                (
                    (album_directories[collection_id], asset_id)
                    for collection_id, asset_id in album_pairs
                ),
            )
            self._connection.executemany(
                "INSERT INTO namespace_memberships VALUES (?, ?)",
                (
                    (person_directories[collection_id], asset_id)
                    for collection_id, asset_id in person_pairs
                ),
            )
            self._replace_namespace()
            if trusted_profile is not None:
                self._store_trusted_profile(profile)

    @staticmethod
    def _date_parts(value: object) -> tuple[str, str, str] | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("catalog asset local date is invalid")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("catalog asset local date is invalid") from error
        if parsed.isoformat() != value:
            raise ValueError("catalog asset local date is invalid")
        return f"{parsed.year:04d}", f"{parsed.month:02d}", f"{parsed.day:02d}"

    def _directory_inode(self, identity: str) -> int:
        row = self._connection.execute(
            "SELECT inode FROM namespace_directories WHERE identity = ?", (identity,)
        ).fetchone()
        if row is None:
            raise ValueError("catalog namespace is incomplete")
        return row["inode"]

    def _project_asset(self, asset_id: str) -> None:
        self._connection.execute(
            "DELETE FROM namespace_links WHERE asset_id = ?", (asset_id,)
        )
        row = self._connection.execute(
            """
            SELECT * FROM assets
             WHERE id = ? AND size IS NOT NULL AND is_trashed = 0
               AND is_offline = 0 AND visibility != 'hidden'
            """,
            (asset_id,),
        ).fetchone()
        if row is None or self._connection.execute(
            "SELECT 1 FROM assets WHERE live_photo_video_id = ? LIMIT 1",
            (asset_id,),
        ).fetchone() is not None:
            return
        if type(row["is_favorite"]) is not int or row["is_favorite"] not in {0, 1}:
            raise ValueError("catalog asset favorite state is invalid")
        directory_inodes = [self._directory_inode("view:all")]
        if row["is_favorite"]:
            directory_inodes.append(self._directory_inode("view:favorites"))
        parts = self._date_parts(row["local_date"])
        if parts is not None:
            year, month, day = parts
            parent = self._directory_inode("view:date")
            parent = self._ensure_directory(f"date:{year}", parent, year)
            parent = self._ensure_directory(
                f"date:{year}-{month}", parent, month
            )
            directory_inodes.append(
                self._ensure_directory(
                    f"date:{year}-{month}-{day}", parent, day
                )
            )
        directory_inodes.extend(
            row["directory_inode"]
            for row in self._connection.execute(
                """
                SELECT memberships.directory_inode
                  FROM namespace_memberships AS memberships
                  JOIN namespace_directories AS directories
                    ON directories.inode = memberships.directory_inode
                 WHERE memberships.asset_id = ? AND directories.active = 1
                 ORDER BY memberships.directory_inode
                """,
                (asset_id,),
            )
        )
        self._connection.executemany(
            "INSERT INTO namespace_links VALUES (?, ?)",
            ((inode, asset_id) for inode in directory_inodes),
        )

    def _replace_namespace(self) -> None:
        self._connection.execute("DELETE FROM namespace_links")
        self._connection.execute(
            "UPDATE namespace_directories SET active = 0 WHERE identity LIKE 'date:%'"
        )
        asset_ids = [
            row["id"]
            for row in self._connection.execute(
                """
                SELECT id FROM assets
                 WHERE size IS NOT NULL AND is_trashed = 0 AND is_offline = 0
                   AND visibility != 'hidden'
                 ORDER BY id
                """
            )
        ]
        for asset_id in asset_ids:
            self._project_asset(asset_id)
        self._refresh_date_activity()

    def _refresh_date_activity(self) -> None:
        rows = self._connection.execute(
            "SELECT inode, parent_inode FROM namespace_directories WHERE identity LIKE 'date:%'"
        ).fetchall()
        parents = {row["inode"]: row["parent_inode"] for row in rows}
        active = {
            row["directory_inode"]
            for row in self._connection.execute(
                """
                SELECT DISTINCT links.directory_inode
                  FROM namespace_links AS links
                  JOIN namespace_directories AS directories
                    ON directories.inode = links.directory_inode
                 WHERE directories.identity LIKE 'date:%'
                """
            )
        }
        pending = list(active)
        while pending:
            parent = parents.get(pending.pop())
            if parent in parents and parent not in active:
                active.add(parent)
                pending.append(parent)
        self._connection.execute(
            "UPDATE namespace_directories SET active = 0 WHERE identity LIKE 'date:%'"
        )
        self._connection.executemany(
            "UPDATE namespace_directories SET active = 1 WHERE inode = ?",
            ((inode,) for inode in sorted(active)),
        )

    def node(self, inode: int) -> CatalogNode | None:
        if type(inode) is not int or inode < ROOT_INODE:
            return None
        if inode == ROOT_INODE:
            children = self._connection.execute(
                "SELECT count(*) FROM namespace_directories WHERE parent_inode = ? AND active = 1",
                (ROOT_INODE,),
            ).fetchone()[0]
            return CatalogDirectory(ROOT_INODE, 2 + children, False)
        directory = self._connection.execute(
            "SELECT * FROM namespace_directories WHERE inode = ? AND active = 1",
            (inode,),
        ).fetchone()
        if directory is not None:
            children = self._connection.execute(
                "SELECT count(*) FROM namespace_directories WHERE parent_inode = ? AND active = 1",
                (inode,),
            ).fetchone()[0]
            return CatalogDirectory(
                inode, 2 + children, bool(directory["mutation_root"])
            )
        row = self._connection.execute(
            """
            SELECT assets.*, count(namespace_links.directory_inode) AS nlink
              FROM assets JOIN namespace_links ON namespace_links.asset_id = assets.id
             WHERE assets.inode = ?
             GROUP BY assets.id
            """,
            (inode,),
        ).fetchone()
        return self._catalog_file(row) if row is not None else None

    def lookup(self, parent_inode: int, name: str) -> CatalogNode | None:
        parent = self.node(parent_inode)
        if parent is None:
            return None
        if isinstance(parent, CatalogFile):
            raise NotADirectoryError(parent_inode)
        if name == ".":
            return parent
        if name == "..":
            if parent_inode == ROOT_INODE:
                return parent
            row = self._connection.execute(
                "SELECT parent_inode FROM namespace_directories WHERE inode = ? AND active = 1",
                (parent_inode,),
            ).fetchone()
            return self.node(row["parent_inode"]) if row is not None else None
        row = self._connection.execute(
            """
            SELECT inode FROM namespace_directories
             WHERE parent_inode = ? AND name = ? AND active = 1
            """,
            (parent_inode, name),
        ).fetchone()
        if row is not None:
            return self.node(row["inode"])
        row = self._connection.execute(
            """
            SELECT assets.*, (
                SELECT count(*) FROM namespace_links AS aliases
                 WHERE aliases.asset_id = assets.id
            ) AS nlink
              FROM namespace_links AS child
              JOIN assets ON assets.id = child.asset_id
             WHERE child.directory_inode = ? AND assets.name = ?
            """,
            (parent_inode, name),
        ).fetchone()
        return self._catalog_file(row) if row is not None else None

    def children(self, directory_inode: int) -> tuple[CatalogDirent, ...]:
        parent = self.node(directory_inode)
        if parent is None:
            raise FileNotFoundError(directory_inode)
        if isinstance(parent, CatalogFile):
            raise NotADirectoryError(directory_inode)
        result = [
            CatalogDirent(row["name"], self.node(row["inode"]))
            for row in self._connection.execute(
                """
                SELECT name, inode FROM namespace_directories
                 WHERE parent_inode = ? AND active = 1
                """,
                (directory_inode,),
            )
        ]
        result.extend(
            CatalogDirent(row["name"], self._catalog_file(row))
            for row in self._connection.execute(
                """
                SELECT assets.*, (
                    SELECT count(*) FROM namespace_links AS aliases
                     WHERE aliases.asset_id = assets.id
                ) AS nlink
                  FROM namespace_links AS child
                  JOIN assets ON assets.id = child.asset_id
                 WHERE child.directory_inode = ?
                """,
                (directory_inode,),
            )
        )
        if any(entry.node is None for entry in result):
            raise ValueError("catalog namespace contains an invalid child")
        return tuple(sorted(result, key=lambda entry: entry.name))

    def aliases(self, asset_id: str) -> tuple[PurePosixPath, ...]:
        UUID(asset_id)
        # ponytail: fixed Views nest at most four directories; raise this with a
        # deliberately deeper View rather than letting corrupt cycles recurse.
        rows = self._connection.execute(
            """
            WITH RECURSIVE directory_paths(parent_inode, path, asset_name, depth) AS (
                SELECT directories.parent_inode, directories.name, assets.name, 1
                  FROM namespace_links
                  JOIN namespace_directories AS directories
                    ON directories.inode = namespace_links.directory_inode
                  JOIN assets ON assets.id = namespace_links.asset_id
                 WHERE namespace_links.asset_id = ? AND directories.active = 1
                UNION ALL
                SELECT parent.parent_inode,
                       parent.name || '/' || directory_paths.path,
                       directory_paths.asset_name,
                       directory_paths.depth + 1
                  FROM namespace_directories AS parent
                  JOIN directory_paths
                    ON parent.inode = directory_paths.parent_inode
                 WHERE parent.active = 1 AND directory_paths.depth < 4
            )
            SELECT path || '/' || asset_name AS path
              FROM directory_paths
             WHERE parent_inode = ?
             ORDER BY path
            """,
            (asset_id, ROOT_INODE),
        ).fetchall()
        return tuple(PurePosixPath(row["path"]) for row in rows)

    def album_ids(self, asset_id: str) -> tuple[str, ...]:
        asset_id = self._canonical_id(asset_id, "asset id")
        rows = self._connection.execute(
            """
            SELECT directories.identity
              FROM namespace_memberships AS memberships
              JOIN namespace_directories AS directories
                ON directories.inode = memberships.directory_inode
             WHERE memberships.asset_id = ? AND directories.active = 1
               AND directories.identity LIKE 'album:%'
             ORDER BY directories.identity
            """,
            (asset_id,),
        ).fetchall()
        return tuple(
            self._canonical_id(
                row["identity"].removeprefix("album:"),
                "album identity",
            )
            for row in rows
        )

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
            live_photo_relationships_valid = (
                self._live_photo_relationships_are_valid()
            )
            namespace_valid = self._namespace_is_valid()
            valid = (
                len(quick_check) == 1
                and len(quick_check[0]) == 1
                and quick_check[0][0] == "ok"
                and full_refresh_pages >= 1
                and asset_count >= 1
                and wrong_owner is None
                and fingerprint_matches
                and profile_matches
                and live_photo_relationships_valid
                and namespace_valid
            )
        except Exception:
            raise ValueError(failure) from None
        if not valid:
            raise ValueError(failure) from None

    def _live_photo_relationships_are_valid(self) -> bool:
        for row in self._connection.execute(
            "SELECT id, owner_id, live_photo_video_id FROM assets "
            "WHERE live_photo_video_id IS NOT NULL"
        ):
            motion_id = row["live_photo_video_id"]
            if not isinstance(motion_id, str):
                return False
            try:
                canonical_motion_id = str(UUID(motion_id))
            except ValueError:
                return False
            if canonical_motion_id != motion_id or motion_id == row["id"]:
                return False
            motion = self._connection.execute(
                "SELECT owner_id, mime_type FROM assets WHERE id = ?",
                (motion_id,),
            ).fetchone()
            if (
                motion is None
                or motion["owner_id"] != row["owner_id"]
                or not isinstance(motion["mime_type"], str)
                or not (
                    motion["mime_type"].startswith("video/")
                    or motion["mime_type"] == "application/mxf"
                )
            ):
                return False
        return True

    def _namespace_is_valid(self) -> bool:
        version = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'namespace_format'"
        ).fetchone()
        if version is None or version[0] != _NAMESPACE_FORMAT:
            return False
        fixed = self._connection.execute(
            """
            SELECT identity, parent_inode, name, active, mutation_root
              FROM namespace_directories
             WHERE identity LIKE 'view:%'
             ORDER BY identity
            """
        ).fetchall()
        expected_fixed = sorted(
            (identity, ROOT_INODE, name, 1, int(mutation_root))
            for identity, name, mutation_root in _FIXED_DIRECTORIES
        )
        if [tuple(row) for row in fixed] != expected_fixed:
            return False
        if self._connection.execute(
            """
            SELECT 1 FROM namespace_links AS links
              LEFT JOIN namespace_directories AS directories
                ON directories.inode = links.directory_inode
              LEFT JOIN assets ON assets.id = links.asset_id
             WHERE directories.inode IS NULL OR directories.active != 1
                OR assets.id IS NULL OR assets.size IS NULL OR assets.is_trashed != 0
                OR assets.is_offline != 0 OR assets.visibility = 'hidden'
                OR EXISTS (
                    SELECT 1 FROM assets AS still
                     WHERE still.live_photo_video_id = assets.id
                )
                OR (directories.identity NOT IN ('view:all', 'view:favorites')
                    AND directories.identity NOT LIKE 'date:%-%-%'
                    AND directories.identity NOT LIKE 'album:%'
                    AND directories.identity NOT LIKE 'person:%')
             LIMIT 1
            """
        ).fetchone():
            return False
        if self._connection.execute(
            """
            SELECT 1 FROM namespace_memberships AS memberships
              LEFT JOIN namespace_directories AS directories
                ON directories.inode = memberships.directory_inode
              LEFT JOIN assets ON assets.id = memberships.asset_id
             WHERE directories.inode IS NULL OR directories.active != 1
                OR directories.mutation_root != 0 OR assets.id IS NULL
                OR (directories.identity NOT LIKE 'album:%'
                    AND directories.identity NOT LIKE 'person:%')
             LIMIT 1
            """
        ).fetchone():
            return False
        for row in self._connection.execute("SELECT * FROM assets"):
            visible = (
                row["size"] is not None
                and row["is_trashed"] == 0
                and row["is_offline"] == 0
                and row["visibility"] != "hidden"
                and self._connection.execute(
                    "SELECT 1 FROM assets WHERE live_photo_video_id = ? LIMIT 1",
                    (row["id"],),
                ).fetchone()
                is None
            )
            identities = {
                item["identity"]
                for item in self._connection.execute(
                    """
                    SELECT directories.identity
                      FROM namespace_links AS links
                     JOIN namespace_directories AS directories
                        ON directories.inode = links.directory_inode
                     WHERE links.asset_id = ?
                    """,
                    (row["id"],),
                )
            }
            expected: set[str] = set()
            if visible:
                expected.add("view:all")
                if row["is_favorite"] == 1:
                    expected.add("view:favorites")
                elif row["is_favorite"] != 0:
                    return False
                parts = self._date_parts(row["local_date"])
                if parts is not None:
                    expected.add(f"date:{'-'.join(parts)}")
                expected.update(
                    item["identity"]
                    for item in self._connection.execute(
                        """
                        SELECT directories.identity
                          FROM namespace_memberships AS memberships
                          JOIN namespace_directories AS directories
                            ON directories.inode = memberships.directory_inode
                         WHERE memberships.asset_id = ?
                        """,
                        (row["id"],),
                    )
                )
            if identities != expected:
                return False
        all_directories = self._connection.execute(
            "SELECT * FROM namespace_directories"
        ).fetchall()
        if any(
            not row["identity"].startswith(
                ("view:", "date:", "album:", "person:")
            )
            for row in all_directories
        ):
            return False
        by_identity = {row["identity"]: row for row in all_directories}
        for kind, parent_identity in (
            ("album", "view:albums"),
            ("person", "view:people"),
        ):
            parent = by_identity[parent_identity]
            for row in all_directories:
                prefix = f"{kind}:"
                if not row["identity"].startswith(prefix):
                    continue
                stable_id = row["identity"].removeprefix(prefix)
                try:
                    canonical_id = str(UUID(stable_id))
                except (TypeError, ValueError):
                    return False
                if (
                    stable_id != canonical_id
                    or row["parent_inode"] != parent["inode"]
                    or type(row["active"]) is not int
                    or row["active"] not in {0, 1}
                    or row["mutation_root"] != 0
                    or not isinstance(row["name"], str)
                    or safe_filename(row["name"], stable_id) != row["name"]
                ):
                    return False
        date_rows = [
            row for row in all_directories if row["identity"].startswith("date:")
        ]
        for row in date_rows:
            parts = row["identity"].removeprefix("date:").split("-")
            if len(parts) not in {1, 2, 3}:
                return False
            try:
                year = int(parts[0])
                month = int(parts[1]) if len(parts) >= 2 else 1
                day = int(parts[2]) if len(parts) == 3 else 1
                date(year, month, day)
            except (TypeError, ValueError):
                return False
            if any(
                len(part) != width or not part.isascii() or not part.isdigit()
                for part, width in zip(parts, (4, 2, 2)[: len(parts)], strict=True)
            ):
                return False
            parent_identity = (
                "view:date"
                if len(parts) == 1
                else f"date:{'-'.join(parts[:-1])}"
            )
            parent = by_identity.get(parent_identity)
            if (
                parent is None
                or row["parent_inode"] != parent["inode"]
                or row["name"] != parts[-1]
                or row["mutation_root"] != 0
            ):
                return False
            has_live_child = self._connection.execute(
                "SELECT 1 FROM namespace_directories WHERE parent_inode = ? AND active = 1 LIMIT 1",
                (row["inode"],),
            ).fetchone()
            has_link = self._connection.execute(
                "SELECT 1 FROM namespace_links WHERE directory_inode = ? LIMIT 1",
                (row["inode"],),
            ).fetchone()
            should_be_active = has_live_child is not None or has_link is not None
            if bool(row["active"]) != should_be_active:
                return False
        return True

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
                   sum(asset.size IS NOT NULL AND asset.is_trashed = 0
                       AND asset.is_offline = 0 AND asset.visibility != 'hidden'
                       AND NOT EXISTS (
                           SELECT 1 FROM assets AS still
                            WHERE still.live_photo_video_id = asset.id
                       )) AS visible,
                   sum(asset.size IS NULL) AS missing_size,
                   sum(asset.is_trashed) AS trashed,
                   sum(asset.visibility = 'hidden') AS hidden,
                   sum(asset.is_offline) AS offline
              FROM assets AS asset
            """
        ).fetchone()
        return CatalogStats(*(int(row[name] or 0) for name in CatalogStats.__dataclass_fields__))

    def list_visible(self) -> list[CatalogAsset]:
        return [
            self._catalog_asset(row)
            for row in self._connection.execute(
                """
                SELECT asset.* FROM assets AS asset
                 WHERE asset.size IS NOT NULL AND asset.is_trashed = 0
                   AND asset.is_offline = 0 AND asset.visibility != 'hidden'
                   AND NOT EXISTS (
                       SELECT 1 FROM assets AS still
                        WHERE still.live_photo_video_id = asset.id
                   )
                 ORDER BY asset.name
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

    def add_uploaded(self, asset: Asset, requested_name: str) -> CatalogAsset:
        with self._connection:
            row = self._connection.execute(
                "SELECT inode, name, live_photo_video_id FROM assets WHERE id = ?",
                (asset.id,),
            ).fetchone()
            relationship_ids = {
                target_id
                for target_id in (
                    row["live_photo_video_id"] if row else None,
                    asset.live_photo_video_id,
                )
                if target_id is not None
            }
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
                INSERT INTO assets (
                    id, inode, name, owner_id, original_name, mime_type, size,
                    created_ns, modified_ns, updated_at, checksum, visibility,
                    is_trashed, is_offline, library_id, local_date, is_favorite,
                    live_photo_video_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    original_name = excluded.original_name,
                    mime_type = excluded.mime_type,
                    size = excluded.size,
                    created_ns = excluded.created_ns,
                    modified_ns = excluded.modified_ns,
                    updated_at = excluded.updated_at,
                    checksum = excluded.checksum,
                    visibility = excluded.visibility,
                    is_trashed = excluded.is_trashed,
                    is_offline = excluded.is_offline,
                    library_id = excluded.library_id,
                    local_date = excluded.local_date,
                    is_favorite = excluded.is_favorite,
                    live_photo_video_id = excluded.live_photo_video_id
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
                    asset.local_date,
                    int(asset.is_favorite),
                    asset.live_photo_video_id,
                ),
            )
            for asset_id in {asset.id, *relationship_ids}:
                self._project_asset(asset_id)
            self._refresh_date_activity()
            inserted = self._connection.execute(
                "SELECT * FROM assets WHERE id = ?", (asset.id,)
            ).fetchone()
        assert inserted is not None
        return self._catalog_asset(inserted)

    def publish_replacement(
        self,
        *,
        old_asset_id: str,
        candidate: Asset,
    ) -> CatalogAsset:
        old_asset_id = self._canonical_id(old_asset_id, "old asset id")
        if type(candidate) is not Asset:
            raise ValueError("replacement candidate must be an Asset")
        candidate_id = self._canonical_id(candidate.id, "replacement candidate id")
        candidate_owner = self._canonical_id(
            candidate.owner_id, "replacement candidate owner"
        )
        if candidate_id == old_asset_id:
            raise ValueError("replacement candidate must have a new asset id")

        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            old = self._connection.execute(
                "SELECT * FROM assets WHERE id = ?", (old_asset_id,)
            ).fetchone()
            if old is None:
                raise ValueError("replacement source is not in the catalog")
            if (
                type(old["is_trashed"]) is not int
                or old["is_trashed"] != 0
                or type(old["is_offline"]) is not int
                or old["is_offline"] != 0
                or type(old["size"]) is not int
                or old["size"] < 0
                or not isinstance(old["visibility"], str)
                or old["visibility"] == "hidden"
                or old["library_id"] is not None
                or type(old["is_favorite"]) is not int
                or old["is_favorite"] not in {0, 1}
            ):
                raise ValueError("replacement source must be a live managed asset")
            if (
                type(candidate.is_trashed) is not bool
                or candidate.is_trashed
                or type(candidate.is_offline) is not bool
                or candidate.is_offline
                or type(candidate.size) is not int
                or candidate.size < 0
                or not isinstance(candidate.visibility, str)
                or candidate.visibility == "hidden"
                or candidate.library_id is not None
            ):
                raise ValueError("replacement candidate must be a live managed asset")
            if candidate_owner != old["owner_id"]:
                raise ValueError("replacement assets must have the same owner")
            if (
                candidate.visibility != old["visibility"]
                or candidate.local_date != old["local_date"]
                or type(candidate.is_favorite) is not bool
                or candidate.is_favorite != bool(old["is_favorite"])
                or candidate.live_photo_video_id != old["live_photo_video_id"]
            ):
                raise ValueError("replacement candidate changed mounted View state")
            self._date_parts(candidate.local_date)
            if self._connection.execute(
                "SELECT 1 FROM assets WHERE id = ?", (candidate_id,)
            ).fetchone() is not None:
                raise ValueError("replacement candidate is already in the catalog")

            old_inode = old["inode"]
            old_name = old["name"]
            if (
                type(old_inode) is not int
                or old_inode <= ROOT_INODE
                or not isinstance(old_name, str)
                or safe_filename(old_name, old_asset_id) != old_name
                or self._connection.execute(
                    "SELECT 1 FROM namespace_directories WHERE inode = ?", (old_inode,)
                ).fetchone()
                is not None
            ):
                raise ValueError("replacement source identity is invalid")
            candidate_inode_row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'next_inode'"
            ).fetchone()
            candidate_inode = candidate_inode_row[0] if candidate_inode_row else None
            if (
                type(candidate_inode) is not int
                or candidate_inode <= ROOT_INODE
                or self._connection.execute(
                    "SELECT 1 FROM assets WHERE inode = ?", (candidate_inode,)
                ).fetchone()
                is not None
                or self._connection.execute(
                    "SELECT 1 FROM namespace_directories WHERE inode = ?",
                    (candidate_inode,),
                ).fetchone()
                is not None
            ):
                raise ValueError("replacement candidate inode is unavailable")

            used_names = {
                row["name"]
                for row in self._connection.execute(
                    "SELECT name FROM assets WHERE id != ?", (old_asset_id,)
                )
            }
            retired_name = None
            for ordinal in range(1, len(used_names) + 2):
                value = collision_name(
                    old_name,
                    old_asset_id,
                    ordinal=ordinal,
                )
                if value not in used_names:
                    retired_name = value
                    break
            if retired_name is None:
                raise ValueError("replacement source name cannot be retired")

            old_pinned = self._connection.execute(
                "SELECT 1 FROM pins WHERE asset_id = ?", (old_asset_id,)
            ).fetchone() is not None
            updated = self._connection.execute(
                """
                UPDATE assets SET name = ?, is_trashed = 1
                 WHERE id = ? AND inode = ? AND name = ? AND is_trashed = 0
                """,
                (retired_name, old_asset_id, old_inode, old_name),
            )
            if updated.rowcount != 1:
                raise ValueError("replacement source changed before publication")
            allocated_inode = self._next_inode()
            if allocated_inode != candidate_inode:
                raise ValueError("replacement candidate inode changed before publication")
            self._connection.execute(
                """
                INSERT INTO assets (
                    id, inode, name, owner_id, original_name, mime_type, size,
                    created_ns, modified_ns, updated_at, checksum, visibility,
                    is_trashed, is_offline, library_id, local_date, is_favorite,
                    live_photo_video_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.id,
                    candidate_inode,
                    old_name,
                    candidate.owner_id,
                    candidate.original_name,
                    candidate.mime_type,
                    candidate.size,
                    candidate.created_ns,
                    candidate.modified_ns,
                    candidate.updated_at,
                    candidate.checksum,
                    candidate.visibility,
                    int(candidate.is_trashed),
                    int(candidate.is_offline),
                    candidate.library_id,
                    candidate.local_date,
                    int(candidate.is_favorite),
                    candidate.live_photo_video_id,
                ),
            )
            self._connection.execute(
                "UPDATE namespace_links SET asset_id = ? WHERE asset_id = ?",
                (candidate_id, old_asset_id),
            )
            self._connection.execute(
                "UPDATE namespace_memberships SET asset_id = ? WHERE asset_id = ?",
                (candidate_id, old_asset_id),
            )
            self._connection.execute(
                "DELETE FROM pins WHERE asset_id IN (?, ?)",
                (old_asset_id, candidate_id),
            )
            if old_pinned:
                self._connection.execute(
                    "INSERT INTO pins(asset_id) VALUES (?)", (candidate_id,)
                )
            inserted = self._connection.execute(
                "SELECT * FROM assets WHERE id = ?", (candidate_id,)
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
            self._project_asset(asset_id)
            self._refresh_date_activity()

    def mark_restored(self, asset_id: str) -> None:
        with self._connection:
            updated = self._connection.execute(
                "UPDATE assets SET is_trashed = 0 WHERE id = ?", (asset_id,)
            )
            if updated.rowcount != 1:
                raise KeyError(asset_id)
            self._project_asset(asset_id)
            self._refresh_date_activity()

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
            local_date=row["local_date"],
            is_favorite=bool(row["is_favorite"]),
            live_photo_video_id=row["live_photo_video_id"],
        )
        return CatalogAsset(asset, row["inode"], row["name"])

    @classmethod
    def _catalog_file(cls, row: sqlite3.Row) -> CatalogFile:
        entry = cls._catalog_asset(row)
        return CatalogFile(entry.asset, entry.inode, entry.name, int(row["nlink"]))
