from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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
    local_style_package_metadata,
    validate_zero_resource_local_adaptation,
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
            "#ffbee8",
            "#a80084",
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
