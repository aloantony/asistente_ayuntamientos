import os
import stat
from dataclasses import replace
from pathlib import Path
from shutil import _ntuple_diskusage
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.reference_layers import gwc_quota as quota_module
from app.reference_layers.geoserver_admin import (
    GeoServerHealth,
    GeoWebCacheDiskQuota,
    GeoWebCacheFileBlobStore,
    GeoWebCacheQuotaHealth,
    expected_geowebcache_tile_blob_store,
)
from app.reference_layers.gwc_quota import (
    GeoWebCachePhysicalLimit,
    GeoWebCacheQuotaSafetyError,
    build_parser,
    cache_capacity_report,
    cache_filesystem_block_size,
    physical_limit_status,
    quota_status,
    runtime_contract_status,
)

GIB = 1024**3


def quota(
    *,
    enabled: bool = True,
    quota_gib: int = 20,
    cleanup_seconds: int = 10,
    policy: str = "LRU",
) -> GeoWebCacheDiskQuota:
    return GeoWebCacheDiskQuota(
        enabled=enabled,
        quota_bytes=quota_gib * GIB,
        quota_value=quota_gib,
        quota_units="GiB",
        cleanup_frequency=cleanup_seconds,
        cleanup_units="SECONDS",
        max_concurrent_cleanups=2,
        expiration_policy=policy,  # type: ignore[arg-type]
    )


def quota_health(
    *,
    healthy: bool = True,
    monitor_enabled: bool = True,
    monitor_running: bool = True,
    scheduled_cleanup_active: bool = True,
    provider_error: bool = False,
    store_class: str | None = quota_module.EXPECTED_QUOTA_STORE_CLASS,
    dialect_class: str | None = quota_module.EXPECTED_QUOTA_DIALECT_CLASS,
    global_used_bytes: int | None = 0,
) -> GeoWebCacheQuotaHealth:
    return GeoWebCacheQuotaHealth(
        healthy=healthy,
        monitor_enabled=monitor_enabled,
        monitor_running=monitor_running,
        scheduled_cleanup_active=scheduled_cleanup_active,
        provider_error=provider_error,
        store_class=store_class,
        dialect_class=dialect_class,
        global_used_bytes=global_used_bytes,
    )


def safe_physical_limit(cache: Path) -> GeoWebCachePhysicalLimit:
    return GeoWebCachePhysicalLimit(
        cache_path=str(cache),
        cache_root_device=2,
        cache_root_inode=3,
        parent_device=1,
        dedicated_mount=True,
        filesystem_total_bytes=28 * GIB,
        filesystem_used_bytes=0,
        filesystem_free_bytes=28 * GIB,
        configured_hard_limit_bytes=28 * GIB,
        configured_soft_quota_bytes=20 * GIB,
        required_free_reserve_bytes=5 * GIB,
        configured_burst_margin_bytes=2 * GIB,
        required_minimum_total_bytes=27 * GIB,
        maximum_runtime_used_bytes=22 * GIB,
        hard_limit_margin_bytes=0,
        minimum_total_margin_bytes=GIB,
        free_reserve_margin_bytes=23 * GIB,
        burst_remaining_bytes=22 * GIB,
        safe=True,
    )


def patch_safe_physical_limit(
    monkeypatch: pytest.MonkeyPatch,
    cache: Path,
) -> None:
    expected = safe_physical_limit(cache)
    monkeypatch.setattr(
        quota_module,
        "physical_limit_status",
        lambda _path, *, configured: expected,
    )


class FakeAdmin:
    def __init__(
        self,
        *,
        before: GeoWebCacheDiskQuota,
        after: GeoWebCacheDiskQuota | None = None,
        before_health: GeoWebCacheQuotaHealth | None = None,
        after_health: GeoWebCacheQuotaHealth | None = None,
        before_blob_stores: tuple[GeoWebCacheFileBlobStore, ...] = (),
        after_blob_store: GeoWebCacheFileBlobStore | None = None,
    ) -> None:
        self.before = before
        self.after = after or before
        self.before_health = before_health or quota_health()
        self.after_health = after_health or self.before_health
        self.before_blob_stores = before_blob_stores
        self.after_blob_store = after_blob_store
        self.read_calls = 0
        self.read_health_calls = 0
        self.read_blob_store_calls = 0
        self.ensure_blob_store_calls: list[int] = []
        self.ensure_block_size_migration_calls: list[bool] = []
        self.call_order: list[str] = []

    def health(self) -> GeoServerHealth:
        self.call_order.append("health")
        return GeoServerHealth(version="3.0.0")

    def read_geowebcache_file_blob_stores(
        self,
    ) -> tuple[GeoWebCacheFileBlobStore, ...]:
        self.call_order.append("read_blob_stores")
        self.read_blob_store_calls += 1
        return self.before_blob_stores

    def read_geowebcache_disk_quota(self) -> GeoWebCacheDiskQuota:
        self.call_order.append("read_quota")
        self.read_calls += 1
        return self.before if self.read_calls == 1 else self.after

    def read_geowebcache_quota_health(self) -> GeoWebCacheQuotaHealth:
        self.call_order.append("read_quota_health")
        self.read_health_calls += 1
        return (
            self.before_health
            if self.read_health_calls == 1
            else self.after_health
        )

    def ensure_geowebcache_tile_blob_store(
        self,
        *,
        file_system_block_size: int,
        allow_file_system_block_size_migration: bool = False,
    ) -> GeoWebCacheFileBlobStore:
        self.call_order.append("ensure_blob_store")
        self.ensure_blob_store_calls.append(file_system_block_size)
        self.ensure_block_size_migration_calls.append(
            allow_file_system_block_size_migration
        )
        return self.after_blob_store or expected_geowebcache_tile_blob_store(
            file_system_block_size=file_system_block_size,
        )

def configured() -> Settings:
    return Settings(
        _env_file=None,
        geowebcache_disk_quota_gib=20,
        geowebcache_disk_quota_min_free_gib=5,
        geowebcache_disk_quota_cleanup_seconds=10,
        geowebcache_disk_quota_policy="LRU",
        geowebcache_physical_hard_limit_gib=28,
        geowebcache_physical_burst_margin_gib=2,
    )


def test_capacity_report_exposes_quota_reserve_and_both_margins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )

    report = cache_capacity_report(
        cache,
        quota_gib=20,
        min_free_gib=5,
    )

    assert report.configured_quota_bytes == 20 * GIB
    assert report.required_free_reserve_bytes == 5 * GIB
    assert report.current_cache_bytes == 0
    assert report.remaining_quota_growth_bytes == 20 * GIB
    assert report.physical_growth_safety_basis_points == 10_000
    assert report.required_physical_growth_bytes == 20 * GIB
    assert report.required_free_now_bytes == 25 * GIB
    assert report.capacity_margin_bytes == (
        100 * GIB
        - report.current_cache_allocated_bytes
        - 20 * GIB
        - 5 * GIB
    )
    assert report.current_free_margin_bytes == 70 * GIB
    assert report.growth_reserve_margin_bytes == 50 * GIB
    assert report.current_cache_inodes == 1
    assert report.required_future_inodes > 5_000_000
    assert report.inode_reserve_margin >= 0
    assert report.safe_to_apply is True


def test_quota_command_is_dry_run_by_default_and_reports_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    admin = FakeAdmin(before=quota(enabled=False))

    result = quota_status(
        cache_path=cache,
        configured=configured(),
        client=admin,  # type: ignore[arg-type]
    )

    assert result["schema_version"] == 4
    assert result["mode"] == "dry-run"
    assert result["verified"] is False
    assert result["blob_store"]["verified"] is False  # type: ignore[index]
    assert result["disk_quota"]["verified"] is False  # type: ignore[index]
    assert admin.read_blob_store_calls == 1
    assert admin.read_calls == 1
    assert admin.read_health_calls == 1
    assert admin.ensure_blob_store_calls == []
    args = build_parser().parse_args(["--cache-path", str(cache)])
    assert args.apply is False


def test_quota_dry_run_rejects_extra_nondefault_blob_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.os.fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_bsize=4096,
            f_frsize=4096,
            f_files=10_000_000,
            f_favail=10_000_000,
        ),
    )
    expected = expected_geowebcache_tile_blob_store(
        file_system_block_size=4096,
    )
    extra = GeoWebCacheFileBlobStore(
        id="extra-cache",
        enabled=True,
        default=False,
        base_directory="/extra/cache",
        file_system_block_size=4096,
        path_generator_type="DEFAULT",
    )
    admin = FakeAdmin(
        before=quota(),
        before_blob_stores=(extra, expected),
    )

    result = quota_status(
        cache_path=cache,
        configured=configured(),
        client=admin,  # type: ignore[arg-type]
    )

    assert result["blob_store"]["verified"] is False  # type: ignore[index]
    assert result["disk_quota"]["verified"] is True  # type: ignore[index]
    assert result["verified"] is False


def test_quota_apply_checks_capacity_then_uses_exact_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    admin = FakeAdmin(before=quota())

    result = quota_status(
        cache_path=cache,
        configured=configured(),
        apply=True,
        client=admin,  # type: ignore[arg-type]
    )

    assert result["mode"] == "apply"
    assert result["verified"] is True
    assert result["blob_store"]["verified"] is True  # type: ignore[index]
    assert result["disk_quota"]["verified"] is True  # type: ignore[index]
    assert admin.ensure_blob_store_calls == [
        result["capacity"]["filesystem_block_size_bytes"]  # type: ignore[index]
    ]
    assert admin.ensure_block_size_migration_calls == [False]
    assert admin.call_order == [
        "read_blob_stores",
        "read_quota",
        "read_quota_health",
        "ensure_blob_store",
        "read_quota",
        "read_quota_health",
    ]


def test_quota_apply_audits_block_size_only_migration_on_empty_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.os.fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_bsize=8192,
            f_frsize=8192,
            f_files=10_000_000,
            f_favail=10_000_000,
        ),
    )
    old_store = expected_geowebcache_tile_blob_store(
        file_system_block_size=4096,
    )
    new_store = expected_geowebcache_tile_blob_store(
        file_system_block_size=8192,
    )
    admin = FakeAdmin(
        before=quota(),
        before_blob_stores=(old_store,),
        after_blob_store=new_store,
    )

    result = quota_status(
        cache_path=cache,
        configured=configured(),
        apply=True,
        allow_block_size_migration=True,
        client=admin,  # type: ignore[arg-type]
    )

    blob_store = result["blob_store"]
    assert blob_store["block_size_migration_required"] is True  # type: ignore[index]
    assert blob_store["block_size_migration_permitted"] is True  # type: ignore[index]
    assert blob_store["block_size_migrated"] is True  # type: ignore[index]
    assert admin.ensure_blob_store_calls == [8192]
    assert admin.ensure_block_size_migration_calls == [True]


def test_generic_quota_apply_cannot_migrate_blob_store_block_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        quota_module.shutil,
        "disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    monkeypatch.setattr(
        quota_module.os,
        "fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_bsize=8192,
            f_frsize=8192,
            f_files=10_000_000,
            f_favail=10_000_000,
        ),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    old_store = expected_geowebcache_tile_blob_store(
        file_system_block_size=4096,
    )
    admin = FakeAdmin(
        before=quota(),
        before_blob_stores=(old_store,),
        after_blob_store=old_store,
    )

    with pytest.raises(
        GeoWebCacheQuotaSafetyError,
        match="blob store re-read",
    ):
        quota_status(
            cache_path=cache,
            configured=configured(),
            apply=True,
            client=admin,  # type: ignore[arg-type]
        )

    assert admin.ensure_block_size_migration_calls == [False]


def test_quota_dry_run_never_permits_block_size_migration_with_tiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    (cache / "tile.png").write_bytes(b"tile")
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.os.fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_bsize=8192,
            f_frsize=8192,
            f_files=10_000_000,
            f_favail=10_000_000,
        ),
    )
    admin = FakeAdmin(
        before=quota(),
        before_blob_stores=(
            expected_geowebcache_tile_blob_store(
                file_system_block_size=4096,
            ),
        ),
    )

    result = quota_status(
        cache_path=cache,
        configured=configured(),
        client=admin,  # type: ignore[arg-type]
    )

    blob_store = result["blob_store"]
    assert blob_store["block_size_migration_required"] is True  # type: ignore[index]
    assert blob_store["block_size_migration_permitted"] is False  # type: ignore[index]
    assert blob_store["block_size_migrated"] is False  # type: ignore[index]


def test_runtime_contract_actively_verifies_store_quota_and_block_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.os.fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_bsize=4096,
            f_frsize=4096,
        ),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    expected = expected_geowebcache_tile_blob_store(
        file_system_block_size=4096,
    )
    admin = FakeAdmin(
        before=quota(),
        before_blob_stores=(expected,),
    )

    report = runtime_contract_status(
        cache_path=cache,
        configured=configured(),
        client=admin,  # type: ignore[arg-type]
    )

    assert report["verified"] is True
    assert report["filesystem_block_size_bytes"] == 4096
    assert admin.call_order == [
        "health",
        "read_blob_stores",
        "read_quota",
        "read_quota_health",
    ]

    admin.before_blob_stores = ()
    with pytest.raises(
        GeoWebCacheQuotaSafetyError,
        match="FileBlobStore contract",
    ):
        runtime_contract_status(
            cache_path=cache,
            configured=configured(),
            client=admin,  # type: ignore[arg-type]
        )


def test_runtime_contract_rejects_dummy_or_stopped_quota_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        quota_module.os,
        "fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_bsize=4096,
            f_frsize=4096,
        ),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    expected = expected_geowebcache_tile_blob_store(
        file_system_block_size=4096,
    )
    admin = FakeAdmin(
        before=quota(),
        before_blob_stores=(expected,),
        before_health=quota_health(
            healthy=False,
            monitor_running=False,
            scheduled_cleanup_active=False,
            provider_error=True,
            store_class="org.geowebcache.diskquota.DummyQuotaStore",
            dialect_class=None,
            global_used_bytes=None,
        ),
    )

    with pytest.raises(
        GeoWebCacheQuotaSafetyError,
        match="quota store or monitor",
    ):
        runtime_contract_status(
            cache_path=cache,
            configured=configured(),
            client=admin,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("root_device", "total_gib", "used_gib", "expected_safe"),
    [
        (2, 28, 7, True),
        (1, 28, 7, False),
        (2, 29, 7, False),
        (2, 28, 23, False),
    ],
)
def test_physical_limit_requires_dedicated_bounded_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_device: int,
    total_gib: int,
    used_gib: int,
    expected_safe: bool,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()

    def metadata(*, device: int, inode: int) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_dev=device,
            st_ino=inode,
            st_size=0,
            st_mtime_ns=1,
            st_ctime_ns=1,
            st_nlink=1,
            st_blocks=0,
        )

    root = metadata(device=root_device, inode=20)
    parent = metadata(device=1, inode=10)
    fstat_results = iter((root, parent, root))
    monkeypatch.setattr(
        quota_module.os,
        "fstat",
        lambda _descriptor: next(fstat_results),
    )
    fragment = 4096
    monkeypatch.setattr(
        quota_module.os,
        "fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_frsize=fragment,
            f_blocks=total_gib * GIB // fragment,
            f_bfree=(total_gib - used_gib) * GIB // fragment,
            f_bavail=(total_gib - used_gib) * GIB // fragment,
        ),
    )

    report = physical_limit_status(cache, configured=configured())

    assert report.dedicated_mount is (root_device != 1)
    assert report.filesystem_total_bytes == total_gib * GIB
    assert report.filesystem_used_bytes == used_gib * GIB
    assert report.safe is expected_safe


def test_quota_apply_tolerates_only_live_filesystem_metric_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    usage = iter(
        (
            _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
            _ntuple_diskusage(100 * GIB, 26 * GIB, 74 * GIB),
        )
    )
    monkeypatch.setattr(
        quota_module.shutil,
        "disk_usage",
        lambda _path: next(usage),
    )
    first_physical = safe_physical_limit(cache)
    second_physical = replace(
        first_physical,
        filesystem_used_bytes=GIB,
        filesystem_free_bytes=27 * GIB,
        free_reserve_margin_bytes=22 * GIB,
        burst_remaining_bytes=21 * GIB,
    )
    physical_reports = iter((first_physical, second_physical))
    monkeypatch.setattr(
        quota_module,
        "physical_limit_status",
        lambda _path, *, configured: next(physical_reports),
    )
    admin = FakeAdmin(before=quota())

    report = quota_status(
        cache_path=cache,
        configured=configured(),
        apply=True,
        client=admin,  # type: ignore[arg-type]
    )

    assert report["verified"] is True
    assert report["capacity"]["filesystem_free_bytes"] == 74 * GIB  # type: ignore[index]
    assert admin.ensure_blob_store_calls


def test_cache_filesystem_block_size_rejects_unstable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    real_fstat = quota_module.os.fstat
    calls = 0

    def changed_fstat(descriptor: int):
        nonlocal calls
        metadata = real_fstat(descriptor)
        calls += 1
        if calls == 2:
            values = list(metadata)
            values[1] = metadata.st_ino + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(quota_module.os, "fstat", changed_fstat)

    with pytest.raises(
        GeoWebCacheQuotaSafetyError,
        match="unstable",
    ):
        cache_filesystem_block_size(cache)


def test_quota_apply_fails_before_mutation_without_capacity_margin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(22 * GIB, 21 * GIB, 1 * GIB),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    admin = FakeAdmin(before=quota(enabled=False))

    with pytest.raises(GeoWebCacheQuotaSafetyError):
        quota_status(
            cache_path=cache,
            configured=configured(),
            apply=True,
            client=admin,  # type: ignore[arg-type]
        )

    assert admin.ensure_blob_store_calls == []


def test_empty_cache_with_only_six_gib_free_cannot_reserve_quota_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 94 * GIB, 6 * GIB),
    )
    admin = FakeAdmin(before=quota(enabled=False))
    patch_safe_physical_limit(monkeypatch, cache)

    report = cache_capacity_report(
        cache,
        quota_gib=20,
        min_free_gib=5,
    )

    assert report.current_cache_bytes == 0
    assert report.remaining_quota_growth_bytes == 20 * GIB
    assert report.required_physical_growth_bytes == 20 * GIB
    assert report.required_free_now_bytes == 25 * GIB
    assert report.current_free_margin_bytes == 1 * GIB
    assert report.growth_reserve_margin_bytes == -19 * GIB
    assert report.safe_to_apply is False
    with pytest.raises(GeoWebCacheQuotaSafetyError):
        quota_status(
            cache_path=cache,
            configured=configured(),
            apply=True,
            client=admin,  # type: ignore[arg-type]
        )
    assert admin.ensure_blob_store_calls == []


def test_quota_apply_fails_before_disk_quota_if_blob_store_is_not_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    patch_safe_physical_limit(monkeypatch, cache)
    admin = FakeAdmin(
        before=quota(),
        after_blob_store=GeoWebCacheFileBlobStore(
            id="siur-tile-cache-v3",
            enabled=True,
            default=True,
            base_directory="/wrong/cache",
            file_system_block_size=4096,
            path_generator_type="DEFAULT",
        ),
    )

    with pytest.raises(
        GeoWebCacheQuotaSafetyError,
        match="blob store re-read",
    ):
        quota_status(
            cache_path=cache,
            configured=configured(),
            apply=True,
            client=admin,  # type: ignore[arg-type]
        )

    assert admin.ensure_blob_store_calls
    assert admin.call_order[-1] == "ensure_blob_store"


def test_capacity_inventory_measures_files_and_rejects_nested_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    nested = cache / "layer"
    nested.mkdir(parents=True)
    (nested / "tile.png").write_bytes(b"x" * 4096)
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )

    report = cache_capacity_report(
        cache,
        quota_gib=20,
        min_free_gib=5,
    )

    assert report.current_cache_bytes == 4096
    assert report.current_cache_allocated_bytes >= 4096
    assert report.current_cache_files == 1
    assert report.current_cache_directories == 1

    (nested / "unsafe").symlink_to(nested / "tile.png")
    with pytest.raises(GeoWebCacheQuotaSafetyError, match="symlink"):
        cache_capacity_report(
            cache,
            quota_gib=20,
            min_free_gib=5,
        )


def test_capacity_inventory_fails_closed_when_cache_changes_between_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    tile = cache / "tile.png"
    tile.write_bytes(b"first")
    original = quota_module._cache_inventory
    calls = 0

    def inventory_then_mutate(path: Path):
        nonlocal calls
        result = original(path)
        calls += 1
        if calls == 1:
            tile.write_bytes(b"second-version")
        return result

    monkeypatch.setattr(quota_module, "_cache_inventory", inventory_then_mutate)

    with pytest.raises(GeoWebCacheQuotaSafetyError, match="changed"):
        cache_capacity_report(
            cache,
            quota_gib=20,
            min_free_gib=5,
        )


def test_capacity_report_rejects_relative_or_symlink_cache_paths(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    link = tmp_path / "gwc-link"
    link.symlink_to(cache, target_is_directory=True)

    with pytest.raises(GeoWebCacheQuotaSafetyError):
        cache_capacity_report(
            Path("relative"),
            quota_gib=20,
            min_free_gib=5,
        )
    with pytest.raises(GeoWebCacheQuotaSafetyError):
        cache_capacity_report(
            link,
            quota_gib=20,
            min_free_gib=5,
        )


def test_capacity_inventory_rejects_legacy_mixed_volume_configuration(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    (cache / "geowebcache.xml").write_text("<legacy/>", encoding="utf-8")

    with pytest.raises(
        GeoWebCacheQuotaSafetyError,
        match="legacy configuration",
    ):
        cache_capacity_report(
            cache,
            quota_gib=20,
            min_free_gib=5,
        )


def test_capacity_fails_closed_without_required_inode_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.shutil.disk_usage",
        lambda _path: _ntuple_diskusage(100 * GIB, 25 * GIB, 75 * GIB),
    )
    monkeypatch.setattr(
        "app.reference_layers.gwc_quota.os.fstatvfs",
        lambda _descriptor: SimpleNamespace(
            f_bsize=4096,
            f_frsize=4096,
            f_files=10_000,
            f_favail=9_000,
        ),
    )

    report = cache_capacity_report(
        cache,
        quota_gib=20,
        min_free_gib=5,
    )

    assert report.growth_reserve_margin_bytes > 0
    assert report.inode_reserve_margin < 0
    assert report.safe_to_apply is False


def test_capacity_inventory_rejects_cross_device_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "gwc"
    cache.mkdir()
    tile = cache / "tile.png"
    tile.write_bytes(b"tile")
    real_scandir = quota_module.os.scandir

    class CrossDeviceEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name

        def stat(self, *, follow_symlinks: bool = True):
            metadata = self._entry.stat(follow_symlinks=follow_symlinks)
            values = list(metadata)
            values[2] = metadata.st_dev + 1
            return os.stat_result(values)

    class CrossDeviceIterator:
        def __init__(self, value: int) -> None:
            self._delegate = real_scandir(value)

        def __enter__(self):
            return iter(
                CrossDeviceEntry(entry) for entry in self._delegate
            )

        def __exit__(self, *args: object) -> None:
            self._delegate.close()

    monkeypatch.setattr(
        quota_module.os,
        "scandir",
        lambda descriptor: CrossDeviceIterator(descriptor),
    )

    with pytest.raises(
        GeoWebCacheQuotaSafetyError,
        match="filesystem boundaries",
    ):
        quota_module._cache_inventory(cache)
