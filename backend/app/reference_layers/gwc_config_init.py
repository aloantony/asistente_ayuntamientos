"""Materialize the mandatory GeoWebCache quota before Tomcat starts.

GeoServer 3.0.0 accepts disk-quota changes over REST but does not reliably
persist them.  This one-shot owns the exact configuration file instead.  It is
safe-by-default, writes atomically beneath a no-symlink data directory and
first proves that the tile cache is a physically bounded dedicated mount.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Sequence
from uuid import uuid4

from app.core.config import Settings, settings
from app.reference_layers.geoserver_admin import (
    expected_geowebcache_tile_blob_store,
)
from app.reference_layers.gwc_quota import (
    GeoWebCacheQuotaSafetyError,
    cache_filesystem_block_size,
    physical_limit_status,
)

CONFIG_DIRECTORY_NAME = "gwc"
CONFIG_FILE_NAME = "geowebcache-diskquota.xml"
MAX_CONFIG_BYTES = 64 * 1024
MAX_OWNER_ID = 2**31 - 1


class GeoWebCacheConfigInitError(ValueError):
    """The pre-start quota file cannot be materialized safely."""


def quota_configuration_bytes(
    *,
    configured: Settings,
    filesystem_block_size: int,
) -> bytes:
    """Return the complete, deterministic GWC 2.0.0 quota configuration."""

    block_size = expected_geowebcache_tile_blob_store(
        file_system_block_size=filesystem_block_size,
    ).file_system_block_size
    quota = configured.geowebcache_disk_quota_gib
    cleanup = configured.geowebcache_disk_quota_cleanup_seconds
    policy = configured.geowebcache_disk_quota_policy
    if cleanup != 10:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache cleanup interval must be exactly 10 seconds"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<gwcQuotaConfiguration>\n"
        "  <enabled>true</enabled>\n"
        f"  <diskBlockSize>{block_size}</diskBlockSize>\n"
        f"  <cacheCleanUpFrequency>{cleanup}</cacheCleanUpFrequency>\n"
        "  <cacheCleanUpUnits>SECONDS</cacheCleanUpUnits>\n"
        "  <maxConcurrentCleanUps>2</maxConcurrentCleanUps>\n"
        f"  <globalExpirationPolicyName>{policy}</globalExpirationPolicyName>\n"
        "  <globalQuota>\n"
        f"    <value>{quota}</value>\n"
        "    <units>GiB</units>\n"
        "  </globalQuota>\n"
        "  <layerQuotas></layerQuotas>\n"
        "  <quotaStore>HSQL</quotaStore>\n"
        "</gwcQuotaConfiguration>\n"
    ).encode("ascii")


def initialize_quota_configuration(
    *,
    geoserver_data_path: Path,
    cache_path: Path,
    configured: Settings = settings,
    apply: bool = False,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> dict[str, object]:
    """Inspect or atomically install the exact pre-start quota file."""

    data_path = _existing_absolute_directory(
        geoserver_data_path,
        label="GeoServer data directory",
    )
    try:
        physical = physical_limit_status(cache_path, configured=configured)
        block_size = cache_filesystem_block_size(cache_path)
    except GeoWebCacheQuotaSafetyError as error:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache physical cache contract is unsafe"
        ) from error
    if not physical.safe:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache dedicated physical hard limit is not safe"
        )
    desired = quota_configuration_bytes(
        configured=configured,
        filesystem_block_size=block_size,
    )
    uid = _owner_id(owner_uid, "owner uid", required=apply)
    gid = _owner_id(owner_gid, "owner gid", required=apply)
    desired_sha256 = hashlib.sha256(desired).hexdigest()

    root_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        root_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        root_flags |= os.O_NOFOLLOW
    try:
        root_descriptor = os.open(data_path, root_flags)
    except OSError as error:
        raise GeoWebCacheConfigInitError(
            "GeoServer data directory cannot be opened safely"
        ) from error
    try:
        gwc_descriptor, created_directory = _open_or_create_gwc_directory(
            root_descriptor,
            apply=apply,
            owner_uid=uid,
            owner_gid=gid,
        )
        if gwc_descriptor is None:
            return {
                "schema_version": 1,
                "mode": "dry-run",
                "path": str(
                    data_path / CONFIG_DIRECTORY_NAME / CONFIG_FILE_NAME
                ),
                "desired_sha256": desired_sha256,
                "current_sha256": None,
                "filesystem_block_size_bytes": block_size,
                "physical_limit": asdict(physical),
                "verified": False,
            }
        try:
            before, current = _read_current_config(gwc_descriptor)
            current_sha256 = (
                hashlib.sha256(current).hexdigest()
                if current is not None
                else None
            )
            if current == desired:
                return {
                    "schema_version": 1,
                    "mode": "apply" if apply else "dry-run",
                    "path": str(
                        data_path / CONFIG_DIRECTORY_NAME / CONFIG_FILE_NAME
                    ),
                    "desired_sha256": desired_sha256,
                    "current_sha256": current_sha256,
                    "filesystem_block_size_bytes": block_size,
                    "physical_limit": asdict(physical),
                    "created_directory": created_directory,
                    "changed": False,
                    "verified": True,
                }
            if not apply:
                return {
                    "schema_version": 1,
                    "mode": "dry-run",
                    "path": str(
                        data_path / CONFIG_DIRECTORY_NAME / CONFIG_FILE_NAME
                    ),
                    "desired_sha256": desired_sha256,
                    "current_sha256": current_sha256,
                    "filesystem_block_size_bytes": block_size,
                    "physical_limit": asdict(physical),
                    "created_directory": False,
                    "changed": False,
                    "verified": False,
                }
            if uid is None or gid is None:  # pragma: no cover - validated.
                raise GeoWebCacheConfigInitError(
                    "GeoWebCache configuration ownership is unavailable"
                )
            _replace_config(
                gwc_descriptor,
                desired,
                expected=before,
                owner_uid=uid,
                owner_gid=gid,
            )
            _after, installed = _read_current_config(gwc_descriptor)
            if installed != desired:
                raise GeoWebCacheConfigInitError(
                    "GeoWebCache pre-start quota re-read differs"
                )
            return {
                "schema_version": 1,
                "mode": "apply",
                "path": str(
                    data_path / CONFIG_DIRECTORY_NAME / CONFIG_FILE_NAME
                ),
                "desired_sha256": desired_sha256,
                "current_sha256": desired_sha256,
                "filesystem_block_size_bytes": block_size,
                "physical_limit": asdict(physical),
                "created_directory": created_directory,
                "changed": True,
                "verified": True,
            }
        finally:
            os.close(gwc_descriptor)
    finally:
        os.close(root_descriptor)


def _open_or_create_gwc_directory(
    root_descriptor: int,
    *,
    apply: bool,
    owner_uid: int | None,
    owner_gid: int | None,
) -> tuple[int | None, bool]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        metadata = os.stat(
            CONFIG_DIRECTORY_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if not apply:
            return None, False
        if owner_uid is None or owner_gid is None:
            raise GeoWebCacheConfigInitError(
                "GeoWebCache configuration ownership is unavailable"
            )
        try:
            os.mkdir(CONFIG_DIRECTORY_NAME, 0o750, dir_fd=root_descriptor)
            descriptor = os.open(
                CONFIG_DIRECTORY_NAME,
                flags,
                dir_fd=root_descriptor,
            )
            os.fchown(descriptor, owner_uid, owner_gid)
            os.fchmod(descriptor, 0o750)
            os.fsync(root_descriptor)
            created = True
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise GeoWebCacheConfigInitError(
                "GeoWebCache configuration directory cannot be created safely"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise GeoWebCacheConfigInitError(
                "GeoWebCache configuration directory is invalid"
            )
        return descriptor, created
    except OSError as error:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache configuration directory cannot be inspected"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise GeoWebCacheConfigInitError(
            "GeoWebCache configuration directory must be a real directory"
        )
    try:
        descriptor = os.open(
            CONFIG_DIRECTORY_NAME,
            flags,
            dir_fd=root_descriptor,
        )
        opened = os.fstat(descriptor)
    except OSError as error:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache configuration directory cannot be opened safely"
        ) from error
    if not _same_node(metadata, opened):
        os.close(descriptor)
        raise GeoWebCacheConfigInitError(
            "GeoWebCache configuration directory changed"
        )
    return descriptor, created


def _read_current_config(
    directory_descriptor: int,
) -> tuple[os.stat_result | None, bytes | None]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        metadata = os.stat(
            CONFIG_FILE_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota cannot be inspected"
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 0 <= metadata.st_size <= MAX_CONFIG_BYTES
    ):
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota file is unsafe"
        )
    try:
        descriptor = os.open(
            CONFIG_FILE_NAME,
            flags,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        if not _same_node(metadata, opened):
            raise GeoWebCacheConfigInitError(
                "GeoWebCache pre-start quota changed"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as error:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota cannot be read safely"
        ) from error
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if (
        len(content) > MAX_CONFIG_BYTES
        or not _same_node(opened, after)
        or after.st_size != len(content)
    ):
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota changed"
        )
    return after, content


def _replace_config(
    directory_descriptor: int,
    content: bytes,
    *,
    expected: os.stat_result | None,
    owner_uid: int,
    owner_gid: int,
) -> None:
    temporary = f".{CONFIG_FILE_NAME}.tmp-{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("short GeoWebCache configuration write")
            view = view[written:]
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, 0o640)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _assert_target_unchanged(directory_descriptor, expected)
        os.replace(
            temporary,
            CONFIG_FILE_NAME,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except (OSError, GeoWebCacheConfigInitError) as error:
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if isinstance(error, GeoWebCacheConfigInitError):
            raise
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota cannot be replaced atomically"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_target_unchanged(
    directory_descriptor: int,
    expected: os.stat_result | None,
) -> None:
    try:
        current = os.stat(
            CONFIG_FILE_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if expected is None:
            return
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota disappeared"
        ) from None
    except OSError as error:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota cannot be revalidated"
        ) from error
    if expected is None or not _same_node(expected, current):
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota changed before replacement"
        )


def _same_node(first: os.stat_result, second: os.stat_result) -> bool:
    return all(
        getattr(first, field) == getattr(second, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_nlink",
        )
    )


def _existing_absolute_directory(path: Path, *, label: str) -> Path:
    normalized = Path(os.path.normpath(path))
    if not path.is_absolute() or path == Path("/") or normalized != path:
        raise GeoWebCacheConfigInitError(
            f"{label} must be an explicit absolute directory"
        )
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise GeoWebCacheConfigInitError(
                f"{label} cannot be inspected safely"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise GeoWebCacheConfigInitError(
                f"{label} cannot contain symlinks"
            )
    if not stat.S_ISDIR(metadata.st_mode):
        raise GeoWebCacheConfigInitError(f"{label} is not a directory")
    return path


def _owner_id(
    value: int | None,
    label: str,
    *,
    required: bool,
) -> int | None:
    if value is None and not required:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_OWNER_ID
    ):
        raise GeoWebCacheConfigInitError(f"invalid {label}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize the exact GeoWebCache disk-quota XML before Tomcat."
        )
    )
    parser.add_argument("--geoserver-data-path", type=Path, required=True)
    parser.add_argument("--cache-path", type=Path, required=True)
    parser.add_argument("--owner-uid", type=int)
    parser.add_argument("--owner-gid", type=int)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = initialize_quota_configuration(
            geoserver_data_path=args.geoserver_data_path,
            cache_path=args.cache_path,
            apply=args.apply,
            owner_uid=args.owner_uid,
            owner_gid=args.owner_gid,
        )
    except GeoWebCacheQuotaSafetyError as error:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache physical cache contract is unsafe"
        ) from error
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
