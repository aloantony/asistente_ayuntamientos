"""Fail-closed operator command for the GeoWebCache disk quota.

The cache volume is reconstructible and is deliberately not part of disaster
recovery backups.  It still needs a hard ceiling: GeoWebCache ships with disk
quotas disabled, so this command checks volume capacity, applies the configured
global quota only with ``--apply``, and always re-reads the REST resource.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Protocol, Sequence

from app.core.config import Settings, settings
from app.reference_layers.geoserver_admin import (
    GeoServerAdminClient,
    GeoWebCacheDiskQuota,
    GeoWebCacheFileBlobStore,
    expected_geowebcache_tile_blob_store,
)

GIB = 1024**3
PHYSICAL_GROWTH_MIN_BASIS_POINTS = 12_500
CONSERVATIVE_LOGICAL_BYTES_PER_INODE = 4 * 1024
FUTURE_INODE_OVERHEAD_NUMERATOR = 11
FUTURE_INODE_OVERHEAD_DENOMINATOR = 10
MIN_FREE_CACHE_INODES = 100_000
LEGACY_GWC_CONFIGURATION_NAMES = frozenset(
    {
        "geowebcache.xml",
        "geowebcache-diskquota.xml",
        "gwc-gs.xml",
        "gwc-layers",
    }
)


class GeoWebCacheQuotaSafetyError(ValueError):
    """The requested quota cannot be proven safe for the mounted cache."""


class _DiskUsage(Protocol):
    total: int
    used: int
    free: int


class _FileSystemStats(Protocol):
    f_bsize: int
    f_frsize: int
    f_files: int
    f_favail: int


@dataclass(frozen=True)
class GeoWebCacheCapacity:
    cache_path: str
    filesystem_total_bytes: int
    filesystem_used_bytes: int
    filesystem_free_bytes: int
    filesystem_block_size_bytes: int
    filesystem_total_inodes: int
    filesystem_free_inodes: int
    configured_quota_bytes: int
    required_free_reserve_bytes: int
    current_cache_bytes: int
    current_cache_allocated_bytes: int
    current_cache_files: int
    current_cache_directories: int
    current_cache_inodes: int
    remaining_quota_growth_bytes: int
    physical_growth_safety_basis_points: int
    required_physical_growth_bytes: int
    conservative_logical_bytes_per_inode: int
    required_future_inodes: int
    required_free_inode_reserve: int
    required_free_now_bytes: int
    capacity_margin_bytes: int
    current_free_margin_bytes: int
    growth_reserve_margin_bytes: int
    inode_reserve_margin: int
    safe_to_apply: bool


@dataclass(frozen=True)
class _CacheEntry:
    path: str
    kind: str
    size_bytes: int
    allocated_bytes: int
    device: int
    inode: int
    mode: int
    mtime_ns: int
    ctime_ns: int
    links: int


@dataclass(frozen=True)
class _CacheInventory:
    entries: tuple[_CacheEntry, ...]

    @property
    def file_count(self) -> int:
        return sum(item.kind == "file" for item in self.entries)

    @property
    def directory_count(self) -> int:
        return sum(
            item.kind == "directory" and item.path != "."
            for item in self.entries
        )

    @property
    def logical_bytes(self) -> int:
        return sum(
            item.size_bytes for item in self.entries if item.kind == "file"
        )

    @property
    def allocated_file_bytes(self) -> int:
        return sum(
            item.allocated_bytes
            for item in self.entries
            if item.kind == "file"
        )

    @property
    def allocated_bytes(self) -> int:
        return sum(item.allocated_bytes for item in self.entries)

    @property
    def inode_count(self) -> int:
        return len(self.entries)


def cache_capacity_report(
    cache_path: Path,
    *,
    quota_gib: int,
    min_free_gib: int,
) -> GeoWebCacheCapacity:
    """Measure the exact mounted filesystem without following symlinks."""

    path = _existing_absolute_directory(cache_path)
    quota_bytes = _gib(quota_gib, minimum=1, label="quota")
    reserve_bytes = _gib(min_free_gib, minimum=0, label="free reserve")
    inventory = _stable_cache_inventory(path)
    root_entry = inventory.entries[0]
    usage, filesystem = _safe_filesystem_usage(
        path,
        expected_root=root_entry,
    )
    physical_growth_basis_points = max(
        PHYSICAL_GROWTH_MIN_BASIS_POINTS,
        _allocation_basis_points(inventory),
    )
    remaining_growth = max(quota_bytes - inventory.logical_bytes, 0)
    required_physical_growth = _ceil_ratio(
        remaining_growth * physical_growth_basis_points,
        10_000,
    )
    logical_bytes_per_inode = _conservative_bytes_per_inode(inventory)
    future_file_inodes = _ceil_ratio(
        remaining_growth,
        logical_bytes_per_inode,
    )
    required_future_inodes = _ceil_ratio(
        future_file_inodes * FUTURE_INODE_OVERHEAD_NUMERATOR,
        FUTURE_INODE_OVERHEAD_DENOMINATOR,
    )
    required_free_inodes = MIN_FREE_CACHE_INODES + required_future_inodes
    projected_cache_allocation = (
        inventory.allocated_bytes + required_physical_growth
    )
    capacity_margin = (
        usage.total - projected_cache_allocation - reserve_bytes
    )
    current_free_margin = usage.free - reserve_bytes
    required_free_now = reserve_bytes + required_physical_growth
    growth_reserve_margin = usage.free - required_free_now
    inode_reserve_margin = filesystem.f_favail - required_free_inodes
    return GeoWebCacheCapacity(
        cache_path=str(path),
        filesystem_total_bytes=usage.total,
        filesystem_used_bytes=usage.used,
        filesystem_free_bytes=usage.free,
        filesystem_block_size_bytes=max(
            filesystem.f_frsize,
            filesystem.f_bsize,
        ),
        filesystem_total_inodes=filesystem.f_files,
        filesystem_free_inodes=filesystem.f_favail,
        configured_quota_bytes=quota_bytes,
        required_free_reserve_bytes=reserve_bytes,
        current_cache_bytes=inventory.logical_bytes,
        current_cache_allocated_bytes=inventory.allocated_bytes,
        current_cache_files=inventory.file_count,
        current_cache_directories=inventory.directory_count,
        current_cache_inodes=inventory.inode_count,
        remaining_quota_growth_bytes=remaining_growth,
        physical_growth_safety_basis_points=physical_growth_basis_points,
        required_physical_growth_bytes=required_physical_growth,
        conservative_logical_bytes_per_inode=logical_bytes_per_inode,
        required_future_inodes=required_future_inodes,
        required_free_inode_reserve=required_free_inodes,
        required_free_now_bytes=required_free_now,
        capacity_margin_bytes=capacity_margin,
        current_free_margin_bytes=current_free_margin,
        growth_reserve_margin_bytes=growth_reserve_margin,
        inode_reserve_margin=inode_reserve_margin,
        safe_to_apply=(
            capacity_margin >= 0
            and current_free_margin >= 0
            and growth_reserve_margin >= 0
            and inode_reserve_margin >= 0
        ),
    )


def quota_status(
    *,
    cache_path: Path,
    configured: Settings = settings,
    apply: bool = False,
    client: GeoServerAdminClient | None = None,
) -> dict[str, object]:
    """Return current/desired quota state and optionally apply it.

    Capacity is checked before any REST mutation. Apply is accepted only when
    the filesystem can reserve both the operator free-space floor and every
    byte by which the measured cache can still grow before reaching its quota.
    The exact default FileBlobStore is then created/revalidated before disk
    quota is configured. Both resources are re-read by the closed client.
    """

    capacity = cache_capacity_report(
        cache_path,
        quota_gib=configured.geowebcache_disk_quota_gib,
        min_free_gib=configured.geowebcache_disk_quota_min_free_gib,
    )
    admin = client or GeoServerAdminClient()
    desired_blob_store = expected_geowebcache_tile_blob_store(
        file_system_block_size=capacity.filesystem_block_size_bytes,
    )
    before_blob_stores = admin.read_geowebcache_file_blob_stores()
    before_quota = admin.read_geowebcache_disk_quota()
    desired_quota = {
        "enabled": True,
        "quota_bytes": configured.geowebcache_disk_quota_gib * GIB,
        "quota_value": configured.geowebcache_disk_quota_gib,
        "quota_units": "GiB",
        "cleanup_frequency": (
            configured.geowebcache_disk_quota_cleanup_seconds
        ),
        "cleanup_units": "SECONDS",
        "expiration_policy": configured.geowebcache_disk_quota_policy,
    }
    blob_store_verified = _blob_store_matches(
        before_blob_stores,
        desired_blob_store,
    )
    quota_verified = _quota_matches(before_quota, desired_quota)
    if not apply:
        return {
            "schema_version": 2,
            "mode": "dry-run",
            "capacity": asdict(capacity),
            "blob_store": {
                "current": [
                    asdict(store) for store in before_blob_stores
                ],
                "desired": asdict(desired_blob_store),
                "verified": blob_store_verified,
            },
            "disk_quota": {
                "current": asdict(before_quota),
                "desired": desired_quota,
                "verified": quota_verified,
            },
            "verified": blob_store_verified and quota_verified,
        }
    apply_capacity = cache_capacity_report(
        cache_path,
        quota_gib=configured.geowebcache_disk_quota_gib,
        min_free_gib=configured.geowebcache_disk_quota_min_free_gib,
    )
    if apply_capacity != capacity:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache or filesystem changed before quota application"
        )
    capacity = apply_capacity
    if not capacity.safe_to_apply:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache quota cannot be applied: capacity cannot reserve "
            "physical cache growth, inodes and the free-space floor"
        )
    after_blob_store = admin.ensure_geowebcache_tile_blob_store(
        file_system_block_size=capacity.filesystem_block_size_bytes,
    )
    if after_blob_store != desired_blob_store:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache tile blob store re-read does not match the "
            "requested state"
        )
    after_quota = admin.configure_geowebcache_disk_quota(
        quota_gib=configured.geowebcache_disk_quota_gib,
        cleanup_seconds=configured.geowebcache_disk_quota_cleanup_seconds,
        expiration_policy=configured.geowebcache_disk_quota_policy,
    )
    if not _quota_matches(after_quota, desired_quota):
        # The client also verifies this; keep the operator boundary
        # independently fail-closed if either implementation changes.
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache quota re-read does not match the requested state"
        )
    return {
        "schema_version": 2,
        "mode": "apply",
        "capacity": asdict(capacity),
        "blob_store": {
            "before": [asdict(store) for store in before_blob_stores],
            "current": asdict(after_blob_store),
            "desired": asdict(desired_blob_store),
            "verified": True,
        },
        "disk_quota": {
            "before": asdict(before_quota),
            "current": asdict(after_quota),
            "desired": desired_quota,
            "verified": True,
        },
        "verified": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or configure the fixed local GeoWebCache disk quota. "
            "The default is read-only dry-run."
        )
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        required=True,
        help="absolute read-only mount of the GeoWebCache volume",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the configured quota and verify it by re-reading GeoServer",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = quota_status(cache_path=args.cache_path, apply=args.apply)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


def _quota_matches(
    actual: GeoWebCacheDiskQuota,
    desired: dict[str, object],
) -> bool:
    return all(getattr(actual, name) == value for name, value in desired.items())


def _blob_store_matches(
    stores: tuple[GeoWebCacheFileBlobStore, ...],
    desired: GeoWebCacheFileBlobStore,
) -> bool:
    defaults = tuple(store for store in stores if store.default)
    return stores == (desired,) and defaults == (desired,)


def _gib(value: int, *, minimum: int, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 1024
    ):
        raise GeoWebCacheQuotaSafetyError(
            f"GeoWebCache {label} must be between {minimum} and 1024 GiB"
        )
    return value * GIB


def _stable_cache_inventory(path: Path) -> _CacheInventory:
    """Measure cache bytes twice and reject ambiguous filesystem entries."""

    first = _cache_inventory(path)
    second = _cache_inventory(path)
    if first != second:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache changed while capacity was measured"
        )
    return first


def _cache_inventory(path: Path) -> _CacheInventory:
    entries: list[_CacheEntry] = []
    directory_flags = os.O_RDONLY
    file_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW

    def record(metadata: os.stat_result, *, relative: str, kind: str) -> None:
        permissions = stat.S_IMODE(metadata.st_mode)
        if permissions & ~0o777:
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache contains special permission bits"
            )
        allocated = metadata.st_blocks * 512
        if allocated < 0:
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache allocation cannot be measured"
            )
        if kind == "file":
            if metadata.st_nlink != 1:
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache cannot contain hard-linked files"
                )
            # Sparse files could consume future filesystem blocks without
            # increasing the quota-accounted logical size, so they make the
            # remaining-growth calculation unsafe.
            if allocated < metadata.st_size:
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache cannot contain sparse files"
                )
        entries.append(
            _CacheEntry(
                path=relative,
                kind=kind,
                size_bytes=metadata.st_size,
                allocated_bytes=allocated,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                mode=permissions,
                mtime_ns=metadata.st_mtime_ns,
                ctime_ns=metadata.st_ctime_ns,
                links=metadata.st_nlink,
            )
        )

    def visit(
        directory_descriptor: int,
        relative: str,
        expected: os.stat_result,
        root_device: int,
    ) -> None:
        try:
            before = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(before.st_mode)
                or not _same_cache_identity(expected, before)
            ):
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache changed during inventory"
                )
            with os.scandir(directory_descriptor) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache cannot be inventoried"
            ) from error
        for child in children:
            child_relative = (
                child.name if relative == "." else f"{relative}/{child.name}"
            )
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache changed during inventory"
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache cannot contain symlinks"
                )
            if metadata.st_dev != root_device:
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache cannot cross filesystem boundaries"
                )
            if (
                relative == "."
                and child.name in LEGACY_GWC_CONFIGURATION_NAMES
            ):
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache tile volume contains legacy configuration"
                )
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child_descriptor = os.open(
                        child.name,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise GeoWebCacheQuotaSafetyError(
                        "GeoWebCache cache changed during inventory"
                    ) from error
                try:
                    opened = os.fstat(child_descriptor)
                    if not _same_cache_identity(metadata, opened):
                        raise GeoWebCacheQuotaSafetyError(
                            "GeoWebCache cache changed during inventory"
                        )
                    record(
                        opened,
                        relative=child_relative,
                        kind="directory",
                    )
                    visit(
                        child_descriptor,
                        child_relative,
                        opened,
                        root_device,
                    )
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache can contain only directories and "
                    "regular files"
                )
            try:
                child_descriptor = os.open(
                    child.name,
                    file_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as error:
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache cache changed during inventory"
                ) from error
            try:
                opened = os.fstat(child_descriptor)
                if not _same_cache_identity(metadata, opened):
                    raise GeoWebCacheQuotaSafetyError(
                        "GeoWebCache cache changed during inventory"
                    )
                record(opened, relative=child_relative, kind="file")
            finally:
                os.close(child_descriptor)
        after = os.fstat(directory_descriptor)
        if not _same_cache_identity(before, after):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache changed during inventory"
            )

    try:
        root_descriptor = os.open(path, directory_flags)
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache cannot be inspected"
        ) from error
    try:
        root = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root.st_mode):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache is not a directory"
            )
        record(root, relative=".", kind="directory")
        visit(root_descriptor, ".", root, root.st_dev)
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache changed during inventory"
        ) from error
    finally:
        os.close(root_descriptor)
    return _CacheInventory(entries=tuple(entries))


def _same_cache_identity(
    expected: os.stat_result,
    actual: os.stat_result,
) -> bool:
    return all(
        getattr(expected, field) == getattr(actual, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_nlink",
            "st_blocks",
        )
    )


def _safe_filesystem_usage(
    path: Path,
    *,
    expected_root: _CacheEntry,
) -> tuple[_DiskUsage, _FileSystemStats]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache filesystem cannot be measured safely"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not _same_cache_entry(expected_root, before):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache root changed before capacity measurement"
            )
        usage = shutil.disk_usage(descriptor)
        filesystem = os.fstatvfs(descriptor)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(after.st_mode)
            or not _same_cache_identity(before, after)
        ):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache filesystem changed during capacity measurement"
            )
        if (
            filesystem.f_bsize <= 0
            or filesystem.f_frsize <= 0
            or filesystem.f_files <= 0
            or filesystem.f_favail < 0
            or filesystem.f_favail > filesystem.f_files
        ):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache filesystem inode capacity is unavailable"
            )
        return usage, filesystem
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache filesystem cannot be measured safely"
        ) from error
    finally:
        os.close(descriptor)


def _same_cache_entry(
    expected: _CacheEntry,
    actual: os.stat_result,
) -> bool:
    return (
        expected.device == actual.st_dev
        and expected.inode == actual.st_ino
        and expected.mode == stat.S_IMODE(actual.st_mode)
        and expected.size_bytes == actual.st_size
        and expected.mtime_ns == actual.st_mtime_ns
        and expected.ctime_ns == actual.st_ctime_ns
        and expected.links == actual.st_nlink
        and expected.allocated_bytes == actual.st_blocks * 512
    )


def _allocation_basis_points(inventory: _CacheInventory) -> int:
    logical = inventory.logical_bytes
    if logical == 0:
        return 10_000
    return _ceil_ratio(inventory.allocated_file_bytes * 10_000, logical)


def _conservative_bytes_per_inode(inventory: _CacheInventory) -> int:
    if inventory.file_count == 0 or inventory.logical_bytes == 0:
        return CONSERVATIVE_LOGICAL_BYTES_PER_INODE
    observed = max(inventory.logical_bytes // inventory.file_count, 1)
    return min(CONSERVATIVE_LOGICAL_BYTES_PER_INODE, observed)


def _ceil_ratio(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache capacity ratio is invalid"
        )
    return (numerator + denominator - 1) // denominator


def _existing_absolute_directory(path: Path) -> Path:
    normalized = Path(os.path.normpath(path))
    if (
        not path.is_absolute()
        or path == Path("/")
        or normalized != path
    ):
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache path must be an explicit absolute directory"
        )
    _reject_symlink_components(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache path is unavailable"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache path must be a non-symlink directory"
        )
    return path


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as error:
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache path cannot be inspected safely"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache cache path cannot contain symlinks"
            )


if __name__ == "__main__":
    raise SystemExit(main())
