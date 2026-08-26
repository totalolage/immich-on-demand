from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from immich_on_demand.uploads import (
    UploadErrorCode,
    UploadOperation,
    UploadQueue,
    UploadQueueError,
    UploadState,
    UploadStateError,
)


OWNER_ID = "87654321-4321-4321-8321-cba987654321"
ASSET_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ASSET_ID = "23456789-2345-4345-8345-23456789abcd"
LIBRARY_ID = "3456789a-3456-4456-8456-3456789abcde"
ALBUM_ID = "456789ab-4567-4567-8567-456789abcdef"
OTHER_ALBUM_ID = "56789abc-5678-4678-8678-56789abcdef0"
ORIGIN = "https://photos.example.test"

REPLACEMENT = {
    "old_asset_id": ASSET_ID,
    "old_inode": 42,
    "old_name": "photo.jpg",
    "source_owner_id": OWNER_ID,
    "source_library_id": LIBRARY_ID,
    "source_checksum": "aGVsbG8=",
    "source_updated_at": "2026-08-25T12:30:00.000Z",
    "source_created_ns": 1_777_777_777_000_000_000,
    "source_is_favorite": True,
    "source_visibility": "timeline",
    "source_album_ids": (ALBUM_ID, OTHER_ALBUM_ID),
}


class UploadQueueTest(unittest.TestCase):
    def test_second_owner_is_rejected_without_touching_a_live_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"still writing")
                with self.assertRaisesRegex(
                    UploadQueueError, "^upload queue is already in use$"
                ):
                    UploadQueue(root)
                queue.write(draft, len(b"still writing"), b" safely")
                self.assertEqual(
                    os.pread(draft.descriptor, 64, 0), b"still writing safely"
                )
                os.close(draft.descriptor)

    def test_cancel_cannot_race_attempt_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"content")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                entered = threading.Event()
                proceed = threading.Event()
                original = queue._verify_sealed_payload
                result: list[object] = []
                cancellation: list[object] = []

                def pause(*args: object) -> None:
                    entered.set()
                    proceed.wait()
                    original(*args)  # type: ignore[arg-type]

                def attempt() -> None:
                    try:
                        result.append(queue.begin_attempt(pending.id))
                    except BaseException as error:
                        result.append(error)

                def cancel() -> None:
                    try:
                        queue.cancel(
                            pending.id,
                            requested_name=pending.requested_name,
                            revision=pending.revision,
                        )
                    except BaseException as error:
                        cancellation.append(error)

                with patch.object(queue, "_verify_sealed_payload", pause):
                    thread = threading.Thread(target=attempt)
                    thread.start()
                    self.assertTrue(entered.wait(1))
                    cancelling = threading.Thread(target=cancel)
                    cancelling.start()
                    cancelling.join(1)
                    self.assertEqual(len(cancellation), 1)
                    self.assertEqual(queue.list()[0].state, UploadState.ATTEMPTING)
                    proceed.set()
                    thread.join(1)

                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], type(pending))
                self.assertIsInstance(cancellation[0], (ValueError, UploadStateError))
                self.assertEqual(queue.status(pending.id).state, UploadState.ATTEMPTING)  # type: ignore[union-attr]

    def test_sealed_upload_survives_restart_with_frozen_payload_metadata(self) -> None:
        content = b"queued original"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                self.assertEqual(queue.write(draft, 0, content), len(content))
                queue.sync(draft)
                job = queue.seal(draft)
                os.close(draft.descriptor)

            with UploadQueue(root) as queue:
                restored = queue.status(job.id)

            assert restored is not None
            self.assertEqual(restored.state, UploadState.PENDING)
            self.assertEqual(restored.requested_name, "photo.jpg")
            self.assertEqual(restored.server_origin, ORIGIN)
            self.assertEqual(restored.owner_id, OWNER_ID)
            self.assertEqual(restored.size, len(content))
            self.assertEqual(restored.sha1, hashlib.sha1(content).hexdigest())
            self.assertIsInstance(restored.created_ns, int)
            self.assertIsInstance(restored.modified_ns, int)
            self.assertEqual(restored.payload_path.read_bytes(), content)
            self.assertGreater(restored.revision, draft.revision)

    def test_writing_replacement_keeps_its_draft_and_fingerprint_across_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("upload.tmp", ORIGIN, OWNER_ID)
                marked = queue.mark_replacement(
                    draft.id,
                    revision=draft.revision,
                    **REPLACEMENT,
                )

                self.assertEqual(marked.state, UploadState.WRITING)
                self.assertEqual(marked.requested_name, "photo.jpg")
                self.assertEqual(marked.operation, UploadOperation.REPLACEMENT)
                self.assertEqual(marked.revision, draft.revision)
                self.assertEqual(marked.old_asset_id, ASSET_ID)
                self.assertEqual(marked.old_inode, 42)
                self.assertEqual(marked.old_name, "photo.jpg")
                self.assertEqual(marked.source_owner_id, OWNER_ID)
                self.assertEqual(marked.source_library_id, LIBRARY_ID)
                self.assertEqual(marked.source_checksum, "aGVsbG8=")
                self.assertEqual(
                    marked.source_updated_at,
                    "2026-08-25T12:30:00.000Z",
                )
                self.assertEqual(
                    marked.source_created_ns,
                    1_777_777_777_000_000_000,
                )
                self.assertIs(marked.source_is_favorite, True)
                self.assertEqual(marked.source_visibility, "timeline")
                self.assertEqual(
                    marked.source_album_ids,
                    (ALBUM_ID, OTHER_ALBUM_ID),
                )

                queue.write(draft, 0, b"replacement bytes")
                pending = queue.seal(draft)
                os.close(draft.descriptor)

            with UploadQueue(root) as queue:
                restored = queue.status(pending.id)

            self.assertEqual(restored, pending)
            assert restored is not None
            self.assertEqual(restored.operation, UploadOperation.REPLACEMENT)
            self.assertEqual(restored.source_album_ids, (ALBUM_ID, OTHER_ALBUM_ID))
            self.assertEqual(restored.payload_path.read_bytes(), b"replacement bytes")

    def test_pending_job_can_be_marked_as_replacement_only_before_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("upload.tmp", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"replacement bytes")
                pending = queue.seal(draft)
                os.close(draft.descriptor)

                self.assertEqual(pending.operation, UploadOperation.ORDINARY)
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^upload changed before replacement$",
                ):
                    queue.mark_replacement(
                        pending.id,
                        revision=pending.revision - 1,
                        **REPLACEMENT,
                    )
                self.assertEqual(queue.status(pending.id), pending)

                marked = queue.mark_replacement(
                    pending.id,
                    revision=pending.revision,
                    **REPLACEMENT,
                )
                self.assertEqual(marked.revision, pending.revision + 1)
                self.assertEqual(marked.requested_name, "photo.jpg")
                self.assertEqual(marked.operation, UploadOperation.REPLACEMENT)
                self.assertEqual(
                    tuple(job.requested_name for job in queue.list()),
                    ("photo.jpg",),
                )
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^upload changed before attempt$",
                ):
                    queue.open_attempt(marked.id, revision=pending.revision)
                self.assertEqual(queue.status(marked.id), marked)
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^upload cannot become a replacement$",
                ):
                    queue.mark_replacement(
                        marked.id,
                        revision=marked.revision,
                        **REPLACEMENT,
                    )

                attempting = queue.begin_attempt(marked.id)
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^upload cannot become a replacement$",
                ):
                    queue.mark_replacement(
                        attempting.id,
                        revision=attempting.revision,
                        **REPLACEMENT,
                    )

    def test_replacing_phase_retains_candidate_and_resumes_after_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"replacement bytes")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                marked = queue.mark_replacement(
                    pending.id,
                    revision=pending.revision,
                    **REPLACEMENT,
                )
                attempting = queue.begin_attempt(marked.id)
                candidate = queue.record_candidate(attempting.id, OTHER_ASSET_ID)
                self.assertIs(candidate.candidate_verified, False)
                deferred = queue.retry(
                    candidate.id,
                    at_ns=0,
                    error=UploadErrorCode.UPLOAD_UNAVAILABLE,
                )
                self.assertEqual(deferred.state, UploadState.ATTEMPTING)
                self.assertEqual(deferred.candidate_asset_id, OTHER_ASSET_ID)
                candidate = queue.begin_attempt(deferred.id)

                with self.assertRaisesRegex(
                    UploadStateError,
                    "^replacement has not entered replacing$",
                ):
                    queue.commit(candidate.id)
                replacing = queue.begin_replacing(candidate.id)
                self.assertEqual(replacing.state, UploadState.REPLACING)
                self.assertIs(replacing.candidate_verified, True)
                self.assertEqual(replacing.candidate_asset_id, OTHER_ASSET_ID)
                self.assertEqual(replacing.old_asset_id, ASSET_ID)

            with UploadQueue(root) as queue:
                restored = queue.status(replacing.id)
                self.assertEqual(restored, replacing)
                self.assertEqual(queue.next_due(), replacing)
                blocked = queue.block(
                    replacing.id,
                    UploadErrorCode.LOCAL_STATE_FAILED,
                )
                retried = queue.retry(blocked.id, at_ns=0)
                self.assertEqual(retried.state, UploadState.REPLACING)
                self.assertEqual(retried.candidate_asset_id, OTHER_ASSET_ID)
                self.assertEqual(retried.source_album_ids, (ALBUM_ID, OTHER_ALBUM_ID))
                committed = queue.commit(retried.id)
                self.assertEqual(committed.state, UploadState.COMMITTED)
                queue.remove(committed.id)
                self.assertIsNone(queue.status(committed.id))

    def test_legacy_v1_manifest_loads_as_an_ordinary_upload(self) -> None:
        replacement_fields = {
            "candidate_verified",
            "operation",
            "old_asset_id",
            "old_inode",
            "old_name",
            "source_owner_id",
            "source_library_id",
            "source_checksum",
            "source_updated_at",
            "source_created_ns",
            "source_is_favorite",
            "source_visibility",
            "source_album_ids",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"ordinary bytes")
                pending = queue.seal(draft)
                os.close(draft.descriptor)

            manifest_path = pending.payload_path.parent / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            for field in replacement_fields:
                manifest.pop(field)
            manifest["format_version"] = 1
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            )

            with UploadQueue(root) as queue:
                restored = queue.status(pending.id)
                assert restored is not None
                self.assertEqual(restored.operation, UploadOperation.ORDINARY)
                self.assertIsNone(restored.old_asset_id)
                attempting = queue.begin_attempt(restored.id)

            rewritten = json.loads(manifest_path.read_text())
            self.assertEqual(rewritten["format_version"], 2)
            self.assertEqual(rewritten["operation"], "ordinary")
            self.assertEqual(attempting.state, UploadState.ATTEMPTING)

    def test_invalid_replacement_fingerprint_leaves_job_ordinary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"ordinary bytes")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                cases = (
                    (
                        "source_album_ids",
                        (ALBUM_ID, ALBUM_ID),
                        "replacement album IDs must be sorted and unique",
                    ),
                    (
                        "source_created_ns",
                        -1,
                        "replacement source creation time is invalid",
                    ),
                    (
                        "source_created_ns",
                        True,
                        "replacement source creation time is invalid",
                    ),
                )
                for field, value, message in cases:
                    with self.subTest(field=field, value=value):
                        invalid = dict(REPLACEMENT)
                        invalid[field] = value
                        with self.assertRaisesRegex(ValueError, f"^{message}$"):
                            queue.mark_replacement(
                                pending.id,
                                revision=pending.revision,
                                **invalid,
                            )
                self.assertEqual(queue.status(pending.id), pending)

    def test_open_local_reads_every_sealed_live_state_without_mutation(self) -> None:
        def assert_read_only(
            queue: UploadQueue,
            job_id: str,
            expected: bytes,
        ) -> None:
            before = queue.status(job_id)
            assert before is not None
            descriptor = queue.open_local(job_id)
            try:
                self.assertEqual(os.read(descriptor, len(expected) + 1), expected)
                self.assertEqual(
                    fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE,
                    os.O_RDONLY,
                )
                self.assertTrue(
                    fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
                )
                self.assertEqual(
                    os.fstat(descriptor).st_ino,
                    before.payload_path.stat().st_ino,
                )
            finally:
                os.close(descriptor)
            self.assertEqual(queue.status(job_id), before)

        content = b"replacement bytes"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("upload.tmp", ORIGIN, OWNER_ID)
                queue.write(draft, 0, content)
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                pending = queue.mark_replacement(
                    pending.id,
                    revision=pending.revision,
                    **REPLACEMENT,
                )
                assert_read_only(queue, pending.id, content)

                blocked = queue.block(
                    pending.id,
                    UploadErrorCode.UPLOAD_UNAVAILABLE,
                )
                assert_read_only(queue, blocked.id, content)

                pending = queue.retry(blocked.id, at_ns=0)
                attempting = queue.begin_attempt(pending.id)
                assert_read_only(queue, attempting.id, content)

                candidate = queue.record_candidate(attempting.id, OTHER_ASSET_ID)
                replacing = queue.begin_replacing(candidate.id)
                assert_read_only(queue, replacing.id, content)

    def test_open_local_rejects_unsealed_and_finished_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                writing = queue.begin("writing.jpg", ORIGIN, OWNER_ID)
                queue.write(writing, 0, b"partial")
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^upload payload is not locally readable$",
                ):
                    queue.open_local(writing.id)
                incomplete = queue.block_writing(
                    writing,
                    UploadErrorCode.LOCAL_WRITE_FAILED,
                )
                os.close(writing.descriptor)
                with self.assertRaisesRegex(
                    UploadQueueError,
                    "^upload payload is invalid$",
                ):
                    queue.open_local(incomplete.id)

                draft = queue.begin("committed.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"committed")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                attempting = queue.begin_attempt(pending.id)
                candidate = queue.record_candidate(attempting.id, ASSET_ID)
                committed = queue.commit(candidate.id)
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^upload payload is not locally readable$",
                ):
                    queue.open_local(committed.id)

                draft = queue.begin("cancelled.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"cancelled")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                with (
                    patch(
                        "immich_on_demand.uploads.os.rename",
                        side_effect=OSError("injected crash"),
                    ),
                    self.assertRaisesRegex(OSError, "^injected crash$"),
                ):
                    queue.cancel(
                        pending.id,
                        requested_name=pending.requested_name,
                        revision=pending.revision,
                    )
                cancelled = queue.status(pending.id)
                assert cancelled is not None
                self.assertEqual(cancelled.state, UploadState.CANCELLED)
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^upload payload is not locally readable$",
                ):
                    queue.open_local(cancelled.id)

    def test_open_local_rejects_tampered_and_symlink_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("tampered.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                pending.payload_path.write_bytes(b"tampered")
                with self.assertRaisesRegex(
                    UploadQueueError,
                    "^upload payload is invalid$",
                ):
                    queue.open_local(pending.id)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            outside = Path(directory) / "outside"
            outside.write_bytes(b"outside")
            outside.chmod(0o600)
            with UploadQueue(root) as queue:
                draft = queue.begin("symlink.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                pending.payload_path.unlink()
                pending.payload_path.symlink_to(outside)
                with self.assertRaises(OSError):
                    queue.open_local(pending.id)

    def test_restart_blocks_an_interrupted_write_without_discarding_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("unfinished.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"incomplete")
                queue.sync(draft)
                os.close(draft.descriptor)

            with UploadQueue(root) as queue:
                job = queue.status(draft.id)

            assert job is not None
            self.assertEqual(job.state, UploadState.BLOCKED)
            self.assertEqual(job.error, UploadErrorCode.INTERRUPTED_WRITE)
            self.assertIsNone(job.sha1)
            self.assertEqual(job.payload_path.read_bytes(), b"incomplete")

    def test_local_write_failure_can_be_durably_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("failed.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"recovery")
                blocked = queue.block_writing(
                    draft, UploadErrorCode.LOCAL_WRITE_FAILED
                )
                os.close(draft.descriptor)

            with UploadQueue(root) as queue:
                restored = queue.status(draft.id)

            self.assertEqual(blocked.state, UploadState.BLOCKED)
            self.assertEqual(blocked.error, UploadErrorCode.LOCAL_WRITE_FAILED)
            self.assertEqual(restored, blocked)
            self.assertEqual(blocked.payload_path.read_bytes(), b"recovery")

    def test_attempt_candidate_commit_and_removal_are_durable_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)

                self.assertEqual(queue.list(), (pending,))
                self.assertEqual(queue.next_due(pending.next_attempt_ns), pending)

                attempting = queue.begin_attempt(pending.id)
                self.assertEqual(attempting.state, UploadState.ATTEMPTING)
                self.assertEqual(attempting.attempt_count, 1)
                self.assertIsNone(attempting.candidate_asset_id)

                candidate = queue.record_candidate(attempting.id, ASSET_ID)
                self.assertEqual(candidate.candidate_asset_id, ASSET_ID)

            with UploadQueue(root) as queue:
                self.assertEqual(queue.next_due(), candidate)
                resumed = queue.begin_attempt(candidate.id)
                self.assertEqual(resumed.candidate_asset_id, ASSET_ID)
                committed = queue.commit(resumed.id)
                self.assertEqual(committed.state, UploadState.COMMITTED)
                queue.remove(committed.id)
                self.assertIsNone(queue.status(committed.id))
                self.assertEqual(queue.list(), ())

    def test_local_publication_failure_retains_a_committed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                attempting = queue.begin_attempt(pending.id)
                queue.record_candidate(attempting.id, ASSET_ID)
                committed = queue.commit(attempting.id)

                blocked = queue.block(
                    committed.id, UploadErrorCode.LOCAL_STATE_FAILED
                )
                self.assertEqual(blocked.state, UploadState.BLOCKED)
                self.assertEqual(blocked.candidate_asset_id, ASSET_ID)
                resumed = queue.retry(blocked.id, at_ns=0)
                self.assertEqual(resumed.state, UploadState.ATTEMPTING)
                self.assertEqual(resumed.candidate_asset_id, ASSET_ID)

    def test_restart_finishes_cleanup_for_a_committed_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                attempting = queue.begin_attempt(pending.id)
                queue.record_candidate(attempting.id, ASSET_ID)
                committed = queue.commit(attempting.id)

            with UploadQueue(root) as queue:
                self.assertIsNone(queue.status(committed.id))
                self.assertEqual(queue.list(), ())
            self.assertFalse(committed.payload_path.parent.exists())

    def test_retry_schedule_and_blocking_use_fixed_persisted_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                attempting = queue.begin_attempt(pending.id)

                with self.assertRaisesRegex(
                    UploadStateError, "^upload is already being attempted$"
                ):
                    queue.retry(
                        attempting.id,
                        at_ns=0,
                        revision=attempting.revision,
                    )
                delayed = queue.retry(
                    attempting.id,
                    at_ns=50,
                    error=UploadErrorCode.UPLOAD_UNAVAILABLE,
                )
                self.assertEqual(delayed.state, UploadState.PENDING)
                self.assertEqual(delayed.next_attempt_ns, 50)
                self.assertEqual(delayed.error, UploadErrorCode.UPLOAD_UNAVAILABLE)
                self.assertIsNone(queue.next_due(49))
                self.assertEqual(queue.next_due(50), delayed)
                with self.assertRaisesRegex(
                    UploadStateError, "^upload may already exist remotely$"
                ):
                    queue.cancel(
                        delayed.id,
                        requested_name=delayed.requested_name,
                        revision=delayed.revision,
                    )

                attempting = queue.begin_attempt(delayed.id)
                blocked = queue.block(
                    attempting.id, UploadErrorCode.UPLOAD_REJECTED
                )
                self.assertEqual(blocked.state, UploadState.BLOCKED)
                self.assertEqual(blocked.error, UploadErrorCode.UPLOAD_REJECTED)
                self.assertIsNone(queue.next_due(10**30))

                retried = queue.retry(blocked.id, at_ns=0)
                self.assertEqual(retried.state, UploadState.PENDING)
                self.assertIsNone(retried.error)
                self.assertEqual(queue.next_due(0), retried)

    def test_cancel_requires_exact_name_and_revision_and_removes_only_one_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                drafts = [
                    queue.begin("one.jpg", ORIGIN, OWNER_ID),
                    queue.begin("two.jpg", ORIGIN, OWNER_ID),
                ]
                jobs = []
                for draft in drafts:
                    queue.write(draft, 0, draft.requested_name.encode())
                    jobs.append(queue.seal(draft))
                    os.close(draft.descriptor)

                with self.assertRaisesRegex(
                    ValueError, "^upload cancellation confirmation does not match$"
                ):
                    queue.cancel(
                        jobs[0].id,
                        requested_name="wrong.jpg",
                        revision=jobs[0].revision,
                    )
                with self.assertRaisesRegex(
                    ValueError, "^upload cancellation confirmation does not match$"
                ):
                    queue.cancel(
                        jobs[0].id,
                        requested_name=jobs[0].requested_name,
                        revision=jobs[0].revision - 1,
                    )

                queue.cancel(
                    jobs[0].id,
                    requested_name=jobs[0].requested_name,
                    revision=jobs[0].revision,
                )

                self.assertIsNone(queue.status(jobs[0].id))
                self.assertEqual(queue.status(jobs[1].id), jobs[1])
                self.assertFalse(jobs[0].payload_path.parent.exists())
                self.assertEqual(jobs[1].payload_path.read_bytes(), b"two.jpg")

    def test_restart_finishes_cleanup_after_cancel_was_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"local only")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                with (
                    patch(
                        "immich_on_demand.uploads.os.rename",
                        side_effect=OSError("injected crash"),
                    ),
                    self.assertRaises(OSError),
                ):
                    queue.cancel(
                        pending.id,
                        requested_name=pending.requested_name,
                        revision=pending.revision,
                    )
                cancelled = queue.status(pending.id)
                assert cancelled is not None
                self.assertEqual(cancelled.state, UploadState.CANCELLED)
                self.assertTrue(cancelled.payload_path.exists())

            with UploadQueue(root) as queue:
                self.assertIsNone(queue.status(pending.id))
                self.assertEqual(queue.list(), ())

    def test_restart_finishes_cleanup_after_manifest_became_a_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"local only")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                real_unlink = os.unlink

                def fail_payload(path: str, *, dir_fd: int | None = None) -> None:
                    if path == "payload":
                        raise OSError("injected crash")
                    real_unlink(path, dir_fd=dir_fd)

                with (
                    patch("immich_on_demand.uploads.os.unlink", fail_payload),
                    self.assertRaises(OSError),
                ):
                    queue.cancel(
                        pending.id,
                        requested_name=pending.requested_name,
                        revision=pending.revision,
                    )

            with UploadQueue(root) as queue:
                self.assertIsNone(queue.status(pending.id))
                self.assertEqual(queue.list(), ())

    def test_cancel_never_overwrites_an_untrusted_cleanup_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"local only")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                marker = root / f".cleanup-{pending.id}"
                marker.write_bytes(b"untrusted")
                marker.chmod(0o600)

                with self.assertRaisesRegex(
                    UploadQueueError, "^upload queue contains unsafe state$"
                ):
                    queue.cancel(
                        pending.id,
                        requested_name=pending.requested_name,
                        revision=pending.revision,
                    )

                self.assertEqual(marker.read_bytes(), b"untrusted")
                self.assertEqual(pending.payload_path.read_bytes(), b"local only")

    def test_interrupted_write_can_be_cancelled_only_after_restart_blocks_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("unfinished.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"partial")
                queue.sync(draft)
                os.close(draft.descriptor)

            with UploadQueue(root) as queue:
                blocked = queue.status(draft.id)
                assert blocked is not None
                with self.assertRaisesRegex(
                    UploadStateError,
                    "^incomplete upload recovery cannot be retried$",
                ):
                    queue.retry(blocked.id)
                queue.cancel(
                    blocked.id,
                    requested_name=blocked.requested_name,
                    revision=blocked.revision,
                )
                self.assertIsNone(queue.status(blocked.id))

    def test_extending_write_reserves_the_free_space_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root, minimum_free_bytes=8) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                with (
                    patch(
                        "immich_on_demand.uploads.shutil.disk_usage",
                        return_value=SimpleNamespace(free=10),
                    ),
                    self.assertRaisesRegex(OSError, "upload queue storage is full"),
                ):
                    queue.write(draft, 0, b"abc")
                self.assertEqual(os.fstat(draft.descriptor).st_size, 0)

                queue.write(draft, 0, b"ab")
                with patch(
                    "immich_on_demand.uploads.shutil.disk_usage",
                    side_effect=AssertionError("overwrite checked free space"),
                ):
                    queue.write(draft, 0, b"z")
                self.assertEqual(os.pread(draft.descriptor, 2, 0), b"zb")
                os.close(draft.descriptor)

    def test_extending_truncate_reserves_the_free_space_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root, minimum_free_bytes=8) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                with (
                    patch(
                        "immich_on_demand.uploads.shutil.disk_usage",
                        return_value=SimpleNamespace(free=10),
                    ),
                    self.assertRaisesRegex(OSError, "upload queue storage is full"),
                ):
                    queue.truncate(draft, 3)
                self.assertEqual(os.fstat(draft.descriptor).st_size, 0)

                queue.truncate(draft, 2)
                with patch(
                    "immich_on_demand.uploads.shutil.disk_usage",
                    side_effect=AssertionError("shrink checked free space"),
                ):
                    queue.truncate(draft, 1)
                self.assertEqual(os.fstat(draft.descriptor).st_size, 1)
                os.close(draft.descriptor)

    def test_private_modes_and_single_link_payload_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(draft.payload_path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(draft.payload_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    (draft.payload_path.parent / "manifest.json").stat().st_mode
                    & 0o777,
                    0o600,
                )
                os.close(draft.descriptor)
            outside = Path(directory) / "payload-link"
            os.link(draft.payload_path, outside)

            with UploadQueue(root) as queue:
                self.assertEqual(queue.quarantined_count, 1)
                self.assertEqual(queue.list(), ())

            self.assertTrue(draft.payload_path.exists())
            self.assertTrue(outside.exists())

    def test_malformed_and_unknown_queue_entries_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            root.mkdir(mode=0o700)
            rogue = root / "not-a-job"
            rogue.mkdir(mode=0o700)
            with UploadQueue(root) as queue:
                self.assertEqual(queue.quarantined_count, 1)
                self.assertEqual(queue.list(), ())
            self.assertTrue(rogue.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"keep")
                os.close(draft.descriptor)
            manifest = draft.payload_path.parent / "manifest.json"
            manifest.write_bytes(b'{"format_version":1,"format_version":1}')
            temporary = draft.payload_path.parent / "manifest.json.tmp"
            temporary.write_bytes(b"{}")
            temporary.chmod(0o600)

            with UploadQueue(root) as queue:
                self.assertEqual(queue.quarantined_count, 1)
                self.assertEqual(queue.list(), ())

            self.assertEqual(draft.payload_path.read_bytes(), b"keep")
            self.assertTrue(manifest.exists())
            self.assertTrue(temporary.exists())

    def test_quarantine_does_not_hide_valid_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"valid")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
            rogue = root / "unknown"
            rogue.write_bytes(b"do not touch")

            with UploadQueue(root) as queue:
                self.assertEqual(queue.quarantined_count, 1)
                self.assertEqual(queue.list(), (pending,))
                self.assertEqual(queue.next_due(pending.next_attempt_ns), pending)

            self.assertEqual(rogue.read_bytes(), b"do not touch")

    def test_orphaned_manifest_temporary_file_is_quarantined_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"valid bytes")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
            temporary = pending.payload_path.parent / "manifest.json.tmp"
            temporary.write_bytes(b"partial transition")
            temporary.chmod(0o600)

            with UploadQueue(root) as queue:
                self.assertEqual(queue.quarantined_count, 1)
                self.assertEqual(queue.list(), ())

            self.assertEqual(temporary.read_bytes(), b"partial transition")
            self.assertEqual(pending.payload_path.read_bytes(), b"valid bytes")

    def test_attempt_revalidates_payload_and_blocks_changed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                pending.payload_path.write_bytes(b"tampered")
                os.utime(
                    pending.payload_path,
                    ns=(pending.created_ns or 0, pending.modified_ns or 0),
                )

                with self.assertRaisesRegex(
                    UploadQueueError, "^upload payload is invalid$"
                ):
                    queue.begin_attempt(pending.id)

                blocked = queue.status(pending.id)
                assert blocked is not None
                self.assertEqual(blocked.state, UploadState.BLOCKED)
                self.assertEqual(blocked.error, UploadErrorCode.PAYLOAD_INVALID)
                self.assertEqual(blocked.attempt_count, 0)
                queue.cancel(
                    blocked.id,
                    requested_name=blocked.requested_name,
                    revision=blocked.revision,
                )
                self.assertIsNone(queue.status(blocked.id))

    def test_attempt_closes_payload_when_blocking_invalid_state_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "uploads"
            with UploadQueue(root) as queue:
                draft = queue.begin("photo.jpg", ORIGIN, OWNER_ID)
                queue.write(draft, 0, b"original")
                pending = queue.seal(draft)
                os.close(draft.descriptor)
                pending.payload_path.write_bytes(b"tampered")
                opened: list[int] = []
                original_open = queue._open_payload
                original_write = queue._write_manifest

                def capture(job_descriptor: int) -> int:
                    descriptor = original_open(job_descriptor)
                    opened.append(descriptor)
                    return descriptor

                def fail_block(job_descriptor: int, manifest: object) -> None:
                    if getattr(manifest, "state", None) == UploadState.BLOCKED.value:
                        raise OSError("manifest unavailable")
                    original_write(job_descriptor, manifest)  # type: ignore[arg-type]

                with (
                    patch.object(queue, "_open_payload", capture),
                    patch.object(queue, "_write_manifest", fail_block),
                    self.assertRaisesRegex(OSError, "^manifest unavailable$"),
                ):
                    queue.begin_attempt(pending.id)

                self.assertEqual(len(opened), 1)
                with self.assertRaises(OSError):
                    os.fstat(opened[0])


if __name__ == "__main__":
    unittest.main()
