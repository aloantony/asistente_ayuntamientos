"""Deterministic acquisition candidates for reference map layers.

The catalog records how SIUR displays a layer.  That endpoint is often not the
best source to mirror: a WMS backed by GeoServer can expose the same collection
through WFS or WCS, while an ArcGIS WMS can have a queryable MapServer.  This
module only builds candidates; network probes must prove that the collection is
actually present before a candidate can become a primary source.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.idecyl_exact_evidence import (
    EVIDENCE_SCHEMA as IDECYL_EXACT_EVIDENCE_SCHEMA,
)
from app.reference_layers.idecyl_exact_evidence import (
    IDECyLExactEvidenceError,
    ReviewedIDECyLExactSource,
    is_idecyl_geoserver_catalog_endpoint,
    reviewed_idecyl_exact_source,
)
from app.reference_layers.idecyl_local_style_evidence import (
    IDECyLLocalStyleEvidenceError,
    ReviewedIDECyLLocalStyle,
    idecyl_local_style_config,
    idecyl_local_style_expected_source_definition,
    reviewed_idecyl_local_style_exclusion_for_source,
    reviewed_idecyl_local_style,
    reviewed_idecyl_local_styles_for_source,
)
from app.reference_layers.idecyl_population_nitrate_style_evidence import (
    MANIFEST_RESOURCE as IDECYL_POPULATION_STYLE_MANIFEST_RESOURCE,
    MANIFEST_SHA256 as IDECYL_POPULATION_STYLE_MANIFEST_SHA256,
    IDECyLPopulationNitrateStyleEvidenceError,
    ReviewedIDECyLNitrateStyle,
    idecyl_nitrate_expected_source_definition,
    idecyl_population_expected_source_definition,
    reviewed_idecyl_nitrate_styles,
    reviewed_idecyl_nitrate_styles_for_source,
    reviewed_idecyl_population_style_exclusion,
    reviewed_idecyl_population_style_exclusion_for_source,
)
from app.reference_layers.idecyl_style_exclusion_evidence import (
    MANIFEST_RESOURCE as IDECYL_STYLE_EXCLUSION_MANIFEST_RESOURCE,
    MANIFEST_SHA256 as IDECYL_STYLE_EXCLUSION_MANIFEST_SHA256,
    IDECyLStyleExclusionEvidenceError,
    reviewed_idecyl_style_exclusion,
)
from app.reference_layers.mirror_coverage import (
    SIUR_LAYER_PREFIX,
    SIUR_TILE_BOUNDS,
    SIUR_WMS_SUPERTILE_COVERAGE_PROFILES,
    SIUR_WMS_SUPERTILE_SIZE,
    reviewed_tile_coverage,
    reviewed_tile_format,
)
from app.reference_layers.reviewed_archive_styles import (
    EXACT_ARCHIVE_STYLE_KEYS,
    ReviewedArchiveStyleError,
    reviewed_archive_style_specs,
)
from app.reference_layers.reviewed_ortho_evidence import (
    SOURCE_PRIORITY as REVIEWED_ORTHO_SOURCE_PRIORITY,
)
from app.reference_layers.reviewed_ortho_evidence import (
    ReviewedIgnOrthoSubstitution,
    ReviewedOrthoEvidenceError,
    reviewed_ign_ortho_equivalence,
    reviewed_ign_ortho_expected_source_definition,
    reviewed_ign_ortho_substitution,
)
from app.reference_layers.reviewed_style_evidence import (
    ReviewedStyleEvidenceError,
    reviewed_miteco_mvt_style_reference,
)

SourceProtocol = Literal[
    "wfs",
    "wcs",
    "ogc_api_features",
    "arcgis_rest",
    "atom",
    "download",
    "wmts",
    "xyz",
    "wms_tiles",
    "local",
]
TargetKind = Literal["vector", "raster", "tiles"]
SyncStrategy = Literal[
    "conditional_get",
    "full_snapshot",
    "paged_snapshot",
    "tile_seed",
    "manual",
]

_GEOSERVER_SERVICE_RE = re.compile(
    r"^(?P<prefix>/(?:geoserver|geoapps)/[^/]+)/(?:wms|ows)/?$",
    re.IGNORECASE,
)
_ARCGIS_WMS_RE = re.compile(
    r"^(?P<prefix>/.+?/MapServer)/(?:WMSServer)/?$",
    re.IGNORECASE,
)
_WMS_INSPIRE_RE = re.compile(
    r"^(?P<prefix>/)(?:wms-inspire)(?P<suffix>/[^/]+/?$)",
    re.IGNORECASE,
)
_REVIEWED_NATIVE_WMS_SCHEMA = "siur-reviewed-native-wms-equivalence/v1"
_REVIEWED_NATIVE_WMS_PRIORITY = 40
_REVIEWED_DATASET_SOURCE_SCHEMA = "siur-reviewed-dataset-source/v1"
_REVIEWED_DATASET_SOURCE_PRIORITY = 5
_REVIEWED_ORTHO_SUBSTITUTION_PRIORITY = REVIEWED_ORTHO_SOURCE_PRIORITY
_BULK_WMS_GUARD_SCHEMA = "siur-bulk-wms-guard/v1"
_REVIEWED_LOCAL_STYLE_SCHEMA = "siur-reviewed-local-style-recipe/v1"
_REVIEWED_IDECYL_BAKED_WMS_SCHEMA = (
    "siur-reviewed-idecyl-baked-wms-fallback/v1"
)
_LEGACY_IDECYL_ARCHIVE_STYLE_EVIDENCE_KEYS = frozenset(
    {"remote_name", "archive_member", "sha256"}
)
_LEGACY_IDECYL_ARCHIVE_DEFAULTS = {
    "telefonia_movil_cyl_cobertura_carreteras": (
        "telefonia_movil_cyl_cobertura_carreteras_tc_cnmc_mj"
    ),
}


@dataclass(frozen=True)
class _ReviewedNativeWMS:
    profile: str
    catalog_endpoint_url: str
    catalog_remote_name: str
    endpoint_url: str
    remote_name: str
    image_format: str


@dataclass(frozen=True)
class _ReviewedDatasetSource:
    profile: str
    catalog_endpoint_url: str
    catalog_remote_name: str
    protocol: SourceProtocol
    target_kind: TargetKind
    endpoint_url: str
    remote_name: str
    sync_strategy: SyncStrategy
    config: dict[str, Any]
    equivalence: dict[str, Any]


@dataclass(frozen=True)
class _BulkWMSGuard:
    profile: str
    catalog_endpoint_url: str
    terms_url: str


@dataclass(frozen=True)
class ReviewedLocalStyleRecipe:
    """One allowlisted, locally authored adaptation for a reviewed dataset."""

    schema: str
    profile: str
    style_kind: Literal[
        "catastro_parcels",
        "eurostat_grid",
        "flood_polygons",
        "idecyl_nitrate_year",
        "idecyl_polygon_outline",
        "ines_raster",
    ]
    catalog_style_source_key: str
    remote_style_name: str
    catalog_layer_name: str
    selected_layer_name: str
    reviewed_equivalence: dict[str, Any]
    reviewed_equivalence_sha256: str
    style_reference: dict[str, Any]
    audit_layer_id: int | None = None
    catalog_style_is_default: bool | None = None
    source_definition: dict[str, Any] | None = None
    source_definition_sha256: str | None = None


_REVIEWED_NATIVE_WMS = {
    (item.catalog_endpoint_url, item.catalog_remote_name): item
    for item in (
        _ReviewedNativeWMS(
            profile="ign-pnoa-current-ortho-wms-v1",
            catalog_endpoint_url="https://www.ign.es/wmts/pnoa-ma",
            catalog_remote_name="OI.OrthoimageCoverage",
            endpoint_url="https://www.ign.es/wms-inspire/pnoa-ma",
            remote_name="OI.OrthoimageCoverage",
            image_format="image/jpeg",
        ),
        _ReviewedNativeWMS(
            profile="ign-base-transparent-wms-v1",
            catalog_endpoint_url="https://www.ign.es/wmts/ign-base",
            catalog_remote_name="IGNBaseTodo-nofondo",
            endpoint_url="https://www.ign.es/wms-inspire/ign-base",
            remote_name="IGNBaseTodo-nofondo",
            image_format="image/png",
        ),
        _ReviewedNativeWMS(
            profile="ign-mtn-raster-wms-v1",
            catalog_endpoint_url="https://www.ign.es/wmts/mapa-raster",
            catalog_remote_name="MTN",
            endpoint_url="https://www.ign.es/wms-inspire/mapa-raster",
            remote_name="mtn_rasterizado",
            image_format="image/jpeg",
        ),
    )
}

_CATASTRO_ATOM_ROOT = (
    "https://www.catastro.hacienda.gob.es/INSPIRE/CadastralParcels/"
    "ES.SDGC.CP.Atom.xml"
)
_CATASTRO_CYL_PROVINCE_CODES = (
    "05",
    "09",
    "24",
    "34",
    "37",
    "40",
    "42",
    "47",
    "49",
)
_CATASTRO_CYL_FEEDS = [
    (
        "https://www.catastro.hacienda.gob.es/INSPIRE/CadastralParcels/"
        f"{code}/ES.SDGC.CP.atom_{code}.xml"
    )
    for code in _CATASTRO_CYL_PROVINCE_CODES
]
_CATASTRO_CATALOG_ENDPOINT = (
    "https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx"
)
_MITECO_OGC_API_ROOT = "https://gis.miteco.gob.es/geoserver/ogc/features/v1/"
_MITECO_OGC_BBOX_CRS = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
_MITECO_CYL_OGC_CONFIG = {
    "bbox": [
        SIUR_TILE_BOUNDS["west"],
        SIUR_TILE_BOUNDS["south"],
        SIUR_TILE_BOUNDS["east"],
        SIUR_TILE_BOUNDS["north"],
    ],
    "bbox_crs": _MITECO_OGC_BBOX_CRS,
    "page_size": 2_000,
    "require_number_matched": True,
}
_MITECO_WMS_TERMS = (
    "https://www.miteco.gob.es/es/cartografia-y-sig/ide/"
    "directorio_datos_servicios/caracteristicas_wms.html"
)
_IDECYL_GRID_WMS = "https://idecyl.jcyl.es/geoserver/rejillas/wms"
_IGN_CASTILLA_Y_LEON_MASK_URL = (
    "https://api-features.ign.es/collections/administrativeunit/items"
    "?f=json&codnut2=ES41&nationallevelname=Comunidad%20aut%C3%B3noma"
    "&limit=10&crs=http%3A%2F%2Fwww.opengis.net%2Fdef%2Fcrs%2FOGC"
    "%2F1.3%2FCRS84"
)
_IGN_CASTILLA_Y_LEON_MASK_IDENTITY_SHA256 = (
    "7512d51bbe505f72390c3402c9e7c28132563efb907a174bb2b0c4c5908b8bd3"
)
_EUROSTAT_GRID_PARITY = {
    "100": (
        19,
        "3b275f7d129ece404f5d30945cfaa01106ac4bf4ae342bc4410a754fc53724e8",
    ),
    "50": (
        59,
        "31dc8e9032f005208e73fe9c0953a8705baa939f073daf173d407fb90245ed29",
    ),
    "20": (
        295,
        "2fec8c573394fb627dca042d857e3add71d49c09914c4769270950b2e80a0e87",
    ),
    "5": (
        4_046,
        "3a1d8be941eca72ebc415920cfcd023a7e40ed6468b42510a68ba40627e996bf",
    ),
    "2": (
        24_323,
        "834260837e2ad9b0bcd186175e23ba143faa7e41718afca9017f331ef3435a5f",
    ),
    "1": (
        95_818,
        "5f7ec0b6a6fc773ead27954380eb54aa63a6ba874ce8e71c7a349333acd7266c",
    ),
}
_EUROSTAT_GRID_LOCAL_STYLES = [
    {
        "catalog_style_source_key": "rejilla_eurostat_cyl_blanco",
        "remote_name": "rejilla_eurostat_cyl_blanco",
        "is_default": False,
        "outline_color": "#ffffff",
    },
    {
        "catalog_style_source_key": "rejilla_eurostat_cyl_fucsia",
        "remote_name": "rejilla_eurostat_cyl_fucsia",
        "is_default": False,
        "outline_color": "#e6007e",
    },
    {
        "catalog_style_source_key": "rejilla_eurostat_cyl_morado",
        "remote_name": "rejilla_eurostat_cyl_morado",
        "is_default": True,
        "outline_color": "#6d28d9",
    },
]
_EUROSTAT_GRID_STYLE_SET_SCHEMA = "siur-reviewed-local-grid-style-set/v1"


def _eurostat_grid_style_evidence(
    profile: str,
    catalog_remote_name: str,
) -> dict[str, Any]:
    identity = {
        "schema": _EUROSTAT_GRID_STYLE_SET_SCHEMA,
        "profile": profile,
        "catalog_endpoint_url": _IDECYL_GRID_WMS,
        "catalog_layer_name": catalog_remote_name,
        "styles": [
            {
                "catalog_style_source_key": item["catalog_style_source_key"],
                "remote_name": item["remote_name"],
                "is_default": item["is_default"],
                "outline_color": item["outline_color"],
            }
            for item in _EUROSTAT_GRID_LOCAL_STYLES
        ],
    }
    return {
        "schema": _EUROSTAT_GRID_STYLE_SET_SCHEMA,
        "style_parity_status": "local_style_adaptation_required",
        "local_style_set_identity_sha256": hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "catalog_default_style_source_key": ("rejilla_eurostat_cyl_morado"),
        "catalog_style_count": 3,
    }


def _eurostat_grid_sources() -> tuple[_ReviewedDatasetSource, ...]:
    sources: list[_ReviewedDatasetSource] = []
    for resolution, (
        feature_count,
        identifier_sha256,
    ) in _EUROSTAT_GRID_PARITY.items():
        catalog_remote_name = f"rejilla_eurostat_cyl_{resolution}x{resolution}"
        source_layer = f"grid_{resolution}km_surf"
        profile = f"eurostat-gisco-grid-{resolution}km-cyl-local-styles-v1"
        sources.append(
            _ReviewedDatasetSource(
                profile=profile,
                catalog_endpoint_url=_IDECYL_GRID_WMS,
                catalog_remote_name=catalog_remote_name,
                protocol="download",
                target_kind="vector",
                endpoint_url=(
                    "https://gisco-services.ec.europa.eu/grid/" f"{source_layer}.gpkg"
                ),
                remote_name=source_layer,
                sync_strategy="full_snapshot",
                config={
                    "data_format": "geopackage",
                    "media_type": "application/geopackage+sqlite3",
                    "source_retention": "discard_after_derivation",
                    "vector_transform": {
                        "schema": "reference-masked-geopackage/v1",
                        "mask_url": _IGN_CASTILLA_Y_LEON_MASK_URL,
                        "mask_media_type": "application/json",
                        "mask_max_bytes": 8 * 1024 * 1024,
                        "mask_identity_sha256": (
                            _IGN_CASTILLA_Y_LEON_MASK_IDENTITY_SHA256
                        ),
                        "source_layer": source_layer,
                        "output_layer": f"{source_layer}_cyl",
                        "selected_fields": [
                            "GRD_ID",
                            "X_LLC",
                            "Y_LLC",
                        ],
                        "identifier_field": "GRD_ID",
                        "cell_size_meters": int(resolution) * 1_000,
                        "expected_feature_count": feature_count,
                        "expected_identifier_sha256": identifier_sha256,
                        "source_crs": "EPSG:3035",
                        "mask_target_crs": "EPSG:3035",
                        "predicate": "intersects",
                        "geometry_mode": ("preserve-whole-source-features"),
                    },
                },
                equivalence={
                    "scope": "castilla-y-leon-whole-intersecting-cells",
                    "official_dataset": (
                        f"Eurostat GISCO {resolution} km surface grid"
                    ),
                    "official_boundary": (
                        "IGN administrative unit ES41, national level "
                        "Comunidad autónoma"
                    ),
                    "selected_fields": ["GRD_ID", "X_LLC", "Y_LLC"],
                    "population_fields_excluded": True,
                    "raw_source_retained": False,
                    "transient_source_processing": True,
                    "whole_source_features_preserved": True,
                    "expected_feature_count": feature_count,
                    "expected_identifier_sha256": identifier_sha256,
                    "expected_style_evidence": (
                        _eurostat_grid_style_evidence(profile, catalog_remote_name)
                    ),
                },
            )
        )
    return tuple(sources)


_INES_HISTORICAL_STYLE_REFERENCE = {
    "source_kind": "official-historical-pdf",
    "url": (
        "https://www.miteco.gob.es/content/dam/miteco/es/biodiversidad/"
        "temas/inventarios-nacionales/Efectos_negativos_tcm30-207684.pdf"
    ),
    "document_title": (
        "Efectos negativos sobre el Patrimonio Natural y la Biodiversidad "
        "relacionados con el Inventario Nacional de Erosión de Suelos"
    ),
    "figure_title": ("Figura 1. Erosión laminar y en regueros (niveles erosivos)"),
    "palette": [
        {"class_value": 1, "label": "0 - 5", "color": "#7b8257"},
        {"class_value": 2, "label": "5 - 10", "color": "#9bb068"},
        {"class_value": 3, "label": "10 - 25", "color": "#edd998"},
        {"class_value": 4, "label": "25 - 50", "color": "#f6ec3c"},
        {"class_value": 5, "label": "50 - 100", "color": "#ffd130"},
        {"class_value": 6, "label": "100 - 200", "color": "#cc8e5d"},
        {"class_value": 7, "label": "> 200", "color": "#ac514d"},
        {
            "class_value": 8,
            "label": "Láminas de agua superficiales y humedales",
            "color": "#1eaae2",
        },
        {
            "class_value": 9,
            "label": "Superficies artificiales",
            "color": "#d0d1d4",
        },
    ],
    "adaptation_status": "adaptation_required",
    "parity_claim": "official_historical_adaptation_not_exact",
}


def _required_miteco_mvt_style_reference(
    profile: str,
) -> dict[str, Any]:
    reference = reviewed_miteco_mvt_style_reference(profile)
    if reference is None:
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style profile is not configured"
        )
    return reference


_REVIEWED_DATASET_SOURCES = {
    (item.catalog_endpoint_url, item.catalog_remote_name): item
    for item in (
        *_eurostat_grid_sources(),
        _ReviewedDatasetSource(
            profile="catastro-cadastral-parcels-castilla-y-leon-atom-v1",
            catalog_endpoint_url=_CATASTRO_CATALOG_ENDPOINT,
            catalog_remote_name="Catastro",
            protocol="atom",
            target_kind="vector",
            endpoint_url=_CATASTRO_ATOM_ROOT,
            remote_name="cp:CadastralParcel",
            sync_strategy="full_snapshot",
            config={
                "data_format": "inspire-cadastral-parcel-gml-zip",
                "media_types": [
                    "application/octet-stream",
                    "application/x-zip-compressed",
                    "application/zip",
                ],
                "nested_feed_urls": _CATASTRO_CYL_FEEDS,
                "input_layer": "CadastralParcel",
            },
            equivalence={
                "scope": "castilla-y-leon",
                "province_codes": list(_CATASTRO_CYL_PROVINCE_CODES),
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-flood-q10-ogc-api-features-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ10/wms.aspx"
            ),
            catalog_remote_name="Z.I. con alta probabilidad",
            protocol="ogc_api_features",
            target_kind="vector",
            endpoint_url=_MITECO_OGC_API_ROOT,
            remote_name="agua:Zi_laminas_q10",
            sync_strategy="paged_snapshot",
            config=_MITECO_CYL_OGC_CONFIG,
            equivalence={
                "scope": "castilla-y-leon-reviewed-bbox",
                "official_collection": "agua:Zi_laminas_q10",
                "wms_bulk_eligible": False,
                "wms_terms_url": _MITECO_WMS_TERMS,
                "official_style_reference": (
                    _required_miteco_mvt_style_reference(
                        "miteco-flood-q10-ogc-api-features-v1"
                    )
                ),
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-flood-q50-ogc-api-features-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ50/wms.aspx"
            ),
            catalog_remote_name="Z.I. frecuente",
            protocol="ogc_api_features",
            target_kind="vector",
            endpoint_url=_MITECO_OGC_API_ROOT,
            remote_name="agua:Zi_laminas_q50",
            sync_strategy="paged_snapshot",
            config=_MITECO_CYL_OGC_CONFIG,
            equivalence={
                "scope": "castilla-y-leon-reviewed-bbox",
                "official_collection": "agua:Zi_laminas_q50",
                "wms_bulk_eligible": False,
                "wms_terms_url": _MITECO_WMS_TERMS,
                "official_style_reference": (
                    _required_miteco_mvt_style_reference(
                        "miteco-flood-q50-ogc-api-features-v1"
                    )
                ),
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-flood-q100-ogc-api-features-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ100/wms.aspx"
            ),
            catalog_remote_name="Z.I. con probabilidad media u ocasional",
            protocol="ogc_api_features",
            target_kind="vector",
            endpoint_url=_MITECO_OGC_API_ROOT,
            remote_name="agua:Zi_laminas_q100",
            sync_strategy="paged_snapshot",
            config=_MITECO_CYL_OGC_CONFIG,
            equivalence={
                "scope": "castilla-y-leon-reviewed-bbox",
                "official_collection": "agua:Zi_laminas_q100",
                "wms_bulk_eligible": False,
                "wms_terms_url": _MITECO_WMS_TERMS,
                "official_style_reference": (
                    _required_miteco_mvt_style_reference(
                        "miteco-flood-q100-ogc-api-features-v1"
                    )
                ),
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-flood-q500-ogc-api-features-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ500/wms.aspx"
            ),
            catalog_remote_name="Z.I. con probabilidad baja o excepcional",
            protocol="ogc_api_features",
            target_kind="vector",
            endpoint_url=_MITECO_OGC_API_ROOT,
            remote_name="agua:Zi_laminas_q500",
            sync_strategy="paged_snapshot",
            config=_MITECO_CYL_OGC_CONFIG,
            equivalence={
                "scope": "castilla-y-leon-reviewed-bbox",
                "official_collection": "agua:Zi_laminas_q500",
                "wms_bulk_eligible": False,
                "wms_terms_url": _MITECO_WMS_TERMS,
                "official_style_reference": (
                    _required_miteco_mvt_style_reference(
                        "miteco-flood-q500-ogc-api-features-v1"
                    )
                ),
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-flood-zfp-ogc-api-features-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasZFP/wms.aspx"
            ),
            catalog_remote_name="Zona de flujo preferente",
            protocol="ogc_api_features",
            target_kind="vector",
            endpoint_url=_MITECO_OGC_API_ROOT,
            remote_name="agua:ZI_Laminas_ZFP",
            sync_strategy="paged_snapshot",
            config=_MITECO_CYL_OGC_CONFIG,
            equivalence={
                "scope": "castilla-y-leon-reviewed-bbox",
                "official_collection": "agua:ZI_Laminas_ZFP",
                "wms_bulk_eligible": False,
                "wms_terms_url": _MITECO_WMS_TERMS,
                "official_style_reference": (
                    _required_miteco_mvt_style_reference(
                        "miteco-flood-zfp-ogc-api-features-v1"
                    )
                ),
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-ines-potential-cyl-geotiff-download-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/Biodiversidad/" "INESErosionPotencial"
            ),
            catalog_remote_name="NZ.HazardArea",
            protocol="download",
            target_kind="raster",
            endpoint_url=(
                "https://www.miteco.gob.es/content/dam/miteco/es/"
                "biodiversidad/servicios/banco-datos-naturaleza/1-ines/"
                "cleon/E_PotencialxNiveles41.zip"
            ),
            remote_name="EroPotNiveles_41",
            sync_strategy="conditional_get",
            config={
                "data_format": "geotiff-zip",
                "max_pixels": 1_600_000_000,
                "media_type": "application/zip",
                "vat_value_field": "Value",
                "vat_class_field": "EroPot_pb",
                "vat_class_values": list(range(1, 10)),
            },
            equivalence={
                "scope": "castilla-y-leon",
                "official_dataset": "erosion-potential-levels",
                "official_style_reference": _INES_HISTORICAL_STYLE_REFERENCE,
                "official_raster_style_evidence": {
                    "source_kind": "official-archive-inspection",
                    "archive_member": "EroPotNiveles_41.tiff",
                    "data_type": "Int32",
                    "tiff_color_map_present": False,
                    "embedded_style_files": [],
                    "attribute_table": {
                        "member": "EroPotNiveles_41.tiff.vat.dbf",
                        "fields": [
                            "Value",
                            "Count",
                            "EroPot_pb",
                            "LimProvPen",
                            "NUTS2",
                        ],
                        "classification_field": "EroPot_pb",
                        "class_values": list(range(1, 10)),
                        "color_fields": [],
                    },
                    "adaptation_status": "adaptation_required",
                },
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-ines-laminar-cyl-geotiff-download-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/Biodiversidad/"
                "INESErosionLaminarRaster"
            ),
            catalog_remote_name="NZ.HazardArea",
            protocol="download",
            target_kind="raster",
            endpoint_url=(
                "https://www.miteco.gob.es/content/dam/miteco/es/"
                "biodiversidad/servicios/banco-datos-naturaleza/1-ines/"
                "cleon/E_LaminarxNiveles41.zip"
            ),
            remote_name="EroLamNiveles_41",
            sync_strategy="conditional_get",
            config={
                "data_format": "geotiff-zip",
                "max_pixels": 1_600_000_000,
                "media_type": "application/zip",
                "vat_value_field": "Value",
                "vat_class_field": "EroLam_pb",
                "vat_class_values": list(range(1, 10)),
            },
            equivalence={
                "scope": "castilla-y-leon",
                "official_dataset": "erosion-laminar-levels",
                "official_style_reference": _INES_HISTORICAL_STYLE_REFERENCE,
                "official_raster_style_evidence": {
                    "source_kind": "official-archive-inspection",
                    "archive_member": "EroLamNIveles_41.tiff",
                    "data_type": "Int32",
                    "tiff_color_map_present": False,
                    "embedded_style_files": [],
                    "attribute_table": {
                        "member": "EroLamNIveles_41.tiff.vat.dbf",
                        "fields": [
                            "Value",
                            "Count",
                            "EroLam_pb",
                            "LimProvPen",
                            "NUTS2",
                        ],
                        "classification_field": "EroLam_pb",
                        "class_values": list(range(1, 10)),
                        "color_fields": [],
                    },
                    "adaptation_status": "adaptation_required",
                },
            },
        ),
    )
}

_REVIEWED_DATASET_SOURCES_BY_PROFILE = {
    item.profile: item for item in _REVIEWED_DATASET_SOURCES.values()
}
_REVIEWED_LOCAL_STYLE_IDENTITIES = {
    "catastro-cadastral-parcels-castilla-y-leon-atom-v1": {
        "style_kind": "catastro_parcels",
        "catalog_style_source_key": "default",
        "remote_style_name": "Default",
    },
    "miteco-flood-q10-ogc-api-features-v1": {
        "style_kind": "flood_polygons",
        "catalog_style_source_key": "default",
        "remote_style_name": "default",
    },
    "miteco-flood-q50-ogc-api-features-v1": {
        "style_kind": "flood_polygons",
        "catalog_style_source_key": "default",
        "remote_style_name": "default",
    },
    "miteco-flood-q100-ogc-api-features-v1": {
        "style_kind": "flood_polygons",
        "catalog_style_source_key": "default",
        "remote_style_name": "default",
    },
    "miteco-flood-q500-ogc-api-features-v1": {
        "style_kind": "flood_polygons",
        "catalog_style_source_key": "default",
        "remote_style_name": "default",
    },
    "miteco-flood-zfp-ogc-api-features-v1": {
        "style_kind": "flood_polygons",
        "catalog_style_source_key": "default",
        "remote_style_name": "default",
    },
    "miteco-ines-potential-cyl-geotiff-download-v1": {
        "style_kind": "ines_raster",
        "catalog_style_source_key": ("biodiversidad_ines_erosionpotencial"),
        "remote_style_name": "Biodiversidad_INES_ErosionPotencial",
    },
    "miteco-ines-laminar-cyl-geotiff-download-v1": {
        "style_kind": "ines_raster",
        "catalog_style_source_key": ("biodiversidad_ines_erosionlaminar"),
        "remote_style_name": "Biodiversidad_INES_ErosionLaminar",
    },
}


def _eurostat_grid_local_style_identities(
    profile: str,
    reviewed: _ReviewedDatasetSource,
) -> tuple[dict[str, Any], ...]:
    transform = reviewed.config["vector_transform"]
    selected_layer_name = transform["output_layer"]
    result: list[dict[str, Any]] = []
    for item in _EUROSTAT_GRID_LOCAL_STYLES:
        semantic = {
            "schema": "siur-owned-eurostat-grid-style/v1",
            "profile": profile,
            "catalog_layer_name": reviewed.catalog_remote_name,
            "selected_layer_name": selected_layer_name,
            "catalog_style_source_key": item["catalog_style_source_key"],
            "remote_style_name": item["remote_name"],
            "is_default": item["is_default"],
            "fill_opacity": 0,
            "outline_color": item["outline_color"],
            "outline_width": 1,
            "max_scale_denominator": 4_000_000,
        }
        result.append(
            {
                "style_kind": "eurostat_grid",
                "catalog_style_source_key": item["catalog_style_source_key"],
                "remote_style_name": item["remote_name"],
                "selected_layer_name": selected_layer_name,
                "outline_color": item["outline_color"],
                "style_identity_sha256": hashlib.sha256(
                    json.dumps(
                        semantic,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    return tuple(result)


_REVIEWED_LOCAL_STYLE_IDENTITY_SETS = {
    profile: _eurostat_grid_local_style_identities(profile, reviewed)
    for profile, reviewed in _REVIEWED_DATASET_SOURCES_BY_PROFILE.items()
    if profile.startswith("eurostat-gisco-grid-")
}

_BULK_WMS_GUARDS = {
    item.catalog_endpoint_url: item
    for item in (
        _BulkWMSGuard(
            profile="catastro-no-successive-wms-requests-v1",
            catalog_endpoint_url=_CATASTRO_CATALOG_ENDPOINT,
            terms_url=(
                "https://ovc.catastro.meh.es/Cartografia/WMS/"
                "ServidorWMS.aspx?service=WMS&request=GetCapabilities"
            ),
        ),
        _BulkWMSGuard(
            profile="miteco-flood-q10-no-bulk-wms-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ10/wms.aspx"
            ),
            terms_url=_MITECO_WMS_TERMS,
        ),
        _BulkWMSGuard(
            profile="miteco-flood-q50-no-bulk-wms-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ50/wms.aspx"
            ),
            terms_url=_MITECO_WMS_TERMS,
        ),
        _BulkWMSGuard(
            profile="miteco-flood-q100-no-bulk-wms-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ100/wms.aspx"
            ),
            terms_url=_MITECO_WMS_TERMS,
        ),
        _BulkWMSGuard(
            profile="miteco-flood-q500-no-bulk-wms-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ500/wms.aspx"
            ),
            terms_url=_MITECO_WMS_TERMS,
        ),
        _BulkWMSGuard(
            profile="miteco-flood-zfp-no-bulk-wms-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasZFP/wms.aspx"
            ),
            terms_url=_MITECO_WMS_TERMS,
        ),
        _BulkWMSGuard(
            profile="miteco-ines-potential-no-bulk-wms-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/Biodiversidad/" "INESErosionPotencial"
            ),
            terms_url=_MITECO_WMS_TERMS,
        ),
        _BulkWMSGuard(
            profile="miteco-ines-laminar-no-bulk-wms-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/Biodiversidad/"
                "INESErosionLaminarRaster"
            ),
            terms_url=_MITECO_WMS_TERMS,
        ),
    )
}


class SourceDiscoveryError(ValueError):
    """The catalog entry cannot safely produce acquisition candidates."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "source_discovery_error",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class SourceCandidate:
    protocol: SourceProtocol
    target_kind: TargetKind
    endpoint_url: str
    remote_name: str
    sync_strategy: SyncStrategy
    priority: int
    config: dict[str, Any]
    source_key: str
    definition_sha256: str


def acquisition_candidates(
    service: ReferenceServiceDefinition,
    layer: ReferenceLayerDefinition,
) -> tuple[SourceCandidate, ...]:
    """Return ordered candidates without asserting remote availability.

    Every renderable WMS/WMTS/XYZ layer receives a finite image fallback.  Data
    endpoints are deliberately preferred, but a later capabilities/metadata
    probe must confirm the exact remote collection and determine whether it is
    vector or raster.
    """

    if layer.node_type != "layer" or not layer.remote_name:
        raise SourceDiscoveryError("acquisition requires a named layer node")
    endpoint = _canonical_endpoint(service.base_url)
    protocol = service.upstream_protocol.lower()
    candidates: list[SourceCandidate] = []

    reviewed_ortho = _reviewed_ortho_substitution(endpoint, layer)
    if reviewed_ortho is not None:
        if not reviewed_ortho.promotion_eligible:
            # A future reviewed mapping may still be recorded as ineligible
            # so operators and the UI can explain it without creating a
            # source candidate.
            return ()
        return (
            _candidate(
                protocol="wms_tiles",
                target_kind="tiles",
                endpoint_url=reviewed_ortho.selected_endpoint_url,
                remote_name=reviewed_ortho.selected_layer,
                sync_strategy="tile_seed",
                priority=_REVIEWED_ORTHO_SUBSTITUTION_PRIORITY,
                config=_reviewed_ortho_substitution_config(
                    service,
                    layer,
                    reviewed_ortho,
                ),
            ),
        )

    if protocol == "wms" and is_idecyl_geoserver_catalog_endpoint(endpoint):
        try:
            reviewed_idecyl = reviewed_idecyl_exact_source(
                catalog_layer_source_key=layer.source_key,
                catalog_endpoint_url=endpoint,
                catalog_remote_name=layer.remote_name,
            )
        except IDECyLExactEvidenceError as error:
            raise SourceDiscoveryError(
                "reviewed IDECyL evidence is invalid",
                code="reviewed_idecyl_evidence_invalid",
            ) from error
        if reviewed_idecyl is not None:
            if (
                layer.role != "overlay"
                or layer.renderer != "raster_tile"
                or not layer.source_key.startswith(SIUR_LAYER_PREFIX)
            ):
                raise SourceDiscoveryError(
                    "reviewed IDECyL layer identity is invalid",
                    code="reviewed_idecyl_identity_invalid",
                )
            if reviewed_idecyl.local_service_status != "candidate":
                status = reviewed_idecyl.local_service_status
                raise SourceDiscoveryError(
                    "IDECyL source cannot produce a local-service candidate",
                    code=(
                        "idecyl_permission_pending"
                        if status == "permission_pending"
                        else "idecyl_local_service_restricted"
                    ),
                    evidence={
                        "schema": reviewed_idecyl.evidence["schema"],
                        "audit_layer_id": reviewed_idecyl.audit_layer_id,
                        "local_service_status": status,
                        "reason_codes": list(reviewed_idecyl.reason_codes),
                        "metadata_binding": deepcopy(
                            reviewed_idecyl.evidence["metadata_binding"]
                        ),
                        "authorization_effect": (
                            reviewed_idecyl.evidence["authorization_effect"]
                        ),
                    },
                )
            if (
                reviewed_idecyl.target_kind != "vector"
                or reviewed_idecyl.endpoint_url is None
                or reviewed_idecyl.remote_name is None
                or reviewed_idecyl.candidate_config is None
                or (
                    reviewed_idecyl.protocol == "download"
                    and reviewed_idecyl.sync_strategy != "conditional_get"
                )
                or (
                    reviewed_idecyl.protocol == "wfs"
                    and reviewed_idecyl.sync_strategy != "full_snapshot"
                )
                or reviewed_idecyl.protocol not in {"download", "wfs"}
            ):
                raise SourceDiscoveryError(
                    "reviewed IDECyL candidate is incomplete",
                    code="reviewed_idecyl_evidence_invalid",
                )
            try:
                population_exclusion = (
                    reviewed_idecyl_population_style_exclusion_for_source(
                        reviewed_idecyl
                    )
                )
                nitrate_styles = reviewed_idecyl_nitrate_styles_for_source(
                    reviewed_idecyl
                )
                style_exclusion = reviewed_idecyl_style_exclusion(
                    reviewed_idecyl.profile
                )
                local_style_exclusion = (
                    reviewed_idecyl_local_style_exclusion_for_source(
                        reviewed_idecyl
                    )
                )
            except (
                IDECyLLocalStyleEvidenceError,
                IDECyLPopulationNitrateStyleEvidenceError,
                IDECyLStyleExclusionEvidenceError,
            ) as error:
                raise SourceDiscoveryError(
                    "reviewed IDECyL local-style evidence is invalid",
                    code="reviewed_idecyl_local_style_evidence_invalid",
                ) from error
            if population_exclusion is not None:
                _idecyl_population_exclusion_source_definition(
                    layer,
                    population_exclusion,
                )
                return _idecyl_baked_wms_candidates(
                    service,
                    layer,
                    reviewed_idecyl,
                    reason_codes=tuple(
                        population_exclusion["reason_codes"]
                    ),
                    expected_catalog_styles=population_exclusion[
                        "catalog_styles"
                    ],
                    exclusion_evidence={
                        "kind": "population_style_exclusion",
                        "manifest_resource": (
                            IDECYL_POPULATION_STYLE_MANIFEST_RESOURCE
                        ),
                        "manifest_sha256": (
                            IDECYL_POPULATION_STYLE_MANIFEST_SHA256
                        ),
                        "evidence_identity_sha256": population_exclusion[
                            "exclusion_identity_sha256"
                        ],
                    },
                )
            if nitrate_styles:
                definition = _idecyl_nitrate_source_definition(
                    layer,
                    nitrate_styles,
                )
                return (
                    _candidate(
                        protocol=definition["protocol"],
                        target_kind=definition["target_kind"],
                        endpoint_url=definition["endpoint_url"],
                        remote_name=definition["remote_name"],
                        sync_strategy=definition["sync_strategy"],
                        priority=definition["priority"],
                        config=definition["config"],
                    ),
                )
            if style_exclusion is not None:
                return _idecyl_baked_wms_candidates(
                    service,
                    layer,
                    reviewed_idecyl,
                    reason_codes=style_exclusion.reason_codes,
                    expected_catalog_styles=(
                        style_exclusion.required_catalog_styles
                    ),
                    exclusion_evidence={
                        "kind": "style_exclusion",
                        "manifest_resource": (
                            IDECYL_STYLE_EXCLUSION_MANIFEST_RESOURCE
                        ),
                        "manifest_sha256": (
                            IDECYL_STYLE_EXCLUSION_MANIFEST_SHA256
                        ),
                        "evidence_identity_sha256": (
                            style_exclusion.evidence[
                                "evidence_identity_sha256"
                            ]
                        ),
                    },
                )
            if local_style_exclusion is not None:
                return _idecyl_baked_wms_candidates(
                    service,
                    layer,
                    reviewed_idecyl,
                    reason_codes=tuple(
                        local_style_exclusion["reason_codes"]
                    ),
                    expected_catalog_style_source_keys=tuple(
                        local_style_exclusion[
                            "catalog_style_source_keys"
                        ]
                    ),
                    exclusion_evidence={
                        "kind": "local_style_exclusion",
                        **local_style_exclusion["evidence_binding"],
                    },
                )
            config = deepcopy(reviewed_idecyl.candidate_config)
            if "archive_styles" in config:
                reviewed_catalog_styles = config.pop(
                    "archive_style_catalog",
                    None,
                )
                config["archive_styles"] = _idecyl_archive_style_config(
                    layer,
                    config["archive_styles"],
                    reviewed_catalog_styles=reviewed_catalog_styles,
                )
            try:
                local_styles = reviewed_idecyl_local_styles_for_source(reviewed_idecyl)
            except IDECyLLocalStyleEvidenceError as error:
                raise SourceDiscoveryError(
                    "reviewed IDECyL local-style evidence is invalid",
                    code="reviewed_idecyl_local_style_evidence_invalid",
                ) from error
            if len(local_styles) == 1:
                config["reviewed_local_style"] = _idecyl_local_style_config(
                    layer, local_styles
                )[0]
            elif local_styles:
                config["reviewed_local_styles"] = _idecyl_local_style_config(
                    layer, local_styles
                )
            if reviewed_idecyl.protocol == "wfs":
                config.update(_geoserver_style_config(endpoint, layer))
            config["reviewed_equivalence"] = deepcopy(reviewed_idecyl.evidence)
            selected_protocol: SourceProtocol = (
                "wfs" if reviewed_idecyl.protocol == "wfs" else "download"
            )
            selected_sync_strategy: SyncStrategy = (
                "full_snapshot" if selected_protocol == "wfs" else "conditional_get"
            )
            return (
                _candidate(
                    protocol=selected_protocol,
                    target_kind="vector",
                    endpoint_url=reviewed_idecyl.endpoint_url,
                    remote_name=reviewed_idecyl.remote_name,
                    sync_strategy=selected_sync_strategy,
                    priority=_REVIEWED_DATASET_SOURCE_PRIORITY,
                    config=config,
                ),
            )
    reviewed_dataset = _reviewed_dataset_source(endpoint, layer)
    if reviewed_dataset is not None:
        return (
            _candidate(
                protocol=reviewed_dataset.protocol,
                target_kind=reviewed_dataset.target_kind,
                endpoint_url=reviewed_dataset.endpoint_url,
                remote_name=reviewed_dataset.remote_name,
                sync_strategy=reviewed_dataset.sync_strategy,
                priority=_REVIEWED_DATASET_SOURCE_PRIORITY,
                config=_reviewed_dataset_source_config(
                    endpoint,
                    layer,
                    reviewed_dataset,
                ),
            ),
        )

    bulk_wms_guard = _bulk_wms_guard(endpoint, layer)
    if bulk_wms_guard is not None:
        evidence = {
            "schema": _BULK_WMS_GUARD_SCHEMA,
            "profile": bulk_wms_guard.profile,
            "catalog_endpoint_url": endpoint,
            "catalog_remote_name": layer.remote_name,
            "wms_tiles_eligible": False,
            "wms_guard": "bulk_wms_prohibited",
            "terms_url": bulk_wms_guard.terms_url,
        }
        raise SourceDiscoveryError(
            "bulk_wms_prohibited: no exact reviewed non-WMS source matches "
            "this layer and mass WMS seeding is prohibited",
            code="bulk_wms_prohibited",
            evidence=evidence,
        )

    if protocol == "wms":
        parts = urlsplit(endpoint)
        geoserver = _GEOSERVER_SERVICE_RE.fullmatch(parts.path)
        if geoserver:
            prefix = geoserver.group("prefix")
            style_config = _geoserver_style_config(
                _with_path(parts, parts.path),
                layer,
            )
            candidates.extend(
                (
                    _candidate(
                        protocol="wfs",
                        target_kind="vector",
                        endpoint_url=_with_path(parts, f"{prefix}/wfs"),
                        remote_name=layer.remote_name,
                        sync_strategy="full_snapshot",
                        priority=20,
                        config={
                            "discovery": "wfs_capabilities",
                            "wfs_snapshot": {
                                "mode": "single_response",
                            },
                            **style_config,
                        },
                    ),
                    _candidate(
                        protocol="wcs",
                        target_kind="raster",
                        endpoint_url=_with_path(parts, f"{prefix}/wcs"),
                        remote_name=layer.remote_name,
                        sync_strategy="full_snapshot",
                        priority=30,
                        config={
                            "discovery": "wcs_capabilities",
                            **style_config,
                        },
                    ),
                )
            )

        inspire = _WMS_INSPIRE_RE.fullmatch(parts.path)
        if inspire:
            candidates.append(
                _candidate(
                    protocol="wfs",
                    target_kind="vector",
                    endpoint_url=_with_path(
                        parts,
                        "/wfs-inspire" + inspire.group("suffix"),
                    ),
                    remote_name=layer.remote_name,
                    sync_strategy="full_snapshot",
                    priority=25,
                    config={
                        "discovery": "wfs_capabilities",
                        "wfs_snapshot": {
                            "mode": "single_response",
                        },
                    },
                )
            )

        arcgis = _ARCGIS_WMS_RE.fullmatch(parts.path)
        if arcgis:
            candidates.append(
                _candidate(
                    protocol="arcgis_rest",
                    target_kind="vector",
                    endpoint_url=_with_path(parts, arcgis.group("prefix")),
                    remote_name=layer.remote_name,
                    sync_strategy="paged_snapshot",
                    priority=15,
                    config={"discovery": "arcgis_mapserver"},
                )
            )

        candidates.append(
            _candidate(
                protocol="wms_tiles",
                target_kind="tiles",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="tile_seed",
                priority=90,
                config=_tile_config(service, layer),
            )
        )
    elif protocol == "wmts":
        reviewed_wms = _reviewed_native_wms(endpoint, layer)
        if reviewed_wms is not None:
            candidates.append(
                _candidate(
                    protocol="wms_tiles",
                    target_kind="tiles",
                    endpoint_url=reviewed_wms.endpoint_url,
                    remote_name=reviewed_wms.remote_name,
                    sync_strategy="tile_seed",
                    priority=_REVIEWED_NATIVE_WMS_PRIORITY,
                    config=_reviewed_native_wms_config(
                        service,
                        layer,
                        reviewed_wms,
                    ),
                )
            )
        candidates.append(
            _candidate(
                protocol="wmts",
                target_kind="tiles",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="tile_seed",
                priority=50,
                config=_tile_config(service, layer),
            )
        )
    elif protocol == "xyz":
        candidates.append(
            _candidate(
                protocol="xyz",
                target_kind="tiles",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="tile_seed",
                priority=50,
                config=_tile_config(service, layer),
            )
        )
    elif protocol == "wfs":
        candidates.append(
            _candidate(
                protocol="wfs",
                target_kind="vector",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="full_snapshot",
                priority=10,
                config={
                    "discovery": "wfs_capabilities",
                    "wfs_snapshot": {
                        "mode": "single_response",
                    },
                },
            )
        )
    elif protocol == "arcgis_rest":
        candidates.append(
            _candidate(
                protocol="arcgis_rest",
                target_kind="vector",
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="paged_snapshot",
                priority=10,
                config={"discovery": "arcgis_mapserver"},
            )
        )
    elif protocol == "local":
        candidates.append(
            _candidate(
                protocol="local",
                target_kind=_local_target_kind(layer),
                endpoint_url=endpoint,
                remote_name=layer.remote_name,
                sync_strategy="manual",
                priority=10,
                config={},
            )
        )
    else:
        raise SourceDiscoveryError(f"unsupported catalog protocol: {protocol}")

    return tuple(sorted(_deduplicate(candidates), key=lambda item: item.priority))


def _idecyl_baked_wms_candidates(
    service: ReferenceServiceDefinition,
    layer: ReferenceLayerDefinition,
    reviewed: ReviewedIDECyLExactSource,
    *,
    reason_codes: tuple[str, ...],
    exclusion_evidence: dict[str, Any],
    expected_catalog_styles: Any = None,
    expected_catalog_style_source_keys: tuple[str, ...] = (),
) -> tuple[SourceCandidate, ...]:
    """Select WMS tiles only when reviewed vector style parity is incomplete."""

    if (
        reviewed.catalog_layer_source_key != layer.source_key
        or reviewed.catalog_endpoint_url
        != _canonical_endpoint(service.base_url)
        or reviewed.catalog_remote_name != layer.remote_name
        or not reason_codes
        or any(not isinstance(item, str) or not item for item in reason_codes)
        or not isinstance(exclusion_evidence, dict)
        or not exclusion_evidence
    ):
        raise SourceDiscoveryError(
            "reviewed IDECyL baked fallback identity is invalid",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    active_styles = [
        style
        for style in layer.styles
        if style.status in {"active", "degraded"}
    ]
    if (
        len({style.source_key for style in active_styles})
        != len(active_styles)
        or len(
            {
                style.remote_name
                for style in active_styles
                if style.remote_name is not None
            }
        )
        != len(active_styles)
        or any(
            not isinstance(style.remote_name, str)
            or not style.remote_name
            for style in active_styles
        )
        or (
            active_styles
            and sum(style.is_default for style in active_styles) != 1
        )
    ):
        raise SourceDiscoveryError(
            "catalog IDECyL styles are invalid for baked fallback",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    actual_style_identities = {
        (
            style.source_key,
            style.remote_name,
            style.title,
            style.is_default,
        )
        for style in active_styles
    }
    if expected_catalog_styles is not None:
        expected_style_identities = {
            (
                item.get("catalog_style_source_key"),
                item.get("remote_name"),
                item.get("title"),
                item.get("is_default"),
            )
            for item in expected_catalog_styles
            if isinstance(item, dict)
        }
        if (
            len(expected_style_identities) != len(expected_catalog_styles)
            or actual_style_identities != expected_style_identities
        ):
            raise SourceDiscoveryError(
                "catalog IDECyL styles differ from their baked fallback evidence",
                code="reviewed_idecyl_local_style_identity_invalid",
            )
    if expected_catalog_style_source_keys and {
        style.source_key for style in active_styles
    } != set(expected_catalog_style_source_keys):
        raise SourceDiscoveryError(
            "catalog IDECyL style coverage differs from its baked fallback evidence",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    catalog_styles = sorted(
        (
            {
                "catalog_style_source_key": style.source_key,
                "remote_name": style.remote_name,
                "is_default": style.is_default,
            }
            for style in active_styles
        ),
        key=lambda item: item["catalog_style_source_key"],
    )
    tile_config = _tile_config(service, layer)
    tile_config["style_name"] = (
        next(
            style.remote_name
            for style in active_styles
            if style.is_default
        )
        if active_styles
        else ""
    )
    tile_config["reviewed_baked_wms_fallback"] = {
        "schema": _REVIEWED_IDECYL_BAKED_WMS_SCHEMA,
        "profile": reviewed.profile,
        "audit_layer_id": reviewed.audit_layer_id,
        "catalog_identity": {
            "catalog_layer_source_key": reviewed.catalog_layer_source_key,
            "catalog_endpoint_url": reviewed.catalog_endpoint_url,
            "catalog_remote_name": reviewed.catalog_remote_name,
        },
        "complete_vector_style_parity": False,
        "required_verification": (
            "capabilities_and_baked_archive_per_catalog_style"
        ),
        "reason_codes": list(reason_codes),
        "catalog_styles": catalog_styles,
        "implicit_default_style": not bool(active_styles),
        "exclusion_evidence": deepcopy(exclusion_evidence),
    }
    return (
        _candidate(
            protocol="wms_tiles",
            target_kind="tiles",
            endpoint_url=reviewed.catalog_endpoint_url,
            remote_name=reviewed.catalog_remote_name,
            sync_strategy="tile_seed",
            priority=_REVIEWED_DATASET_SOURCE_PRIORITY,
            config=tile_config,
        ),
    )


def _idecyl_archive_style_config(
    layer: ReferenceLayerDefinition,
    raw_styles: Any,
    *,
    reviewed_catalog_styles: Any = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_styles, list) or not raw_styles:
        raise SourceDiscoveryError(
            "reviewed IDECyL archive styles are invalid",
            code="reviewed_idecyl_style_identity_invalid",
        )
    catalog_styles = [
        style for style in layer.styles if style.status in {"active", "degraded"}
    ]
    if (
        not catalog_styles
        or len({style.source_key for style in catalog_styles}) != len(catalog_styles)
        or len(
            {
                style.remote_name
                for style in catalog_styles
                if style.remote_name is not None
            }
        )
        != len(catalog_styles)
        or sum(style.is_default for style in catalog_styles) != 1
    ):
        raise SourceDiscoveryError(
            "catalog IDECyL styles differ from the reviewed archive",
            code="reviewed_idecyl_style_identity_invalid",
        )
    key_sets = {
        frozenset(item) if isinstance(item, dict) else frozenset()
        for item in raw_styles
    }
    if key_sets == {_LEGACY_IDECYL_ARCHIVE_STYLE_EVIDENCE_KEYS}:
        expected_default = _LEGACY_IDECYL_ARCHIVE_DEFAULTS.get(layer.remote_name or "")
        defaults = [style.remote_name for style in catalog_styles if style.is_default]
        reviewed_by_remote = {
            item["remote_name"]: item
            for item in raw_styles
            if isinstance(item.get("remote_name"), str)
        }
        if (
            len(raw_styles) != 3
            or len(reviewed_by_remote) != len(raw_styles)
            or expected_default is None
            or defaults != [expected_default]
            or {style.remote_name for style in catalog_styles}
            != set(reviewed_by_remote)
        ):
            raise SourceDiscoveryError(
                "legacy IDECyL archive styles differ from their reviewed identity",
                code="reviewed_idecyl_style_identity_invalid",
            )
        result = [
            {
                "catalog_style_source_key": style.source_key,
                "remote_name": style.remote_name,
                "archive_member": reviewed_by_remote[style.remote_name][
                    "archive_member"
                ],
                "sha256": reviewed_by_remote[style.remote_name]["sha256"],
            }
            for style in catalog_styles
            if style.remote_name is not None
        ]
        result.sort(key=lambda item: item["catalog_style_source_key"])
    elif key_sets == {EXACT_ARCHIVE_STYLE_KEYS}:
        result = deepcopy(raw_styles)
        expected_identities = {
            (
                style.source_key,
                style.remote_name,
                style.is_default,
            )
            for style in catalog_styles
        }
        reviewed_identities = {
            (
                item.get("catalog_style_source_key"),
                item.get("remote_name"),
                item.get("is_default"),
            )
            for item in result
        }
        if reviewed_catalog_styles is None:
            catalog_inventory_matches = reviewed_identities == expected_identities
        else:
            if not isinstance(reviewed_catalog_styles, list):
                catalog_inventory_matches = False
            else:
                reviewed_catalog_identities = {
                    (
                        item.get("catalog_style_source_key"),
                        item.get("remote_name"),
                        item.get("is_default"),
                    )
                    for item in reviewed_catalog_styles
                    if isinstance(item, dict)
                    and set(item)
                    == {
                        "catalog_style_source_key",
                        "remote_name",
                        "is_default",
                    }
                }
                catalog_inventory_matches = (
                    len(reviewed_catalog_identities) == len(reviewed_catalog_styles)
                    and reviewed_catalog_identities == expected_identities
                    and reviewed_identities == expected_identities
                )
        if not catalog_inventory_matches:
            raise SourceDiscoveryError(
                "catalog IDECyL styles differ from the reviewed archive",
                code="reviewed_idecyl_style_identity_invalid",
            )
    else:
        raise SourceDiscoveryError(
            "reviewed IDECyL archive style evidence is incomplete or mixed",
            code="reviewed_idecyl_style_identity_invalid",
        )
    try:
        reviewed_archive_style_specs(
            {
                "data_format": "geopackage-zip",
                "archive_styles": result,
            },
            protocol="download",
            target_kind="vector",
            selected_layer_name=layer.remote_name or "",
        )
    except ReviewedArchiveStyleError as error:
        raise SourceDiscoveryError(
            "reviewed IDECyL archive styles are invalid",
            code="reviewed_idecyl_style_identity_invalid",
        ) from error
    return result


def _idecyl_local_style_config(
    layer: ReferenceLayerDefinition,
    reviewed: tuple[ReviewedIDECyLLocalStyle, ...],
) -> list[dict[str, Any]]:
    expected = sorted(
        (idecyl_local_style_config(item) for item in reviewed),
        key=lambda item: item["catalog_style_source_key"],
    )
    styles = [style for style in layer.styles if style.status in {"active", "degraded"}]
    reviewed_identities = {
        (
            item.catalog_style_source_key,
            item.remote_style_name,
            item.style_title,
            item.is_default,
        )
        for item in reviewed
    }
    catalog_identities = {
        (
            style.source_key,
            style.remote_name,
            style.title,
            style.is_default,
        )
        for style in styles
    }
    defaults = [item.catalog_style_source_key for item in reviewed if item.is_default]
    if (
        not reviewed
        or len(layer.styles) != len(reviewed)
        or len(styles) != len(reviewed)
        or len(reviewed_identities) != len(reviewed)
        or reviewed_identities != catalog_identities
        or len(defaults) != 1
        or layer.style_name != defaults[0]
    ):
        raise SourceDiscoveryError(
            "catalog IDECyL styles differ from reviewed local adaptations",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    return expected


def _idecyl_nitrate_source_definition(
    layer: ReferenceLayerDefinition,
    reviewed: tuple[ReviewedIDECyLNitrateStyle, ...],
) -> dict[str, Any]:
    """Bind fifteen adapted years plus the one exact archive style."""

    if len(reviewed) != 15:
        raise SourceDiscoveryError(
            "reviewed IDECyL nitrate styles are incomplete",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    definition = idecyl_nitrate_expected_source_definition(reviewed[0])
    catalog = definition["config"].get("archive_style_catalog")
    if not isinstance(catalog, list):
        raise SourceDiscoveryError(
            "reviewed IDECyL nitrate catalog is missing",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    expected = {
        (
            item["catalog_style_source_key"],
            item["remote_name"],
            item["is_default"],
        )
        for item in catalog
    }
    actual_styles = [
        style for style in layer.styles if style.status in {"active", "degraded"}
    ]
    actual = {
        (style.source_key, style.remote_name, style.is_default)
        for style in actual_styles
    }
    expected_titles = {
        item.catalog_style_source_key: item.style_title for item in reviewed
    }
    expected_titles["coad_cyl_nitrat_aguas_subterr_2021"] = (
        "Recintos municipales 2021 paleta color"
    )
    if (
        len(layer.styles) != 16
        or len(actual_styles) != 16
        or len(actual) != 16
        or actual != expected
        or {style.source_key: style.title for style in actual_styles} != expected_titles
        or layer.style_name != "coad_cyl_nitrat_aguas_subterr_2021"
    ):
        raise SourceDiscoveryError(
            "catalog IDECyL nitrate styles differ from reviewed evidence",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    return definition


def _idecyl_population_exclusion_source_definition(
    layer: ReferenceLayerDefinition,
    exclusion: dict[str, Any],
) -> dict[str, Any]:
    """Bind the catalog styles which lack safe local render evidence."""

    definition = idecyl_population_expected_source_definition(exclusion)
    catalog = exclusion.get("catalog_styles")
    if not isinstance(catalog, list):
        raise SourceDiscoveryError(
            "reviewed IDECyL population exclusion catalog is missing",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    expected = {
        (
            item["catalog_style_source_key"],
            item["remote_name"],
            item["title"],
            item["is_default"],
        )
        for item in catalog
    }
    actual_styles = [
        style for style in layer.styles if style.status in {"active", "degraded"}
    ]
    actual = {
        (
            style.source_key,
            style.remote_name,
            style.title,
            style.is_default,
        )
        for style in actual_styles
    }
    default = next(
        item["catalog_style_source_key"]
        for item in catalog
        if item["is_default"] is True
    )
    if (
        exclusion.get("local_service_eligible") is not False
        or len(layer.styles) != len(catalog)
        or len(actual_styles) != len(catalog)
        or len(actual) != len(catalog)
        or actual != expected
        or layer.style_name != default
    ):
        raise SourceDiscoveryError(
            "catalog IDECyL population styles differ from their exclusion",
            code="reviewed_idecyl_local_style_identity_invalid",
        )
    return definition


def _reviewed_dataset_source(
    endpoint: str,
    layer: ReferenceLayerDefinition,
) -> _ReviewedDatasetSource | None:
    if (
        not layer.source_key.startswith(SIUR_LAYER_PREFIX)
        or layer.role != "overlay"
        or layer.renderer != "raster_tile"
        or not layer.remote_name
    ):
        return None
    return _REVIEWED_DATASET_SOURCES.get((endpoint, layer.remote_name))


def _reviewed_ortho_substitution(
    endpoint: str,
    layer: ReferenceLayerDefinition,
) -> ReviewedIgnOrthoSubstitution | None:
    if (
        not layer.source_key.startswith(SIUR_LAYER_PREFIX)
        or layer.role != "overlay"
        or layer.renderer != "raster_tile"
        or not layer.remote_name
    ):
        return None
    try:
        return reviewed_ign_ortho_substitution(endpoint, layer.remote_name)
    except ReviewedOrthoEvidenceError as error:
        raise SourceDiscoveryError(
            "reviewed IGN ortho evidence is invalid",
            code="reviewed_ortho_evidence_invalid",
        ) from error


def _reviewed_ortho_substitution_config(
    service: ReferenceServiceDefinition,
    layer: ReferenceLayerDefinition,
    reviewed: ReviewedIgnOrthoSubstitution,
) -> dict[str, Any]:
    config = _tile_config(service, layer)
    expected_operations = {
        "bounds": dict(SIUR_TILE_BOUNDS),
        "min_zoom": 0,
        "max_zoom": 15,
        "coverage_profile": "siur-castilla-y-leon-ortho-native-z15-v1",
        "wms_supertile_size": SIUR_WMS_SUPERTILE_SIZE,
        "max_tile_count": 2_000_000,
    }
    actual_operations = {
        "bounds": reviewed.bounds,
        "min_zoom": reviewed.min_zoom,
        "max_zoom": reviewed.max_zoom,
        "coverage_profile": reviewed.coverage_profile,
        "wms_supertile_size": reviewed.wms_supertile_size,
        "max_tile_count": reviewed.max_tile_count,
    }
    if actual_operations != expected_operations:
        raise SourceDiscoveryError(
            "reviewed IGN ortho operational profile is not allowed"
        )
    config.update(
        {
            **expected_operations,
            "format": reviewed.image_format,
            "style_name": reviewed.style_name,
            "coverage_required": True,
            "reviewed_equivalence": reviewed_ign_ortho_equivalence(reviewed),
        }
    )
    expected = reviewed_ign_ortho_expected_source_definition(reviewed)
    if config != expected["config"]:
        raise SourceDiscoveryError("reviewed IGN ortho source definition is not exact")
    return config


def _reviewed_dataset_source_config(
    catalog_endpoint: str,
    layer: ReferenceLayerDefinition,
    reviewed: _ReviewedDatasetSource,
) -> dict[str, Any]:
    config = deepcopy(reviewed.config)
    expected_style_evidence = reviewed.equivalence.get("expected_style_evidence")
    if expected_style_evidence is not None and not isinstance(
        expected_style_evidence, dict
    ):
        raise SourceDiscoveryError(
            "reviewed dataset style evidence is invalid",
            code="reviewed_dataset_style_changed",
        )
    if (
        isinstance(expected_style_evidence, dict)
        and expected_style_evidence.get("schema") == _EUROSTAT_GRID_STYLE_SET_SCHEMA
    ):
        catalog_styles = sorted(
            (
                {
                    "catalog_style_source_key": style.source_key,
                    "remote_name": style.remote_name,
                    "is_default": style.is_default,
                }
                for style in layer.styles
                if style.status in {"active", "degraded"}
            ),
            key=lambda item: item["catalog_style_source_key"],
        )
        expected_catalog_styles = [
            {
                "catalog_style_source_key": item["catalog_style_source_key"],
                "remote_name": item["remote_name"],
                "is_default": item["is_default"],
            }
            for item in _EUROSTAT_GRID_LOCAL_STYLES
        ]
        expected_catalog_styles.sort(key=lambda item: item["catalog_style_source_key"])
        if (
            catalog_endpoint != reviewed.catalog_endpoint_url
            or layer.remote_name != reviewed.catalog_remote_name
            or catalog_styles != expected_catalog_styles
        ):
            raise SourceDiscoveryError(
                "reviewed grid style identities no longer match the catalog",
                code="reviewed_dataset_style_changed",
            )
        style_evidence = _eurostat_grid_style_evidence(
            reviewed.profile,
            reviewed.catalog_remote_name,
        )
        if style_evidence != expected_style_evidence:
            raise SourceDiscoveryError(
                "reviewed grid style identity is invalid",
                code="reviewed_dataset_style_changed",
            )
    else:
        style_evidence = _reviewed_style_evidence(
            catalog_endpoint,
            layer,
        )
    if (
        isinstance(expected_style_evidence, dict)
        and expected_style_evidence.get("schema") != _EUROSTAT_GRID_STYLE_SET_SCHEMA
    ):
        observed_expected = deepcopy(expected_style_evidence)
        observed_expected["style_parity_status"] = style_evidence["style_parity_status"]
        if observed_expected != style_evidence:
            raise SourceDiscoveryError(
                "reviewed dataset style identities no longer match the catalog",
                code="reviewed_dataset_style_changed",
            )
        style_evidence = deepcopy(expected_style_evidence)
    config["reviewed_equivalence"] = _reviewed_dataset_equivalence(
        reviewed,
        style_evidence,
        catalog_endpoint_url=catalog_endpoint,
    )
    return config


def _reviewed_dataset_equivalence(
    reviewed: _ReviewedDatasetSource,
    style_evidence: dict[str, Any],
    *,
    catalog_endpoint_url: str,
) -> dict[str, Any]:
    details = deepcopy(reviewed.equivalence)
    details.pop("expected_style_evidence", None)
    equivalence = {
        "schema": _REVIEWED_DATASET_SOURCE_SCHEMA,
        "profile": reviewed.profile,
        "catalog_protocol": "wms",
        "catalog_remote_name": reviewed.catalog_remote_name,
        "selected_protocol": reviewed.protocol,
        "selected_endpoint_url": reviewed.endpoint_url,
        "selected_remote_name": reviewed.remote_name,
        "target_kind": reviewed.target_kind,
        **details,
        "style_evidence": style_evidence,
    }
    if not reviewed.profile.startswith("eurostat-gisco-grid-"):
        equivalence["catalog_endpoint_url"] = catalog_endpoint_url
    return equivalence


def reviewed_cross_origin_style_source(
    candidate: SourceCandidate,
) -> bool:
    """Prove a cross-origin style request against the immutable allowlist."""

    if "style_endpoint_url" not in candidate.config:
        return False
    raw_equivalence = candidate.config.get("reviewed_equivalence")
    if not isinstance(raw_equivalence, dict):
        return False
    profile = raw_equivalence.get("profile")
    if not isinstance(profile, str):
        return False
    reviewed = _REVIEWED_DATASET_SOURCES_BY_PROFILE.get(profile)
    if reviewed is None:
        return False
    expected_style_evidence = reviewed.equivalence.get("expected_style_evidence")
    if not isinstance(expected_style_evidence, dict):
        return False
    expected_equivalence = _reviewed_dataset_equivalence(
        reviewed,
        deepcopy(expected_style_evidence),
        catalog_endpoint_url=reviewed.catalog_endpoint_url,
    )
    expected_candidate = _candidate(
        protocol=reviewed.protocol,
        target_kind=reviewed.target_kind,
        endpoint_url=reviewed.endpoint_url,
        remote_name=reviewed.remote_name,
        sync_strategy=reviewed.sync_strategy,
        priority=_REVIEWED_DATASET_SOURCE_PRIORITY,
        config={
            **deepcopy(reviewed.config),
            "reviewed_equivalence": expected_equivalence,
        },
    )
    return candidate == expected_candidate


def _reviewed_style_evidence(
    catalog_endpoint: str,
    layer: ReferenceLayerDefinition,
) -> dict[str, Any]:
    styles = sorted(
        (
            {
                "catalog_style_source_key": style.source_key,
                "remote_name": style.remote_name,
                "is_default": style.is_default,
            }
            for style in layer.styles
            if style.status in {"active", "degraded"}
        ),
        key=lambda item: item["catalog_style_source_key"],
    )
    defaults = [item for item in styles if item["is_default"]]
    default = defaults[0] if len(defaults) == 1 else None
    return {
        "style_parity_status": "local_style_adaptation_required",
        "original_wms_endpoint_url": catalog_endpoint,
        "original_wms_layer_name": layer.remote_name,
        "catalog_default_style_source_key": (
            default["catalog_style_source_key"]
            if default is not None
            else layer.style_name
        ),
        "original_wms_style_name": (
            default["remote_name"] if default is not None else layer.style_name
        ),
        "catalog_styles": styles,
    }


def _bulk_wms_guard(
    endpoint: str,
    layer: ReferenceLayerDefinition,
) -> _BulkWMSGuard | None:
    if layer.renderer != "raster_tile" or not layer.remote_name:
        return None
    return _BULK_WMS_GUARDS.get(endpoint)


def _reviewed_native_wms(
    endpoint: str,
    layer: ReferenceLayerDefinition,
) -> _ReviewedNativeWMS | None:
    """Return an exact, reviewed SIUR WMTS-to-WMS equivalence.

    The hard-coded identity deliberately includes the catalog endpoint and
    remote collection.  A similarly named third-party layer, a query-bearing
    endpoint, or a non-SIUR catalog entry never inherits this preference.
    """

    if (
        not layer.source_key.startswith(SIUR_LAYER_PREFIX)
        or layer.role != "base"
        or layer.renderer != "raster_tile"
        or not layer.remote_name
    ):
        return None
    parts = urlsplit(endpoint)
    if parts.query:
        return None
    normalized_endpoint = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path.rstrip("/") or "/",
            "",
            "",
        )
    )
    return _REVIEWED_NATIVE_WMS.get((normalized_endpoint, layer.remote_name))


def _reviewed_native_wms_config(
    service: ReferenceServiceDefinition,
    layer: ReferenceLayerDefinition,
    reviewed: _ReviewedNativeWMS,
) -> dict[str, Any]:
    config = _tile_config(service, layer)
    coverage_profile = config.get("coverage_profile")
    if coverage_profile not in SIUR_WMS_SUPERTILE_COVERAGE_PROFILES:
        raise SourceDiscoveryError(
            "reviewed native WMS requires an allowed SIUR coverage profile"
        )
    config.update(
        {
            "format": reviewed.image_format,
            "style_name": "",
            "wms_supertile_size": SIUR_WMS_SUPERTILE_SIZE,
            "reviewed_equivalence": {
                "schema": _REVIEWED_NATIVE_WMS_SCHEMA,
                "profile": reviewed.profile,
                "catalog_protocol": "wmts",
                "catalog_endpoint_url": reviewed.catalog_endpoint_url,
                "catalog_remote_name": reviewed.catalog_remote_name,
                "selected_protocol": "wms_tiles",
                "selected_endpoint_url": reviewed.endpoint_url,
                "selected_remote_name": reviewed.remote_name,
                "image_format": reviewed.image_format,
                "coverage_profile": coverage_profile,
                "wms_supertile_size": SIUR_WMS_SUPERTILE_SIZE,
            },
        }
    )
    return config


def _candidate(
    *,
    protocol: SourceProtocol,
    target_kind: TargetKind,
    endpoint_url: str,
    remote_name: str,
    sync_strategy: SyncStrategy,
    priority: int,
    config: dict[str, Any],
) -> SourceCandidate:
    identity = {
        "protocol": protocol,
        "target_kind": target_kind,
        "endpoint_url": endpoint_url,
        "remote_name": remote_name,
        "sync_strategy": sync_strategy,
        "priority": priority,
        "config": config,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return SourceCandidate(
        **identity,
        source_key=f"auto:{protocol}:{digest[:32]}",
        definition_sha256=digest,
    )


def candidate_definition(candidate: SourceCandidate) -> dict[str, Any]:
    """Return the stable payload whose hash identifies a source definition."""

    value = asdict(candidate)
    value.pop("source_key")
    value.pop("definition_sha256")
    return value


def _reviewed_local_style_expected_equivalence(
    reviewed: _ReviewedDatasetSource,
    identities: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    configured = reviewed.equivalence.get("expected_style_evidence")
    if isinstance(configured, dict):
        expected_style_evidence = deepcopy(configured)
    else:
        identity = identities[0]
        expected_style_evidence = {
            "style_parity_status": "local_style_adaptation_required",
            "original_wms_endpoint_url": reviewed.catalog_endpoint_url,
            "original_wms_layer_name": reviewed.catalog_remote_name,
            "catalog_default_style_source_key": (identity["catalog_style_source_key"]),
            "original_wms_style_name": identity["remote_style_name"],
            "catalog_styles": [
                {
                    "catalog_style_source_key": (identity["catalog_style_source_key"]),
                    "remote_name": identity["remote_style_name"],
                    "is_default": True,
                }
            ],
        }
    return _reviewed_dataset_equivalence(
        reviewed,
        expected_style_evidence,
        catalog_endpoint_url=reviewed.catalog_endpoint_url,
    )


def _reviewed_local_style_reference(
    reviewed: _ReviewedDatasetSource,
    identity: dict[str, Any],
) -> dict[str, Any]:
    if identity["style_kind"] == "catastro_parcels":
        return {
            "source_kind": "reviewed-local-cartographic-reproduction",
            "adaptation_status": "adaptation_required",
            "parity_claim": "reviewed_local_adaptation_not_exact",
            "polygon_fill_opacity": 0,
            "outline_color": "#000000",
            "outline_width": 1,
            "label_field": "label",
            "font_family": "DejaVu Sans",
            "font_size": 10,
            "label_color": "#000000",
        }
    if identity["style_kind"] == "flood_polygons":
        style_reference = deepcopy(reviewed.equivalence["official_style_reference"])
        style_reference["parity_claim"] = "official_mvt_style_adapted_to_sld_not_exact"
        return style_reference
    if identity["style_kind"] == "eurostat_grid":
        return {
            "source_kind": "siur-owned-deterministic-style",
            "style_identity_sha256": identity["style_identity_sha256"],
            "adaptation_status": "adaptation_required",
            "parity_claim": "siur_local_adaptation_not_exact",
            "fill_opacity": 0,
            "outline_color": identity["outline_color"],
            "outline_width": 1,
            "max_scale_denominator": 4_000_000,
        }
    return {
        "official_style_reference": deepcopy(
            reviewed.equivalence["official_style_reference"]
        ),
        "official_raster_style_evidence": deepcopy(
            reviewed.equivalence["official_raster_style_evidence"]
        ),
    }


def _reviewed_local_style_identities(
    profile: str,
) -> tuple[dict[str, Any], ...]:
    identities = _REVIEWED_LOCAL_STYLE_IDENTITY_SETS.get(profile)
    if identities is not None:
        return identities
    identity = _REVIEWED_LOCAL_STYLE_IDENTITIES.get(profile)
    return (identity,) if identity is not None else ()


def reviewed_local_style_profile_identity(
    profile: str,
    catalog_style_source_key: str | None = None,
) -> tuple[str, str, str, str, str, str] | None:
    """Return the immutable style identity for an allowlisted recipe."""

    nitrate = reviewed_idecyl_nitrate_styles(profile)
    if nitrate:
        matching = [
            item
            for item in nitrate
            if catalog_style_source_key is None
            or item.catalog_style_source_key == catalog_style_source_key
        ]
        if len(matching) != 1:
            return None
        item = matching[0]
        definition = idecyl_nitrate_expected_source_definition(item)
        reviewed_equivalence = definition["config"]["reviewed_equivalence"]
        return (
            item.style_kind,
            item.catalog_style_source_key,
            item.remote_style_name,
            item.catalog_remote_name,
            item.selected_layer_name,
            hashlib.sha256(
                json.dumps(
                    reviewed_equivalence,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )
    idecyl = reviewed_idecyl_local_style(
        profile,
        catalog_style_source_key,
    )
    if idecyl is not None:
        definition = idecyl_local_style_expected_source_definition(idecyl)
        reviewed_equivalence = definition["config"]["reviewed_equivalence"]
        return (
            "idecyl_polygon_outline",
            idecyl.catalog_style_source_key,
            idecyl.remote_style_name,
            idecyl.catalog_remote_name,
            idecyl.selected_layer_name,
            hashlib.sha256(
                json.dumps(
                    reviewed_equivalence,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )
    identities = _reviewed_local_style_identities(profile)
    reviewed = _REVIEWED_DATASET_SOURCES_BY_PROFILE.get(profile)
    if reviewed is None or not identities:
        return None
    if catalog_style_source_key is None:
        if len(identities) != 1:
            return None
        identity = identities[0]
    else:
        matching = [
            item
            for item in identities
            if item["catalog_style_source_key"] == catalog_style_source_key
        ]
        if len(matching) != 1:
            return None
        identity = matching[0]
    equivalence_sha256 = hashlib.sha256(
        json.dumps(
            _reviewed_local_style_expected_equivalence(
                reviewed,
                identities,
            ),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return (
        identity["style_kind"],
        identity["catalog_style_source_key"],
        identity["remote_style_name"],
        reviewed.catalog_remote_name,
        identity.get("selected_layer_name", reviewed.remote_name),
        equivalence_sha256,
    )


def reviewed_local_style_profile_reference(
    profile: str,
    catalog_style_source_key: str | None = None,
) -> dict[str, Any] | None:
    """Return the exact official reference accepted for persisted evidence."""

    nitrate = reviewed_idecyl_nitrate_styles(profile)
    if nitrate:
        matching = [
            item
            for item in nitrate
            if catalog_style_source_key is None
            or item.catalog_style_source_key == catalog_style_source_key
        ]
        if len(matching) != 1:
            return None
        return deepcopy(matching[0].evidence)
    idecyl = reviewed_idecyl_local_style(
        profile,
        catalog_style_source_key,
    )
    if idecyl is not None:
        return deepcopy(idecyl.evidence)
    identities = _reviewed_local_style_identities(profile)
    reviewed = _REVIEWED_DATASET_SOURCES_BY_PROFILE.get(profile)
    if reviewed is None or not identities:
        return None
    if catalog_style_source_key is None:
        if len(identities) != 1:
            return None
        identity = identities[0]
    else:
        matching = [
            item
            for item in identities
            if item["catalog_style_source_key"] == catalog_style_source_key
        ]
        if len(matching) != 1:
            return None
        identity = matching[0]
    return _reviewed_local_style_reference(reviewed, identity)


def reviewed_local_style_expected_source_definition(
    profile: str,
    catalog_style_source_key: str,
) -> tuple[dict[str, Any], str] | None:
    """Return the exact full source definition for an IDECyL adaptation."""

    nitrate = reviewed_idecyl_nitrate_styles(profile)
    if nitrate:
        matching = [
            item
            for item in nitrate
            if item.catalog_style_source_key == catalog_style_source_key
        ]
        if len(matching) != 1:
            return None
        definition = idecyl_nitrate_expected_source_definition(matching[0])
        digest = hashlib.sha256(
            json.dumps(
                definition,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return deepcopy(definition), digest
    reviewed = reviewed_idecyl_local_style(
        profile,
        catalog_style_source_key,
    )
    if (
        reviewed is None
        or catalog_style_source_key != reviewed.catalog_style_source_key
    ):
        return None
    definition = idecyl_local_style_expected_source_definition(reviewed)
    digest = hashlib.sha256(
        json.dumps(
            definition,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return deepcopy(definition), digest


def reviewed_local_style_recipes(
    candidate: SourceCandidate,
) -> tuple[ReviewedLocalStyleRecipe, ...]:
    """Validate and expose local recipes only for an exact reviewed source.

    The persisted candidate is untrusted input at this boundary.  A matching
    schema or profile string is insufficient: protocol, endpoints, collection,
    dataset settings, catalog style identity, candidate digest and generated
    source key must all still match the source-discovery allowlist.
    """

    raw_equivalence = candidate.config.get("reviewed_equivalence")
    if raw_equivalence is None:
        return ()
    if not isinstance(raw_equivalence, dict):
        return ()
    if raw_equivalence.get("schema") == IDECYL_EXACT_EVIDENCE_SCHEMA:
        return _reviewed_idecyl_local_style_recipes(
            candidate,
            raw_equivalence,
        )
    if raw_equivalence.get("schema") != _REVIEWED_DATASET_SOURCE_SCHEMA:
        return ()
    profile = raw_equivalence.get("profile")
    if not isinstance(profile, str):
        raise SourceDiscoveryError(
            "reviewed local style profile is invalid",
            code="reviewed_local_style_invalid",
        )
    reviewed = _REVIEWED_DATASET_SOURCES_BY_PROFILE.get(profile)
    identities = _reviewed_local_style_identities(profile)
    if reviewed is None or not identities:
        raise SourceDiscoveryError(
            "reviewed local style profile is not allowlisted",
            code="reviewed_local_style_invalid",
        )
    expected_equivalence = _reviewed_local_style_expected_equivalence(
        reviewed,
        identities,
    )
    expected_config = {
        **deepcopy(reviewed.config),
        "reviewed_equivalence": expected_equivalence,
    }
    expected_candidate = _candidate(
        protocol=reviewed.protocol,
        target_kind=reviewed.target_kind,
        endpoint_url=reviewed.endpoint_url,
        remote_name=reviewed.remote_name,
        sync_strategy=reviewed.sync_strategy,
        priority=_REVIEWED_DATASET_SOURCE_PRIORITY,
        config=expected_config,
    )
    if (
        candidate.protocol != expected_candidate.protocol
        or candidate.target_kind != expected_candidate.target_kind
        or candidate.endpoint_url != expected_candidate.endpoint_url
        or candidate.remote_name != expected_candidate.remote_name
        or candidate.sync_strategy != expected_candidate.sync_strategy
        or candidate.priority != expected_candidate.priority
        or candidate.config != expected_candidate.config
        or candidate.source_key != expected_candidate.source_key
        or candidate.definition_sha256 != expected_candidate.definition_sha256
    ):
        raise SourceDiscoveryError(
            "reviewed local style evidence does not match its allowlisted source",
            code="reviewed_local_style_invalid",
        )

    equivalence_sha256 = hashlib.sha256(
        json.dumps(
            expected_equivalence,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return tuple(
        ReviewedLocalStyleRecipe(
            schema=_REVIEWED_LOCAL_STYLE_SCHEMA,
            profile=profile,
            style_kind=identity["style_kind"],
            catalog_style_source_key=identity["catalog_style_source_key"],
            remote_style_name=identity["remote_style_name"],
            catalog_layer_name=reviewed.catalog_remote_name,
            selected_layer_name=identity.get(
                "selected_layer_name",
                reviewed.remote_name,
            ),
            reviewed_equivalence=deepcopy(expected_equivalence),
            reviewed_equivalence_sha256=equivalence_sha256,
            style_reference=_reviewed_local_style_reference(
                reviewed,
                identity,
            ),
        )
        for identity in identities
    )


def _reviewed_idecyl_local_style_recipes(
    candidate: SourceCandidate,
    raw_equivalence: dict[str, Any],
) -> tuple[ReviewedLocalStyleRecipe, ...]:
    profile = raw_equivalence.get("profile")
    if not isinstance(profile, str):
        raise SourceDiscoveryError(
            "reviewed IDECyL local-style profile is invalid",
            code="reviewed_local_style_invalid",
        )
    population_exclusion = reviewed_idecyl_population_style_exclusion(profile)
    if population_exclusion is not None:
        expected_definition = idecyl_population_expected_source_definition(
            population_exclusion
        )
        expected = _candidate(
            protocol=expected_definition["protocol"],
            target_kind=expected_definition["target_kind"],
            endpoint_url=expected_definition["endpoint_url"],
            remote_name=expected_definition["remote_name"],
            sync_strategy=expected_definition["sync_strategy"],
            priority=expected_definition["priority"],
            config=deepcopy(expected_definition["config"]),
        )
        if candidate != expected:
            raise SourceDiscoveryError(
                "reviewed IDECyL population exclusion changed",
                code="reviewed_local_style_invalid",
            )
        return ()
    nitrate = reviewed_idecyl_nitrate_styles(profile)
    if nitrate:
        expected_definition = idecyl_nitrate_expected_source_definition(nitrate[0])
        expected = _candidate(
            protocol=expected_definition["protocol"],
            target_kind=expected_definition["target_kind"],
            endpoint_url=expected_definition["endpoint_url"],
            remote_name=expected_definition["remote_name"],
            sync_strategy=expected_definition["sync_strategy"],
            priority=expected_definition["priority"],
            config=deepcopy(expected_definition["config"]),
        )
        if candidate != expected:
            raise SourceDiscoveryError(
                "reviewed IDECyL nitrate styles do not match their source",
                code="reviewed_local_style_invalid",
            )
        reviewed_equivalence = expected_definition["config"]["reviewed_equivalence"]
        reviewed_equivalence_sha256 = hashlib.sha256(
            json.dumps(
                reviewed_equivalence,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return tuple(
            ReviewedLocalStyleRecipe(
                schema=_REVIEWED_LOCAL_STYLE_SCHEMA,
                profile=item.profile,
                style_kind="idecyl_nitrate_year",
                catalog_style_source_key=(item.catalog_style_source_key),
                remote_style_name=item.remote_style_name,
                catalog_layer_name=item.catalog_remote_name,
                selected_layer_name=item.selected_layer_name,
                reviewed_equivalence=deepcopy(reviewed_equivalence),
                reviewed_equivalence_sha256=(reviewed_equivalence_sha256),
                style_reference=deepcopy(item.evidence),
                audit_layer_id=item.audit_layer_id,
                catalog_style_is_default=item.is_default,
                source_definition=candidate_definition(expected),
                source_definition_sha256=expected.definition_sha256,
            )
            for item in nitrate
        )
    identity = raw_equivalence.get("catalog_identity")
    if not isinstance(identity, dict):
        raise SourceDiscoveryError(
            "reviewed IDECyL local-style catalog identity is invalid",
            code="reviewed_local_style_invalid",
        )
    try:
        exact_source = reviewed_idecyl_exact_source(
            catalog_layer_source_key=str(identity.get("catalog_layer_source_key", "")),
            catalog_endpoint_url=str(identity.get("catalog_endpoint_url", "")),
            catalog_remote_name=str(identity.get("catalog_remote_name", "")),
        )
        reviewed_items = (
            reviewed_idecyl_local_styles_for_source(exact_source)
            if exact_source is not None and exact_source.profile == profile
            else ()
        )
    except (
        IDECyLExactEvidenceError,
        IDECyLLocalStyleEvidenceError,
    ) as error:
        raise SourceDiscoveryError(
            "reviewed IDECyL local-style identity is invalid",
            code="reviewed_local_style_invalid",
        ) from error
    if not reviewed_items:
        return ()
    expected_definition = idecyl_local_style_expected_source_definition(
        reviewed_items[0]
    )
    expected = _candidate(
        protocol=expected_definition["protocol"],
        target_kind=expected_definition["target_kind"],
        endpoint_url=expected_definition["endpoint_url"],
        remote_name=expected_definition["remote_name"],
        sync_strategy=expected_definition["sync_strategy"],
        priority=expected_definition["priority"],
        config=deepcopy(expected_definition["config"]),
    )
    if candidate != expected:
        raise SourceDiscoveryError(
            "reviewed IDECyL local style does not match its source definition",
            code="reviewed_local_style_invalid",
        )
    reviewed_equivalence = expected_definition["config"]["reviewed_equivalence"]
    reviewed_equivalence_sha256 = hashlib.sha256(
        json.dumps(
            reviewed_equivalence,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return tuple(
        ReviewedLocalStyleRecipe(
            schema=_REVIEWED_LOCAL_STYLE_SCHEMA,
            profile=reviewed.profile,
            style_kind="idecyl_polygon_outline",
            catalog_style_source_key=(reviewed.catalog_style_source_key),
            remote_style_name=reviewed.remote_style_name,
            catalog_layer_name=reviewed.catalog_remote_name,
            selected_layer_name=reviewed.selected_layer_name,
            reviewed_equivalence=deepcopy(reviewed_equivalence),
            reviewed_equivalence_sha256=(reviewed_equivalence_sha256),
            style_reference=deepcopy(reviewed.evidence),
            audit_layer_id=reviewed.audit_layer_id,
            catalog_style_is_default=reviewed.is_default,
            source_definition=candidate_definition(expected),
            source_definition_sha256=expected.definition_sha256,
        )
        for reviewed in reviewed_items
    )


def reviewed_local_style_recipe(
    candidate: SourceCandidate,
) -> ReviewedLocalStyleRecipe | None:
    """Compatibility wrapper for the existing one-style reviewed profiles."""

    recipes = reviewed_local_style_recipes(candidate)
    if not recipes:
        return None
    if len(recipes) != 1:
        raise SourceDiscoveryError(
            "reviewed local style profile defines multiple styles",
            code="reviewed_local_style_ambiguous",
        )
    return recipes[0]


def _tile_config(
    service: ReferenceServiceDefinition,
    layer: ReferenceLayerDefinition,
) -> dict[str, Any]:
    coverage = reviewed_tile_coverage(
        layer_source_key=layer.source_key,
        bounds=layer.bounds,
        min_zoom=layer.min_zoom,
        max_zoom=layer.max_zoom,
        endpoint_url=service.base_url,
        remote_name=layer.remote_name,
    )
    requested_format = layer.image_format or service.default_format
    image_format = reviewed_tile_format(
        layer_source_key=layer.source_key,
        endpoint_url=service.base_url,
        remote_name=layer.remote_name or "",
        requested_format=requested_format,
    )
    config = {
        "bounds": coverage.bounds,
        "min_zoom": coverage.min_zoom,
        "max_zoom": coverage.max_zoom,
        "format": image_format or _protocol_default_tile_format(service),
        "style_name": layer.style_name or "",
        "coverage_required": True,
    }
    if coverage.max_tile_count is not None:
        config["max_tile_count"] = coverage.max_tile_count
    if coverage.profile is not None:
        config["coverage_profile"] = coverage.profile
        if service.upstream_protocol.casefold() == "wms":
            config["wms_supertile_size"] = SIUR_WMS_SUPERTILE_SIZE
    return config


def _protocol_default_tile_format(
    service: ReferenceServiceDefinition,
) -> str:
    if service.upstream_protocol.casefold() == "xyz":
        path = urlsplit(service.base_url).path.casefold()
        if path.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
    return "image/png"


def _geoserver_style_config(
    style_endpoint_url: str,
    layer: ReferenceLayerDefinition,
) -> dict[str, Any]:
    """Freeze catalog style identities without database-specific IDs."""

    styles = sorted(
        (
            {
                "catalog_style_source_key": style.source_key,
                "remote_name": style.remote_name or style.source_key,
            }
            for style in layer.styles
            if style.status in {"active", "degraded"}
        ),
        key=lambda item: item["catalog_style_source_key"],
    )
    return {
        "style_endpoint_url": style_endpoint_url,
        "style_layer_name": layer.remote_name,
        "styles": styles,
    }


def _local_target_kind(layer: ReferenceLayerDefinition) -> TargetKind:
    return "vector" if layer.renderer == "vector_tile" else "raster"


def _canonical_endpoint(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise SourceDiscoveryError("source endpoint must be credential-free HTTPS")
    port = parts.port
    if port not in {None, 443}:
        raise SourceDiscoveryError("source endpoint uses an unsupported port")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    path = parts.path or "/"
    return urlunsplit(("https", host, path, parts.query, ""))


def _with_path(parts, path: str) -> str:
    host = parts.hostname.encode("idna").decode("ascii").lower()
    return urlunsplit(("https", host, path, "", ""))


def _deduplicate(
    candidates: list[SourceCandidate],
) -> list[SourceCandidate]:
    result: list[SourceCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        key = (
            candidate.protocol,
            candidate.endpoint_url,
            candidate.remote_name,
        )
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result
