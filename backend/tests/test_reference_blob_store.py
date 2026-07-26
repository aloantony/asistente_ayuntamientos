from __future__ import annotations

import hashlib
import io
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.reference_layers import blob_store as blob_store_module
from app.reference_layers.blob_store import (
    InvalidReferenceBlobKeyError,
    ReferenceBlobCollisionError,
    ReferenceBlobEmptyError,
    ReferenceBlobIntegrityError,
    ReferenceBlobStore,
    ReferenceBlobStoreError,
    ReferenceBlobTooLargeError,
    ReferenceStorageQuotaError,
    ReferenceStorageSpaceError,
)


@pytest.fixture()
def store(tmp_path):
    instance = ReferenceBlobStore(tmp_path / "reference-data", max_blob_bytes=1024)
    try:
        yield instance
    finally:
        instance.close()


def test_streams_to_private_staging_and_commits_content_addressed_blob(store) -> None:
    payload = b"cartografia oficial"
    expected = hashlib.sha256(payload).hexdigest()

    with store.stage() as staging:
        assert stat.S_IMODE(staging.staging_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(staging.staging_path.parent.stat().st_mode) == 0o700
        assert staging.write(payload[:8]) == 8
        assert staging.write(memoryview(payload[8:])) == len(payload) - 8
        stored = staging.commit(expected_sha256=expected, expected_size=len(payload))

    assert stored.storage_backend == "filesystem"
    assert stored.sha256 == expected
    assert stored.size_bytes == len(payload)
    assert stored.storage_key == f"blobs/sha256/{expected[:2]}/{expected}"
    resolved = store.resolve_blob(stored.storage_key)
    assert resolved.read_bytes() == payload
    assert stat.S_IMODE(resolved.stat().st_mode) == 0o640
    with store.open_blob(stored.storage_key) as opened:
        assert opened.read() == payload
    assert list((store.root / "staging").iterdir()) == []


def test_put_stream_is_bounded_and_validates_expected_identity(store) -> None:
    payload = b"abc" * 100
    expected = hashlib.sha256(payload).hexdigest()

    stored = store.put_stream(
        io.BytesIO(payload),
        max_bytes=len(payload),
        expected_sha256=expected,
        expected_size=len(payload),
    )

    assert stored.sha256 == expected
    assert store.resolve_blob(stored.storage_key).read_bytes() == payload


def test_read_only_store_opens_existing_blobs_without_mutating_root(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "read-only"
    writer = ReferenceBlobStore(root, max_blob_bytes=1024)
    stored = writer.put_stream(io.BytesIO(b"immutable local metadata"))
    writer.close()
    (root / ".reference-blob-store.lock").unlink()
    staging = root / "staging"
    staging.rmdir()

    real_open = os.open

    def reject_write_open(path, flags, *args, **kwargs):
        if flags & (os.O_CREAT | os.O_RDWR | os.O_WRONLY):
            raise AssertionError(
                f"read-only store tried to open {path!s} for writing"
            )
        return real_open(path, flags, *args, **kwargs)

    def reject_chmod(*_args, **_kwargs):
        raise AssertionError("read-only store tried to change permissions")

    monkeypatch.setattr(blob_store_module.os, "open", reject_write_open)
    monkeypatch.setattr(blob_store_module.os, "chmod", reject_chmod)

    with ReferenceBlobStore(
        root,
        max_blob_bytes=1024,
        read_only=True,
    ) as reader:
        with reader.open_blob(stored.storage_key) as stream:
            assert stream.read() == b"immutable local metadata"
        with pytest.raises(ReferenceBlobStoreError, match="read-only"):
            reader.put_stream(io.BytesIO(b"forbidden"))
        with pytest.raises(ReferenceBlobStoreError, match="read-only"):
            reader.cleanup_staging(older_than_seconds=0)

    assert not (root / ".reference-blob-store.lock").exists()
    assert not staging.exists()


def test_read_only_store_requires_an_existing_root(tmp_path) -> None:
    root = tmp_path / "absent"

    with pytest.raises(ReferenceBlobStoreError, match="initialize"):
        ReferenceBlobStore(root, read_only=True)

    assert not root.exists()


def test_completed_staging_file_is_adopted_without_copying(store) -> None:
    directory = store.root / "staging" / "derived"
    directory.mkdir(mode=0o700)
    candidate = directory / "snapshot.mbtiles"
    payload = b"immutable mbtiles payload"
    candidate.write_bytes(payload)
    inode = candidate.stat().st_ino

    stored = store.commit_staged_file(
        candidate,
        max_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
    )

    destination = store.resolve_blob(stored.storage_key)
    assert destination.read_bytes() == payload
    assert destination.stat().st_ino == inode
    assert not candidate.exists()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


def test_completed_file_outside_staging_or_with_wrong_identity_is_rejected(
    store,
    tmp_path,
) -> None:
    outside = tmp_path / "outside.mbtiles"
    outside.write_bytes(b"outside")
    with pytest.raises(ReferenceBlobStoreError, match="outside"):
        store.commit_staged_file(outside)

    directory = store.root / "staging" / "derived-invalid"
    directory.mkdir(mode=0o700)
    candidate = directory / "snapshot.mbtiles"
    candidate.write_bytes(b"payload")
    with pytest.raises(ReferenceBlobIntegrityError, match="digest"):
        store.commit_staged_file(candidate, expected_sha256="0" * 64)
    assert candidate.read_bytes() == b"payload"


def test_capacity_preflight_uses_quota_and_free_space(tmp_path, monkeypatch) -> None:
    instance = ReferenceBlobStore(
        tmp_path / "preflight",
        max_blob_bytes=8,
        quota_bytes=8,
        min_free_bytes=3,
    )
    try:
        instance.put_stream(io.BytesIO(b"1234"))
        with pytest.raises(ReferenceStorageQuotaError):
            instance.ensure_capacity(5)
        monkeypatch.setattr(
            blob_store_module.shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(total=100, used=94, free=6),
        )
        with pytest.raises(ReferenceStorageSpaceError):
            instance.ensure_capacity(4)
    finally:
        instance.close()


def test_mutable_input_cannot_change_hash_after_bytes_are_written(
    store,
    monkeypatch,
) -> None:
    payload = bytearray(b"original")
    real_write = os.write

    def write_then_mutate(descriptor, data):
        written = real_write(descriptor, data)
        payload[:] = b"tampered"
        return written

    monkeypatch.setattr(blob_store_module.os, "write", write_then_mutate)
    with store.stage() as staging:
        staging.write(payload)
        stored = staging.commit()

    assert store.resolve_blob(stored.storage_key).read_bytes() == b"original"
    assert stored.sha256 == hashlib.sha256(b"original").hexdigest()


def test_oversized_write_aborts_and_removes_partial_staging(store) -> None:
    with pytest.raises(ReferenceBlobTooLargeError):
        with store.stage(max_bytes=4) as staging:
            staging.write(b"12345")

    assert list((store.root / "staging").iterdir()) == []


def test_empty_and_identity_mismatches_never_publish(store) -> None:
    with pytest.raises(ReferenceBlobEmptyError):
        with store.stage() as staging:
            staging.commit()

    with pytest.raises(ReferenceBlobIntegrityError, match="digest"):
        with store.stage() as staging:
            staging.write(b"payload")
            staging.commit(expected_sha256="0" * 64)

    with pytest.raises(ReferenceBlobIntegrityError, match="size"):
        with store.stage() as staging:
            staging.write(b"payload")
            staging.commit(expected_size=999)

    assert list((store.root / "staging").iterdir()) == []
    assert list((store.root / "blobs" / "sha256").rglob("[0-9a-f]" * 64)) == []


def test_context_manager_aborts_uncommitted_or_failed_staging(store) -> None:
    with store.stage() as staging:
        staging.write(b"not committed")
        path = staging.staging_path
    assert not path.exists()

    with pytest.raises(RuntimeError):
        with store.stage() as failed:
            failed.write(b"partial")
            failed_path = failed.staging_path
            raise RuntimeError("validation failed")
    assert not failed_path.exists()


def test_failed_staging_lock_closes_descriptor_and_removes_file(
    store,
    monkeypatch,
) -> None:
    closed_descriptors = []
    real_close = os.close

    def reject_lock(_descriptor, _operation):
        raise OSError("lock unavailable")

    def record_close(descriptor):
        closed_descriptors.append(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(blob_store_module.fcntl, "flock", reject_lock)
    monkeypatch.setattr(blob_store_module.os, "close", record_close)

    with pytest.raises(ReferenceBlobStoreError, match="staging"):
        store.stage()

    assert closed_descriptors
    assert list((store.root / "staging").iterdir()) == []


def test_quota_counts_committed_and_staging_bytes(tmp_path) -> None:
    instance = ReferenceBlobStore(
        tmp_path / "quota",
        max_blob_bytes=8,
        quota_bytes=8,
    )
    try:
        instance.put_stream(io.BytesIO(b"123456"))
        with pytest.raises(ReferenceStorageQuotaError):
            with instance.stage() as staging:
                staging.write(b"789")
        assert list((instance.root / "staging").iterdir()) == []
    finally:
        instance.close()


def test_minimum_free_space_is_enforced_before_write(tmp_path, monkeypatch) -> None:
    instance = ReferenceBlobStore(
        tmp_path / "space",
        max_blob_bytes=8,
        min_free_bytes=3,
    )
    monkeypatch.setattr(
        blob_store_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=96, free=4),
    )
    try:
        with pytest.raises(ReferenceStorageSpaceError):
            with instance.stage() as staging:
                staging.write(b"12")
    finally:
        instance.close()


def test_identical_blob_is_deduplicated_after_verifying_existing_content(store) -> None:
    first = store.put_stream(io.BytesIO(b"same bytes"))
    first_path = store.resolve_blob(first.storage_key)
    first_inode = first_path.stat().st_ino

    second = store.put_stream(io.BytesIO(b"same bytes"))

    assert second == first
    assert store.resolve_blob(second.storage_key).stat().st_ino == first_inode
    assert list((store.root / "staging").iterdir()) == []


def test_corrupt_existing_digest_path_is_a_collision_and_is_not_overwritten(store) -> None:
    payload = b"expected"
    first = store.put_stream(io.BytesIO(payload))
    destination = store.resolve_blob(first.storage_key)
    destination.write_bytes(b"tampered")

    with pytest.raises(ReferenceBlobCollisionError):
        store.put_stream(io.BytesIO(payload))

    assert destination.read_bytes() == b"tampered"
    assert list((store.root / "staging").iterdir()) == []


def test_commit_uses_replace_and_fsyncs_file_and_directories(store, monkeypatch) -> None:
    replace_calls = []
    fsync_calls = []
    staging_lock_verified = []
    real_replace = os.replace
    real_fsync = os.fsync

    def recorded_replace(source, destination):
        replace_calls.append((source, destination))
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(BlockingIOError):
                blob_store_module.fcntl.flock(
                    descriptor,
                    blob_store_module.fcntl.LOCK_EX
                    | blob_store_module.fcntl.LOCK_NB,
                )
            staging_lock_verified.append(True)
        finally:
            os.close(descriptor)
        return real_replace(source, destination)

    def recorded_fsync(descriptor):
        fsync_calls.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr(blob_store_module.os, "replace", recorded_replace)
    monkeypatch.setattr(blob_store_module.os, "fsync", recorded_fsync)

    store.put_stream(io.BytesIO(b"durable"))

    assert len(replace_calls) == 1
    assert staging_lock_verified == [True]
    assert len(fsync_calls) >= 3


def test_staging_cleanup_deletes_only_old_unlocked_regular_store_files(store) -> None:
    staging_dir = store.root / "staging"
    old = staging_dir / ("a" * 32 + ".part")
    fresh = staging_dir / ("b" * 32 + ".part")
    unknown = staging_dir / "operator-note.txt"
    target = staging_dir / "target"
    symlink = staging_dir / ("c" * 32 + ".part")
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    unknown.write_text("keep", encoding="utf-8")
    target.write_bytes(b"target")
    symlink.symlink_to(target.name)
    os.utime(old, (100, 100))
    os.utime(fresh, (950, 950))

    active = store.stage()
    active.write(b"active")
    os.utime(active.staging_path, (100, 100))
    try:
        result = store.cleanup_staging(older_than_seconds=100, now=1000)
        assert result.deleted_count == 1
        assert result.deleted_bytes == 3
        assert result.skipped_count == 5
        assert not old.exists()
        assert fresh.exists()
        assert unknown.exists()
        assert symlink.is_symlink()
        assert target.exists()
        assert active.staging_path.exists()
    finally:
        active.abort()


@pytest.mark.parametrize(
    ("older_than_seconds", "now"),
    [
        (float("nan"), 1000),
        (float("inf"), 1000),
        (0, float("nan")),
        (0, float("inf")),
        (True, 1000),
    ],
)
def test_staging_cleanup_rejects_unsafe_time_values(
    store,
    older_than_seconds,
    now,
) -> None:
    candidate = store.root / "staging" / ("d" * 32 + ".part")
    candidate.write_bytes(b"must remain")

    with pytest.raises(ValueError):
        store.cleanup_staging(older_than_seconds=older_than_seconds, now=now)

    assert candidate.read_bytes() == b"must remain"


def test_staging_cleanup_does_not_block_on_named_pipe(store) -> None:
    candidate = store.root / "staging" / ("e" * 32 + ".part")
    os.mkfifo(candidate, mode=0o600)

    result = store.cleanup_staging(older_than_seconds=0)

    assert result.deleted_count == 0
    assert result.skipped_count == 1
    assert stat.S_ISFIFO(candidate.stat().st_mode)


def test_blob_key_validation_rejects_noncanonical_and_symlinked_paths(store) -> None:
    stored = store.put_stream(io.BytesIO(b"blob"))
    digest = stored.sha256
    for invalid in (
        f"/blobs/sha256/{digest[:2]}/{digest}",
        f"blobs/sha256/../{digest}",
        f"blobs/sha256/ff/{digest}",
        "documents/example",
    ):
        with pytest.raises(InvalidReferenceBlobKeyError):
            store.resolve_blob(invalid)

    destination = store.resolve_blob(stored.storage_key)
    real = destination.with_name("real")
    destination.rename(real)
    destination.symlink_to(real.name)
    with pytest.raises(InvalidReferenceBlobKeyError):
        store.resolve_blob(stored.storage_key)


def test_two_store_instances_concurrently_deduplicate_the_same_blob(tmp_path) -> None:
    root = tmp_path / "shared"
    first_store = ReferenceBlobStore(root, max_blob_bytes=1024)
    second_store = ReferenceBlobStore(root, max_blob_bytes=1024)
    first = first_store.stage()
    second = second_store.stage()
    first.write(b"concurrent")
    second.write(b"concurrent")
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda writer: writer.commit(), (first, second)))
        assert results[0] == results[1]
        assert first_store.resolve_blob(results[0].storage_key).read_bytes() == b"concurrent"
        assert list((root / "staging").iterdir()) == []
    finally:
        first.abort()
        second.abort()
        first_store.close()
        second_store.close()


def test_concurrent_writes_cannot_race_past_shared_quota(tmp_path) -> None:
    root = tmp_path / "shared-quota"
    stores = (
        ReferenceBlobStore(root, max_blob_bytes=5, quota_bytes=5),
        ReferenceBlobStore(root, max_blob_bytes=5, quota_bytes=5),
    )
    writers = tuple(instance.stage() for instance in stores)

    def attempt(writer):
        try:
            writer.write(b"123")
        except ReferenceStorageQuotaError:
            return "quota"
        return "written"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, writers))
        assert sorted(outcomes) == ["quota", "written"]
    finally:
        for writer in writers:
            writer.abort()
        for instance in stores:
            instance.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_blob_bytes": 0}, "max_blob_bytes"),
        ({"max_blob_bytes": True}, "max_blob_bytes"),
        ({"quota_bytes": 0}, "quota_bytes"),
        ({"min_free_bytes": -1}, "min_free_bytes"),
        ({"chunk_bytes": 0}, "chunk_bytes"),
        ({"max_blob_bytes": 10, "quota_bytes": 9}, "cannot exceed"),
    ],
)
def test_invalid_store_limits_fail_closed(tmp_path, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        ReferenceBlobStore(tmp_path / "invalid", **kwargs)


def test_lock_file_cannot_be_a_symlink(tmp_path) -> None:
    root = tmp_path / "symlink-lock"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"operator data")
    (root / ".reference-blob-store.lock").symlink_to(outside)

    with pytest.raises(ReferenceBlobStoreError, match="lock"):
        ReferenceBlobStore(root, max_blob_bytes=8)

    assert outside.read_bytes() == b"operator data"


def test_closed_store_rejects_writes(store) -> None:
    staging = store.stage()
    store.close()
    with pytest.raises(ReferenceBlobStoreError, match="closed"):
        staging.write(b"data")
    staging.abort()
    with pytest.raises(ReferenceBlobStoreError, match="closed"):
        store.stage()


def test_invalid_commit_identity_aborts_even_without_context_manager(store) -> None:
    invalid_digest = store.stage()
    invalid_digest.write(b"data")
    with pytest.raises(ValueError, match="expected_sha256"):
        invalid_digest.commit(expected_sha256=123)

    invalid_size = store.stage()
    invalid_size.write(b"data")
    with pytest.raises(ValueError, match="expected_size"):
        invalid_size.commit(expected_size=-1)

    assert list((store.root / "staging").iterdir()) == []
