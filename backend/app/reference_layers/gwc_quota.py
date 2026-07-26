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
)

GIB = 1024**3


class GeoWebCacheQuotaSafetyError(ValueError):
    """The requested quota cannot be proven safe for the mounted cache."""


class _DiskUsage(Protocol):
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class GeoWebCacheCapacity:
    cache_path: str
    filesystem_total_bytes: int
    filesystem_used_bytes: int
    filesystem_free_bytes: int
    configured_quota_bytes: int
    required_free_reserve_bytes: int
    current_cache_bytes: int
    current_cache_files: int
    current_cache_directories: int
    remaining_quota_growth_bytes: int
    required_free_now_bytes: int
    capacity_margin_bytes: int
    current_free_margin_bytes: int
    growth_reserve_margin_bytes: int
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
    usage = _safe_disk_usage(path)
    capacity_margin = usage.total - quota_bytes - reserve_bytes
    current_free_margin = usage.free - reserve_bytes
    remaining_growth = max(quota_bytes - inventory.logical_bytes, 0)
    required_free_now = reserve_bytes + remaining_growth
    growth_reserve_margin = usage.free - required_free_now
    return GeoWebCacheCapacity(
        cache_path=str(path),
        filesystem_total_bytes=usage.total,
        filesystem_used_bytes=usage.used,
        filesystem_free_bytes=usage.free,
        configured_quota_bytes=quota_bytes,
        required_free_reserve_bytes=reserve_bytes,
        current_cache_bytes=inventory.logical_bytes,
        current_cache_files=inventory.file_count,
        current_cache_directories=inventory.directory_count,
        remaining_quota_growth_bytes=remaining_growth,
        required_free_now_bytes=required_free_now,
        capacity_margin_bytes=capacity_margin,
        current_free_margin_bytes=current_free_margin,
        growth_reserve_margin_bytes=growth_reserve_margin,
        safe_to_apply=(
            capacity_margin >= 0
            and current_free_margin >= 0
            and growth_reserve_margin >= 0
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

    Capacity is checked before any REST mutation.  Apply is accepted only when
    the filesystem can reserve both the operator free-space floor and every
    byte by which the measured cache can still grow before reaching its
    quota. ``configure_geowebcache_disk_quota`` then performs PUT + GET and
    rejects a server that does not persist the exact requested values.
    """

    capacity = cache_capacity_report(
        cache_path,
        quota_gib=configured.geowebcache_disk_quota_gib,
        min_free_gib=configured.geowebcache_disk_quota_min_free_gib,
    )
    admin = client or GeoServerAdminClient()
    before = admin.read_geowebcache_disk_quota()
    desired = {
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
    if not apply:
        return {
            "schema_version": 1,
            "mode": "dry-run",
            "capacity": asdict(capacity),
            "current": asdict(before),
            "desired": desired,
            "verified": _matches(before, desired),
        }
    if not capacity.safe_to_apply:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache quota cannot be applied: capacity cannot reserve "
            "the cache's remaining growth plus the free-space floor"
        )
    after = admin.configure_geowebcache_disk_quota(
        quota_gib=configured.geowebcache_disk_quota_gib,
        cleanup_seconds=configured.geowebcache_disk_quota_cleanup_seconds,
        expiration_policy=configured.geowebcache_disk_quota_policy,
    )
    if not _matches(after, desired):
        # The client also verifies this; keep the operator boundary
        # independently fail-closed if either implementation changes.
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache quota re-read does not match the requested state"
        )
    return {
        "schema_version": 1,
        "mode": "apply",
        "capacity": asdict(capacity),
        "before": asdict(before),
        "current": asdict(after),
        "desired": desired,
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


def _matches(
    actual: GeoWebCacheDiskQuota,
    desired: dict[str, object],
) -> bool:
    return all(getattr(actual, name) == value for name, value in desired.items())


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
                size_bytes=metadata.st_size if kind == "file" else 0,
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
                    visit(child_descriptor, child_relative, opened)
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
        visit(root_descriptor, ".", root)
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


def _safe_disk_usage(path: Path) -> _DiskUsage:
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
        usage = shutil.disk_usage(descriptor)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(after.st_mode)
            or not _same_cache_identity(before, after)
        ):
            raise GeoWebCacheQuotaSafetyError(
                "GeoWebCache filesystem changed during capacity measurement"
            )
        return usage
    except OSError as error:
        raise GeoWebCacheQuotaSafetyError(
            "GeoWebCache filesystem cannot be measured safely"
        ) from error
    finally:
        os.close(descriptor)


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
