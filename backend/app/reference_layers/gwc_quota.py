"""Fail-closed operator command for the GeoWebCache disk quota.

The cache volume is reconstructible and is deliberately not part of disaster
recovery backups.  It still needs a hard ceiling: GeoWebCache ships with disk
quotas disabled, so this command checks volume capacity, applies the configured
global quota only with ``--apply``, and always re-reads the REST resource.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
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
    GeoWebCacheQuotaHealth,
    expected_geowebcache_tile_blob_store,
)

GIB = 1024**3
PHYSICAL_GROWTH_MIN_BASIS_POINTS = 10_000
CONSERVATIVE_LOGICAL_BYTES_PER_INODE = 4 * 1024
FUTURE_INODE_OVERHEAD_NUMERATOR = 11
FUTURE_INODE_OVERHEAD_DENOMINATOR = 10
MIN_FREE_CACHE_INODES = 100_000
EXPECTED_QUOTA_STORE_CLASS = (
    "org.geowebcache.diskquota.jdbc.JDBCQuotaStore"
)
EXPECTED_QUOTA_DIALECT_CLASS = (
    "org.geowebcache.diskquota.jdbc.HSQLDialect"
)
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
    f_blocks: int
    f_bfree: int
    f_bavail: int
    f_files: int
    f_favail: int


@dataclass(frozen=True)
class GeoWebCacheCapacity:
    cache_path: str
    cache_root_device: int
    cache_root_inode: int
    inventory_sha256: str
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
class GeoWebCachePhysicalLimit:
    cache_path: str
    cache_root_device: int
    cache_root_inode: int
    parent_device: int
    dedicated_mount: bool
    filesystem_total_bytes: int
    filesystem_used_bytes: int
    filesystem_free_bytes: int
    configured_hard_limit_bytes: int
    configured_soft_quota_bytes: int
    required_free_reserve_bytes: int
    configured_burst_margin_bytes: int
    required_minimum_total_bytes: int
    maximum_runtime_used_bytes: int
    hard_limit_margin_bytes: int
    minimum_total_margin_bytes: int
    free_reserve_margin_bytes: int
    burst_remaining_bytes: int
    safe: bool


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
    inventory_sha256 = hashlib.sha256(
        json.dumps(
            [asdict(entry) for entry in inventory.entries],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return GeoWebCacheCapacity(
        cache_path=str(path),
        cache_root_device=root_entry.device,
        cache_root_inode=root_entry.inode,
        inventory_sha256=inventory_sha256,
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


def physical_limit_status(
    cache_path: Path,
    *,
    configured: Settings = settings,
) -> GeoWebCachePhysicalLimit:
    """Prove the cache is an exact dedicated, physically bounded mount.

    Docker's ordinary local volumes share the host filesystem and provide no
    hard size ceiling. The accepted deployment therefore mounts an
    operator-provisioned filesystem at the cache root. Its statvfs capacity is
    checked against the configured ceiling on every gateway request.
    """

    path = _existing_absolute_directory(cache_path)
    hard_limit = _gib(
        configured.geowebcache_physical_hard_limit_gib,
        minimum=1,
        label="physical hard limit",
    )
    soft_quota = _gib(
        configured.geowebcache_disk_quota_gib,
        minimum=1,
        label="quota",
    )
    free_reserve = _gib(
        configured.geowebcache_disk_quota_min_free_gib,
        minimum=0,
        label="free reserve",
    )
    burst_margin = _gib(
        configured.geowebcache_physical_burst_margin_gib,
        minimum=0,
        label="physical burst margin",
    )
    required_minimum = soft_quota + free_reserve + burst_margin
    if required_minimum > hard_limit:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache hard limit is smaller than quota, reserve and burst"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    root_descriptor: int | None = None
    parent_descriptor: int | None = None
    try:
        root_descriptor = os.open(path, flags)
        parent_descriptor = os.open(path.parent, flags)
        root_before = os.fstat(root_descriptor)
        parent = os.fstat(parent_descriptor)
        filesystem = os.fstatvfs(root_descriptor)
        root_after = os.fstat(root_descriptor)
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache physical limit cannot be measured safely"
        ) from error
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)

    if (
        not stat.S_ISDIR(root_before.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or not _same_cache_identity(root_before, root_after)
        or filesystem.f_frsize <= 0
        or filesystem.f_blocks <= 0
        or filesystem.f_bfree < 0
        or filesystem.f_bavail < 0
        or filesystem.f_bfree > filesystem.f_blocks
        or filesystem.f_bavail > filesystem.f_bfree
    ):
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache physical limit metadata is invalid"
        )
    fragment = filesystem.f_frsize
    total = filesystem.f_blocks * fragment
    used = (filesystem.f_blocks - filesystem.f_bfree) * fragment
    free = filesystem.f_bavail * fragment
    dedicated_mount = root_before.st_dev != parent.st_dev
    maximum_runtime_used = soft_quota + burst_margin
    hard_margin = hard_limit - total
    minimum_total_margin = total - required_minimum
    reserve_margin = free - free_reserve
    burst_remaining = maximum_runtime_used - used
    safe = (
        dedicated_mount
        and hard_margin >= 0
        and minimum_total_margin >= 0
        and reserve_margin >= 0
        and burst_remaining >= 0
    )
    return GeoWebCachePhysicalLimit(
        cache_path=str(path),
        cache_root_device=root_before.st_dev,
        cache_root_inode=root_before.st_ino,
        parent_device=parent.st_dev,
        dedicated_mount=dedicated_mount,
        filesystem_total_bytes=total,
        filesystem_used_bytes=used,
        filesystem_free_bytes=free,
        configured_hard_limit_bytes=hard_limit,
        configured_soft_quota_bytes=soft_quota,
        required_free_reserve_bytes=free_reserve,
        configured_burst_margin_bytes=burst_margin,
        required_minimum_total_bytes=required_minimum,
        maximum_runtime_used_bytes=maximum_runtime_used,
        hard_limit_margin_bytes=hard_margin,
        minimum_total_margin_bytes=minimum_total_margin,
        free_reserve_margin_bytes=reserve_margin,
        burst_remaining_bytes=burst_remaining,
        safe=safe,
    )


def quota_status(
    *,
    cache_path: Path,
    configured: Settings = settings,
    apply: bool = False,
    allow_block_size_migration: bool = False,
    client: GeoServerAdminClient | None = None,
) -> dict[str, object]:
    """Verify the pre-start quota and optionally configure the FileBlobStore.

    The quota itself is never mutated through REST: GeoServer 3.0.0 does not
    reliably persist that PUT. A separate one-shot writes the exact XML before
    Tomcat starts. Apply mode is therefore limited to the FileBlobStore and
    re-reads the quota plus the real HSQL monitor health afterwards.
    """

    if not isinstance(allow_block_size_migration, bool):
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache block-size migration mode is invalid"
        )
    physical = physical_limit_status(cache_path, configured=configured)
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
    before_health = admin.read_geowebcache_quota_health()
    desired_quota = _desired_quota(configured)
    blob_store_verified = _blob_store_matches(
        before_blob_stores,
        desired_blob_store,
    )
    quota_verified = _quota_matches(before_quota, desired_quota)
    health_verified = _quota_health_matches(before_health)
    block_size_migration_required = _block_size_migration_required(
        before_blob_stores,
        desired_blob_store,
    )
    block_size_migration_permitted = (
        allow_block_size_migration
        and block_size_migration_required
        and _cache_is_empty(capacity)
    )
    if not apply:
        return {
            "schema_version": 4,
            "mode": "dry-run",
            "physical_limit": asdict(physical),
            "capacity": asdict(capacity),
            "blob_store": {
                "current": [
                    asdict(store) for store in before_blob_stores
                ],
                "desired": asdict(desired_blob_store),
                "block_size_migration_required": (
                    block_size_migration_required
                ),
                "block_size_migration_permitted": (
                    block_size_migration_permitted
                ),
                "block_size_migrated": False,
                "verified": blob_store_verified,
            },
            "disk_quota": {
                "current": asdict(before_quota),
                "desired": desired_quota,
                "verified": quota_verified,
            },
            "quota_store_health": {
                "current": asdict(before_health),
                "verified": health_verified,
            },
            "verified": (
                physical.safe
                and capacity.safe_to_apply
                and blob_store_verified
                and quota_verified
                and health_verified
            ),
        }
    apply_physical = physical_limit_status(
        cache_path,
        configured=configured,
    )
    apply_capacity = cache_capacity_report(
        cache_path,
        quota_gib=configured.geowebcache_disk_quota_gib,
        min_free_gib=configured.geowebcache_disk_quota_min_free_gib,
    )
    if (
        not _same_physical_identity(physical, apply_physical)
        or not _same_capacity_identity(capacity, apply_capacity)
    ):
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache cache identity or inventory changed before bootstrap"
        )
    capacity = apply_capacity
    physical = apply_physical
    if not physical.safe:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache dedicated physical hard limit is not safe"
        )
    if not capacity.safe_to_apply:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache quota cannot be applied: capacity cannot reserve "
            "physical cache growth, inodes and the free-space floor"
        )
    if not quota_verified:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache pre-start disk quota does not match"
        )
    if not health_verified:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache real quota store or monitor is unhealthy"
        )
    after_blob_store = admin.ensure_geowebcache_tile_blob_store(
        file_system_block_size=capacity.filesystem_block_size_bytes,
        allow_file_system_block_size_migration=(
            block_size_migration_permitted
        ),
    )
    if after_blob_store != desired_blob_store:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache tile blob store re-read does not match the "
            "requested state"
        )
    after_quota = admin.read_geowebcache_disk_quota()
    if not _quota_matches(after_quota, desired_quota):
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache pre-start quota changed during bootstrap"
        )
    after_health = admin.read_geowebcache_quota_health()
    if not _quota_health_matches(after_health):
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache real quota store failed during bootstrap"
        )
    return {
        "schema_version": 4,
        "mode": "apply",
        "physical_limit": asdict(physical),
        "capacity": asdict(capacity),
        "blob_store": {
            "before": [asdict(store) for store in before_blob_stores],
            "current": asdict(after_blob_store),
            "desired": asdict(desired_blob_store),
            "block_size_migration_required": (
                block_size_migration_required
            ),
            "block_size_migration_permitted": (
                block_size_migration_permitted
            ),
            "block_size_migrated": (
                block_size_migration_required
                and block_size_migration_permitted
            ),
            "verified": True,
        },
        "disk_quota": {
            "before": asdict(before_quota),
            "current": asdict(after_quota),
            "desired": desired_quota,
            "verified": True,
        },
        "quota_store_health": {
            "before": asdict(before_health),
            "current": asdict(after_health),
            "verified": True,
        },
        "verified": True,
    }


def runtime_contract_status(
    *,
    cache_path: Path,
    configured: Settings = settings,
    client: GeoServerAdminClient | None = None,
) -> dict[str, object]:
    """Actively verify the current serving contract without mutating it."""

    admin = client or GeoServerAdminClient()
    health = admin.health()
    physical = physical_limit_status(cache_path, configured=configured)
    if not physical.safe:
        raise GeoWebCacheQuotaSafetyError(
            "active GeoWebCache physical hard limit is not safe"
        )
    block_size = cache_filesystem_block_size(cache_path)
    desired_blob_store = expected_geowebcache_tile_blob_store(
        file_system_block_size=block_size,
    )
    stores = admin.read_geowebcache_file_blob_stores()
    desired_quota = _desired_quota(configured)
    current_quota = admin.read_geowebcache_disk_quota()
    quota_health = admin.read_geowebcache_quota_health()
    if not _blob_store_matches(stores, desired_blob_store):
        raise GeoWebCacheQuotaSafetyError(
            "active GeoWebCache FileBlobStore contract does not match"
        )
    if not _quota_matches(current_quota, desired_quota):
        raise GeoWebCacheQuotaSafetyError(
            "active GeoWebCache disk-quota contract does not match"
        )
    if not _quota_health_matches(quota_health):
        raise GeoWebCacheQuotaSafetyError(
            "active GeoWebCache quota store or monitor is unhealthy"
        )
    return {
        "schema_version": 2,
        "mode": "active-contract",
        "geoserver": asdict(health),
        "physical_limit": asdict(physical),
        "filesystem_block_size_bytes": block_size,
        "blob_store": asdict(desired_blob_store),
        "disk_quota": desired_quota,
        "quota_store_health": asdict(quota_health),
        "verified": True,
    }


def cache_filesystem_block_size(cache_path: Path) -> int:
    """Read the stable block size of the exact mounted cache root."""

    path = _existing_absolute_directory(cache_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache filesystem block size cannot be measured safely"
        ) from error
    try:
        before = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(before.st_mode)
            or not _same_cache_identity(before, after)
            or filesystem.f_bsize <= 0
            or filesystem.f_frsize <= 0
        ):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache filesystem block size is unstable"
            )
        block_size = max(filesystem.f_bsize, filesystem.f_frsize)
        try:
            expected_geowebcache_tile_blob_store(
                file_system_block_size=block_size,
            )
        except ValueError as error:
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache filesystem block size is unsupported"
            ) from error
        return block_size
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache filesystem block size cannot be measured safely"
        ) from error
    finally:
        os.close(descriptor)


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


def _desired_quota(configured: Settings) -> dict[str, object]:
    return {
        "enabled": True,
        "quota_bytes": configured.geowebcache_disk_quota_gib * GIB,
        "quota_value": configured.geowebcache_disk_quota_gib,
        "quota_units": "GiB",
        "cleanup_frequency": (
            configured.geowebcache_disk_quota_cleanup_seconds
        ),
        "cleanup_units": "SECONDS",
        "max_concurrent_cleanups": 2,
        "expiration_policy": configured.geowebcache_disk_quota_policy,
    }


def _quota_health_matches(actual: GeoWebCacheQuotaHealth) -> bool:
    return (
        actual.healthy
        and actual.monitor_enabled
        and actual.monitor_running
        and actual.scheduled_cleanup_active
        and not actual.provider_error
        and actual.store_class == EXPECTED_QUOTA_STORE_CLASS
        and actual.dialect_class == EXPECTED_QUOTA_DIALECT_CLASS
        and actual.global_used_bytes is not None
        and actual.global_used_bytes >= 0
    )


def _same_capacity_identity(
    first: GeoWebCacheCapacity,
    second: GeoWebCacheCapacity,
) -> bool:
    """Compare stable mount/inventory facts, not live free-space metrics."""

    return all(
        getattr(first, field) == getattr(second, field)
        for field in (
            "cache_path",
            "cache_root_device",
            "cache_root_inode",
            "inventory_sha256",
            "filesystem_block_size_bytes",
            "current_cache_bytes",
            "current_cache_allocated_bytes",
            "current_cache_files",
            "current_cache_directories",
            "current_cache_inodes",
            "configured_quota_bytes",
            "required_free_reserve_bytes",
        )
    )


def _same_physical_identity(
    first: GeoWebCachePhysicalLimit,
    second: GeoWebCachePhysicalLimit,
) -> bool:
    return all(
        getattr(first, field) == getattr(second, field)
        for field in (
            "cache_path",
            "cache_root_device",
            "cache_root_inode",
            "parent_device",
            "dedicated_mount",
            "filesystem_total_bytes",
            "configured_hard_limit_bytes",
            "configured_soft_quota_bytes",
            "required_free_reserve_bytes",
            "configured_burst_margin_bytes",
        )
    )


def _blob_store_matches(
    stores: tuple[GeoWebCacheFileBlobStore, ...],
    desired: GeoWebCacheFileBlobStore,
) -> bool:
    defaults = tuple(store for store in stores if store.default)
    return stores == (desired,) and defaults == (desired,)


def _block_size_migration_required(
    stores: tuple[GeoWebCacheFileBlobStore, ...],
    desired: GeoWebCacheFileBlobStore,
) -> bool:
    if len(stores) != 1:
        return False
    current = stores[0]
    return (
        current.file_system_block_size != desired.file_system_block_size
        and all(
            getattr(current, name) == getattr(desired, name)
            for name in (
                "id",
                "enabled",
                "default",
                "base_directory",
                "path_generator_type",
            )
        )
    )


def _cache_is_empty(capacity: GeoWebCacheCapacity) -> bool:
    return (
        capacity.current_cache_files == 0
        and capacity.current_cache_directories == 0
        and capacity.current_cache_bytes == 0
    )


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
