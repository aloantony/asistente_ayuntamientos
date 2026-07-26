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
from typing import Sequence

from app.core.config import Settings, settings
from app.reference_layers.geoserver_admin import (
    GeoServerAdminClient,
    GeoWebCacheDiskQuota,
)

GIB = 1024**3


class GeoWebCacheQuotaSafetyError(ValueError):
    """The requested quota cannot be proven safe for the mounted cache."""


@dataclass(frozen=True)
class GeoWebCacheCapacity:
    cache_path: str
    filesystem_total_bytes: int
    filesystem_used_bytes: int
    filesystem_free_bytes: int
    configured_quota_bytes: int
    required_free_reserve_bytes: int
    capacity_margin_bytes: int
    current_free_margin_bytes: int
    safe_to_apply: bool


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
    usage = shutil.disk_usage(path)
    capacity_margin = usage.total - quota_bytes - reserve_bytes
    current_free_margin = usage.free - reserve_bytes
    return GeoWebCacheCapacity(
        cache_path=str(path),
        filesystem_total_bytes=usage.total,
        filesystem_used_bytes=usage.used,
        filesystem_free_bytes=usage.free,
        configured_quota_bytes=quota_bytes,
        required_free_reserve_bytes=reserve_bytes,
        capacity_margin_bytes=capacity_margin,
        current_free_margin_bytes=current_free_margin,
        safe_to_apply=capacity_margin >= 0 and current_free_margin >= 0,
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
    both the static quota-plus-reserve and current free-space margins are
    non-negative.  ``configure_geowebcache_disk_quota`` then performs PUT +
    GET and rejects a server that does not persist the exact requested values.
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
            "GeoWebCache quota cannot be applied: capacity or free-space "
            "reserve is insufficient"
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


def _existing_absolute_directory(path: Path) -> Path:
    if not path.is_absolute() or path == Path("/"):
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
