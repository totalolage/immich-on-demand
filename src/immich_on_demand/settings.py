from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


APP_ID = "immich-on-demand"


def _xdg(name: str, fallback: str | None = None) -> Path:
    value = os.environ.get(name)
    if value:
        path = Path(value)
        if not path.is_absolute():
            raise RuntimeError(f"{name} must be an absolute path")
        return path
    if fallback is None:
        raise RuntimeError(f"{name} is not set")
    return Path.home() / fallback


@dataclass(frozen=True, slots=True)
class Settings:
    server_url: str
    mount_path: Path
    cache_max_bytes: int = 10 * 1024**3
    cache_max_age_seconds: int = 30 * 24 * 60 * 60
    minimum_free_bytes: int = 5 * 1024**3
    refresh_seconds: int = 300
    remote_delete: bool = False

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.server_url)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise ValueError(
                "server_url must be an HTTPS origin without credentials, query, or fragment"
            ) from None
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(
                ord(character) <= 32 or ord(character) == 127
                for character in parsed.netloc
            )
        ):
            raise ValueError("server_url must be an HTTPS origin without credentials, query, or fragment")
        if not self.mount_path.is_absolute():
            raise ValueError("mount_path must be absolute")
        for value in (
            self.cache_max_bytes,
            self.cache_max_age_seconds,
            self.minimum_free_bytes,
            self.refresh_seconds,
        ):
            if value <= 0:
                raise ValueError("cache and refresh limits must be positive")

    @property
    def server_name(self) -> str:
        hostname = urlsplit(self.server_url).hostname
        assert hostname is not None
        return hostname

    @property
    def server_origin(self) -> str:
        parsed = urlsplit(self.server_url)
        hostname = parsed.hostname
        assert hostname is not None
        host = hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        suffix = "" if port in {None, 443} else f":{port}"
        return f"https://{host}{suffix}"


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_ID / "config.json"


def state_path() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_ID


def data_path() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_ID


def cache_path() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_ID


def runtime_path() -> Path:
    return _xdg("XDG_RUNTIME_DIR") / APP_ID


def _api_key_attributes(settings: Settings, purpose: str) -> dict[str, str]:
    if purpose not in {"read-only", "mutation"}:
        raise ValueError("API key purpose must be read-only or mutation")
    return {
        "application": APP_ID,
        "server": settings.server_origin,
        "purpose": purpose,
    }


def _secret_collection():
    import secretstorage

    connection = secretstorage.dbus_init()
    return secretstorage.get_default_collection(connection)


def load(path: Path | None = None) -> Settings:
    source = path or config_path()
    value = json.loads(source.read_text(encoding="utf-8"))
    value["mount_path"] = Path(value["mount_path"]).expanduser()
    return Settings(**value)


def save(settings: Settings, path: Path | None = None) -> Path:
    destination = path or config_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = asdict(settings)
    value["mount_path"] = str(settings.mount_path)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def load_api_key(settings: Settings, purpose: str = "read-only") -> str:
    attributes = _api_key_attributes(settings, purpose)
    legacy_items: list[object] = []
    migrated = False
    try:
        collection = _secret_collection()
        items = list(collection.search_items(attributes))
        if len(items) <= 1 and urlsplit(settings.server_url).port in {None, 443}:
            legacy_attributes = {**attributes, "server": settings.server_name}
            legacy_items = list(collection.search_items(legacy_attributes))
            if not items:
                items = legacy_items
                migrated = True
    except Exception as error:
        raise RuntimeError("could not read API key from Secret Service") from error
    if len(items) != 1:
        raise RuntimeError(f"expected one {purpose} API key in Secret Service, found {len(items)}")
    try:
        locked = items[0].is_locked()
    except Exception as error:
        raise RuntimeError("could not read API key from Secret Service") from error
    if locked:
        try:
            unlocked = items[0].unlock()
        except Exception as error:
            raise RuntimeError("could not read API key from Secret Service") from error
        if not unlocked:
            raise RuntimeError("Secret Service item remains locked")
    try:
        secret = items[0].get_secret().decode("utf-8")
    except Exception as error:
        raise RuntimeError("could not read API key from Secret Service") from error
    if not secret:
        raise RuntimeError("Secret Service returned an empty API key")
    try:
        if migrated:
            migrated_item = collection.create_item(
                f"Immich On-Demand {purpose} API key",
                attributes,
                secret.encode("utf-8"),
                replace=False,
            )
            canonical_items = list(collection.search_items(attributes))
            if (
                len(canonical_items) != 1
                or canonical_items[0].item_path != migrated_item.item_path
            ):
                migrated_item.delete()
                raise RuntimeError("canonical API key changed during migration")
        for legacy_item in legacy_items:
            legacy_item.delete()
    except Exception as error:
        raise RuntimeError("could not migrate API key in Secret Service") from error
    return secret


def store_api_key(settings: Settings, purpose: str, secret: str) -> None:
    if not isinstance(secret, str) or not secret:
        raise ValueError("API key must not be empty")
    attributes = _api_key_attributes(settings, purpose)
    try:
        collection = _secret_collection()
        legacy_items = []
        if urlsplit(settings.server_url).port in {None, 443}:
            legacy_attributes = {**attributes, "server": settings.server_name}
            legacy_items = list(collection.search_items(legacy_attributes))
        collection.create_item(
            f"Immich On-Demand {purpose} API key",
            attributes,
            secret.encode("utf-8"),
            replace=True,
        )
        for legacy_item in legacy_items:
            legacy_item.delete()
    except Exception as error:
        raise RuntimeError("could not store API key in Secret Service") from error
