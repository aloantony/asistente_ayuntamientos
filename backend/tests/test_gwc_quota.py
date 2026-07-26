from pathlib import Path
from shutil import _ntuple_diskusage

import pytest

from app.core.config import Settings
from app.reference_layers import gwc_quota as quota_module
from app.reference_layers.geoserver_admin import GeoWebCacheDiskQuota
from app.reference_layers.gwc_quota import (
    GeoWebCacheQuotaSafetyError,
    build_parser,
    cache_capacity_report,
    quota_status,
)

GIB = 1024**3


def quota(
    *,
    enabled: bool = True,
    quota_gib: int = 20,
    cleanup_seconds: int = 60,
    policy: str = "LRU",
) -> GeoWebCacheDiskQuota:
    return GeoWebCacheDiskQuota(
        enabled=enabled,
        quota_bytes=quota_gib * GIB,
        quota_value=quota_gib,
        quota_units="GiB",
        cleanup_frequency=cleanup_seconds,
        cleanup_units="SECONDS",
        expiration_policy=policy,  # type: ignore[arg-type]
    )


class FakeAdmin:
    def __init__(
        self,
        *,
        before: GeoWebCacheDiskQuota,
        after: GeoWebCacheDiskQuota | None = None,
    ) -> None:
        self.before = before
        self.after = after or before
        self.read_calls = 0
        self.configure_calls: list[dict[str, object]] = []

    def read_geowebcache_disk_quota(self) -> GeoWebCacheDiskQuota:
        self.read_calls += 1
        return self.before

    def configure_geowebcache_disk_quota(
        self,
        *,
        quota_gib: int,
        cleanup_seconds: int,
        expiration_policy: str,
    ) -> GeoWebCacheDiskQuota:
        self.configure_calls.append(
            {
                "quota_gib": quota_gib,
                "cleanup_seconds": cleanup_seconds,
                "expiration_policy": expiration_policy,
            }
        )
        return self.after


def configured() -> Settings:
    return Settings(
        _env_file=None,
        geowebcache_disk_quota_gib=20,
        geowebcache_disk_quota_min_free_gib=5,
        geowebcache_disk_quota_cleanup_seconds=60,
        geowebcache_disk_quota_policy="LRU",
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
    assert report.required_free_now_bytes == 25 * GIB
    assert report.capacity_margin_bytes == 75 * GIB
    assert report.current_free_margin_bytes == 70 * GIB
    assert report.growth_reserve_margin_bytes == 50 * GIB
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
    admin = FakeAdmin(before=quota(enabled=False))

    result = quota_status(
        cache_path=cache,
        configured=configured(),
        client=admin,  # type: ignore[arg-type]
    )

    assert result["mode"] == "dry-run"
    assert result["verified"] is False
    assert admin.read_calls == 1
    assert admin.configure_calls == []
    args = build_parser().parse_args(["--cache-path", str(cache)])
    assert args.apply is False


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
    admin = FakeAdmin(before=quota(enabled=False), after=quota())

    result = quota_status(
        cache_path=cache,
        configured=configured(),
        apply=True,
        client=admin,  # type: ignore[arg-type]
    )

    assert result["mode"] == "apply"
    assert result["verified"] is True
    assert admin.configure_calls == [
        {
            "quota_gib": 20,
            "cleanup_seconds": 60,
            "expiration_policy": "LRU",
        }
    ]


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
    admin = FakeAdmin(before=quota(enabled=False))

    with pytest.raises(GeoWebCacheQuotaSafetyError):
        quota_status(
            cache_path=cache,
            configured=configured(),
            apply=True,
            client=admin,  # type: ignore[arg-type]
        )

    assert admin.configure_calls == []


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

    report = cache_capacity_report(
        cache,
        quota_gib=20,
        min_free_gib=5,
    )

    assert report.current_cache_bytes == 0
    assert report.remaining_quota_growth_bytes == 20 * GIB
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
    assert admin.configure_calls == []


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
