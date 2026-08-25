from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


APP_ID = "immich-on-demand"


def _xdg(name: str, fallback: str) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else Path.home() / fallback


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
        parsed = urlsplit(self.server_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
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


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_ID / "config.json"


def state_path() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_ID


def cache_path() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_ID


def runtime_path() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        raise RuntimeError("XDG_RUNTIME_DIR is not set")
    return Path(value) / APP_ID


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
    try:
        import secretstorage

        connection = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(connection)
        items = list(
            collection.search_items(
                {"application": APP_ID, "server": settings.server_name, "purpose": purpose}
            )
        )
        if len(items) != 1:
            raise RuntimeError(f"expected one {purpose} API key in Secret Service, found {len(items)}")
        if items[0].is_locked() and not items[0].unlock():
            raise RuntimeError("Secret Service item remains locked")
        secret = items[0].get_secret().decode("utf-8")
        if not secret:
            raise RuntimeError("Secret Service returned an empty API key")
        return secret
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"could not read API key from Secret Service: {error}") from error
