from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

APP_ID = "immich-on-demand"
_PROFILE_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
_UNIX_SOCKET_PATH_BYTES = 108


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    config: Path
    state: Path
    data: Path
    cache: Path
    runtime: Path


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


def _application_roots() -> tuple[Path, Path, Path, Path, Path]:
    return (
        _xdg("XDG_CONFIG_HOME", ".config") / APP_ID,
        _xdg("XDG_STATE_HOME", ".local/state") / APP_ID,
        _xdg("XDG_DATA_HOME", ".local/share") / APP_ID,
        _xdg("XDG_CACHE_HOME", ".cache") / APP_ID,
        _xdg("XDG_RUNTIME_DIR") / APP_ID,
    )


def _legacy_config_exists(config_root: Path) -> bool:
    return os.path.lexists(config_root / "config.json")


def select_profile(profile_id: str) -> Profile:
    if not isinstance(profile_id, str) or _PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError("invalid Profile ID")

    config, state, data, cache, runtime = _application_roots()
    if profile_id != "default" and _legacy_config_exists(config):
        raise RuntimeError("legacy config must migrate to Profile default first")

    profile = Profile(
        profile_id,
        config / "profiles" / profile_id,
        state / "profiles" / profile_id,
        data / "profiles" / profile_id,
        cache / "profiles" / profile_id,
        runtime / "profiles" / profile_id,
    )
    if len(os.fsencode(profile.runtime / "control.sock")) >= _UNIX_SOCKET_PATH_BYTES:
        raise ValueError("Profile control socket path is too long")
    return profile


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise RuntimeError(f"unsafe Profile directory: {path}") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError(f"unsafe Profile directory: {path}")


def _has_strict_config(directory: Path) -> bool:
    path = directory / "config.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(f"unsafe Profile config: {path}")
    return True


def profiles() -> tuple[Profile, ...]:
    config_root = _xdg("XDG_CONFIG_HOME", ".config") / APP_ID
    if _legacy_config_exists(config_root):
        return (select_profile("default"),)
    if not os.path.lexists(config_root):
        return ()
    _require_private_directory(config_root)

    registry = config_root / "profiles"
    if not os.path.lexists(registry):
        return ()
    _require_private_directory(registry)

    profile_ids: list[str] = []
    for entry in registry.iterdir():
        if _PROFILE_ID.fullmatch(entry.name) is None:
            continue
        _require_private_directory(entry)
        if _has_strict_config(entry):
            profile_ids.append(entry.name)
    return tuple(select_profile(profile_id) for profile_id in sorted(profile_ids))
