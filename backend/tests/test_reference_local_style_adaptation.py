from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import io
from types import SimpleNamespace
from xml.etree import ElementTree
import zipfile

import pytest

from app.reference_layers.acquisition import _store_style_package
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.local_style_adaptation import (
    LocalStyleAdaptationError,
    canonical_json_sha256,
    generate_reviewed_local_style,
    generate_reviewed_local_styles,
    local_style_package_metadata,
    validate_zero_resource_local_adaptation,
)
from app.reference_layers.idecyl_exact_evidence import (
    idecyl_exact_source_inventory,
)
from app.reference_layers.idecyl_local_style_evidence import (
    idecyl_local_style_inventory,
)
from app.reference_layers.source_discovery import acquisition_candidates
from app.reference_layers.style_parity import _RequiredStyle, _sld_item


SLD = {"sld": "http://www.opengis.net/sld"}
OGC = {"ogc": "http://www.opengis.net/ogc"}


def _candidate(
    endpoint: str,
    layer_name: str,
    style_key: str,
    style_name: str,
):
    service = ReferenceServiceDefinition(
        source_key="service",
        title="Reviewed service",
        upstream_protocol="wms",
        base_url=endpoint,
        default_format="image/png",
    )
    layer = ReferenceLayerDefinition(
        source_key="layer:siur:" + "a" * 64,
        node_type="layer",
        title="Reviewed layer",
        service_key="service",
        remote_name=layer_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={
            "west": -7.6,
            "south": 39.9,
            "east": -1.3,
            "north": 43.4,
        },
        style_name=style_key,
        styles=(
            ReferenceLayerStyleDefinition(
                source_key=style_key,
                title=style_name,
                remote_name=style_name,
                is_default=True,
            ),
        ),
    )
    return acquisition_candidates(service, layer)[0]


def _catastro_candidate():
    return _candidate(
        "https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx",
        "Catastro",
        "default",
        "Default",
    )


def _eurostat_grid_candidate(resolution: str = "100"):
    endpoint = "https://idecyl.jcyl.es/geoserver/rejillas/wms"
    service = ReferenceServiceDefinition(
        source_key="service",
        title="IDECyL Eurostat grids",
        upstream_protocol="wms",
        base_url=endpoint,
        default_format="image/png",
    )
    styles = (
        ReferenceLayerStyleDefinition(
            source_key="rejilla_eurostat_cyl_morado",
            title="Borde celdas morado",
            remote_name="rejilla_eurostat_cyl_morado",
            is_default=True,
        ),
        ReferenceLayerStyleDefinition(
            source_key="rejilla_eurostat_cyl_blanco",
            title="Borde celdas blanco",
            remote_name="rejilla_eurostat_cyl_blanco",
        ),
        ReferenceLayerStyleDefinition(
            source_key="rejilla_eurostat_cyl_fucsia",
            title="Borde celdas fucsia",
            remote_name="rejilla_eurostat_cyl_fucsia",
        ),
    )
    layer = ReferenceLayerDefinition(
        source_key="layer:siur:" + "8" * 64,
        node_type="layer",
        title=f"Rejilla Eurostat {resolution} km",
        service_key="service",
        remote_name=f"rejilla_eurostat_cyl_{resolution}x{resolution}",
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={
            "west": -7.6,
            "south": 39.9,
            "east": -1.3,
            "north": 43.4,
        },
        style_name="rejilla_eurostat_cyl_morado",
        styles=styles,
    )
    selected = acquisition_candidates(service, layer)
    assert len(selected) == 1
    return selected[0]


def _ines_candidate(*, potential: bool = True):
    return _candidate(
        (
            "https://wms.mapama.gob.es/sig/Biodiversidad/"
            + (
                "INESErosionPotencial"
                if potential
                else "INESErosionLaminarRaster"
            )
        ),
        "NZ.HazardArea",
        (
            "biodiversidad_ines_erosionpotencial"
            if potential
            else "biodiversidad_ines_erosionlaminar"
        ),
        (
            "Biodiversidad_INES_ErosionPotencial"
            if potential
            else "Biodiversidad_INES_ErosionLaminar"
        ),
    )


def _idecyl_cami_candidate():
    reviewed = next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == 86
    )
    service = ReferenceServiceDefinition(
        source_key="service:idecyl-mineria",
        title="IDECyL minería",
        upstream_protocol="wms",
        base_url=reviewed.catalog_endpoint_url,
        default_format="image/png",
    )
    style_name = "cami_cyl_cuadricula_default"
    layer = ReferenceLayerDefinition(
        source_key=reviewed.catalog_layer_source_key,
        node_type="layer",
        title="Cuadrícula minera",
        service_key=service.source_key,
        remote_name=reviewed.catalog_remote_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={
            "west": -7.6,
            "south": 39.9,
            "east": -1.3,
            "north": 43.4,
        },
        style_name=style_name,
        styles=(
            ReferenceLayerStyleDefinition(
                source_key=style_name,
                title="Borde celdas negro",
                remote_name=style_name,
                is_default=True,
            ),
        ),
    )
    selected = acquisition_candidates(service, layer)
    assert len(selected) == 1
    return selected[0]


def _idecyl_cami_dataset_metadata():
    data_schema = [
        {
            "declared_type": "INTEGER",
            "default": None,
            "hidden": 0,
            "name": "fid",
            "not_null": True,
            "ordinal": 0,
            "primary_key_ordinal": 1,
        },
        {
            "declared_type": "MULTIPOLYGON",
            "default": None,
            "hidden": 0,
            "name": "geometry",
            "not_null": False,
            "ordinal": 1,
            "primary_key_ordinal": 0,
        },
        {
            "declared_type": "REAL",
            "default": None,
            "hidden": 0,
            "name": "area",
            "not_null": False,
            "ordinal": 2,
            "primary_key_ordinal": 0,
        },
    ]
    return {
        "input_layer": "cuadricula",
        "geopackage_inspection": {
            "schema_version": "reference-geopackage-zip-inspection/v1",
            "archive_member": "cami_cyl.gpkg",
            "feature_layer": "cuadricula",
            "feature_layers": ["cuadricula"],
            "geometry_column": "geometry",
            "geometry_type": "MULTIPOLYGON",
            "crs": "EPSG:25830",
            "data_schema": data_schema,
            "data_schema_sha256": canonical_json_sha256(data_schema),
        },
    }


def _idecyl_boundary_candidate(layer_id: int):
    reviewed_styles = [
        item
        for item in idecyl_local_style_inventory()
        if item.audit_layer_id == layer_id
    ]
    assert len(reviewed_styles) == 6
    reviewed = next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == layer_id
    )
    service = ReferenceServiceDefinition(
        source_key="service:idecyl-limites",
        title="IDECyL límites",
        upstream_protocol="wms",
        base_url=reviewed.catalog_endpoint_url,
        default_format="image/png",
    )
    default_style = next(
        item.catalog_style_source_key
        for item in reviewed_styles
        if item.is_default
    )
    layer = ReferenceLayerDefinition(
        source_key=reviewed.catalog_layer_source_key,
        node_type="layer",
        title=reviewed.catalog_remote_name,
        service_key=service.source_key,
        remote_name=reviewed.catalog_remote_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={
            "west": -7.6,
            "south": 39.9,
            "east": -1.3,
            "north": 43.4,
        },
        style_name=default_style,
        styles=tuple(
            ReferenceLayerStyleDefinition(
                source_key=item.catalog_style_source_key,
                title=item.style_title,
                remote_name=item.remote_style_name,
                is_default=item.is_default,
            )
            for item in reviewed_styles
        ),
    )
    selected = acquisition_candidates(service, layer)
    assert len(selected) == 1
    return selected[0]


def _idecyl_boundary_dataset_metadata(layer_id: int):
    reviewed = next(
        item
        for item in idecyl_local_style_inventory()
        if item.audit_layer_id == layer_id
    )
    inspection = reviewed.evidence["dataset_inspection"]
    return {
        "input_layer": inspection["feature_layer"],
        "geopackage_inspection": {
            "schema_version": inspection["inspection_schema"],
            "archive_member": inspection["archive_member"],
            "feature_layer": inspection["feature_layer"],
            "feature_layers": inspection["feature_layers"],
            "geometry_column": inspection["geometry_column"],
            "geometry_type": inspection["geometry_type"],
            "crs": inspection["srs"],
            "data_schema": deepcopy(inspection["data_schema"]),
            "data_schema_sha256": inspection["data_schema_sha256"],
        },
    }


def _vat_metadata(*, potential: bool = True):
    mapping = [
        {"value": 64, "class_value": 7},
        {"value": 65, "class_value": 1},
        {"value": 66, "class_value": 6},
        {"value": 67, "class_value": 5},
        {"value": 68, "class_value": 4},
        {"value": 69, "class_value": 3},
        {"value": 75, "class_value": 2},
        {"value": 80, "class_value": 9},
        {"value": 92, "class_value": 8},
        {"value": 120, "class_value": 1},
        {"value": 130, "class_value": 7},
    ]
    return {
        "raster_value_attribute_table": {
            "member": (
                "EroPotNiveles_41.tiff.vat.dbf"
                if potential
                else "EroLamNiveles_41.tiff.vat.dbf"
            ),
            "sha256": "a" * 64,
            "row_count": len(mapping),
            "value_field": "Value",
            "class_field": "EroPot_pb" if potential else "EroLam_pb",
            "expected_class_values": list(range(1, 10)),
            "value_class_mapping": mapping,
        }
    }


def test_catastro_authored_sld_is_transparent_outlined_and_labelled() -> None:
    authored = generate_reviewed_local_style(_catastro_candidate())

    assert authored is not None
    root = ElementTree.fromstring(authored.document)
    css = {
        element.attrib["name"]: (element.text or "")
        for element in root.findall(".//sld:CssParameter", SLD)
    }
    assert css["fill-opacity"] == "0"
    assert css["stroke"] == "#000000"
    assert css["stroke-width"] == "1"
    assert css["font-family"] == "DejaVu Sans"
    assert css["font-size"] == "10"
    assert css["fill"] == "#000000"
    assert root.findtext(".//sld:Label/ogc:PropertyName", namespaces={**SLD, **OGC}) == (
        "label"
    )
    evidence = authored.metadata["authored_local_evidence"]
    assert evidence["parity_kind"] == "adapted"
    assert evidence["exact_style_claim"] is False
    assert evidence["resource_count"] == 0


@pytest.mark.parametrize("resolution", ["100", "50", "20", "5", "2", "1"])
def test_eurostat_grid_authors_three_closed_deterministic_styles(
    resolution: str,
) -> None:
    candidate = _eurostat_grid_candidate(resolution)
    first = generate_reviewed_local_styles(candidate)
    second = generate_reviewed_local_styles(candidate)

    assert len(first) == 3
    assert [item.sld_sha256 for item in first] == [
        item.sld_sha256 for item in second
    ]
    expected = {
        "rejilla_eurostat_cyl_blanco": "#ffffff",
        "rejilla_eurostat_cyl_fucsia": "#e6007e",
        "rejilla_eurostat_cyl_morado": "#6d28d9",
    }
    assert {
        item.metadata["catalog_style_source_key"] for item in first
    } == set(expected)
    for authored in first:
        source_key = authored.metadata["catalog_style_source_key"]
        root = ElementTree.fromstring(authored.document)
        css = {
            element.attrib["name"]: (element.text or "")
            for element in root.findall(".//sld:CssParameter", SLD)
        }
        assert css == {
            "fill": "#ffffff",
            "fill-opacity": "0",
            "stroke": expected[source_key],
            "stroke-width": "1",
        }
        assert root.findtext(".//sld:MaxScaleDenominator", namespaces=SLD) == (
            "4000000"
        )
        assert authored.metadata["style_layer_name"] == (
            f"grid_{resolution}km_surf_cyl"
        )
        evidence = authored.metadata["authored_local_evidence"]
        assert evidence["parity_kind"] == "adapted"
        assert evidence["exact_style_claim"] is False
        assert evidence["resource_count"] == 0
        assert b"idecyl.jcyl.es" not in authored.document
        validate_zero_resource_local_adaptation(
            style_metadata=authored.metadata,
            package_metadata=local_style_package_metadata(authored),
            sld_sha256=authored.sld_sha256,
        )


def test_singular_generator_rejects_plural_grid_profile() -> None:
    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_style(_eurostat_grid_candidate())

    assert captured.value.code == "reviewed_local_style_ambiguous"


def test_eurostat_grid_style_parity_is_adapted_for_all_three_styles() -> None:
    authored_styles = generate_reviewed_local_styles(
        _eurostat_grid_candidate()
    )
    for index, authored in enumerate(authored_styles, start=1):
        source_key = authored.metadata["catalog_style_source_key"]
        remote_name = authored.metadata["remote_name"]
        style = SimpleNamespace(
            artifact_id=index,
            sha256=authored.sld_sha256,
            metadata_json=authored.metadata,
        )
        package = SimpleNamespace(
            artifact_id=index + 10,
            metadata_json=local_style_package_metadata(authored),
        )
        required = _RequiredStyle(
            style_id=index + 20,
            source_key=source_key,
            remote_name=remote_name,
            is_default=source_key.endswith("_morado"),
        )

        item = _sld_item(
            required,
            style_artifacts={source_key: style},
            package_artifacts={source_key: package},
            resources_by_sha={},
        )

        assert item.verified is True
        assert item.parity_kind == "adapted"
        assert item.resources == ()


def test_idecyl_cami_authors_hash_bound_black_outline_from_inspected_schema() -> None:
    candidate = _idecyl_cami_candidate()
    metadata = _idecyl_cami_dataset_metadata()

    first = generate_reviewed_local_style(
        candidate,
        dataset_metadata=metadata,
    )
    second = generate_reviewed_local_style(
        candidate,
        dataset_metadata=deepcopy(metadata),
    )

    assert first is not None
    assert second == first
    root = ElementTree.fromstring(first.document)
    css = {
        element.attrib["name"]: (element.text or "")
        for element in root.findall(".//sld:CssParameter", SLD)
    }
    assert css == {
        "fill": "#ffffff",
        "fill-opacity": "0",
        "stroke": "#000000",
        "stroke-width": "1",
    }
    assert root.findtext(".//sld:NamedLayer/sld:Name", namespaces=SLD) == (
        "cuadricula"
    )
    assert root.findtext(".//sld:UserStyle/sld:Name", namespaces=SLD) == (
        "cami_cyl_cuadricula_default"
    )
    assert root.findall(".//sld:ExternalGraphic", SLD) == []
    assert root.findall(".//sld:OnlineResource", SLD) == []
    assert root.findall(".//sld:InlineContent", SLD) == []

    evidence = first.metadata["authored_local_evidence"]
    assert evidence["schema"] == (
        "siur-authored-idecyl-local-style-adaptation/v1"
    )
    assert evidence["generator_version"] == (
        "siur-sld-1.0-idecyl-local-adaptation/v1"
    )
    assert evidence["audit_layer_id"] == 86
    assert evidence["profile"] == (
        "idecyl-cami-cyl-cuadricula-archive-20260727-v3"
    )
    assert evidence["catalog_style_source_key"] == (
        "cami_cyl_cuadricula_default"
    )
    assert evidence["remote_style_name"] == (
        "cami_cyl_cuadricula_default"
    )
    assert evidence["catalog_style_is_default"] is True
    assert evidence["parity_kind"] == "adapted"
    assert evidence["exact_style_claim"] is False
    assert evidence["source_definition_sha256"] == (
        "1d000ad61b43edf1a37d80bf92bf025"
        "3e09efc4c0bb430daa5f814f39fe2c4d0"
    )
    assert canonical_json_sha256(evidence["source_definition"]) == (
        evidence["source_definition_sha256"]
    )
    assert evidence["reviewed_equivalence_sha256"] == (
        "3555e2cea32ac246bd1da6847f23aadb"
        "a76a23e308504afe03f6889d86950db8"
    )
    assert evidence["recipe"]["dataset_inspection"][
        "geometry_type"
    ] == "MULTIPOLYGON"
    assert evidence["recipe"]["dataset_inspection"][
        "data_schema"
    ] == metadata["geopackage_inspection"]["data_schema"]
    assert evidence["dataset_schema_sha256"] == evidence["recipe"][
        "dataset_schema_sha256"
    ]

    validate_zero_resource_local_adaptation(
        style_metadata=first.metadata,
        package_metadata=local_style_package_metadata(first),
        sld_sha256=first.sld_sha256,
    )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "geometry", "schema", "layer", "crs"],
)
def test_idecyl_cami_rejects_missing_or_changed_dataset_schema(
    mutation: str,
) -> None:
    metadata = _idecyl_cami_dataset_metadata()
    if mutation == "missing":
        metadata.pop("geopackage_inspection")
        expected_code = "local_style_dataset_schema_missing"
    elif mutation == "geometry":
        metadata["geopackage_inspection"]["geometry_type"] = "POLYGON"
        expected_code = "local_style_dataset_schema_changed"
    elif mutation == "schema":
        metadata["geopackage_inspection"]["data_schema"][2]["name"] = (
            "changed"
        )
        metadata["geopackage_inspection"]["data_schema_sha256"] = (
            canonical_json_sha256(
                metadata["geopackage_inspection"]["data_schema"]
            )
        )
        expected_code = "local_style_dataset_schema_changed"
    elif mutation == "layer":
        metadata["input_layer"] = "changed"
        expected_code = "local_style_dataset_schema_changed"
    else:
        metadata["geopackage_inspection"]["crs"] = "EPSG:4326"
        expected_code = "local_style_dataset_schema_changed"

    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_style(
            _idecyl_cami_candidate(),
            dataset_metadata=metadata,
        )

    assert captured.value.code == expected_code


def test_idecyl_cami_rejects_changed_source_definition() -> None:
    candidate = _idecyl_cami_candidate()
    changed = replace(
        candidate,
        endpoint_url="https://opendata.jcyl.es/changed.zip",
    )

    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_style(
            changed,
            dataset_metadata=_idecyl_cami_dataset_metadata(),
        )

    assert captured.value.code == "reviewed_local_style_invalid"


def test_idecyl_cami_persisted_evidence_rejects_rehashed_source_binding() -> None:
    authored = generate_reviewed_local_style(
        _idecyl_cami_candidate(),
        dataset_metadata=_idecyl_cami_dataset_metadata(),
    )
    assert authored is not None
    style_metadata = deepcopy(authored.metadata)
    evidence = style_metadata["authored_local_evidence"]
    evidence["source_definition"]["endpoint_url"] = (
        "https://opendata.jcyl.es/changed.zip"
    )
    evidence["source_definition_sha256"] = canonical_json_sha256(
        evidence["source_definition"]
    )
    evidence_sha256 = canonical_json_sha256(evidence)
    style_metadata["authored_local_evidence_sha256"] = evidence_sha256
    package_metadata = local_style_package_metadata(authored)
    package_metadata["authored_local_evidence_sha256"] = evidence_sha256

    with pytest.raises(LocalStyleAdaptationError) as captured:
        validate_zero_resource_local_adaptation(
            style_metadata=style_metadata,
            package_metadata=package_metadata,
            sld_sha256=authored.sld_sha256,
        )

    assert captured.value.code == "local_style_evidence_invalid"


@pytest.mark.parametrize(
    ("layer_id", "label_field", "default_style"),
    [
        (
            223,
            "n_auton",
            "limites_autonomias_negro_etiquetado",
        ),
        (234, "n_prov", "limites_provincias_negro"),
    ],
)
def test_idecyl_boundaries_author_all_six_hash_bound_catalog_styles(
    layer_id: int,
    label_field: str,
    default_style: str,
) -> None:
    candidate = _idecyl_boundary_candidate(layer_id)
    metadata = _idecyl_boundary_dataset_metadata(layer_id)

    first = generate_reviewed_local_styles(
        candidate,
        dataset_metadata=metadata,
    )
    second = generate_reviewed_local_styles(
        candidate,
        dataset_metadata=deepcopy(metadata),
    )

    assert first == second
    assert len(first) == 6
    assert {
        item.metadata["catalog_style_source_key"] for item in first
    } == {
        item["catalog_style_source_key"]
        for item in candidate.config["reviewed_local_styles"]
    }
    assert sum(
        item["is_default"]
        for item in candidate.config["reviewed_local_styles"]
    ) == 1
    assert next(
        item["catalog_style_source_key"]
        for item in candidate.config["reviewed_local_styles"]
        if item["is_default"]
    ) == default_style

    expected_colors = {
        "amarillo": ("#ffff00", "1.1"),
        "blanco": ("#ffffff", "0.1"),
        "fucsia": ("#ff00ff", "1.1"),
        "negro": ("#000000", "0.1"),
    }
    for authored in first:
        style_key = authored.metadata["catalog_style_source_key"]
        suffix = next(
            name for name in expected_colors if name in style_key
        )
        root = ElementTree.fromstring(authored.document)
        css = [
            (element.attrib["name"], element.text or "")
            for element in root.findall(".//sld:CssParameter", SLD)
        ]
        css_by_name: dict[str, list[str]] = {}
        for name, value in css:
            css_by_name.setdefault(name, []).append(value)
        assert css_by_name["fill"][0] == "#ffffff"
        assert css_by_name["fill-opacity"] == ["0"]
        assert css_by_name["stroke"] == [
            expected_colors[suffix][0]
        ]
        assert css_by_name["stroke-width"] == [
            expected_colors[suffix][1]
        ]
        labels = root.findall(
            ".//sld:Label/ogc:PropertyName",
            {**SLD, **OGC},
        )
        if style_key.endswith("_etiquetado"):
            assert [item.text for item in labels] == [label_field]
            assert css_by_name["font-family"] == ["DejaVu Sans"]
            assert css_by_name["font-size"] == ["10"]
            assert root.find(".//sld:Halo", SLD) is not None
            assert len(css_by_name["fill"]) == 3
        else:
            assert labels == []
            assert root.find(".//sld:Halo", SLD) is None
            assert css_by_name["fill"] == ["#ffffff"]
        assert root.findall(".//sld:ExternalGraphic", SLD) == []
        assert root.findall(".//sld:OnlineResource", SLD) == []
        assert root.findall(".//sld:InlineContent", SLD) == []

        evidence = authored.metadata["authored_local_evidence"]
        assert evidence["audit_layer_id"] == layer_id
        assert evidence["catalog_style_is_default"] is (
            style_key == default_style
        )
        assert evidence["parity_kind"] == "adapted"
        assert evidence["exact_style_claim"] is False
        assert evidence["dataset_schema_sha256"] == evidence[
            "recipe"
        ]["dataset_schema_sha256"]
        validate_zero_resource_local_adaptation(
            style_metadata=authored.metadata,
            package_metadata=local_style_package_metadata(authored),
            sld_sha256=authored.sld_sha256,
        )


@pytest.mark.parametrize("layer_id", [223, 234])
def test_idecyl_boundary_singular_api_rejects_six_style_profile(
    layer_id: int,
) -> None:
    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_style(
            _idecyl_boundary_candidate(layer_id),
            dataset_metadata=_idecyl_boundary_dataset_metadata(
                layer_id
            ),
        )

    assert captured.value.code == "reviewed_local_style_ambiguous"


@pytest.mark.parametrize("layer_id", [223, 234])
@pytest.mark.parametrize(
    "mutation",
    ["geometry", "schema", "layer", "crs"],
)
def test_idecyl_boundaries_reject_changed_inspected_dataset(
    layer_id: int,
    mutation: str,
) -> None:
    metadata = _idecyl_boundary_dataset_metadata(layer_id)
    if mutation == "geometry":
        metadata["geopackage_inspection"]["geometry_type"] = "POLYGON"
    elif mutation == "schema":
        metadata["geopackage_inspection"]["data_schema"][2]["name"] = (
            "changed"
        )
        metadata["geopackage_inspection"]["data_schema_sha256"] = (
            canonical_json_sha256(
                metadata["geopackage_inspection"]["data_schema"]
            )
        )
    elif mutation == "layer":
        metadata["input_layer"] = "changed"
    else:
        metadata["geopackage_inspection"]["crs"] = "EPSG:25830"

    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_styles(
            _idecyl_boundary_candidate(layer_id),
            dataset_metadata=metadata,
        )

    assert captured.value.code == "local_style_dataset_schema_changed"


@pytest.mark.parametrize("layer_id", [223, 234])
def test_idecyl_boundary_adaptations_verify_full_style_parity(
    layer_id: int,
) -> None:
    authored_styles = generate_reviewed_local_styles(
        _idecyl_boundary_candidate(layer_id),
        dataset_metadata=_idecyl_boundary_dataset_metadata(layer_id),
    )
    for index, authored in enumerate(authored_styles, start=1):
        evidence = authored.metadata["authored_local_evidence"]
        source_key = authored.metadata["catalog_style_source_key"]
        item = _sld_item(
            _RequiredStyle(
                style_id=index,
                source_key=source_key,
                remote_name=authored.metadata["remote_name"],
                is_default=evidence["catalog_style_is_default"],
            ),
            style_artifacts={
                source_key: SimpleNamespace(
                    artifact_id=index + 20,
                    sha256=authored.sld_sha256,
                    metadata_json=authored.metadata,
                )
            },
            package_artifacts={
                source_key: SimpleNamespace(
                    artifact_id=index + 40,
                    metadata_json=local_style_package_metadata(
                        authored
                    ),
                )
            },
            resources_by_sha={},
        )

        assert item.verified is True
        assert item.parity_kind == "adapted"
        assert item.resources == ()


def test_pluralization_preserves_existing_profile_hashes() -> None:
    cases = (
        (
            _catastro_candidate(),
            None,
            "67ad5949c693b0a099d5f2eef17c99227da37023c10c0434c5748e7a269d6f31",
            "f54274bf25ad328f9eb1efc20a696e0c4e9b1748a2c164be6ddc1dacb4411d22",
        ),
        (
            _candidate(
                "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ10/wms.aspx",
                "Z.I. con alta probabilidad",
                "default",
                "default",
            ),
            None,
            "80603f7a30a45b87540293952a93b7896868621f821a6c2f48bf25eb1788aec2",
            "34808ba3fd1601ef01586dc3879513fd1ef11767854ebb18d3fda3a5e6a8c96c",
        ),
        (
            _ines_candidate(),
            _vat_metadata(),
            "9614c533474f4083405b6ca0787c7f94e041a8174c36cd7f9c94d5fd3edb753c",
            "f39c88e1e00d0c26e1cf38f14e620387940f4ea4607967ff50f14b0a5b31f069",
        ),
    )
    for candidate, metadata, expected_sld, expected_evidence in cases:
        singular = generate_reviewed_local_style(
            candidate,
            dataset_metadata=metadata,
        )
        plural = generate_reviewed_local_styles(
            candidate,
            dataset_metadata=metadata,
        )

        assert singular is not None
        assert plural == (singular,)
        assert singular.sld_sha256 == expected_sld
        assert singular.evidence_sha256 == expected_evidence


def test_local_style_never_activates_from_a_tampered_reviewed_profile() -> None:
    candidate = _catastro_candidate()
    config = {
        **candidate.config,
        "reviewed_equivalence": {
            **candidate.config["reviewed_equivalence"],
            "profile": "miteco-flood-q10-ogc-api-features-v1",
        },
    }

    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_style(replace(candidate, config=config))

    assert captured.value.code == "reviewed_local_style_invalid"


@pytest.mark.parametrize(
    ("endpoint", "layer_name", "fill", "outline"),
    [
        (
            "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ10/wms.aspx",
            "Z.I. con alta probabilidad",
            "#ff0000",
            "#c80000",
        ),
        (
            "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ50/wms.aspx",
            "Z.I. frecuente",
            "#df73ff",
            "#df41ff",
        ),
        (
            "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ100/wms.aspx",
            "Z.I. con probabilidad media u ocasional",
            "#e8beff",
            "#b68cff",
        ),
        (
            "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ500/wms.aspx",
            "Z.I. con probabilidad baja o excepcional",
            "#ff73df",
            "#ff32df",
        ),
        (
            "https://wms.mapama.gob.es/sig/agua/ZI_LaminasZFP/wms.aspx",
            "Zona de flujo preferente",
            "#cccccc",
            "#e6e600",
        ),
    ],
)
def test_each_reviewed_flood_recipe_uses_its_allowlisted_colors(
    endpoint: str,
    layer_name: str,
    fill: str,
    outline: str,
) -> None:
    candidate = _candidate(endpoint, layer_name, "default", "default")
    authored = generate_reviewed_local_style(candidate)

    assert authored is not None
    root = ElementTree.fromstring(authored.document)
    css = {
        element.attrib["name"]: (element.text or "")
        for element in root.findall(".//sld:CssParameter", SLD)
    }
    assert css == {
        "fill": fill,
        "fill-opacity": "1",
        "stroke": outline,
        "stroke-width": "1",
    }
    assert authored.metadata["catalog_style_source_key"] == "default"
    assert authored.metadata["remote_name"] == "default"


@pytest.mark.parametrize("potential", [True, False])
def test_ines_colormap_covers_every_composite_vat_value(
    potential: bool,
) -> None:
    metadata = _vat_metadata(potential=potential)
    authored = generate_reviewed_local_style(
        _ines_candidate(potential=potential),
        dataset_metadata=metadata,
    )

    assert authored is not None
    root = ElementTree.fromstring(authored.document)
    color_map = root.find(".//sld:ColorMap", SLD)
    assert color_map is not None
    assert color_map.attrib["type"] == "values"
    entries = root.findall(".//sld:ColorMapEntry", SLD)
    mapping = metadata["raster_value_attribute_table"][
        "value_class_mapping"
    ]
    assert [int(item.attrib["quantity"]) for item in entries] == [
        item["value"] for item in mapping
    ]
    assert len(entries) == len(mapping)
    assert {int(item.attrib["quantity"]) for item in entries} != set(
        range(1, 10)
    )
    colors = {1: "#7b8257", 7: "#ac514d", 8: "#1eaae2", 9: "#d0d1d4"}
    for entry, mapping_item in zip(entries, mapping, strict=True):
        if mapping_item["class_value"] in colors:
            assert entry.attrib["color"] == colors[
                mapping_item["class_value"]
            ]
    recipe = authored.metadata["authored_local_evidence"]["recipe"]
    assert recipe["vat_sha256"] == "a" * 64
    assert recipe["vat_row_count"] == len(mapping)
    assert len(recipe["value_class_mapping_sha256"]) == 64


@pytest.mark.parametrize("failure", ["duplicate", "incomplete", "tampered_hash"])
def test_ines_rejects_tampered_or_incomplete_vat_evidence(
    failure: str,
) -> None:
    metadata = _vat_metadata()
    vat = metadata["raster_value_attribute_table"]
    if failure == "duplicate":
        vat["value_class_mapping"][1]["value"] = 64
    elif failure == "incomplete":
        vat["value_class_mapping"] = [
            item
            for item in vat["value_class_mapping"]
            if item["class_value"] != 9
        ]
        vat["row_count"] = len(vat["value_class_mapping"])
    else:
        vat["sha256"] = "not-a-digest"

    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_style(
            _ines_candidate(),
            dataset_metadata=metadata,
        )

    assert captured.value.code == "local_style_vat_invalid"


def test_zero_resource_style_package_is_deterministic_and_hash_bound(
    tmp_path,
) -> None:
    authored = generate_reviewed_local_style(_catastro_candidate())
    assert authored is not None
    store = ReferenceBlobStore(tmp_path / "styles")
    try:
        first = _store_style_package(
            store,
            sld=authored.document,
            resources=(),
            max_bytes=1024 * 1024,
        )
        second = _store_style_package(
            store,
            sld=authored.document,
            resources=(),
            max_bytes=1024 * 1024,
        )
        assert first.sha256 == second.sha256
        with store.open_blob(first.storage_key) as source:
            with zipfile.ZipFile(io.BytesIO(source.read())) as archive:
                assert archive.namelist() == ["style.sld"]
                assert archive.read("style.sld") == authored.document
    finally:
        store.close()

    package_metadata = local_style_package_metadata(authored)
    validate_zero_resource_local_adaptation(
        style_metadata=authored.metadata,
        package_metadata=package_metadata,
        sld_sha256=authored.sld_sha256,
    )
    tampered = dict(package_metadata)
    tampered["sld_sha256"] = "f" * 64
    with pytest.raises(LocalStyleAdaptationError):
        validate_zero_resource_local_adaptation(
            style_metadata=authored.metadata,
            package_metadata=tampered,
            sld_sha256=authored.sld_sha256,
        )


def test_zero_resource_style_rejects_rehashed_noncanonical_sld() -> None:
    authored = generate_reviewed_local_style(_catastro_candidate())
    assert authored is not None
    tampered_document = authored.document.replace(
        b"#000000",
        b"#ff0000",
        1,
    )
    assert tampered_document != authored.document
    tampered_sha256 = hashlib.sha256(tampered_document).hexdigest()

    style_metadata = deepcopy(authored.metadata)
    evidence = style_metadata["authored_local_evidence"]
    evidence["sld_sha256"] = tampered_sha256
    evidence_sha256 = canonical_json_sha256(evidence)
    style_metadata["authored_local_evidence_sha256"] = evidence_sha256

    package_metadata = local_style_package_metadata(authored)
    package_metadata["sld_sha256"] = tampered_sha256
    package_metadata["package_members"] = [
        {"path": "style.sld", "sha256": tampered_sha256}
    ]
    package_metadata["authored_local_evidence_sha256"] = evidence_sha256

    with pytest.raises(LocalStyleAdaptationError):
        validate_zero_resource_local_adaptation(
            style_metadata=style_metadata,
            package_metadata=package_metadata,
            sld_sha256=tampered_sha256,
        )


def test_style_parity_accepts_zero_resources_only_with_authored_evidence() -> None:
    authored = generate_reviewed_local_style(_catastro_candidate())
    assert authored is not None
    package_metadata = local_style_package_metadata(authored)
    style = SimpleNamespace(
        artifact_id=1,
        sha256=authored.sld_sha256,
        metadata_json=authored.metadata,
    )
    package = SimpleNamespace(
        artifact_id=2,
        metadata_json=package_metadata,
    )
    required = _RequiredStyle(
        style_id=10,
        source_key="default",
        remote_name="Default",
        is_default=True,
    )

    item = _sld_item(
        required,
        style_artifacts={"default": style},
        package_artifacts={"default": package},
        resources_by_sha={},
    )

    assert item.verified is True
    assert item.parity_kind == "adapted"
    assert item.resources == ()
    assert item.package_artifact_id == 2

    invalid_style = SimpleNamespace(
        artifact_id=1,
        sha256=authored.sld_sha256,
        metadata_json={
            **authored.metadata,
            "authored_local_evidence_sha256": "0" * 64,
        },
    )
    invalid = _sld_item(
        required,
        style_artifacts={"default": invalid_style},
        package_artifacts={"default": package},
        resources_by_sha={},
    )
    assert invalid.verified is False
    assert invalid.parity_kind == "missing"


def test_persisted_flood_evidence_rejects_rehashed_official_reference() -> None:
    authored = generate_reviewed_local_style(
        _candidate(
            "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ10/wms.aspx",
            "Z.I. con alta probabilidad",
            "default",
            "default",
        )
    )
    assert authored is not None
    style_metadata = deepcopy(authored.metadata)
    evidence = style_metadata["authored_local_evidence"]
    evidence["recipe"]["source_reference"]["url"] = (
        "https://example.invalid/rehashed.json"
    )
    evidence["recipe_sha256"] = canonical_json_sha256(evidence["recipe"])
    evidence_sha256 = canonical_json_sha256(evidence)
    style_metadata["authored_local_evidence_sha256"] = evidence_sha256
    package_metadata = local_style_package_metadata(authored)
    package_metadata["authored_local_evidence_sha256"] = evidence_sha256

    with pytest.raises(LocalStyleAdaptationError):
        validate_zero_resource_local_adaptation(
            style_metadata=style_metadata,
            package_metadata=package_metadata,
            sld_sha256=authored.sld_sha256,
        )
