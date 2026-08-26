from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, replace
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .settings import Settings

APP_ID = "immich-on-demand"
_PROFILE_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
_UNIX_SOCKET_PATH_BYTES = 108
_RENAME_NOREPLACE = 1
_RENAMEAT2 = ctypes.CDLL(None, use_errno=True).renameat2
_RENAMEAT2.argtypes = (
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
)
_RENAMEAT2.restype = ctypes.c_int


class ProfileError(RuntimeError):
    exit_status = 78


class ProfileBusyError(ProfileError):
    exit_status = 75


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
            raise ProfileError(f"{name} must be an absolute path")
        return path
    if fallback is None:
        raise ProfileError(f"{name} is not set")
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
        raise ProfileError("invalid Profile ID")

    config, state, data, cache, runtime = _application_roots()
    if profile_id != "default" and _legacy_config_exists(config):
        raise ProfileError("legacy config must migrate to Profile default first")

    profile = Profile(
        profile_id,
        config / "profiles" / profile_id,
        state / "profiles" / profile_id,
        data / "profiles" / profile_id,
        cache / "profiles" / profile_id,
        runtime / "profiles" / profile_id,
    )
    if len(os.fsencode(profile.runtime / "control.sock")) >= _UNIX_SOCKET_PATH_BYTES:
        raise ProfileError("Profile control socket path is too long")
    return profile


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ProfileError(f"unsafe Profile directory: {path}") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProfileError(f"unsafe Profile directory: {path}")


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
        raise ProfileError(f"unsafe Profile config: {path}")
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


def _checked_profile(profile: Profile) -> Profile:
    if not isinstance(profile, Profile):
        raise ProfileError("expected a selected Profile")
    expected = select_profile(profile.id)
    if profile != expected:
        raise ProfileError("Profile paths do not match the selected environment")
    return expected


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise ProfileError(f"could not create Profile directory: {path}") from error
    _require_private_directory(path)


def _ensure_profile_tree(root: Path, *, final: bool) -> None:
    application = root.parent.parent
    try:
        application.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise ProfileError(
            f"could not create XDG directory: {application.parent}"
        ) from error
    _ensure_private_directory(application)
    _ensure_private_directory(root.parent)
    if final:
        _ensure_private_directory(root)


def _prepare_runtime(profile: Profile) -> tuple[Path, Path]:
    registry = profile.runtime.parent
    application = registry.parent
    runtime = application.parent
    _require_private_directory(runtime)
    _ensure_private_directory(application)
    _ensure_private_directory(registry)
    _ensure_private_directory(profile.runtime)
    return application, profile.runtime


def _acquire_lock(
    path: Path,
    operation: int,
    contention: type[ProfileError],
    message: str,
) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        raise ProfileError(f"unsafe Profile lock: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ProfileError(f"unsafe Profile lock: {path}")
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise contention(message) from None
            raise ProfileError(f"could not claim Profile lock: {path}") from error
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release(descriptor: int | None) -> None:
    if descriptor is not None:
        os.close(descriptor)


def _legacy_config(profile: Profile) -> Path:
    return profile.config.parent.parent / "config.json"


def _reject_nondefault_legacy(profile: Profile) -> None:
    if profile.id != "default" and os.path.lexists(_legacy_config(profile)):
        raise ProfileError("legacy config must migrate to Profile default first")


def _mounts_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _validate_mount_separation(profile: Profile, mount_path: Path | None) -> None:
    if mount_path is None:
        return
    if not isinstance(mount_path, Path) or not mount_path.is_absolute():
        raise ProfileError("Profile mount path must be absolute")
    candidate = mount_path.resolve(strict=False)
    from .settings import load

    for other in profiles():
        if other.id == profile.id:
            continue
        try:
            configured = load(other.config / "config.json")
            existing = configured.mount_path.resolve(strict=False)
        except Exception as error:
            raise ProfileError(f"could not inspect Profile {other.id} config") from error
        if _mounts_overlap(candidate, existing):
            raise ProfileError(
                f"Profile mount path overlaps Profile {other.id}: {candidate}"
            )


def _prepare_config_scaffold(profile: Profile) -> None:
    if os.path.lexists(profile.config):
        _require_private_directory(profile.config.parent.parent)
        _require_private_directory(profile.config.parent)
        _require_private_directory(profile.config)
        if _has_strict_config(profile.config):
            return
        if next(profile.config.iterdir(), None) is not None:
            raise ProfileError(f"Profile {profile.id} has config residue")

    for root in (profile.state, profile.data, profile.cache):
        if os.path.lexists(root):
            raise ProfileError(f"Profile {profile.id} has local residue")

    from .settings import has_profile_api_keys

    try:
        has_keys = has_profile_api_keys(profile.id)
    except Exception as error:
        raise ProfileError("could not inspect Profile API keys") from error
    if has_keys:
        raise ProfileError(f"Profile {profile.id} has an API key without config")
    if not os.path.lexists(profile.config):
        _ensure_profile_tree(profile.config, final=True)


@contextmanager
def manage_profile(
    profile: Profile, mount_path: Path | None = None
) -> Iterator[Profile]:
    profile = _checked_profile(profile)
    application, runtime = _prepare_runtime(profile)
    global_lock = _acquire_lock(
        application / "profiles.lock",
        fcntl.LOCK_EX,
        ProfileBusyError,
        "Profile management is busy",
    )
    service_lock: int | None = None
    try:
        service_lock = _acquire_lock(
            runtime / "service.lock",
            fcntl.LOCK_EX,
            ProfileError,
            f"Profile {profile.id} is already claimed",
        )
        _reject_nondefault_legacy(profile)
        if os.path.lexists(_legacy_config(profile)):
            _migrate_default_locked(profile)
        _validate_mount_separation(profile, mount_path)
        _prepare_config_scaffold(profile)
        yield profile
    finally:
        _release(service_lock)
        _release(global_lock)


def _valid_entry(metadata: os.stat_result, kind: str) -> bool:
    if kind == "directory":
        return (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        )
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
    )


def _open_private_directory(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ProfileError(f"unsafe Profile directory: {path}") from error
    if not _valid_entry(os.fstat(descriptor), "directory"):
        os.close(descriptor)
        raise ProfileError(f"unsafe Profile directory: {path}")
    return descriptor


def _strict_entry_at(
    directory: int, path: Path, kind: str, operation: str = "legacy migration"
) -> os.stat_result | None:
    try:
        metadata = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not _valid_entry(metadata, kind):
        raise ProfileError(f"unsafe {operation} entry: {path}")
    return metadata


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    names = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(first, name) == getattr(second, name) for name in names)


def _require_directory_identity(path: Path, descriptor: int) -> None:
    from .settings import _open_config_directory

    try:
        current_descriptor = _open_config_directory(path / "config.json")
    except (OSError, RuntimeError, ValueError) as error:
        raise ProfileError(f"Profile directory changed: {path}") from error
    try:
        current = os.fstat(current_descriptor)
        opened = os.fstat(descriptor)
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            raise ProfileError(f"Profile directory changed: {path}")
    finally:
        os.close(current_descriptor)


def _migration_entries(profile: Profile) -> tuple[tuple[Path, Path, str], ...]:
    return (
        (
            profile.state.parent.parent / "catalog.db",
            profile.state / "catalog.db",
            "file",
        ),
        (
            profile.state.parent.parent / "catalog.db-wal",
            profile.state / "catalog.db-wal",
            "file",
        ),
        (
            profile.state.parent.parent / "catalog.db-shm",
            profile.state / "catalog.db-shm",
            "file",
        ),
        (
            profile.data.parent.parent / "uploads",
            profile.data / "uploads",
            "directory",
        ),
        (
            profile.cache.parent.parent / "originals",
            profile.cache / "originals",
            "directory",
        ),
        (_legacy_config(profile), profile.config / "config.json", "file"),
    )


def _rename_and_fsync(
    source_directory: int,
    destination_directory: int,
    source: Path,
    destination: Path,
    kind: str,
    expected: os.stat_result,
) -> None:
    try:
        current = os.stat(
            source.name,
            dir_fd=source_directory,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise ProfileError(f"Profile source changed: {source}") from None
    if not _valid_entry(current, kind) or not _same_entry(current, expected):
        raise ProfileError(f"Profile source changed: {source}")
    result = _RENAMEAT2(
        source_directory,
        os.fsencode(source.name),
        destination_directory,
        os.fsencode(destination.name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise ProfileError(
                f"Profile destination appeared: {destination}"
            )
        raise OSError(number, os.strerror(number), source)
    os.fsync(source_directory)
    os.fsync(destination_directory)


def _migrate_default_locked(profile: Profile) -> None:
    if profile.id != "default":
        raise ProfileError("legacy installation can migrate only to Profile default")
    for root in (profile.config, profile.state, profile.data, profile.cache):
        _ensure_profile_tree(root, final=True)

    roots = (profile.config, profile.state, profile.data, profile.cache, profile.runtime)
    for root in roots:
        for entry in root.parent.iterdir():
            if entry.name != "default":
                raise ProfileError(f"legacy migration found other Profile artifact: {entry.name}")

    from .settings import has_nondefault_profile_api_keys

    try:
        if has_nondefault_profile_api_keys():
            raise ProfileError("legacy migration found another Profile API key")
    except ProfileError:
        raise
    except Exception as error:
        raise ProfileError("could not inspect Profile API keys") from error

    allowed_catalog = {"catalog.db", "catalog.db-wal", "catalog.db-shm"}
    for entry in profile.state.parent.parent.iterdir():
        if entry.name.startswith("catalog.db") and entry.name not in allowed_catalog:
            raise ProfileError(f"legacy migration found unknown catalog entry: {entry.name}")

    entries = _migration_entries(profile)
    opened: list[int] = []
    checked: list[
        tuple[
            Path,
            Path,
            str,
            int,
            int,
            os.stat_result | None,
            os.stat_result | None,
        ]
    ] = []
    try:
        for source, destination, kind in entries:
            source_directory = _open_private_directory(source.parent)
            destination_directory = _open_private_directory(destination.parent)
            opened.extend((source_directory, destination_directory))
            source_exists = _strict_entry_at(source_directory, source, kind)
            destination_exists = _strict_entry_at(
                destination_directory, destination, kind
            )
            if source_exists and destination_exists:
                raise ProfileError(
                    f"legacy migration source and destination both exist: {source}"
                )
            checked.append(
                (
                    source,
                    destination,
                    kind,
                    source_directory,
                    destination_directory,
                    source_exists,
                    destination_exists,
                )
            )
        if checked[-1][5] is None:
            raise ProfileError("legacy config disappeared during migration")

        from .settings import copy_legacy_api_keys_to_default, load

        try:
            settings = load(_legacy_config(profile))
        except Exception as error:
            raise ProfileError("could not load legacy Profile config") from error
        if os.path.ismount(settings.mount_path):
            raise ProfileError("legacy Profile mount is still mounted")
        try:
            copy_legacy_api_keys_to_default(settings)
        except Exception as error:
            raise ProfileError("could not migrate legacy Profile credentials") from error

        for (
            source,
            destination,
            kind,
            source_directory,
            destination_directory,
            expected_source,
            expected_destination,
        ) in checked:
            current_source = _strict_entry_at(source_directory, source, kind)
            current_destination = _strict_entry_at(
                destination_directory, destination, kind
            )
            if (expected_source is None) != (current_source is None) or (
                expected_source is not None
                and current_source is not None
                and not _same_entry(expected_source, current_source)
            ):
                raise ProfileError(f"legacy migration source changed: {source}")
            if (expected_destination is None) != (current_destination is None) or (
                expected_destination is not None
                and current_destination is not None
                and not _same_entry(expected_destination, current_destination)
            ):
                raise ProfileError(
                    f"legacy migration destination changed: {destination}"
                )
            if expected_source is None:
                continue
            try:
                _rename_and_fsync(
                    source_directory,
                    destination_directory,
                    source,
                    destination,
                    kind,
                    expected_source,
                )
            except ProfileError:
                raise
            except OSError as error:
                raise ProfileError(
                    f"could not migrate legacy entry: {source}"
                ) from error
    finally:
        for descriptor in opened:
            os.close(descriptor)
    if not _has_strict_config(profile.config):
        raise ProfileError("legacy Profile migration did not publish config")


def _resolved_mount(settings: Settings) -> Path:
    try:
        return settings.mount_path.resolve(strict=False)
    except OSError as error:
        raise ProfileError("could not resolve Profile mount path") from error


def _mount_lock_path(directory: Path, mount: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(mount)).hexdigest()
    return directory / digest


def _acquire_mount_locks(application: Path, mount: Path) -> list[int]:
    directory = application / "mounts"
    _ensure_private_directory(directory)
    locks: list[int] = []
    try:
        paths = (*reversed(mount.parents), mount)
        for index, path in enumerate(paths):
            operation = fcntl.LOCK_EX if index == len(paths) - 1 else fcntl.LOCK_SH
            locks.append(
                _acquire_lock(
                    _mount_lock_path(directory, path),
                    operation,
                    ProfileError,
                    f"mount path is already claimed: {mount}",
                )
            )
        return locks
    except BaseException:
        for descriptor in reversed(locks):
            _release(descriptor)
        raise


def retire_profile(profile: Profile) -> None:
    profile = _checked_profile(profile)
    application, runtime = _prepare_runtime(profile)
    global_lock = _acquire_lock(
        application / "profiles.lock",
        fcntl.LOCK_EX,
        ProfileBusyError,
        "Profile management is busy",
    )
    service_lock: int | None = None
    mount_locks: list[int] = []
    directory: int | None = None
    try:
        service_lock = _acquire_lock(
            runtime / "service.lock",
            fcntl.LOCK_EX,
            ProfileError,
            f"Profile {profile.id} is already claimed",
        )
        if os.path.lexists(_legacy_config(profile)):
            raise ProfileError("legacy config must migrate to Profile default first")

        active = profile.config / "config.json"
        retired = profile.config / "config.retired.json"
        from .settings import _open_config_directory, load

        try:
            directory = _open_config_directory(active)
        except (OSError, RuntimeError, ValueError) as error:
            raise ProfileError(f"unsafe Profile directory: {profile.config}") from error
        if (
            _strict_entry_at(directory, retired, "file", "Profile retirement")
            is not None
        ):
            raise ProfileError(f"Profile {profile.id} is already retired")
        expected = _strict_entry_at(directory, active, "file", "Profile retirement")
        if expected is None:
            raise ProfileError(f"Profile {profile.id} is not active")

        try:
            settings = load(active)
        except Exception as error:
            raise ProfileError(f"could not load Profile {profile.id} config") from error
        _require_directory_identity(profile.config, directory)
        loaded = _strict_entry_at(
            directory, active, "file", "Profile retirement"
        )
        if loaded is None or not _same_entry(loaded, expected):
            raise ProfileError(f"Profile source changed: {active}")
        mount = _resolved_mount(settings)
        mount_locks = _acquire_mount_locks(application, mount)
        if _resolved_mount(settings) != mount:
            raise ProfileError("Profile mount path changed while it was claimed")
        if os.path.ismount(mount):
            raise ProfileError(f"Profile {profile.id} mount is still mounted")
        if (
            _strict_entry_at(directory, retired, "file", "Profile retirement")
            is not None
        ):
            raise ProfileError(f"Profile {profile.id} is already retired")
        _require_directory_identity(profile.config, directory)
        try:
            _rename_and_fsync(
                directory,
                directory,
                active,
                retired,
                "file",
                expected,
            )
        except OSError as error:
            raise ProfileError(f"could not retire Profile {profile.id}") from error
    finally:
        if directory is not None:
            os.close(directory)
        for descriptor in reversed(mount_locks):
            _release(descriptor)
        _release(service_lock)
        _release(global_lock)


@contextmanager
def claim_service(profile: Profile) -> Iterator[Settings]:
    profile = _checked_profile(profile)
    application, runtime = _prepare_runtime(profile)
    global_lock = _acquire_lock(
        application / "profiles.lock",
        fcntl.LOCK_EX,
        ProfileBusyError,
        "Profile management is busy",
    )
    service_lock: int | None = None
    mount_locks: list[int] = []
    try:
        try:
            service_lock = _acquire_lock(
                runtime / "service.lock",
                fcntl.LOCK_EX,
                ProfileError,
                f"Profile {profile.id} is already claimed",
            )
            _reject_nondefault_legacy(profile)
            if os.path.lexists(_legacy_config(profile)):
                _migrate_default_locked(profile)
        finally:
            _release(global_lock)

        from .settings import load

        try:
            settings = load(profile.config / "config.json")
        except Exception as error:
            raise ProfileError(f"could not load Profile {profile.id} config") from error

        mount = _resolved_mount(settings)
        mount_locks = _acquire_mount_locks(application, mount)
        if _resolved_mount(settings) != mount:
            raise ProfileError("Profile mount path changed while it was claimed")
        for root in (profile.state, profile.data, profile.cache):
            _ensure_profile_tree(root, final=False)
            if os.path.lexists(root):
                _require_private_directory(root)
        yield replace(settings, mount_path=mount)
    finally:
        for descriptor in reversed(mount_locks):
            _release(descriptor)
        _release(service_lock)
