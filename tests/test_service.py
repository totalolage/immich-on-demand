from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import base64
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import trio

from immich_on_demand.catalog import Catalog, CatalogAsset, CatalogStats, TrustedProfile
from immich_on_demand.control import ControlError, send_request, serve_control
from immich_on_demand.immich import (
    ImmichPageLimitError,
    ImmichResponseError,
    ImmichUnavailableError,
    MUTATION_PERMISSIONS,
    ServerSession,
    UploadResult,
    UPLOAD_PERMISSIONS,
)
from immich_on_demand.model import Asset
from immich_on_demand.service import (
    FULL_REFRESH_SECONDS,
    _RestoreJob,
    _periodic_refresh,
    _pin_worker,
    _upload_matches,
    _process_upload,
    _restore_worker,
    run_service,
)
from immich_on_demand.settings import Settings
from immich_on_demand.uploads import UploadErrorCode, UploadQueue, UploadState


OWNER_ID = "87654321-4321-4321-8321-cba987654321"
ASSET_ID = "12345678-1234-4234-8234-123456789abc"
PINNED_ID = "aaaaaaaa-1234-4234-8234-123456789abc"
OTHER_UPLOAD_ID = "bbbbbbbb-1234-4234-8234-123456789abc"


def trusted_profile(read_key: str = "read") -> TrustedProfile:
    return TrustedProfile(
        "https://photos.example.test",
        OWNER_ID,
        "3.0.3",
        frozenset({"user.read", "asset.read", "asset.view", "asset.download"}),
        hashlib.sha256(read_key.encode()).hexdigest(),
    )


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
        pin_persist_error: bool = False,
        restore_error: Exception | None = None,
        block_restore: bool = False,
        read_validation_error: Exception | None = None,
        trusted_profile: TrustedProfile | None = None,
    ) -> None:
        self.root = root
        self.mutation = mutation
        self.block_preview = block_preview
        self.preview_error_call = preview_error_call
        self.refresh_error_call = refresh_error_call
        self.incremental_page_limit = incremental_page_limit
        self.incremental_response_error = incremental_response_error
        self.pin_persist_error = pin_persist_error
        self.restore_error = restore_error
        self.block_restore = block_restore
        self.read_validation_error = read_validation_error
        self.trusted_profile_value = trusted_profile
        self.events: list[str] = []
        self.clients: list[ServiceFakes.Client] = []
        self.handlers: dict[str, object] = {}
        self.cache: ServiceFakes.Cache | None = None
        self.upload_queue: ServiceFakes.Queue | None = None
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
        self.restore_attempts: list[str] = []
        self.restored_ids: list[str] = []
        self.restore_started = trio.Event()
        self.restore_gate = trio.Event()
        self.persisted_pins = {PINNED_ID}
        self.pin_hydrated = trio.Event()
        self.promoted = trio.Event()
        self.upload_retries: list[str] = []
        self.upload_cancellations: list[tuple[str, str, int]] = []
        outer = self

        class Client:
            def __init__(self, server_url: str, key: str) -> None:
                self.key = key
                self.validations: list[object] = []
                outer.clients.append(self)

            async def validate(self, permissions: object = None) -> ServerSession:
                self.validations.append(permissions)
                outer.events.append(f"validate:{self.key}")
                if self.key == "read" and outer.read_validation_error is not None:
                    raise outer.read_validation_error
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

            def pinned_ids(self) -> frozenset[str]:
                return frozenset(outer.persisted_pins)

            def trusted_profile(self) -> TrustedProfile | None:
                return outer.trusted_profile_value

            def require_offline_profile(self, profile: TrustedProfile) -> None:
                if profile != outer.trusted_profile_value:
                    raise ValueError("catalog is not trusted for offline startup")

            def pin(self, asset_id: str) -> None:
                if outer.pin_persist_error:
                    raise OSError("private catalog path")
                outer.persisted_pins.add(asset_id)

            def unpin(self, asset_id: str) -> None:
                outer.persisted_pins.discard(asset_id)

            def by_id(self, asset_id: str) -> CatalogAsset | None:
                return upload_entry() if asset_id == ASSET_ID else None

        class Cache:
            def __init__(
                self,
                path: Path,
                client: object,
                *,
                max_bytes: int,
                minimum_free_bytes: int,
                pinned_ids: frozenset[str],
                downloads_enabled: bool = True,
            ) -> None:
                self.limit_calls: list[dict[str, int]] = []
                self.asset_evictions: list[str] = []
                self.pinned_ids = set(pinned_ids)
                self.downloads_enabled = downloads_enabled
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

            def describe(self, asset: Asset) -> dict[str, bool]:
                return {
                    "cached": True,
                    "busy": False,
                    "pinned": asset.id in self.pinned_ids,
                }

            def pin(self, asset_id: str) -> None:
                self.pinned_ids.add(asset_id)

            def unpin(self, asset_id: str) -> None:
                self.pinned_ids.discard(asset_id)

            def enable_downloads(self) -> None:
                self.downloads_enabled = True
                outer.events.append("downloads-enabled")

            async def hydrate(self, asset: Asset) -> Path:
                outer.pin_hydrated.set()
                return root / "cache" / "originals" / asset.id

        class Library:
            def __init__(
                self,
                catalog: object,
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
                return [upload_entry()]

            def enable_mutations(
                self, mutation_client: object, mutation_session: object
            ) -> None:
                self.mutation_enabled = True
                outer.events.append("mutations-enabled")
                outer.promoted.set()

            def upload_access(self):
                if not self.mutation_enabled:
                    raise RuntimeError("mutations are disabled")
                return outer.clients[-1], ServerSession(
                    OWNER_ID, "3.0.3", frozenset({".jpg"}), True
                )

            def lookup(self, identity: str | int) -> CatalogAsset | None:
                return upload_entry() if identity == "new.jpg" else None

            async def remote_restore(self, asset_id: str) -> None:
                if not self.mutation_enabled:
                    raise PermissionError("mutations are disabled")
                outer.restore_attempts.append(asset_id)
                outer.restore_started.set()
                if outer.block_restore:
                    await outer.restore_gate.wait()
                if outer.restore_error is not None:
                    raise outer.restore_error
                outer.restored_ids.append(asset_id)

        class Filesystem:
            def __init__(
                self,
                library: object,
                queue: object,
                server_origin: str,
                owner_id: str,
                *,
                on_pending: object,
            ) -> None:
                outer.events.append("filesystem:uploads")

        class Queue:
            def __init__(self, path: Path, *, minimum_free_bytes: int) -> None:
                self.jobs: list[object] = []
                self.quarantined_count = 0
                outer.upload_queue = self
                outer.events.append(f"uploads:{path.relative_to(root)}")

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                outer.events.append("uploads-close")

            def list(self) -> tuple[object, ...]:
                return tuple(self.jobs)

            def next_due(self):
                return None

            def status(self, job_id: str):
                return next(
                    (job for job in self.jobs if job.id == job_id), None
                )

            def retry(self, job_id: str, *, at_ns: int, revision: int | None = None):
                outer.upload_retries.append(job_id)
                return self.status(job_id)

            def cancel(
                self, job_id: str, *, requested_name: str, revision: int
            ) -> None:
                outer.upload_cancellations.append(
                    (job_id, requested_name, revision)
                )
                self.jobs = [job for job in self.jobs if job.id != job_id]

        self.Client = Client
        self.Catalog = Catalog
        self.Cache = Cache
        self.Library = Library
        self.Filesystem = Filesystem
        self.Queue = Queue

    def key(self, settings: Settings, purpose: str) -> str:
        if purpose == "read-only":
            return "read"
        if self.mutation:
            return "mutation"
        raise RuntimeError("expected one mutation API key in Secret Service, found 0")

    async def refresh(
        self,
        catalog: object,
        client: object,
        session: object,
        catalog_lock: object,
        *,
        trusted_profile: TrustedProfile | None = None,
    ) -> CatalogStats:
        self.catalog_locks.append(catalog_lock)
        self.events.append("refresh")
        if self.events.count("refresh") == 2:
            self.second_refresh.set()
        if self.events.count("refresh") == self.refresh_error_call:
            raise OSError("Immich unavailable")
        if trusted_profile is not None:
            self.trusted_profile_value = trusted_profile
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
        downloads_enabled: bool = True,
        mount_ready: trio.Event | None = None,
        task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED,
    ) -> object:
        self.events.append("suppress")
        if self.events.count("suppress") == self.preview_error_call:
            raise OSError("thumbnail cache unavailable")
        task_status.started()
        if not downloads_enabled:
            self.events.append("offline-suppress")
            return None
        if mount_ready is not None:
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

    async def upload_worker(self, *args: object) -> None:
        self.on_uploaded = args[-1]
        await trio.sleep_forever()

    def fuse_init(self, filesystem: object, mountpoint: str, options: set[str]) -> None:
        self.fuse_options = options
        self.events.append("fuse-init")

    async def fuse_main(self) -> None:
        self.events.append("fuse-main")
        await trio.lowlevel.checkpoint()
        self.main_started.set()
        await self.stop_main.wait()

    def fuse_close(self, *, unmount: bool) -> None:
        self.events.append(f"fuse-close:{unmount}")

    def fuse_terminate(self) -> None:
        self.events.append("fuse-terminate")
        self.terminated.set()
        self.stop_main.set()

    def patches(self, *, real_control: bool = False) -> ExitStack:
        stack = ExitStack()
        replacements = {
            "ImmichClient": self.Client,
            "Catalog": self.Catalog,
            "ContentCache": self.Cache,
            "Library": self.Library,
            "ImmichFilesystem": self.Filesystem,
            "UploadQueue": self.Queue,
            "load_api_key": self.key,
            "refresh_catalog": self.refresh,
            "refresh_catalog_incremental": self.incremental_refresh,
            "populate_previews": self.previews,
            "_upload_worker": self.upload_worker,
            "state_path": lambda: self.root / "state",
            "data_path": lambda: self.root / "data",
            "cache_path": lambda: self.root / "cache",
            "runtime_path": lambda: self.root / "runtime",
        }
        if not real_control:
            replacements["serve_control"] = self.control
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
    def test_upload_candidate_must_be_visible_in_the_upload_library(self) -> None:
        content = b"queued original"
        uploaded = Asset(
            ASSET_ID,
            OWNER_ID,
            "photo.jpg",
            "image/jpeg",
            len(content),
            1,
            2,
            "2026-08-26T00:00:00Z",
            base64.b64encode(
                hashlib.sha1(content, usedforsecurity=False).digest()
            ).decode(),
            "timeline",
            False,
            False,
            None,
        )
        job = SimpleNamespace(
            id=PINNED_ID,
            owner_id=OWNER_ID,
            size=len(content),
            sha1=hashlib.sha1(content, usedforsecurity=False).hexdigest(),
        )

        self.assertTrue(_upload_matches(job, uploaded, PINNED_ID))  # type: ignore[arg-type]
        for unsafe in (
            replace(uploaded, is_trashed=True),
            replace(uploaded, is_offline=True),
            replace(uploaded, visibility="hidden"),
            replace(uploaded, library_id=ASSET_ID),
        ):
            self.assertFalse(_upload_matches(job, unsafe, PINNED_ID))  # type: ignore[arg-type]

    def test_pending_upload_resumes_candidate_verification_without_reposting(self) -> None:
        content = b"queued original"
        checksum = base64.b64encode(
            hashlib.sha1(content, usedforsecurity=False).digest()
        ).decode()
        uploaded = Asset(
            ASSET_ID,
            OWNER_ID,
            "photo.jpg",
            "image/jpeg",
            len(content),
            1,
            2,
            "2026-08-26T00:00:00Z",
            checksum,
            "timeline",
            False,
            False,
            None,
        )

        class Mutation:
            def __init__(self) -> None:
                self.calls: list[tuple[int, str, frozenset[str], str]] = []

            async def upload(
                self,
                descriptor: int,
                name: str,
                media_types: frozenset[str],
                upload_id: str,
            ) -> UploadResult:
                self.calls.append((descriptor, name, media_types, upload_id))
                return UploadResult(ASSET_ID, True)

        class ReadClient:
            def __init__(self) -> None:
                self.fail_once = True
                self.error: Exception | None = None

            async def asset(self, asset_id: str) -> Asset:
                self.assert_id = asset_id
                if self.error is not None:
                    raise self.error
                if self.fail_once:
                    self.fail_once = False
                    raise ImmichUnavailableError("offline")
                return uploaded

            async def asset_metadata(self, asset_id: str) -> str:
                self.assert_metadata_id = asset_id
                return job.id

        class Mounted:
            def __init__(self, mutation: Mutation) -> None:
                self.mutation = mutation
                self.mutation_enabled = True

            def upload_access(self):
                return self.mutation, ServerSession(
                    OWNER_ID, "3.0.3", frozenset({".jpg"}), True
                )

        async def scenario(root: Path) -> None:
            nonlocal job
            mutation = Mutation()
            read_client = ReadClient()
            callbacks: list[CatalogAsset] = []
            with (
                Catalog(root / "state" / "catalog.db") as catalog,
                UploadQueue(root / "data" / "uploads") as queue,
            ):
                draft = queue.begin("photo.jpg", "https://photos.example.test", OWNER_ID)
                queue.write(draft, 0, content)
                job = queue.seal(draft)
                os.close(draft.descriptor)

                async def published(entry: CatalogAsset) -> None:
                    callbacks.append(entry)

                class ReadOnly:
                    mutation_enabled = False

                await _process_upload(
                    queue,
                    catalog,
                    trio.Lock(),
                    ReadOnly(),  # type: ignore[arg-type]
                    read_client,  # type: ignore[arg-type]
                    Settings("https://photos.example.test", root / "mount"),
                    job,
                    published,
                )
                self.assertEqual(queue.status(job.id), job)
                self.assertEqual(mutation.calls, [])

                await _process_upload(
                    queue,
                    catalog,
                    trio.Lock(),
                    Mounted(mutation),  # type: ignore[arg-type]
                    read_client,  # type: ignore[arg-type]
                    Settings("https://photos.example.test", root / "mount"),
                    job,
                    published,
                )

                waiting = queue.status(job.id)
                assert waiting is not None
                self.assertEqual(waiting.state, UploadState.ATTEMPTING)
                self.assertEqual(waiting.candidate_asset_id, ASSET_ID)
                self.assertEqual(waiting.error, UploadErrorCode.UPLOAD_UNAVAILABLE)
                self.assertIsNone(catalog.by_id(ASSET_ID))

                waiting = queue.retry(job.id, at_ns=0)
                await _process_upload(
                    queue,
                    catalog,
                    trio.Lock(),
                    Mounted(mutation),  # type: ignore[arg-type]
                    read_client,  # type: ignore[arg-type]
                    Settings("https://photos.example.test", root / "mount"),
                    waiting,
                    published,
                )

                self.assertIsNone(queue.status(job.id))
                entry = catalog.by_id(ASSET_ID)
                assert entry is not None
                self.assertEqual((entry.name, entry.asset), ("photo.jpg", uploaded))
                self.assertEqual(callbacks, [entry])
                self.assertEqual(
                    [call[1:] for call in mutation.calls],
                    [("photo.jpg", frozenset({".jpg"}), job.id)],
                )

                draft = queue.begin(
                    "broken.jpg", "https://photos.example.test", OWNER_ID
                )
                queue.write(draft, 0, content)
                broken = queue.seal(draft)
                os.close(draft.descriptor)
                read_client.error = ValueError("malformed remote state")
                await _process_upload(
                    queue,
                    catalog,
                    trio.Lock(),
                    Mounted(mutation),  # type: ignore[arg-type]
                    read_client,  # type: ignore[arg-type]
                    Settings("https://photos.example.test", root / "mount"),
                    broken,
                    published,
                )
                blocked = queue.status(broken.id)
                assert blocked is not None
                self.assertEqual(blocked.state, UploadState.BLOCKED)
                self.assertEqual(blocked.error, UploadErrorCode.LOCAL_STATE_FAILED)

        job = None  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

    def test_matching_trust_mounts_offline_without_remote_or_eviction_work(self) -> None:
        async def scenario(root: Path) -> ServiceFakes:
            fakes = ServiceFakes(
                root,
                block_preview=False,
                read_validation_error=ImmichUnavailableError("unreachable"),
                trusted_profile=trusted_profile(),
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
                    status = fakes.handlers["status"]
                    self.assertEqual(
                        await status({}),  # type: ignore[operator]
                        {
                            "total": 7,
                            "visible": 6,
                            "missing_size": 1,
                            "trashed": 0,
                            "hidden": 0,
                            "offline": 0,
                            "online": False,
                            "mutation_enabled": False,
                            "pending_uploads": 0,
                            "upload_quarantined": 0,
                        },
                    )
                    self.assertNotIn("refresh", fakes.events)
                    self.assertNotIn("fetch", fakes.events)
                    self.assertFalse(any(event.startswith("evict:") for event in fakes.events))
                    self.assertIn("offline-suppress", fakes.events)
                    assert fakes.cache is not None
                    self.assertFalse(fakes.cache.downloads_enabled)
                    restore = fakes.handlers["restore"]
                    with self.assertRaisesRegex(PermissionError, "mutations are disabled"):
                        await restore({"asset": ASSET_ID})  # type: ignore[operator]
                    await trio.lowlevel.checkpoint()
                    fakes.stop_main.set()
            return fakes

        with tempfile.TemporaryDirectory() as directory:
            fakes = trio.run(scenario, Path(directory))

        self.assertEqual(
            [event for event in fakes.events if event.startswith("validate:")],
            ["validate:read"],
        )
        self.assertNotIn("validate:mutation", fakes.events)

    def test_offline_refresh_promotes_only_after_validation_and_full_refresh(self) -> None:
        async def scenario(root: Path) -> ServiceFakes:
            fakes = ServiceFakes(
                root,
                block_preview=False,
                read_validation_error=ImmichUnavailableError("unreachable"),
                trusted_profile=trusted_profile(),
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
                    fakes.read_validation_error = None
                    refresh = fakes.handlers["refresh"]
                    self.assertEqual(
                        await refresh({}),  # type: ignore[operator]
                        {"scheduled": True},
                    )
                    with trio.fail_after(0.2):
                        await fakes.promoted.wait()
                    status = fakes.handlers["status"]
                    self.assertTrue((await status({}))["online"])  # type: ignore[operator]
                    assert fakes.cache is not None
                    self.assertTrue(fakes.cache.downloads_enabled)
                    fakes.stop_main.set()
            return fakes

        with tempfile.TemporaryDirectory() as directory:
            fakes = trio.run(scenario, Path(directory))

        validation = len(fakes.events) - 1 - fakes.events[::-1].index("validate:read")
        refresh = fakes.events.index("refresh")
        downloads = fakes.events.index("downloads-enabled")
        mutations = fakes.events.index("mutations-enabled")
        self.assertLess(validation, refresh)
        self.assertLess(refresh, downloads)
        self.assertLess(downloads, mutations)
        self.assertIn("close:mutation", fakes.events)

    def test_offline_fallback_rejects_missing_trust_and_authoritative_errors(self) -> None:
        async def scenario(
            root: Path,
            error: Exception,
            with_trust: bool,
            expected: type[Exception],
        ) -> ServiceFakes:
            fakes = ServiceFakes(
                root,
                block_preview=False,
                read_validation_error=error,
                trusted_profile=trusted_profile() if with_trust else None,
            )
            settings = Settings("https://photos.example.test", root / "mount")
            with fakes.patches(), self.assertRaises(expected):
                await run_service(settings)
            return fakes

        with tempfile.TemporaryDirectory() as directory:
            missing = trio.run(
                scenario,
                Path(directory) / "missing",
                ImmichUnavailableError("unreachable"),
                False,
                RuntimeError,
            )
            authoritative = trio.run(
                scenario,
                Path(directory) / "authoritative",
                ImmichResponseError("invalid response"),
                True,
                ImmichResponseError,
            )

        self.assertNotIn("fuse-init", missing.events + authoritative.events)

    def test_restore_worker_does_not_retain_failed_jobs(self) -> None:
        async def scenario() -> None:
            class Library:
                async def remote_restore(self, asset_id: str) -> None:
                    raise RuntimeError("restore failed")

            notifications, received = trio.open_memory_channel[bool](1)
            refreshes, refreshed = trio.open_memory_channel[bool](1)
            jobs = {
                asset_id: _RestoreJob(asset_id)
                for asset_id in (
                    ASSET_ID,
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                )
            }
            pending = list(jobs.values())
            async with notifications, received, refreshes, refreshed:
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(
                        _restore_worker,
                        Library(),
                        received,
                        pending,
                        jobs,
                        refreshes,
                    )
                    await notifications.send(True)
                    for job in tuple(jobs.values()):
                        await job.done.wait()
                    self.assertEqual(jobs, {})
                    nursery.cancel_scope.cancel()

        with self.assertLogs("immich_on_demand.service", level="WARNING"):
            trio.run(scenario)

    def test_restore_control_schedules_repair_and_sanitizes_failure(self) -> None:
        async def run_case(root: Path, error: Exception | None) -> ServiceFakes:
            fakes = ServiceFakes(
                root,
                block_preview=False,
                restore_error=error,
            )
            settings = Settings(
                "https://photos.example.test",
                root / "mount",
                refresh_seconds=3600,
            )
            with fakes.patches(real_control=True):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    socket = root / "runtime" / "control.sock"
                    if error is None:
                        self.assertEqual(
                            await send_request(
                                socket,
                                1,
                                "restore",
                                {"asset": ASSET_ID},
                            ),
                            {"restored": True, "scheduled": True},
                        )
                        with trio.fail_after(0.2):
                            await fakes.incremental_done.wait()
                    else:
                        with self.assertRaisesRegex(
                            ControlError, "^request failed$"
                        ):
                            await send_request(
                                socket,
                                1,
                                "restore",
                                {"asset": ASSET_ID},
                            )
                        self.assertEqual(
                            (await send_request(socket, 2, "status", {}))["total"],
                            7,
                        )
                        with trio.fail_after(0.2):
                            await fakes.incremental_done.wait()
                    fakes.stop_main.set()
            return fakes

        with tempfile.TemporaryDirectory() as directory:
            success = trio.run(run_case, Path(directory) / "success", None)
            with self.assertLogs(
                "immich_on_demand.service", level="WARNING"
            ) as logs:
                failure = trio.run(
                    run_case,
                    Path(directory) / "failure",
                    RuntimeError("api-key=do-not-display"),
                )

        self.assertEqual(success.restored_ids, [ASSET_ID])
        self.assertEqual(failure.restored_ids, [])
        self.assertNotIn("api-key", "\n".join(logs.output))
        self.assertNotIn("fuse-terminate", success.events + failure.events)

    def test_restore_survives_the_request_timeout_and_repairs_catalog(self) -> None:
        async def scenario(root: Path) -> ServiceFakes:
            fakes = ServiceFakes(
                root,
                block_preview=False,
                block_restore=True,
            )
            settings = Settings(
                "https://photos.example.test",
                root / "mount",
                refresh_seconds=3600,
            )
            with fakes.patches(real_control=True):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    socket = root / "runtime" / "control.sock"
                    with self.assertRaisesRegex(
                        ControlError, "^control request timed out$"
                    ):
                        await send_request(
                            socket,
                            1,
                            "restore",
                            {"asset": ASSET_ID},
                            timeout=0.05,
                        )
                    await fakes.restore_started.wait()
                    self.assertEqual(
                        (await send_request(socket, 2, "status", {}))["total"],
                        7,
                    )
                    fakes.restore_gate.set()
                    with trio.fail_after(0.2):
                        await fakes.incremental_done.wait()
                    self.assertEqual(
                        await send_request(
                            socket,
                            3,
                            "restore",
                            {"asset": ASSET_ID},
                        ),
                        {"restored": True, "scheduled": True},
                    )
                    fakes.stop_main.set()
            return fakes

        with tempfile.TemporaryDirectory() as directory:
            fakes = trio.run(scenario, Path(directory))

        self.assertEqual(fakes.restored_ids, [ASSET_ID])
        self.assertEqual(fakes.restore_attempts, [ASSET_ID])
        self.assertIn("fuse-close:True", fakes.events)
        self.assertNotIn("fuse-terminate", fakes.events)

    def test_failed_restore_is_retried_after_the_server_handler_times_out(self) -> None:
        async def short_control(
            path: Path,
            handlers: object,
            *,
            task_status: trio.TaskStatus[object] = trio.TASK_STATUS_IGNORED,
        ) -> None:
            await serve_control(
                path,
                handlers,
                timeout=0.05,
                task_status=task_status,
            )

        async def scenario(root: Path) -> ServiceFakes:
            fakes = ServiceFakes(
                root,
                block_preview=False,
                block_restore=True,
                restore_error=RuntimeError("api-key=do-not-display"),
            )
            settings = Settings(
                "https://photos.example.test",
                root / "mount",
                refresh_seconds=3600,
            )
            with fakes.patches(real_control=True), patch(
                "immich_on_demand.service.serve_control", short_control
            ):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    socket = root / "runtime" / "control.sock"
                    with self.assertRaisesRegex(ControlError, "^request timed out$"):
                        await send_request(
                            socket,
                            1,
                            "restore",
                            {"asset": ASSET_ID},
                        )
                    fakes.restore_gate.set()
                    with trio.fail_after(0.2):
                        await fakes.incremental_done.wait()
                    fakes.restore_error = None
                    self.assertEqual(
                        await send_request(
                            socket,
                            2,
                            "restore",
                            {"asset": ASSET_ID},
                        ),
                        {"restored": True, "scheduled": True},
                    )
                    fakes.stop_main.set()
            return fakes

        with tempfile.TemporaryDirectory() as directory, self.assertLogs(
            "immich_on_demand.service", level="WARNING"
        ) as logs:
            fakes = trio.run(scenario, Path(directory))

        self.assertEqual(fakes.restore_attempts, [ASSET_ID, ASSET_ID])
        self.assertEqual(fakes.restored_ids, [ASSET_ID])
        self.assertNotIn("api-key", "\n".join(logs.output))

    def test_failed_pinned_hydration_keeps_the_worker_retryable(self) -> None:
        async def scenario() -> None:
            entry = upload_entry()
            failed = trio.Event()
            hydrated = trio.Event()
            pending = {entry.asset.id: entry}
            inflight: set[str] = set()

            class Catalog:
                def pinned_ids(self) -> frozenset[str]:
                    return frozenset({entry.asset.id})

            class Cache:
                calls = 0

                async def hydrate(self, _asset: Asset) -> Path:
                    self.calls += 1
                    if self.calls == 1:
                        failed.set()
                        raise OSError("protected path must not reach the log")
                    hydrated.set()
                    return Path("/unused")

            cache = Cache()
            sends, receives = trio.open_memory_channel[bool](1)
            with self.assertLogs("immich_on_demand.service", level="WARNING") as logs:
                async with sends, receives, trio.open_nursery() as nursery:
                    nursery.start_soon(
                        _pin_worker,
                        Catalog(),
                        cache,  # type: ignore[arg-type]
                        receives,
                        pending,
                        inflight,
                    )
                    await sends.send(True)
                    await failed.wait()
                    pending[entry.asset.id] = entry
                    try:
                        sends.send_nowait(True)
                    except trio.WouldBlock:
                        pass
                    await hydrated.wait()
                    await sends.aclose()

            self.assertEqual(cache.calls, 2)
            self.assertEqual(pending, {})
            self.assertEqual(inflight, set())
            self.assertNotIn("protected path", "\n".join(logs.output))

        trio.run(scenario)

    def test_pin_persistence_failure_leaves_the_mounted_service_running(self) -> None:
        async def scenario(root: Path) -> None:
            fakes = ServiceFakes(root, pin_persist_error=True)
            settings = Settings(
                "https://photos.example.test", root / "mount", refresh_seconds=3600
            )
            with fakes.patches():
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(run_service, settings)
                    await fakes.main_started.wait()
                    pin = fakes.handlers["pin"]
                    with self.assertRaisesRegex(OSError, "private catalog"):
                        await pin(  # type: ignore[operator]
                            {
                                "uri": (root / "mount" / "new.jpg").as_uri(),
                                "pinned": True,
                            }
                        )
                    status = fakes.handlers["status"]
                    self.assertEqual((await status({}))["total"], 7)  # type: ignore[index,operator]
                    assert fakes.cache is not None
                    self.assertNotIn(ASSET_ID, fakes.cache.pinned_ids)
                    fakes.stop_main.set()

            self.assertNotIn("fuse-terminate", fakes.events)

        with tempfile.TemporaryDirectory() as directory:
            trio.run(scenario, Path(directory))

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
                        [True],
                    )
            self.assertEqual(sleeps, [FULL_REFRESH_SECONDS, FULL_REFRESH_SECONDS])
            self.assertTrue(full_requested[0])
            self.assertTrue(refreshes.receive_nowait())

        trio.run(scenario)

    def test_periodic_refresh_is_silent_while_offline(self) -> None:
        async def scenario() -> None:
            calls = 0

            async def stop_after_two_sleeps(seconds: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("stop")

            requests, refreshes = trio.open_memory_channel[bool](1)
            full_requested = [False]
            with patch("immich_on_demand.service.trio.sleep", stop_after_two_sleeps):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    await _periodic_refresh(
                        requests,
                        1,
                        True,
                        full_requested,
                        [False],
                    )
            self.assertFalse(full_requested[0])
            with self.assertRaises(trio.WouldBlock):
                refreshes.receive_nowait()

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
                    describe = fakes.handlers["describe"]
                    pin = fakes.handlers["pin"]
                    restore = fakes.handlers["restore"]
                    uploads = fakes.handlers["uploads"]
                    retry_upload = fakes.handlers["retry-upload"]
                    cancel_upload = fakes.handlers["cancel-upload"]
                    self.assertEqual(
                        await status({}),  # type: ignore[operator]
                        {
                            "total": 7,
                            "visible": 6,
                            "missing_size": 1,
                            "trashed": 0,
                            "hidden": 0,
                            "offline": 0,
                            "online": True,
                            "mutation_enabled": True,
                            "pending_uploads": 0,
                            "upload_quarantined": 0,
                        },
                    )
                    assert fakes.upload_queue is not None
                    fakes.upload_queue.jobs = [
                        SimpleNamespace(
                            id=PINNED_ID,
                            requested_name="foreign.jpg",
                            server_origin=settings.server_origin,
                            owner_id=PINNED_ID,
                            state=UploadState.PENDING,
                            size=1,
                            error=None,
                            revision=2,
                        ),
                        SimpleNamespace(
                            id=ASSET_ID,
                            requested_name="retry.jpg",
                            server_origin=settings.server_origin,
                            owner_id=OWNER_ID,
                            state=UploadState.BLOCKED,
                            size=2,
                            error=UploadErrorCode.UPLOAD_UNAVAILABLE,
                            revision=3,
                        ),
                        SimpleNamespace(
                            id=OTHER_UPLOAD_ID,
                            requested_name="cancel.jpg",
                            server_origin=settings.server_origin,
                            owner_id=OWNER_ID,
                            state=UploadState.PENDING,
                            size=3,
                            error=None,
                            revision=4,
                        ),
                    ]
                    self.assertEqual((await status({}))["pending_uploads"], 2)  # type: ignore[index,operator]
                    first_page = await uploads({"after": None, "limit": 1})  # type: ignore[operator]
                    self.assertEqual(
                        [item["id"] for item in first_page["items"]],  # type: ignore[index]
                        [ASSET_ID],
                    )
                    self.assertEqual(first_page["next"], ASSET_ID)  # type: ignore[index]
                    self.assertEqual(
                        await retry_upload({"id": ASSET_ID}),  # type: ignore[operator]
                        {"id": ASSET_ID, "scheduled": True},
                    )
                    with self.assertRaises(PermissionError):
                        await cancel_upload(  # type: ignore[operator]
                            {
                                "id": PINNED_ID,
                                "revision": 2,
                                "confirm_name": "foreign.jpg",
                            }
                        )
                    self.assertEqual(
                        await cancel_upload(  # type: ignore[operator]
                            {
                                "id": OTHER_UPLOAD_ID,
                                "revision": 4,
                                "confirm_name": "cancel.jpg",
                            }
                        ),
                        {"id": OTHER_UPLOAD_ID, "cancelled": True},
                    )
                    self.assertEqual(fakes.upload_retries, [ASSET_ID])
                    self.assertEqual(
                        fakes.upload_cancellations,
                        [(OTHER_UPLOAD_ID, "cancel.jpg", 4)],
                    )
                    with trio.fail_after(0.1):
                        self.assertEqual(await refresh({}), {"scheduled": True})  # type: ignore[operator]
                        self.assertEqual(await refresh({}), {"scheduled": True})  # type: ignore[operator]
                    self.assertEqual(await evict({}), {"evicted": 1})  # type: ignore[operator]
                    self.assertEqual(
                        await evict({"asset": ASSET_ID}), {"evicted": True}  # type: ignore[operator]
                    )
                    self.assertEqual(
                        await evict(
                            {"uri": (root / "mount" / "new.jpg").as_uri()}
                        ),
                        {"evicted": True},
                    )  # type: ignore[operator]
                    with self.assertRaises(ValueError):
                        await evict({"asset": "not-a-uuid"})  # type: ignore[operator]
                    for uri in (
                        (root / "outside.jpg").as_uri(),
                        "https://photos.example.test/new.jpg",
                        (root / "mount" / "folder" / "new.jpg").as_uri(),
                    ):
                        with self.subTest(uri=uri), self.assertRaises(ValueError):
                            await evict({"uri": uri})  # type: ignore[operator]

                    uri = (root / "mount" / "new.jpg").as_uri()
                    unknown_uri = (root / "mount" / "unknown.jpg").as_uri()
                    self.assertEqual(
                        await describe({"uris": [uri, unknown_uri]}),
                        {
                            "items": [
                                {
                                    "uri": uri,
                                    "cached": True,
                                    "busy": False,
                                    "pinned": False,
                                    "recoverable": False,
                                }
                            ]
                        },
                    )  # type: ignore[operator]
                    self.assertEqual(
                        await pin({"asset": PINNED_ID, "pinned": False}),
                        {
                            "pinned": False,
                            "cached": False,
                            "busy": False,
                            "scheduled": False,
                        },
                    )  # type: ignore[operator]
                    self.assertNotIn(PINNED_ID, fakes.persisted_pins)
                    self.assertEqual(
                        await pin({"uri": uri, "pinned": True}),
                        {
                            "pinned": True,
                            "cached": True,
                            "busy": False,
                            "scheduled": True,
                        },
                    )  # type: ignore[operator]
                    await fakes.pin_hydrated.wait()
                    self.assertIn(ASSET_ID, fakes.persisted_pins)
                    self.assertEqual(
                        await pin({"asset": ASSET_ID, "pinned": False}),
                        {
                            "pinned": False,
                            "cached": True,
                            "busy": False,
                            "scheduled": False,
                        },
                    )  # type: ignore[operator]
                    self.assertNotIn(ASSET_ID, fakes.persisted_pins)
                    self.assertEqual(
                        await restore({"asset": ASSET_ID}),
                        {"restored": True, "scheduled": True},
                    )  # type: ignore[operator]
                    self.assertEqual(fakes.restored_ids, [ASSET_ID])
                    for invalid in (
                        {},
                        {"asset": "not-a-uuid"},
                        {"asset": ASSET_ID.upper()},
                        {"asset": ASSET_ID, "extra": True},
                    ):
                        with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                            await restore(invalid)  # type: ignore[operator]
                    self.assertEqual(fakes.restored_ids, [ASSET_ID])
                    for params in (
                        {},
                        {"uris": []},
                        {"uris": [uri] * 65},
                        {"uris": [(root / "outside.jpg").as_uri()]},
                    ):
                        with self.subTest(params=params), self.assertRaises(ValueError):
                            await describe(params)  # type: ignore[operator]

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
            self.assertEqual(fakes.cache.asset_evictions, [ASSET_ID, ASSET_ID])
            self.assertEqual(fakes.cache.pinned_ids, set())
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
