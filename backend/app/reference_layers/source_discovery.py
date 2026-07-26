"""Deterministic acquisition candidates for reference map layers.

The catalog records how SIUR displays a layer.  That endpoint is often not the
best source to mirror: a WMS backed by GeoServer can expose the same collection
through WFS or WCS, while an ArcGIS WMS can have a queryable MapServer.  This
module only builds candidates; network probes must prove that the collection is
actually present before a candidate can become a primary source.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.mirror_coverage import (
    SIUR_LAYER_PREFIX,
    SIUR_TILE_BOUNDS,
    SIUR_WMS_SUPERTILE_SIZE,
    SIUR_WMS_SUPERTILE_COVERAGE_PROFILES,
    reviewed_tile_coverage,
    reviewed_tile_format,
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
_BULK_WMS_GUARD_SCHEMA = "siur-bulk-wms-guard/v1"


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
    "figure_title": (
        "Figura 1. Erosión laminar y en regueros (niveles erosivos)"
    ),
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
_REVIEWED_DATASET_SOURCES = {
    (item.catalog_endpoint_url, item.catalog_remote_name): item
    for item in (
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
                "official_style_reference": {
                    "source_kind": "official-mvt-json",
                    "url": (
                        "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
                        "ZI_LaminasQ10.json"
                    ),
                    "source_layer": "ZI_LaminasQ10",
                    "adaptation_status": "adaptation_required",
                    "fill_color": "#ff0000",
                    "outline_color": "#c80000",
                },
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
                "official_style_reference": {
                    "source_kind": "official-mvt-json",
                    "url": (
                        "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
                        "ZI_LaminasQ50.json"
                    ),
                    "source_layer": "ZI_LaminasQ50",
                    "adaptation_status": "adaptation_required",
                    "fill_color": "#ffbee8",
                    "outline_color": "#a80084",
                },
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
                "official_style_reference": {
                    "source_kind": "official-mvt-json",
                    "url": (
                        "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
                        "ZI_LaminasQ100.json"
                    ),
                    "source_layer": "ZI_LaminasQ100",
                    "adaptation_status": "adaptation_required",
                    "fill_color": "#e8beff",
                    "outline_color": "#b68cff",
                },
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
                "official_style_reference": {
                    "source_kind": "official-mvt-json",
                    "url": (
                        "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
                        "ZI_LaminasQ500.json"
                    ),
                    "source_layer": "ZI_LaminasQ500",
                    "adaptation_status": "adaptation_required",
                    "fill_color": "#ff73df",
                    "outline_color": "#ff32df",
                },
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
                "official_style_reference": {
                    "source_kind": "official-mvt-json",
                    "url": (
                        "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
                        "ZI_LaminasZFP.json"
                    ),
                    "source_layer": "ZI_LaminasZFP",
                    "style_id": "zona_flujo_preferente_fill",
                    "adaptation_status": "adaptation_required",
                    "fill_color": "#cccccc",
                    "outline_color": "#e6e600",
                },
            },
        ),
        _ReviewedDatasetSource(
            profile="miteco-ines-potential-cyl-geotiff-download-v1",
            catalog_endpoint_url=(
                "https://wms.mapama.gob.es/sig/Biodiversidad/"
                "INESErosionPotencial"
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
                "https://wms.mapama.gob.es/sig/Biodiversidad/"
                "INESErosionPotencial"
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
                        sync_strategy="paged_snapshot",
                        priority=20,
                        config={
                            "discovery": "wfs_capabilities",
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
                    sync_strategy="paged_snapshot",
                    priority=25,
                    config={"discovery": "wfs_capabilities"},
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
                sync_strategy="paged_snapshot",
                priority=10,
                config={"discovery": "wfs_capabilities"},
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


def _reviewed_dataset_source_config(
    catalog_endpoint: str,
    layer: ReferenceLayerDefinition,
    reviewed: _ReviewedDatasetSource,
) -> dict[str, Any]:
    config = deepcopy(reviewed.config)
    config["reviewed_equivalence"] = {
        "schema": _REVIEWED_DATASET_SOURCE_SCHEMA,
        "profile": reviewed.profile,
        "catalog_protocol": "wms",
        "catalog_endpoint_url": catalog_endpoint,
        "catalog_remote_name": layer.remote_name,
        "selected_protocol": reviewed.protocol,
        "selected_endpoint_url": reviewed.endpoint_url,
        "selected_remote_name": reviewed.remote_name,
        "target_kind": reviewed.target_kind,
        **deepcopy(reviewed.equivalence),
        "style_evidence": _reviewed_style_evidence(
            catalog_endpoint,
            layer,
        ),
    }
    return config


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
            default["remote_name"]
            if default is not None
            else layer.style_name
        ),
        "catalog_styles": styles,
    }


def _bulk_wms_guard(
    endpoint: str,
    layer: ReferenceLayerDefinition,
) -> _BulkWMSGuard | None:
    if (
        layer.renderer != "raster_tile"
        or not layer.remote_name
    ):
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
