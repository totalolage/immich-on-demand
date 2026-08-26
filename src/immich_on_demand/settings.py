from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import urlsplit

from .profiles import APP_ID


_CONFIG_NAME = "config.json"


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


def _api_key_attributes(
    settings: Settings, purpose: str, profile_id: str
) -> dict[str, str]:
    if purpose not in {"read-only", "mutation"}:
        raise ValueError("API key purpose must be read-only or mutation")
    return {
        "application": APP_ID,
        "profile": profile_id,
        "server": settings.server_origin,
        "purpose": purpose,
    }


def _secret_collection():
    import secretstorage

    connection = secretstorage.dbus_init()
    return secretstorage.get_default_collection(connection)


def _open_config_directory(path: Path) -> int:
    if not path.is_absolute() or path.name != _CONFIG_NAME:
        raise ValueError("config path must be an absolute config.json path")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    profiled = (
        path.parent.parent.name == "profiles"
        and path.parent.parent.parent.name == APP_ID
    )
    required = (
        {path.parent.parent.parent, path.parent.parent, path.parent}
        if profiled
        else {path.parent}
    )
    current = Path(path.anchor)
    try:
        for component in path.parent.parts[1:]:
            try:
                opened = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                raise
            except OSError as error:
                raise RuntimeError("unsafe config directory") from error
            os.close(descriptor)
            descriptor = opened
            current /= component
            if current in required:
                metadata = os.fstat(descriptor)
                if (
                    metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    raise RuntimeError("unsafe config directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _config_metadata(directory: int) -> os.stat_result | None:
    try:
        metadata = os.stat(_CONFIG_NAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("unsafe config file")
    return metadata


def load(path: Path) -> Settings:
    directory = _open_config_directory(path)
    try:
        if _config_metadata(directory) is None:
            raise FileNotFoundError(path)
        try:
            descriptor = os.open(
                _CONFIG_NAME,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory,
            )
        except OSError as error:
            raise RuntimeError("unsafe config file") from error
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise RuntimeError("unsafe config file")
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                descriptor = -1
                value = json.load(stream)
        finally:
            if descriptor != -1:
                os.close(descriptor)
    finally:
        os.close(directory)
    value["mount_path"] = Path(value["mount_path"]).expanduser()
    return Settings(**value)


def save(settings: Settings, path: Path) -> Path:
    directory = _open_config_directory(path)
    value = asdict(settings)
    value["mount_path"] = str(settings.mount_path)
    try:
        _config_metadata(directory)
        while True:
            temporary = f".{_CONFIG_NAME}.{secrets.token_hex(8)}"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                break
            except FileExistsError:
                continue
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                _CONFIG_NAME,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            temporary = ""
            os.fsync(directory)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    finally:
        os.close(directory)
    return path


def _exact_items(collection, attributes: dict[str, str]) -> list[object]:
    return [
        item
        for item in collection.search_items(attributes)
        if item.get_attributes() == attributes
    ]


def _secret_bytes(item: object) -> bytes:
    if item.is_locked():
        if not item.unlock():
            raise RuntimeError("Secret Service item remains locked")
    return item.get_secret()


def has_profile_api_keys(profile_id: str) -> bool:
    query = {"application": APP_ID, "profile": profile_id}
    expected_names = {"application", "profile", "server", "purpose"}
    try:
        collection = _secret_collection()
        for item in collection.search_items(query):
            attributes = item.get_attributes()
            if (
                set(attributes) == expected_names
                and attributes["application"] == APP_ID
                and attributes["profile"] == profile_id
            ):
                return True
        return False
    except Exception as error:
        raise RuntimeError(
            "could not inspect Profile API keys in Secret Service"
        ) from error


def has_nondefault_profile_api_keys() -> bool:
    query = {"application": APP_ID}
    expected_names = {"application", "profile", "server", "purpose"}
    try:
        collection = _secret_collection()
        return any(
            set(attributes := item.get_attributes()) == expected_names
            and attributes["application"] == APP_ID
            and attributes["profile"] != "default"
            for item in collection.search_items(query)
        )
    except Exception as error:
        raise RuntimeError(
            "could not inspect Profile API keys in Secret Service"
        ) from error


def _legacy_api_key(
    collection, settings: Settings, purpose: str
) -> bytes | None:
    canonical = {
        "application": APP_ID,
        "server": settings.server_origin,
        "purpose": purpose,
    }
    items = _exact_items(collection, canonical)
    if urlsplit(settings.server_url).port in {None, 443}:
        hostname = {**canonical, "server": settings.server_name}
        items.extend(_exact_items(collection, hostname))
    if not items:
        return None
    secrets = [_secret_bytes(item) for item in items]
    try:
        decoded = [secret.decode("utf-8") for secret in secrets]
    except UnicodeDecodeError as error:
        raise RuntimeError("legacy API key is not UTF-8") from error
    if any(not secret for secret in decoded) or any(
        secret != secrets[0] for secret in secrets[1:]
    ):
        raise RuntimeError("legacy API keys do not match")
    return secrets[0]


def _copy_api_key_to_default(
    collection, settings: Settings, purpose: str, secret: bytes
) -> None:
    attributes = _api_key_attributes(settings, purpose, "default")
    items = _exact_items(collection, attributes)
    if len(items) > 1:
        raise RuntimeError("duplicate default Profile API keys")
    if items and _secret_bytes(items[0]) != secret:
        raise RuntimeError("default Profile API key does not match")
    if not items:
        collection.create_item(
            f"Immich On-Demand default {purpose} API key",
            attributes,
            secret,
            replace=False,
        )
    items = _exact_items(collection, attributes)
    if len(items) != 1 or _secret_bytes(items[0]) != secret:
        raise RuntimeError("default Profile API key changed during copy")


def copy_legacy_api_keys_to_default(settings: Settings) -> None:
    try:
        collection = _secret_collection()
        read_key = _legacy_api_key(collection, settings, "read-only")
        if read_key is None:
            raise RuntimeError("legacy read-only API key is missing")
        mutation_key = _legacy_api_key(collection, settings, "mutation")
        for purpose, secret in (
            ("read-only", read_key),
            ("mutation", mutation_key),
        ):
            destination = _api_key_attributes(settings, purpose, "default")
            items = _exact_items(collection, destination)
            if len(items) > 1 or (
                items
                and (secret is None or _secret_bytes(items[0]) != secret)
            ):
                raise RuntimeError(f"inconsistent default {purpose} API key")

        _copy_api_key_to_default(collection, settings, "read-only", read_key)
        if mutation_key is not None:
            _copy_api_key_to_default(
                collection, settings, "mutation", mutation_key
            )
    except Exception as error:
        raise RuntimeError(
            "could not copy legacy API keys in Secret Service"
        ) from error


def load_api_key(
    settings: Settings,
    purpose: str = "read-only",
    *,
    profile_id: str,
) -> str:
    attributes = _api_key_attributes(settings, purpose, profile_id)
    try:
        collection = _secret_collection()
        items = _exact_items(collection, attributes)
    except Exception as error:
        raise RuntimeError("could not read API key from Secret Service") from error
    if len(items) != 1:
        raise RuntimeError(f"expected one {purpose} API key in Secret Service, found {len(items)}")
    try:
        secret = _secret_bytes(items[0]).decode("utf-8")
    except Exception as error:
        raise RuntimeError("could not read API key from Secret Service") from error
    if not secret:
        raise RuntimeError("Secret Service returned an empty API key")
    return secret


def store_api_key(
    settings: Settings,
    purpose: str,
    secret: str,
    *,
    profile_id: str,
) -> None:
    if not isinstance(secret, str) or not secret:
        raise ValueError("API key must not be empty")
    attributes = _api_key_attributes(settings, purpose, profile_id)
    encoded = secret.encode("utf-8")
    try:
        collection = _secret_collection()
        collection.create_item(
            f"Immich On-Demand {profile_id} {purpose} API key",
            attributes,
            encoded,
            replace=True,
        )
        items = _exact_items(collection, attributes)
        if len(items) != 1 or _secret_bytes(items[0]) != encoded:
            raise RuntimeError("stored API key did not match")
    except Exception as error:
        raise RuntimeError("could not store API key in Secret Service") from error
