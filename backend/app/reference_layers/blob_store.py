"""Durable local storage for immutable reference-data artifacts.

The store deliberately has no database dependency.  Callers stream an artifact
to a private staging file, validate it, and only then commit it to a
content-addressed key.  Database metadata can therefore be committed after the
blob exists without ever pointing at a partial download.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import re
import shutil
import stat
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator
from uuid import uuid4


DEFAULT_CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_BLOB_BYTES = 20 * 1024 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STAGING_NAME_RE = re.compile(r"^[0-9a-f]{32}\.part$")
BLOB_KEY_RE = re.compile(
    r"^blobs/sha256/(?P<prefix>[0-9a-f]{2})/(?P<digest>[0-9a-f]{64})$"
)


class ReferenceBlobStoreError(Exception):
    """Base error for local reference artifact storage."""


class ReferenceBlobTooLargeError(ReferenceBlobStoreError):
    pass


class ReferenceBlobEmptyError(ReferenceBlobStoreError):
    pass


class ReferenceBlobIntegrityError(ReferenceBlobStoreError):
    pass


class ReferenceBlobCollisionError(ReferenceBlobStoreError):
    pass


class ReferenceStorageQuotaError(ReferenceBlobStoreError):
    pass


class ReferenceStorageSpaceError(ReferenceBlobStoreError):
    pass


class InvalidReferenceBlobKeyError(ReferenceBlobStoreError):
    pass


@dataclass(frozen=True)
class StoredReferenceBlob:
    storage_backend: str
    storage_key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class StagingCleanupResult:
    deleted_count: int
    deleted_bytes: int
    skipped_count: int


class ReferenceBlobStore:
    """A content-addressed blob store rooted in one local filesystem."""

    storage_backend = "filesystem"

    def __init__(
        self,
        root: str | Path,
        *,
        max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES,
        quota_bytes: int | None = None,
        min_free_bytes: int = 0,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        self.max_blob_bytes = _positive_integer(max_blob_bytes, "max_blob_bytes")
        self.chunk_bytes = _positive_integer(chunk_bytes, "chunk_bytes")
        self.quota_bytes = _optional_positive_integer(quota_bytes, "quota_bytes")
        self.min_free_bytes = _non_negative_integer(
            min_free_bytes,
            "min_free_bytes",
        )
        if self.quota_bytes is not None and self.max_blob_bytes > self.quota_bytes:
            raise ValueError("max_blob_bytes cannot exceed quota_bytes")

        configured = Path(root).expanduser()
        try:
            configured.mkdir(parents=True, exist_ok=True)
            self.root = configured.resolve(strict=True)
        except OSError as error:
            raise ReferenceBlobStoreError("could not initialize blob store root") from error
        if not self.root.is_dir():
            raise ReferenceBlobStoreError("blob store root is not a directory")

        self._staging_dir = self._ensure_directory_tree(
            ("staging",),
            leaf_mode=0o700,
        )
        self._ensure_directory_tree(("blobs", "sha256"), leaf_mode=0o750)
        self._thread_lock = threading.RLock()
        self._lock_fd: int | None = None
        lock_fd: int | None = None
        try:
            os.chmod(self._staging_dir, 0o700)
            lock_flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_CLOEXEC"):
                lock_flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                lock_flags |= os.O_NOFOLLOW
            lock_fd = os.open(
                self.root / ".reference-blob-store.lock",
                lock_flags,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ReferenceBlobStoreError("blob store lock is not a regular file")
            os.fchmod(lock_fd, 0o600)
            self._lock_fd = lock_fd
        except (OSError, ReferenceBlobStoreError) as error:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            if isinstance(error, ReferenceBlobStoreError):
                raise
            raise ReferenceBlobStoreError("could not initialize blob store lock") from error

    def close(self) -> None:
        with self._thread_lock:
            lock_fd = getattr(self, "_lock_fd", None)
            if lock_fd is None:
                return
            self._lock_fd = None
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def __enter__(self) -> "ReferenceBlobStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def stage(self, *, max_bytes: int | None = None) -> "ReferenceStagingWriter":
        if self._lock_fd is None:
            raise ReferenceBlobStoreError("blob store is closed")
        limit = self.max_blob_bytes
        if max_bytes is not None:
            requested = _positive_integer(max_bytes, "max_bytes")
            if requested > self.max_blob_bytes:
                raise ValueError("max_bytes cannot exceed the store maximum")
            limit = requested
        return ReferenceStagingWriter(self, max_bytes=limit)

    def put_stream(
        self,
        stream: BinaryIO,
        *,
        max_bytes: int | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> StoredReferenceBlob:
        with self.stage(max_bytes=max_bytes) as staging:
            while True:
                try:
                    chunk = stream.read(self.chunk_bytes)
                except Exception as error:
                    raise ReferenceBlobStoreError("could not read artifact stream") from error
                if chunk is None:
                    raise ReferenceBlobStoreError(
                        "artifact stream returned None instead of bytes"
                    )
                if chunk == b"":
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ReferenceBlobStoreError("artifact stream returned non-bytes data")
                staging.write(chunk)
            return staging.commit(
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )

    def ensure_capacity(self, additional_bytes: int) -> None:
        """Fail before expensive work when the configured storage cannot fit it.

        This is a preflight rather than a reservation.  Writers still repeat
        quota and free-space checks while they make progress, so a concurrent
        consumer cannot turn this check into permission to overfill the
        filesystem.
        """

        amount = _non_negative_integer(additional_bytes, "additional_bytes")
        if self._lock_fd is None:
            raise ReferenceBlobStoreError("blob store is closed")
        with self._exclusive_lock():
            self._ensure_write_capacity(amount)

    def commit_staged_file(
        self,
        path: str | Path,
        *,
        max_bytes: int | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> StoredReferenceBlob:
        """Atomically adopt a completed regular file already under staging.

        Large derived artifacts such as MBTiles databases are created by
        libraries that need a filesystem path.  Copying them through
        ``put_stream`` would temporarily require twice their size.  This
        method verifies the complete immutable identity under an exclusive
        file lock and then moves the same inode into the CAS.
        """

        if self._lock_fd is None:
            raise ReferenceBlobStoreError("blob store is closed")
        limit = self.max_blob_bytes
        if max_bytes is not None:
            requested = _positive_integer(max_bytes, "max_bytes")
            if requested > self.max_blob_bytes:
                raise ValueError("max_bytes cannot exceed the store maximum")
            limit = requested
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        if expected_size is not None:
            _non_negative_integer(expected_size, "expected_size")

        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._staging_dir)
        except (OSError, ValueError) as error:
            raise ReferenceBlobStoreError(
                "completed artifact is outside reference staging"
            ) from error
        if resolved == self._staging_dir or candidate.is_symlink():
            raise ReferenceBlobStoreError("completed staging artifact is invalid")
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(resolved, flags)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size <= 0
                or before.st_size > limit
            ):
                raise ReferenceBlobTooLargeError(
                    "completed artifact size is outside its byte limit"
                )
            digest = _sha256_descriptor(descriptor)
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise ReferenceBlobIntegrityError(
                    "completed staging artifact changed during verification"
                )
            if expected_sha256 is not None and digest != expected_sha256:
                raise ReferenceBlobIntegrityError(
                    "completed artifact digest mismatch"
                )
            if expected_size is not None and before.st_size != expected_size:
                raise ReferenceBlobIntegrityError("completed artifact size mismatch")
            os.fchmod(descriptor, 0o640)
            os.fsync(descriptor)

            with self._exclusive_lock():
                # The file is already counted by quota because staging lives
                # below the store root.  Rechecking zero catches an exhausted
                # quota or reserve without double-counting these bytes.
                self._ensure_write_capacity(0)
                storage_key, destination = self._blob_path(digest)
                try:
                    metadata = destination.lstat()
                except FileNotFoundError:
                    metadata = None
                if metadata is not None:
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or destination.is_symlink()
                        or metadata.st_size != before.st_size
                        or _sha256_file(destination) != digest
                    ):
                        raise ReferenceBlobCollisionError(
                            "content-addressed destination does not match its digest"
                        )
                    resolved.unlink()
                else:
                    os.replace(resolved, destination)
                    self._fsync_directory(destination.parent)
                self._fsync_directory(resolved.parent)
        except ReferenceBlobStoreError:
            raise
        except BlockingIOError as error:
            raise ReferenceBlobStoreError(
                "completed staging artifact is still being written"
            ) from error
        except OSError as error:
            raise ReferenceBlobStoreError(
                "could not commit completed staging artifact"
            ) from error
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return StoredReferenceBlob(
            storage_backend=self.storage_backend,
            storage_key=storage_key,
            sha256=digest,
            size_bytes=before.st_size,
        )

    def resolve_blob(self, storage_key: str) -> Path:
        match = _validated_blob_key(storage_key)
        path = self.root.joinpath(*PurePosixPath(storage_key).parts)
        self._require_safe_path_components(path.parent)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise InvalidReferenceBlobKeyError("reference blob does not exist") from error
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise InvalidReferenceBlobKeyError("reference blob is not a regular file")
        if match.group("prefix") != match.group("digest")[:2]:
            raise InvalidReferenceBlobKeyError("reference blob key prefix is invalid")
        return path

    def open_blob(self, storage_key: str) -> BinaryIO:
        path = self.resolve_blob(storage_key)
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise InvalidReferenceBlobKeyError("reference blob could not be opened") from error
        return os.fdopen(descriptor, "rb")

    def cleanup_staging(
        self,
        *,
        older_than_seconds: float,
        now: float | None = None,
    ) -> StagingCleanupResult:
        age = _non_negative_finite(older_than_seconds, "older_than_seconds")
        current_time = time.time() if now is None else _finite_number(now, "now")
        cutoff = current_time - age
        deleted_count = 0
        deleted_bytes = 0
        skipped_count = 0

        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW

        with self._exclusive_lock():
            try:
                directory_fd = os.open(self._staging_dir, directory_flags)
            except OSError as error:
                raise ReferenceBlobStoreError("could not open staging directory") from error
            try:
                for name in os.listdir(directory_fd):
                    if STAGING_NAME_RE.fullmatch(name) is None:
                        skipped_count += 1
                        continue
                    file_flags = os.O_RDONLY
                    if hasattr(os, "O_CLOEXEC"):
                        file_flags |= os.O_CLOEXEC
                    if hasattr(os, "O_NOFOLLOW"):
                        file_flags |= os.O_NOFOLLOW
                    if hasattr(os, "O_NONBLOCK"):
                        file_flags |= os.O_NONBLOCK
                    try:
                        candidate_fd = os.open(
                            name,
                            file_flags,
                            dir_fd=directory_fd,
                        )
                    except OSError:
                        skipped_count += 1
                        continue
                    try:
                        try:
                            fcntl.flock(
                                candidate_fd,
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                        except BlockingIOError:
                            skipped_count += 1
                            continue
                        candidate = os.fstat(candidate_fd)
                        if not stat.S_ISREG(candidate.st_mode) or candidate.st_mtime > cutoff:
                            skipped_count += 1
                            continue
                        current = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(current.st_mode)
                            or current.st_dev != candidate.st_dev
                            or current.st_ino != candidate.st_ino
                        ):
                            skipped_count += 1
                            continue
                        os.unlink(name, dir_fd=directory_fd)
                        deleted_count += 1
                        deleted_bytes += candidate.st_size
                    except FileNotFoundError:
                        skipped_count += 1
                    except OSError as error:
                        raise ReferenceBlobStoreError(
                            "could not clean a staging artifact"
                        ) from error
                    finally:
                        os.close(candidate_fd)
                if deleted_count:
                    os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        return StagingCleanupResult(
            deleted_count=deleted_count,
            deleted_bytes=deleted_bytes,
            skipped_count=skipped_count,
        )

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        with self._thread_lock:
            lock_fd = self._lock_fd
            if lock_fd is None:
                raise ReferenceBlobStoreError("blob store is closed")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as error:
                raise ReferenceBlobStoreError("blob store lock failed") from error
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    def _ensure_write_capacity(self, additional_bytes: int) -> None:
        if self.quota_bytes is not None:
            used_bytes = self._storage_usage_bytes()
            if used_bytes + additional_bytes > self.quota_bytes:
                raise ReferenceStorageQuotaError("reference storage quota exceeded")
        try:
            free_bytes = shutil.disk_usage(self.root).free
        except OSError as error:
            raise ReferenceStorageSpaceError(
                "reference storage free space is unavailable"
            ) from error
        if free_bytes - additional_bytes < self.min_free_bytes:
            raise ReferenceStorageSpaceError("reference storage free space is too low")

    def _storage_usage_bytes(self) -> int:
        total = 0
        try:
            for directory, names, filenames in os.walk(
                self.root,
                topdown=True,
                onerror=_raise_walk_error,
                followlinks=False,
            ):
                safe_names: list[str] = []
                for name in names:
                    candidate = Path(directory, name)
                    try:
                        if stat.S_ISDIR(candidate.lstat().st_mode) and not candidate.is_symlink():
                            safe_names.append(name)
                    except OSError:
                        continue
                names[:] = safe_names
                for filename in filenames:
                    candidate = Path(directory, filename)
                    try:
                        metadata = candidate.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        total += metadata.st_size
        except OSError as error:
            raise ReferenceBlobStoreError("could not measure reference storage") from error
        return total

    def _blob_path(self, digest: str) -> tuple[str, Path]:
        key = f"blobs/sha256/{digest[:2]}/{digest}"
        parent = self._ensure_directory_tree(
            ("blobs", "sha256", digest[:2]),
            leaf_mode=0o750,
        )
        return key, parent / digest

    def _ensure_directory_tree(
        self,
        parts: tuple[str, ...],
        *,
        leaf_mode: int,
    ) -> Path:
        current = self.root
        for index, part in enumerate(parts):
            if not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in {".", ".."}:
                raise ReferenceBlobStoreError("invalid internal storage path")
            current = current / part
            created = False
            try:
                current.mkdir(mode=leaf_mode if index == len(parts) - 1 else 0o750)
                created = True
            except FileExistsError:
                pass
            except OSError as error:
                raise ReferenceBlobStoreError(
                    "could not create reference storage directory"
                ) from error
            try:
                metadata = current.lstat()
            except OSError as error:
                raise ReferenceBlobStoreError(
                    "could not inspect reference storage directory"
                ) from error
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise ReferenceBlobStoreError(
                    "reference storage directory is not safe"
                )
            if created:
                self._fsync_directory(current.parent)
        return current

    def _require_safe_path_components(self, directory: Path) -> None:
        try:
            relative = directory.relative_to(self.root)
        except ValueError as error:
            raise InvalidReferenceBlobKeyError("reference blob escapes storage") from error
        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as error:
                raise InvalidReferenceBlobKeyError(
                    "reference blob path does not exist"
                ) from error
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise InvalidReferenceBlobKeyError("reference blob path is not safe")

    def _fsync_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(directory, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ReferenceBlobStoreError("could not fsync storage directory") from error


class ReferenceStagingWriter:
    """One private staging artifact with bounded streaming writes."""

    def __init__(self, store: ReferenceBlobStore, *, max_bytes: int) -> None:
        self._store = store
        self._max_bytes = max_bytes
        self._size_bytes = 0
        self._digest = hashlib.sha256()
        self._finished = False
        self._committed: StoredReferenceBlob | None = None
        self._path = store._staging_dir / f"{uuid4().hex}.part"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(self._path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = descriptor
        except OSError as error:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ReferenceBlobStoreError("could not create staging artifact") from error

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def staging_path(self) -> Path:
        return self._path

    def write(self, data: bytes | bytearray | memoryview) -> int:
        self._require_open()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            self._abort_after_error()
            raise TypeError("staging writes require bytes-like data")
        try:
            view = memoryview(data).cast("B")
        except (TypeError, ValueError) as error:
            self._abort_after_error()
            raise TypeError("staging writes require contiguous bytes-like data") from error
        if not view:
            return 0
        payload = bytes(view)
        if self._size_bytes + len(payload) > self._max_bytes:
            self._abort_after_error()
            raise ReferenceBlobTooLargeError("reference artifact exceeds its byte limit")

        try:
            with self._store._exclusive_lock():
                self._store._ensure_write_capacity(len(payload))
                written = 0
                while written < len(payload):
                    count = os.write(self._fd, payload[written:])
                    if count <= 0:
                        raise OSError("staging write made no progress")
                    written += count
        except ReferenceBlobStoreError:
            self._abort_after_error()
            raise
        except OSError as error:
            self._abort_after_error()
            raise ReferenceBlobStoreError("could not write staging artifact") from error

        self._digest.update(payload)
        self._size_bytes += len(payload)
        return len(payload)

    def commit(
        self,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> StoredReferenceBlob:
        self._require_open()
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or SHA256_RE.fullmatch(expected_sha256) is None
        ):
            self._abort_after_error()
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        if expected_size is not None:
            try:
                _non_negative_integer(expected_size, "expected_size")
            except ValueError:
                self._abort_after_error()
                raise
        if self._size_bytes == 0:
            self._abort_after_error()
            raise ReferenceBlobEmptyError("reference artifact is empty")
        digest = self.sha256
        if expected_sha256 is not None and digest != expected_sha256:
            self._abort_after_error()
            raise ReferenceBlobIntegrityError("reference artifact digest mismatch")
        if expected_size is not None and self._size_bytes != expected_size:
            self._abort_after_error()
            raise ReferenceBlobIntegrityError("reference artifact size mismatch")

        try:
            os.fchmod(self._fd, 0o640)
            os.fsync(self._fd)
        except OSError as error:
            self._abort_after_error()
            raise ReferenceBlobStoreError("could not fsync staging artifact") from error

        try:
            with self._store._exclusive_lock():
                storage_key, destination = self._store._blob_path(digest)
                try:
                    metadata = destination.lstat()
                except FileNotFoundError:
                    metadata = None
                except OSError as error:
                    raise ReferenceBlobStoreError(
                        "could not inspect content-addressed destination"
                    ) from error
                if metadata is not None:
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or destination.is_symlink()
                        or metadata.st_size != self._size_bytes
                        or _sha256_file(destination) != digest
                    ):
                        raise ReferenceBlobCollisionError(
                            "content-addressed destination does not match its digest"
                        )
                    self._path.unlink()
                    self._store._fsync_directory(self._store._staging_dir)
                else:
                    os.replace(self._path, destination)
                    self._store._fsync_directory(destination.parent)
                    self._store._fsync_directory(self._store._staging_dir)
        except ReferenceBlobStoreError:
            self._abort_after_error()
            raise
        except OSError as error:
            self._abort_after_error()
            raise ReferenceBlobStoreError("could not commit reference artifact") from error

        descriptor = self._fd
        self._fd = -1
        try:
            os.close(descriptor)
        except OSError as error:
            self._finished = True
            raise ReferenceBlobStoreError("could not close committed artifact") from error

        self._finished = True
        self._committed = StoredReferenceBlob(
            storage_backend=self._store.storage_backend,
            storage_key=storage_key,
            sha256=digest,
            size_bytes=self._size_bytes,
        )
        return self._committed

    def abort(self) -> None:
        if self._finished:
            return
        self._finished = True
        descriptor = getattr(self, "_fd", -1)
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._fd = -1
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    def _abort_after_error(self) -> None:
        self.abort()

    def _require_open(self) -> None:
        if self._finished or getattr(self, "_fd", -1) < 0:
            raise ReferenceBlobStoreError("staging artifact is closed")

    def __enter__(self) -> "ReferenceStagingWriter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._finished:
            self.abort()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            while chunk := source.read(DEFAULT_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise ReferenceBlobCollisionError(
            "content-addressed destination could not be verified"
        ) from error
    return digest.hexdigest()


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, DEFAULT_CHUNK_BYTES):
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise ReferenceBlobIntegrityError(
            "completed staging artifact could not be hashed"
        ) from error
    return digest.hexdigest()


def _raise_walk_error(error: OSError) -> None:
    raise error


def _validated_blob_key(storage_key: str):
    if not isinstance(storage_key, str):
        raise InvalidReferenceBlobKeyError("reference blob key must be text")
    key_path = PurePosixPath(storage_key)
    if key_path.is_absolute() or any(part in {"", ".", ".."} for part in key_path.parts):
        raise InvalidReferenceBlobKeyError("reference blob key is invalid")
    match = BLOB_KEY_RE.fullmatch(storage_key)
    if match is None or match.group("prefix") != match.group("digest")[:2]:
        raise InvalidReferenceBlobKeyError("reference blob key is invalid")
    return match


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_positive_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _positive_integer(value, name)


def _non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _finite_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _non_negative_finite(value: object, name: str) -> float:
    normalized = _finite_number(value, name)
    if normalized < 0:
        raise ValueError(f"{name} must be non-negative")
    return normalized
