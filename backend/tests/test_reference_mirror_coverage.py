from app.reference_layers.mirror_coverage import (
    SIUR_ORTHO_TILE_MAX_ZOOM,
    SIUR_ORTHO_TILE_PROFILE,
    SIUR_TILE_BOUNDS,
    SIUR_TILE_MAX_COUNT,
    SIUR_TILE_MAX_ZOOM,
    SIUR_TILE_MIN_ZOOM,
    SIUR_TILE_PROFILE,
    reviewed_tile_coverage,
    reviewed_tile_format,
)


def test_siur_missing_bounds_and_zooms_use_reviewed_finite_profile() -> None:
    coverage = reviewed_tile_coverage(
        layer_source_key="layer:siur:" + "a" * 64,
        bounds=None,
        min_zoom=None,
        max_zoom=None,
    )

    assert coverage.bounds == SIUR_TILE_BOUNDS
    assert coverage.bounds is not SIUR_TILE_BOUNDS
    assert coverage.min_zoom == SIUR_TILE_MIN_ZOOM
    assert coverage.max_zoom == SIUR_TILE_MAX_ZOOM
    assert coverage.max_tile_count == SIUR_TILE_MAX_COUNT
    assert coverage.profile == SIUR_TILE_PROFILE


def test_explicit_siur_coverage_is_preserved_but_still_bounded_by_quota() -> None:
    bounds = {"west": -5, "south": 40, "east": -4, "north": 41}
    coverage = reviewed_tile_coverage(
        layer_source_key="layer:siur:" + "b" * 64,
        bounds=bounds,
        min_zoom=6,
        max_zoom=14,
    )

    assert coverage.bounds == bounds
    assert coverage.min_zoom == 6
    assert coverage.max_zoom == 14
    assert coverage.max_tile_count == SIUR_TILE_MAX_COUNT


def test_unknown_provider_never_receives_implicit_coverage() -> None:
    coverage = reviewed_tile_coverage(
        layer_source_key="layer:other:roads",
        bounds=None,
        min_zoom=None,
        max_zoom=None,
    )

    assert coverage.bounds is None
    assert coverage.min_zoom is None
    assert coverage.max_zoom is None
    assert coverage.max_tile_count is None
    assert coverage.profile is None


def test_siur_historical_ortho_uses_bounded_native_zoom() -> None:
    coverage = reviewed_tile_coverage(
        layer_source_key="layer:siur:" + "c" * 64,
        bounds=None,
        min_zoom=None,
        max_zoom=None,
        endpoint_url="https://orto.wms.itacyl.es/WMS",
        remote_name="Ortofoto_2002",
    )

    assert coverage.max_zoom == SIUR_ORTHO_TILE_MAX_ZOOM
    assert coverage.profile == SIUR_ORTHO_TILE_PROFILE


def test_explicit_ortho_zoom_is_not_silently_reduced() -> None:
    coverage = reviewed_tile_coverage(
        layer_source_key="layer:siur:" + "d" * 64,
        bounds=None,
        min_zoom=4,
        max_zoom=17,
        endpoint_url="https://orto.wms.itacyl.es/WMS",
        remote_name="Ortofoto_2023",
    )

    assert coverage.min_zoom == 4
    assert coverage.max_zoom == 17


def test_reviewed_opaque_siur_imagery_prefers_jpeg() -> None:
    assert reviewed_tile_format(
        layer_source_key="layer:siur:" + "e" * 64,
        endpoint_url="https://orto.wms.itacyl.es/WMS",
        remote_name="Ortofoto_2023",
        requested_format=None,
    ) == "image/jpeg"
    assert reviewed_tile_format(
        layer_source_key="layer:siur:" + "f" * 64,
        endpoint_url="https://www.ign.es/wmts/pnoa-ma",
        remote_name="OI.OrthoimageCoverage",
        requested_format=None,
    ) == "image/jpeg"


def test_explicit_or_unreviewed_tile_format_is_preserved() -> None:
    assert reviewed_tile_format(
        layer_source_key="layer:siur:" + "1" * 64,
        endpoint_url="https://orto.wms.itacyl.es/WMS",
        remote_name="Ortofoto_2023",
        requested_format="image/png",
    ) == "image/png"
    assert reviewed_tile_format(
        layer_source_key="layer:other:roads",
        endpoint_url="https://tiles.example.test/base",
        remote_name="roads",
        requested_format=None,
    ) is None
