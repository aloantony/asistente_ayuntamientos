import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.reference_layers import gwc_config_init as init_module
from app.reference_layers.gwc_config_init import (
    CONFIG_FILE_NAME,
    GeoWebCacheConfigInitError,
    initialize_quota_configuration,
    quota_configuration_bytes,
)
from app.reference_layers.gwc_quota import GeoWebCachePhysicalLimit

GIB = 1024**3


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


def safe_physical(cache: Path) -> GeoWebCachePhysicalLimit:
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


def patch_safe_cache(
    monkeypatch: pytest.MonkeyPatch,
    cache: Path,
) -> None:
    monkeypatch.setattr(
        init_module,
        "physical_limit_status",
        lambda _path, *, configured: safe_physical(cache),
    )
    monkeypatch.setattr(
        init_module,
        "cache_filesystem_block_size",
        lambda _path: 4096,
    )


def test_quota_configuration_is_exact_and_deterministic() -> None:
    expected = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<gwcQuotaConfiguration>\n"
        "  <enabled>true</enabled>\n"
        "  <diskBlockSize>4096</diskBlockSize>\n"
        "  <cacheCleanUpFrequency>10</cacheCleanUpFrequency>\n"
        "  <cacheCleanUpUnits>SECONDS</cacheCleanUpUnits>\n"
        "  <maxConcurrentCleanUps>2</maxConcurrentCleanUps>\n"
        "  <globalExpirationPolicyName>LRU</globalExpirationPolicyName>\n"
        "  <globalQuota>\n"
        "    <value>20</value>\n"
        "    <units>GiB</units>\n"
        "  </globalQuota>\n"
        "  <layerQuotas></layerQuotas>\n"
        "  <quotaStore>HSQL</quotaStore>\n"
        "</gwcQuotaConfiguration>\n"
    ).encode("ascii")

    first = quota_configuration_bytes(
        configured=configured(),
        filesystem_block_size=4096,
    )
    second = quota_configuration_bytes(
        configured=configured(),
        filesystem_block_size=4096,
    )

    assert first == expected
    assert second == expected


def test_dry_run_does_not_create_missing_gwc_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    data.mkdir()
    cache.mkdir()
    patch_safe_cache(monkeypatch, cache)

    report = initialize_quota_configuration(
        geoserver_data_path=data,
        cache_path=cache,
        configured=configured(),
    )

    assert report["mode"] == "dry-run"
    assert report["verified"] is False
    assert not (data / "gwc").exists()


def test_apply_materializes_atomically_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    data.mkdir()
    cache.mkdir()
    patch_safe_cache(monkeypatch, cache)

    first = initialize_quota_configuration(
        geoserver_data_path=data,
        cache_path=cache,
        configured=configured(),
        apply=True,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    target = data / "gwc" / CONFIG_FILE_NAME
    second = initialize_quota_configuration(
        geoserver_data_path=data,
        cache_path=cache,
        configured=configured(),
        apply=True,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert first["verified"] is True
    assert first["changed"] is True
    assert target.read_bytes() == quota_configuration_bytes(
        configured=configured(),
        filesystem_block_size=4096,
    )
    assert target.stat().st_mode & 0o777 == 0o640
    assert list((data / "gwc").glob(f".{CONFIG_FILE_NAME}.tmp-*")) == []
    assert second["verified"] is True
    assert second["changed"] is False


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_apply_rejects_linked_configuration_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    gwc = data / "gwc"
    gwc.mkdir(parents=True)
    cache.mkdir()
    patch_safe_cache(monkeypatch, cache)
    target = gwc / CONFIG_FILE_NAME
    other = gwc / "other.xml"
    other.write_text("<unsafe/>", encoding="ascii")
    if unsafe_kind == "symlink":
        target.symlink_to(other)
    else:
        os.link(other, target)

    with pytest.raises(
        GeoWebCacheConfigInitError,
        match="quota file is unsafe",
    ):
        initialize_quota_configuration(
            geoserver_data_path=data,
            cache_path=cache,
            configured=configured(),
            apply=True,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_apply_keeps_original_when_target_changes_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    gwc = data / "gwc"
    gwc.mkdir(parents=True)
    cache.mkdir()
    patch_safe_cache(monkeypatch, cache)
    target = gwc / CONFIG_FILE_NAME
    target.write_text("<old/>", encoding="ascii")

    def reject_race(
        _directory_descriptor: int,
        _expected: os.stat_result | None,
    ) -> None:
        raise GeoWebCacheConfigInitError(
            "GeoWebCache pre-start quota changed before replacement"
        )

    monkeypatch.setattr(
        init_module,
        "_assert_target_unchanged",
        reject_race,
    )

    with pytest.raises(
        GeoWebCacheConfigInitError,
        match="changed before replacement",
    ):
        initialize_quota_configuration(
            geoserver_data_path=data,
            cache_path=cache,
            configured=configured(),
            apply=True,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert target.read_text(encoding="ascii") == "<old/>"
    assert list(gwc.glob(f".{CONFIG_FILE_NAME}.tmp-*")) == []


def test_unsafe_physical_limit_prevents_configuration_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    data.mkdir()
    cache.mkdir()
    unsafe = safe_physical(cache)
    monkeypatch.setattr(
        init_module,
        "physical_limit_status",
        lambda _path, *, configured: type(unsafe)(
            **{**unsafe.__dict__, "safe": False}
        ),
    )

    with pytest.raises(
        GeoWebCacheConfigInitError,
        match="physical hard limit",
    ):
        initialize_quota_configuration(
            geoserver_data_path=data,
            cache_path=cache,
            configured=configured(),
            apply=True,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )

    assert not (data / "gwc").exists()
