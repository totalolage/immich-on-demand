from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import trio

from immich_on_demand.catalog import CatalogAsset, CatalogStats
from immich_on_demand.immich import (
    ImmichPageLimitError,
    ImmichResponseError,
    MUTATION_PERMISSIONS,
    ServerSession,
    UPLOAD_PERMISSIONS,
)
from immich_on_demand.model import Asset
from immich_on_demand.service import FULL_REFRESH_SECONDS, _periodic_refresh, run_service
from immich_on_demand.settings import Settings


OWNER_ID = "87654321-4321-4321-8321-cba987654321"
ASSET_ID = "12345678-1234-4234-8234-123456789abc"


def upload_entry() -> CatalogAsset:
    return CatalogAsset(
        Asset(
            ASSET_ID,
            OWNER_ID,
            "new.jpg",
            "image/jpeg",
            None,
            1,
            2,
            "2026-08-25T12:00:00Z",
            "abc=",
            "timeline",
            False,
            False,
            None,
        ),
        2,
        "new.jpg",
    )


class ServiceFakes:
    def __init__(
        self,
        root: Path,
        *,
        mutation: bool = True,
        mutation_owner: str = OWNER_ID,
        block_preview: bool = True,
        preview_error_call: int | None = None,
        refresh_error_call: int | None = None,
        incremental_page_limit: bool = False,
        incremental_response_error: bool = False,
    ) -> None:
        self.root = root
        self.mutation = mutation
        self.block_preview = block_preview
        self.preview_error_call = preview_error_call
        self.refresh_error_call = refresh_error_call
        self.incremental_page_limit = incremental_page_limit
        self.incremental_response_error = incremental_response_error
        self.events: list[str] = []
        self.clients: list[ServiceFakes.Client] = []
        self.handlers: dict[str, object] = {}
        self.cache: ServiceFakes.Cache | None = None
        self.preview_gate = trio.Event()
        self.main_started = trio.Event()
        self.stop_main = trio.Event()
        self.terminated = trio.Event()
        self.second_refresh = trio.Event()
        self.background_done = trio.Event()
        self.incremental_done = trio.Event()
        self.on_uploaded: object = None
        self.fuse_options: set[str] = set()
        self.catalog_locks: list[object] = []
        outer = self

        class Client:
            def __init__(self, server_url: str, key: str) -> None:
                self.key = key
                self.validations: list[object] = []
                outer.clients.append(self)

            async def validate(self, permissions: object = None) -> ServerSession:
                self.validations.append(permissions)
                outer.events.append(f"validate:{self.key}")
                owner_id = mutation_owner if self.key == "mutation" else OWNER_ID
                return ServerSession(owner_id, "3.0.3", frozenset({".jpg"}), True)

            async def close(self) -> None:
                outer.events.append(f"close:{self.key}")

        class Catalog:
            def __init__(self, path: Path) -> None:
                outer.events.append(f"catalog:{path.relative_to(root)}")

            def __enter__(self) -> ServiceFakes.Catalog:
                return self

            def __exit__(self, *args: object) -> None:
                outer.events.append("catalog-close")

            def stats(self) -> CatalogStats:
                return CatalogStats(7, 6, 1, 0, 0, 0)

        class Cache:
            def __init__(
                self,
                path: Path,
                client: object,
                *,
                max_bytes: int,
                minimum_free_bytes: int,
            ) -> None:
                self.limit_calls: list[dict[str, int]] = []
                self.asset_evictions: list[str] = []
                outer.cache = self
                outer.events.append(f"cache:{path.relative_to(root)}")
                outer.events.append(f"cache-policy:{max_bytes}:{minimum_free_bytes}")

            def evict_to_limits(self, **kwargs: int) -> list[str]:
                self.limit_calls.append(kwargs)
                outer.events.append(f"evict:{kwargs['max_bytes']}")
                if len(self.limit_calls) == 3:
                    outer.background_done.set()
                return ["old"]

            def evict(self, asset_id: str) -> bool:
                self.asset_evictions.append(asset_id)
                return True

        class Library:
            def __init__(
                self,
                catalog: object,
                read_client: object,
                content_cache: object,
                settings: Settings,
                *,
                mutation_client: object = None,
                mutation_session: object = None,
                catalog_lock: object,
            ) -> None:
                outer.catalog_locks.append(catalog_lock)
                self.mutation_enabled = mutation_client is not None and mutation_session is not None
                outer.events.append(f"library:mutation={self.mutation_enabled}")

            def list(self) -> list[object]:
                return []

        class Filesystem:
            def __init__(self, library: object, path: Path, *, on_uploaded: object) -> None:
                outer.on_uploaded = on_uploaded
                outer.events.append(f"filesystem:{path.relative_to(root)}")

        self.Client = Client
        self.Catalog = Catalog
        self.Cache = Cache
        self.Library = Library
        self.Filesystem = Filesystem

    def key(self, settings: Settings, purpose: str) -> str:
        if purpose == "read-only":
            return "read"
        if self.mutation:
            return "mutation"
        raise RuntimeError("expected one mutation API key in Secret Service, found 0")

    async def refresh(
        self, catalog: object, client: object, session: object, catalog_lock: object
    ) -> CatalogStats:
        self.catalog_locks.append(catalog_lock)
        self.events.append("refresh")
        if self.events.count("refresh") == 2:
            self.second_refresh.set()
        if self.events.count("refresh") == self.refresh_error_call:
            raise OSError("Immich unavailable")
        return CatalogStats(7, 6, 1, 0, 0, 0)

    async def incremental_refresh(
        self,
        catalog: object,
        client: object,
        session: object,
        catalog_lock: object,
        *,
        refresh_seconds: int,
    ) -> CatalogStats:
        self.catalog_locks.append(catalog_lock)
        self.events.append(f"incremental:{refresh_seconds}")
        self.incremental_done.set()
        if self.incremental_page_limit:
            raise ImmichPageLimitError("page limit")
        if self.incremental_response_error:
            raise ImmichResponseError("invalid search response")
        return CatalogStats(7, 6, 1, 0, 0, 0)

    async def previews(
        self,
        entries: object,
        client: object,
        mount: Path,
        *,
        mount_ready: trio.Event,
        task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
    ) -> object:
        self.events.append("suppress")
        if self.events.count("suppress") == self.preview_error_call:
            raise OSError("thumbnail cache unavailable")
        task_status.started()
        await mount_ready.wait()
        self.events.append("sort")
        self.events.append("fetch")
        if self.block_preview and self.events.count("fetch") == 1:
            await self.preview_gate.wait()
        self.events.append("preview-done")
        return None

    async def control(
        self,
        path: Path,
        handlers: dict[str, object],
        *,
        task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
    ) -> None:
        self.handlers = handlers
        self.events.append(f"control:{path.relative_to(self.root)}")
        task_status.started()
        try:
            await trio.sleep_forever()
        finally:
            self.events.append("control-close")

    def fuse_init(self, filesystem: object, mountpoint: str, options: set[str]) -> None:
        self.fuse_options = options
        self.events.append("fuse-init")

    async def fuse_main(self) -> None:
        self.events.append("fuse-main")
        self.main_started.set()
        await self.stop_main.wait()

    def fuse_close(self, *, unmount: bool) -> None:
        self.events.append(f"fuse-close:{unmount}")

    def fuse_terminate(self) -> None:
        self.events.append("fuse-terminate")
        self.terminated.set()
        self.stop_main.set()

    def patches(self) -> ExitStack:
        stack = ExitStack()
        replacements = {
            "ImmichClient": self.Client,
            "Catalog": self.Catalog,
            "ContentCache": self.Cache,
            "Library": self.Library,
            "ImmichFilesystem": self.Filesystem,
            "load_api_key": self.key,
            "refresh_catalog": self.refresh,
            "refresh_catalog_incremental": self.incremental_refresh,
            "populate_previews": self.previews,
            "serve_control": self.control,
            "state_path": lambda: self.root / "state",
            "cache_path": lambda: self.root / "cache",
            "runtime_path": lambda: self.root / "runtime",
        }
        for name, value in replacements.items():
            stack.enter_context(patch(f"immich_on_demand.service.{name}", value))
        stack.enter_context(patch("immich_on_demand.service.pyfuse3.init", self.fuse_init))
        stack.enter_context(patch("immich_on_demand.service.pyfuse3.main", self.fuse_main))
        stack.enter_context(patch("immich_on_demand.service.pyfuse3.close", self.fuse_close))
        stack.enter_context(
            patch("immich_on_demand.service.pyfuse3.terminate", self.fuse_terminate)
        )
        return stack


class ServiceTest(unittest.TestCase):
    def test_periodic_refresh_wakes_for_the_daily_full_sweep(self) -> None:
        async def scenario() -> None:
            sleeps: list[int] = []

            async def stop_after_sleep(seconds: int) -> None:
                sleeps.append(seconds)
                if len(sleeps) == 2:
                    raise RuntimeError("stop")

            requests, refreshes = trio.open_memory_channel[bool](1)
            full_requested = [False]
            with patch("immich_on_demand.service.trio.sleep", stop_after_sleep):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    await _periodic_refresh(
                        requests,
                        FULL_REFRESH_SECONDS,
                        True,
                        full_requested,
                    )
            self.assertEqual(sleeps, [FULL_REFRESH_SECONDS, FULL_REFRESH_SECONDS])
            self.assertTrue(full_requested[0])
            self.assertTrue(refreshes.receive_nowait())

        trio.run(scenario)

    def test_manual_refresh_upgrades_an_already_queued_incremental_refresh(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root)
            settings = Settings(
                "https://photos.example.test",
                root / "mount",
                refresh_seconds=3600,
            )
            with fakes.patches():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    callback = fakes.on_uploaded
                    await callback(upload_entry())  # type: ignore[operator]
                    refresh = fakes.handlers["refresh"]
                    self.assertEqual(  # type: ignore[operator]
                        await refresh({}),
                        {"scheduled": True},
                    )
                    fakes.preview_gate.set()
                    with trio.fail_after(0.1):
                        await fakes.second_refresh.wait()
                    fakes.stop_main.set()

            self.assertEqual(fakes.events.count("refresh"), 2)
            self.assertNotIn("incremental:3600", fakes.events)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_incremental_anomalies_fall_back_to_a_full_sweep(self) -> None:
        async def scenario(root: Path, option: str) -> None:
            fakes = ServiceFakes(
                root,
                block_preview=False,
                **{option: True},
            )
            settings = Settings(
                "https://photos.example.test",
                root / "mount",
                refresh_seconds=3600,
            )
            with fakes.patches():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    callback = fakes.on_uploaded
                    await callback(upload_entry())  # type: ignore[operator]
                    await fakes.second_refresh.wait()
                    fakes.stop_main.set()

            self.assertEqual(fakes.events.count("refresh"), 2)
            self.assertLess(
                fakes.events.index("incremental:3600"),
                len(fakes.events) - 1 - fakes.events[::-1].index("refresh"),
            )

        for option in ("incremental_page_limit", "incremental_response_error"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                trio.run(scenario, Path(directory), option)

    def test_lifecycle_prompt_refresh_controls_and_cleanup(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root)
            settings = Settings(
                "https://photos.example.test",
                root / "mount",
                cache_max_bytes=11,
                cache_max_age_seconds=22,
                minimum_free_bytes=33,
                refresh_seconds=3600,
            )
            with fakes.patches():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    self.assertLess(fakes.events.index("refresh"), fakes.events.index("suppress"))
                    self.assertLess(fakes.events.index("suppress"), fakes.events.index("fuse-init"))
                    self.assertLess(fakes.events.index("fuse-init"), fakes.events.index("sort"))
                    self.assertLess(fakes.events.index("evict:11"), fakes.events.index("fuse-init"))
                    self.assertNotIn("preview-done", fakes.events)
                    self.assertIn("auto_unmount", fakes.fuse_options)

                    status = fakes.handlers["status"]
                    refresh = fakes.handlers["refresh"]
                    evict = fakes.handlers["evict"]
                    self.assertEqual(
                        await status({}),  # type: ignore[operator]
                        {
                            "total": 7,
                            "visible": 6,
                            "missing_size": 1,
                            "trashed": 0,
                            "hidden": 0,
                            "offline": 0,
                            "mutation_enabled": True,
                        },
                    )
                    with trio.fail_after(0.1):
                        self.assertEqual(await refresh({}), {"scheduled": True})  # type: ignore[operator]
                        self.assertEqual(await refresh({}), {"scheduled": True})  # type: ignore[operator]
                    self.assertEqual(await evict({}), {"evicted": 1})  # type: ignore[operator]
                    self.assertEqual(
                        await evict({"asset": ASSET_ID}), {"evicted": True}  # type: ignore[operator]
                    )
                    with self.assertRaises(ValueError):
                        await evict({"asset": "not-a-uuid"})  # type: ignore[operator]

                    fakes.preview_gate.set()
                    await fakes.second_refresh.wait()
                    await fakes.background_done.wait()
                    callback = fakes.on_uploaded
                    await callback(upload_entry())  # type: ignore[operator]
                    await fakes.incremental_done.wait()
                    evictions = [
                        index
                        for index, event in enumerate(fakes.events)
                        if event == "evict:11"
                    ]
                    fetches = [
                        index for index, event in enumerate(fakes.events) if event == "fetch"
                    ]
                    self.assertLess(evictions[1], fetches[1])
                    self.assertIn("incremental:3600", fakes.events)
                    fakes.stop_main.set()

            assert fakes.cache is not None
            expected_limits = {
                "max_age_seconds": 22,
                "max_bytes": 11,
                "minimum_free_bytes": 33,
            }
            self.assertEqual(
                fakes.cache.limit_calls[0],
                expected_limits,
            )
            self.assertEqual(
                fakes.cache.limit_calls[1],
                {"max_age_seconds": 0, "max_bytes": 0, "minimum_free_bytes": 0},
            )
            self.assertTrue(all(call == expected_limits for call in fakes.cache.limit_calls[2:]))
            self.assertEqual(fakes.cache.asset_evictions, [ASSET_ID])
            self.assertIn("fuse-close:True", fakes.events)
            self.assertIn("control-close", fakes.events)
            self.assertIn("cache-policy:11:33", fakes.events)
            self.assertTrue(fakes.catalog_locks)
            self.assertTrue(
                all(lock is fakes.catalog_locks[0] for lock in fakes.catalog_locks)
            )
            self.assertIn("catalog-close", fakes.events)
            self.assertIn("close:read", fakes.events)
            self.assertIn("close:mutation", fakes.events)
            self.assertNotIn("fuse-terminate", fakes.events)
            self.assertEqual(fakes.clients[1].validations, [UPLOAD_PERMISSIONS])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_runtime_preview_suppression_failure_terminates_the_mount(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root, block_preview=False, preview_error_call=2)
            settings = Settings(
                "https://photos.example.test", root / "mount", refresh_seconds=3600
            )
            failures: list[RuntimeError] = []

            async def serve() -> None:
                try:
                    await run_service(settings)
                except RuntimeError as error:
                    failures.append(error)

            with fakes.patches(), self.assertLogs(
                "immich_on_demand.service", level="ERROR"
            ) as logs:
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(serve)
                    await fakes.main_started.wait()
                    refresh = fakes.handlers["refresh"]
                    self.assertEqual(await refresh({}), {"scheduled": True})  # type: ignore[operator]
                    with trio.fail_after(0.1):
                        await fakes.terminated.wait()

            self.assertLess(
                fakes.events.index("fuse-terminate"),
                fakes.events.index("fuse-close:True"),
            )
            self.assertEqual(str(failures[0]), "preview suppression failed; mount terminated")
            self.assertIn("preview suppression failed", "\n".join(logs.output))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_refresh_outage_keeps_the_existing_mount_running(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(
                root, block_preview=False, refresh_error_call=2
            )
            settings = Settings(
                "https://photos.example.test", root / "mount", refresh_seconds=3600
            )

            with fakes.patches(), self.assertLogs(
                "immich_on_demand.service", level="WARNING"
            ) as logs:
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    refresh = fakes.handlers["refresh"]
                    self.assertEqual(await refresh({}), {"scheduled": True})  # type: ignore[operator]
                    with trio.fail_after(0.1):
                        await fakes.second_refresh.wait()
                    await trio.sleep(0)
                    self.assertNotIn("fuse-terminate", fakes.events)
                    self.assertNotIn("fuse-close:True", fakes.events)
                    fakes.stop_main.set()

            self.assertIn("background refresh failed", "\n".join(logs.output))
            self.assertIn("fuse-close:True", fakes.events)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_background_refresh_cannot_yield_before_preview_suppression(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root, block_preview=False)
            settings = Settings(
                "https://photos.example.test", root / "mount", refresh_seconds=3600
            )

            async def observe_commit() -> None:
                await fakes.second_refresh.wait()
                self.assertEqual(fakes.events.count("suppress"), 2)
                fakes.stop_main.set()

            with fakes.patches():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    nursery.start_soon(observe_commit)
                    await fakes.main_started.wait()
                    refresh = fakes.handlers["refresh"]
                    self.assertEqual(await refresh({}), {"scheduled": True})  # type: ignore[operator]

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_upload_suppression_failure_terminates_and_fails_the_service(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root, block_preview=False)
            settings = Settings(
                "https://photos.example.test", root / "mount", refresh_seconds=3600
            )
            entry = CatalogAsset(
                Asset(
                    ASSET_ID,
                    OWNER_ID,
                    "uploaded.jpg",
                    "image/jpeg",
                    123,
                    1,
                    4_999_999_999,
                    "2026-08-25T12:00:00Z",
                    "abc=",
                    "timeline",
                    False,
                    False,
                    None,
                ),
                3,
                "uploaded.jpg",
            )
            failures: list[RuntimeError] = []

            async def serve() -> None:
                try:
                    await run_service(settings)
                except RuntimeError as error:
                    failures.append(error)

            with fakes.patches(), patch(
                "immich_on_demand.service.prepare_thumbnail_cache",
                side_effect=OSError("thumbnail cache unavailable"),
            ):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(serve)
                    await fakes.main_started.wait()
                    on_uploaded = fakes.on_uploaded
                    assert callable(on_uploaded)
                    with self.assertRaises(OSError):
                        await on_uploaded(entry)
                    fakes.fuse_terminate()

            self.assertLess(
                fakes.events.index("fuse-terminate"),
                fakes.events.index("fuse-close:True"),
            )
            self.assertEqual(str(failures[0]), "preview suppression failed; mount terminated")

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_upload_suppresses_fallback_before_coalesced_background_refresh(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root)
            settings = Settings("https://photos.example.test", root / "mount", refresh_seconds=3600)
            entry = CatalogAsset(
                Asset(
                    ASSET_ID,
                    OWNER_ID,
                    "uploaded.jpg",
                    "image/jpeg",
                    123,
                    1,
                    4_999_999_999,
                    "2026-08-25T12:00:00Z",
                    "abc=",
                    "timeline",
                    False,
                    False,
                    None,
                ),
                3,
                "uploaded.jpg",
            )
            prepared: list[tuple[Path, int, int, str | None]] = []

            def prepare(
                source: Path,
                mtime: int,
                size: int,
                *,
                retain_size: str | None,
            ) -> bool:
                prepared.append((source, mtime, size, retain_size))
                fakes.events.append("upload-suppressed")
                return False

            with fakes.patches(), patch(
                "immich_on_demand.service.prepare_thumbnail_cache", prepare
            ):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    on_uploaded = fakes.on_uploaded
                    assert callable(on_uploaded)
                    await on_uploaded(entry)
                    await on_uploaded(entry)
                    self.assertEqual(
                        prepared,
                        [
                            (root / "mount" / "uploaded.jpg", 4, 123, None),
                            (root / "mount" / "uploaded.jpg", 4, 123, None),
                        ],
                    )
                    self.assertEqual(fakes.events.count("refresh"), 1)

                    fakes.preview_gate.set()
                    await fakes.incremental_done.wait()
                    await trio.sleep(0)
                    self.assertEqual(fakes.events.count("refresh"), 1)
                    self.assertIn("incremental:3600", fakes.events)
                    fakes.stop_main.set()

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_missing_mutation_key_runs_read_only_with_exact_read_scope(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root, mutation=False, block_preview=False)
            settings = Settings("https://photos.example.test", root / "mount", refresh_seconds=3600)
            fakes.stop_main.set()
            with fakes.patches():
                await run_service(settings)

            self.assertEqual(len(fakes.clients), 1)
            self.assertEqual(fakes.clients[0].validations, [None])
            self.assertIn("library:mutation=False", fakes.events)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_remote_delete_key_uses_exact_mutation_permissions(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root, block_preview=False)
            settings = Settings(
                "https://photos.example.test",
                root / "mount",
                refresh_seconds=3600,
                remote_delete=True,
            )
            fakes.stop_main.set()
            with fakes.patches():
                await run_service(settings)

            self.assertEqual(fakes.clients[1].validations, [MUTATION_PERMISSIONS])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_rejects_mutation_key_for_a_different_user_and_closes_clients(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(
                root,
                mutation_owner="97654321-4321-4321-8321-cba987654321",
                block_preview=False,
            )
            settings = Settings("https://photos.example.test", root / "mount")
            with fakes.patches(), self.assertRaisesRegex(RuntimeError, "different Immich users"):
                await run_service(settings)

            self.assertIn("close:mutation", fakes.events)
            self.assertIn("close:read", fakes.events)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_refuses_unsafe_mountpoint_before_credentials(self) -> None:
        async def scenario(root: Path) -> None:
            target = root / "target"
            target.mkdir()
            symlink = root / "symlink"
            symlink.symlink_to(target, target_is_directory=True)
            nonempty = root / "nonempty"
            nonempty.mkdir()
            (nonempty / "keep").touch()
            for mount in (symlink, nonempty):
                with self.assertRaises((PermissionError, OSError)):
                    await run_service(Settings("https://photos.example.test", mount))
            foreign = root / "foreign"
            foreign.mkdir()
            with patch(
                "immich_on_demand.service.os.getuid", return_value=foreign.stat().st_uid + 1
            ):
                with self.assertRaises(PermissionError):
                    await run_service(Settings("https://photos.example.test", foreign))

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_refuses_a_symlinked_cache_root_before_credentials(self) -> None:
        async def scenario(root: Path) -> None:
            target = root / "cache-target"
            target.mkdir(mode=0o755)
            (root / "cache").symlink_to(target, target_is_directory=True)
            fakes = ServiceFakes(root, block_preview=False)

            with fakes.patches(), self.assertRaisesRegex(PermissionError, "cache root"):
                await run_service(
                    Settings("https://photos.example.test", root / "mount")
                )

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            self.assertEqual(fakes.clients, [])

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))


if __name__ == "__main__":
    unittest.main()
