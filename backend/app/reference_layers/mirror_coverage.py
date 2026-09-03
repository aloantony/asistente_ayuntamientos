"""Reviewed finite coverage profiles for tile-only mirror fallbacks.

SIUR's authoritative settings file stores overlay extents as EPSG:25830
evidence and does not publish bounds or zooms for its three background maps.
Those values cannot be passed to an XYZ/WMTS seed implicitly.  This module
holds the explicit operational coverage reviewed for the local mirror: the
envelope of the municipality being served plus a working ring of its
neighbours, and a native zoom that fits the deployed storage budget.  Requests
above the native zoom remain local and are rendered from the nearest archived
parent tile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


SIUR_LAYER_PREFIX = "layer:siur:"
SIUR_TILE_PROFILE = "siur-municipal-native-z17-v1"
# The historical-ortho substitution keeps the regional profile it was reviewed
# under: its equivalence evidence is committed and byte-verified, so its
# operational profile cannot move without regenerating that file. It is a
# separate feature from the background maps this deployment mirrors.
SIUR_ORTHO_TILE_PROFILE = "siur-castilla-y-leon-ortho-native-z15-v1"
SIUR_WMS_SUPERTILE_COVERAGE_PROFILES = frozenset(
    {
        SIUR_TILE_PROFILE,
        SIUR_ORTHO_TILE_PROFILE,
    }
)
# The deployment envelope for background maps: the served municipality plus a
# working ring of its neighbours, roughly 30 x 31 km.  It replaces the Castilla
# y Leon envelope this profile used to carry, which was measured at 1.3 million
# tiles and 28 GB for the base map alone -- almost six hours of upstream
# traffic to serve one village, and more than the reviewed cache can hold.  The
# municipal envelope is 24,500 tiles and some 400 MB while still reaching a
# zoom level closer, so the town hall sees its own plots instead of a coarser
# region.  Native zoom stops at 17 because IGN Base is drawn cartography, not
# imagery: past that the same linework is only magnified, and the seed cost
# quadruples per level -- z18 measured at nine hours against z17's two.
# Serving a second municipality means reviewing a second profile.  See ADR-057.
SIUR_TILE_BOUNDS = {
    "west": -3.763,
    "south": 41.493,
    "east": -3.403,
    "north": 41.773,
}
SIUR_ORTHO_TILE_BOUNDS = {
    "west": -7.6,
    "south": 39.9,
    "east": -1.3,
    "north": 43.4,
}
SIUR_TILE_MIN_ZOOM = 0
SIUR_TILE_MAX_ZOOM = 17
SIUR_ORTHO_TILE_MAX_ZOOM = 15
SIUR_TILE_MAX_COUNT = 2_000_000
SIUR_WMS_SUPERTILE_SIZE = 8

_SIUR_ORTHO_HOST = "orto.wms.itacyl.es"
_SIUR_JPEG_LAYERS = frozenset(
    {
        ("www.ign.es", "OI.OrthoimageCoverage"),
        ("www.ign.es", "MTN"),
    }
)
_SIUR_JPEG_HOSTS = frozenset({_SIUR_ORTHO_HOST, "tms-relieve.idee.es"})


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
    endpoint_url: str | None = None,
    remote_name: str | None = None,
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
    ortho_profile = _is_siur_ortho(endpoint_url, remote_name)
    default_bounds = (
        SIUR_ORTHO_TILE_BOUNDS if ortho_profile else SIUR_TILE_BOUNDS
    )
    return ReviewedTileCoverage(
        bounds=normalized_bounds or dict(default_bounds),
        min_zoom=SIUR_TILE_MIN_ZOOM if min_zoom is None else min_zoom,
        max_zoom=(
            SIUR_ORTHO_TILE_MAX_ZOOM
            if max_zoom is None and ortho_profile
            else SIUR_TILE_MAX_ZOOM if max_zoom is None else max_zoom
        ),
        max_tile_count=SIUR_TILE_MAX_COUNT,
        profile=SIUR_ORTHO_TILE_PROFILE if ortho_profile else SIUR_TILE_PROFILE,
    )


def reviewed_tile_format(
    *,
    layer_source_key: str,
    endpoint_url: str,
    remote_name: str,
    requested_format: str | None,
) -> str | None:
    """Select JPEG only for reviewed opaque SIUR imagery sources.

    An explicit catalog format always wins.  Unknown providers and unreviewed
    SIUR layers retain their normal protocol default, including PNG alpha for
    thematic overlays.
    """

    if requested_format:
        return requested_format
    if not layer_source_key.startswith(SIUR_LAYER_PREFIX):
        return None
    host = _hostname(endpoint_url)
    if host in _SIUR_JPEG_HOSTS or (host, remote_name) in _SIUR_JPEG_LAYERS:
        return "image/jpeg"
    return None


def _is_siur_ortho(endpoint_url: str | None, remote_name: str | None) -> bool:
    return bool(
        endpoint_url
        and remote_name
        and _hostname(endpoint_url) == _SIUR_ORTHO_HOST
        and remote_name.startswith("Ortofoto_")
    )


def _hostname(endpoint_url: str) -> str | None:
    try:
        return urlsplit(endpoint_url).hostname
    except ValueError:
        return None
