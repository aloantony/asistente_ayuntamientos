"""Reviewed finite coverage profiles for tile-only mirror fallbacks.

SIUR's authoritative settings file stores overlay extents as EPSG:25830
evidence and does not publish bounds or zooms for its three background maps.
Those values cannot be passed to an XYZ/WMTS seed implicitly.  This module
holds the explicit operational coverage reviewed for the local mirror: the
Castilla y Leon view envelope (padded beyond the public WMC extent) and a
native zoom that fits the deployed storage budget.  Requests above the native
zoom remain local and are rendered from the nearest archived parent tile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SIUR_LAYER_PREFIX = "layer:siur:"
SIUR_TILE_PROFILE = "siur-castilla-y-leon-native-z16-v1"
SIUR_TILE_BOUNDS = {
    "west": -7.6,
    "south": 39.9,
    "east": -1.3,
    "north": 43.4,
}
SIUR_TILE_MIN_ZOOM = 0
SIUR_TILE_MAX_ZOOM = 16
SIUR_TILE_MAX_COUNT = 2_000_000


@dataclass(frozen=True)
class ReviewedTileCoverage:
    bounds: dict[str, float] | None
    min_zoom: int | None
    max_zoom: int | None
    max_tile_count: int | None
    profile: str | None


def reviewed_tile_coverage(
    *,
    layer_source_key: str,
    bounds: Mapping[str, Any] | None,
    min_zoom: int | None,
    max_zoom: int | None,
) -> ReviewedTileCoverage:
    """Fill only missing SIUR coverage; generic sources remain fail-closed."""

    normalized_bounds = dict(bounds) if isinstance(bounds, Mapping) else None
    if not layer_source_key.startswith(SIUR_LAYER_PREFIX):
        return ReviewedTileCoverage(
            bounds=normalized_bounds,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            max_tile_count=None,
            profile=None,
        )
    return ReviewedTileCoverage(
        bounds=normalized_bounds or dict(SIUR_TILE_BOUNDS),
        min_zoom=SIUR_TILE_MIN_ZOOM if min_zoom is None else min_zoom,
        max_zoom=SIUR_TILE_MAX_ZOOM if max_zoom is None else max_zoom,
        max_tile_count=SIUR_TILE_MAX_COUNT,
        profile=SIUR_TILE_PROFILE,
    )
