import hashlib
import json
from pathlib import Path
import unicodedata

import pytest

from app.reference_layers.siur_settings import (
    SiurSettingsBaseline,
    SiurSettingsError,
    SiurSettingsLimits,
    SiurSettingsPromotionError,
    analyze_siur_settings,
)


NATIVE_FIXTURE = Path(__file__).parent / "fixtures" / "siur_settings_native.json"


def settings_bytes(
    *,
    group_title="Planeamiento",
    layer_title="Clasificación",
    service_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
    extra=None,
):
    value = {
        "version": 1,
        "services": {
            "urbanismo": {
                "title": "Urbanismo de Castilla y León",
                "serviceType": "WMS",
                "serviceUrl": service_url,
                "crs": "EPSG:25830",
                "format": "image/png",
            }
        },
        "layerGroups": [
            {
                "key": "planning",
                "label": group_title,
                "layers": [
                    {
                        "key": "classification",
                        "label": layer_title,
                        "serviceId": "urbanismo",
                        "layerName": "urbanismo:plau_cyl_clasificacion",
                        "styleName": (
                            "urbanismo:plau_cyl_clasificacion_color"
                        ),
                        "visible": True,
                        "opacity": 75,
                        "queryable": True,
                        **(extra or {}),
                    }
                ],
            }
        ],
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def baseline(document=None, **overrides):
    document = document or settings_bytes()
    probe = analyze_siur_settings(document)
    values = {
        "top_level_groups": 1,
        "groups": 1,
        "layers": 1,
        "services": 1,
        "raw_sha256": hashlib.sha256(document).hexdigest(),
        "layer_keys": frozenset(
            node.source_key
            for node in probe.nodes
            if node.kind == "layer" and node.source_key is not None
        ),
    }
    values.update(overrides)
    return SiurSettingsBaseline(**values)


def native_settings_bytes() -> bytes:
    return NATIVE_FIXTURE.read_bytes()


def baseline_for(document: bytes, **overrides) -> SiurSettingsBaseline:
    probe = analyze_siur_settings(document)
    values = {
        "top_level_groups": probe.top_level_group_count,
        "groups": probe.group_count,
        "layers": probe.layer_count,
        "services": len(probe.services),
        "raw_sha256": probe.raw_sha256,
        "layer_keys": frozenset(
            node.source_key
            for node in probe.nodes
            if node.kind == "layer" and node.source_key is not None
        ),
    }
    values.update(overrides)
    return SiurSettingsBaseline(**values)


def test_complete_reviewed_catalog_yields_definition_and_raw_hash() -> None:
    document = settings_bytes()

    analysis = analyze_siur_settings(document, baseline=baseline(document))

    assert analysis.can_apply is True
    assert analysis.raw_sha256 == hashlib.sha256(document).hexdigest()
    assert analysis.top_level_group_count == 1
    assert analysis.group_count == 1
    assert analysis.layer_count == 1
    definition = analysis.require_definition()
    assert len(definition.services) == 1
    assert definition.services[0].source_key == (
        "service:wms:idecyl:urbanismo"
    )
    assert [node.node_type for node in definition.layers] == ["group", "layer"]
    layer = definition.layers[1]
    assert layer.parent_key == definition.layers[0].source_key
    assert layer.default_opacity == 0.75
    assert layer.remote_name == "urbanismo:plau_cyl_clasificacion"
    assert layer.style_name == "urbanismo:plau_cyl_clasificacion_color"
    assert [style.source_key for style in layer.styles] == [
        "urbanismo:plau_cyl_clasificacion_color"
    ]
    assert layer.styles[0].remote_name == (
        "urbanismo:plau_cyl_clasificacion_color"
    )
    assert layer.styles[0].is_default is True


def test_style_identity_is_stable_but_remote_name_preserves_exact_case() -> None:
    document = settings_bytes().replace(
        b"urbanismo:plau_cyl_clasificacion_color",
        b"Urbanismo:Plau_Cyl_Clasificacion_Color",
    )

    analysis = analyze_siur_settings(document, baseline=baseline(document))
    style = analysis.require_definition().layers[1].styles[0]

    assert style.source_key == "urbanismo:plau_cyl_clasificacion_color"
    assert style.remote_name == "Urbanismo:Plau_Cyl_Clasificacion_Color"


def test_real_siur_profile_maps_tree_backgrounds_and_nested_evidence() -> None:
    document = native_settings_bytes()

    analysis = analyze_siur_settings(document, baseline=baseline_for(document))

    assert analysis.can_apply is True
    assert analysis.top_level_group_count == 1
    assert analysis.group_count == 2
    assert analysis.layer_count == 5
    definition = analysis.require_definition()
    assert len(definition.services) == 5
    assert sorted(service.upstream_protocol for service in definition.services) == [
        "wms",
        "wmts",
        "wmts",
        "wmts",
        "xyz",
    ]
    assert all(service.license_status == "pending" for service in definition.services)

    overlays = [
        layer
        for layer in definition.layers
        if layer.node_type == "layer" and layer.role == "overlay"
    ]
    assert len(overlays) == 2
    classification = next(
        layer for layer in overlays if layer.remote_name == "plau_cyl_clasificacion"
    )
    classification_service = next(
        service
        for service in definition.services
        if service.source_key == classification.service_key
    )
    assert classification_service.upstream_protocol == "wms"
    assert classification_service.base_url == (
        "https://www.ign.es/wms-inspire/unidades-administrativas"
    )
    assert classification.default_visible is False
    assert classification.queryable is False
    assert classification.image_format is None
    assert classification.bounds is None
    assert classification.supported_crs == ()
    assert classification.options == {
        "settings_path": (
            "$.settings[0].groupLayers.children[0].children[0]"
        ),
        "source_extent": {
            "srs": "EPSG:25830",
            "minx": "146569.61819428788",
            "miny": "4430813.815909118",
            "maxx": "608202.6727624929",
            "maxy": "4798875.983524372",
        },
        "source_legend": {
            "url": "https://idecyl.jcyl.es/geoserver/urbanismo/ows",
            "format": "image/png",
        },
    }
    assert classification.metadata_url.endswith("#/metadata/urbanismo")
    assert classification.legend_url.endswith("/urbanismo/ows")
    assert classification.style_name == "plau_cyl_clasificacion_color"
    assert [style.remote_name for style in classification.styles] == [
        "plau_cyl_clasificacion_color",
        "plau_cyl_clasificacion_trama",
    ]
    assert [style.is_default for style in classification.styles] == [True, False]

    implicit = next(layer for layer in overlays if layer.remote_name == "catastrones")
    assert implicit.style_name is None
    assert implicit.styles == ()

    backgrounds = [
        layer
        for layer in definition.layers
        if layer.node_type == "layer" and layer.role == "base"
    ]
    assert [layer.title for layer in backgrounds] == ["IMAGEN", "MAPA", "RELIEVE"]
    assert [layer.default_visible for layer in backgrounds] == [True, False, False]
    assert all(layer.parent_key is None for layer in backgrounds)


def test_native_layer_identity_preserves_repeated_placements() -> None:
    value = json.loads(native_settings_bytes())
    urbanismo = value["settings"][0]["groupLayers"]["children"][0]
    original = urbanismo["children"][0]
    urbanismo["children"].append(
        {
            "name": "Otra ubicación",
            "children": [json.loads(json.dumps(original))],
        }
    )
    document = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    analysis = analyze_siur_settings(document, baseline=baseline_for(document))

    repeated = [
        node
        for node in analysis.nodes
        if node.kind == "layer" and node.remote_name == "plau_cyl_clasificacion"
    ]
    assert analysis.can_apply is True
    assert len(repeated) == 2
    assert len({node.source_key for node in repeated}) == 2


def test_native_identities_normalize_case_and_unicode_composition() -> None:
    original_document = native_settings_bytes()
    original = analyze_siur_settings(
        original_document,
        baseline=baseline_for(original_document),
    )
    value = json.loads(original_document)
    root_group = value["settings"][0]["groupLayers"]["children"][0]
    root_group["name"] = root_group["name"].upper()
    leaf = root_group["children"][0]
    leaf["name"] = unicodedata.normalize("NFD", leaf["name"])
    normalized_document = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    normalized = analyze_siur_settings(
        normalized_document,
        baseline=baseline_for(normalized_document),
    )

    assert original.can_apply is True
    assert normalized.can_apply is True
    assert [node.source_key for node in original.nodes] == [
        node.source_key for node in normalized.nodes
    ]


def test_native_wms_version_is_extracted_before_endpoint_normalization() -> None:
    value = json.loads(native_settings_bytes())
    endpoint = value["settings"][0]["groupLayers"]["children"][0]["children"][0][
        "endPoint"
    ]
    endpoint["url"] = "https://mapas.igme.es/gis/services/Cartografia/WMS?version=1.3.0"
    document = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    analysis = analyze_siur_settings(document, baseline=baseline_for(document))

    service = next(
        service
        for service in analysis.require_definition().services
        if service.base_url.startswith("https://mapas.igme.es/")
    )
    assert service.base_url == "https://mapas.igme.es/gis/services/Cartografia/WMS"
    assert service.version == "1.3.0"


def test_native_profile_rejects_unknown_nested_fields() -> None:
    value = json.loads(native_settings_bytes())
    layer = value["settings"][0]["groupLayers"]["children"][0]["children"][0][
        "endPoint"
    ]["layer"]
    layer["futureRendererOptions"] = {"x": 1}
    document = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    analysis = analyze_siur_settings(document, baseline=baseline_for(document))

    assert analysis.definition is None
    assert any(
        issue.code == "unrecognized_field"
        and issue.path.endswith(".futureRendererOptions")
        for issue in analysis.unresolved
    )


@pytest.mark.parametrize(
    "mutation, issue_code",
    [
        ("selected", "selected_setting_unresolved"),
        ("protocol", "layer_protocol_missing"),
        ("extent", "invalid_extent"),
        ("service_fragment", "invalid_service_url"),
        ("wmc_url", "invalid_relative_url"),
        ("control_character", "invalid_text"),
    ],
)
def test_native_profile_rejects_unsafe_or_ambiguous_input(
    mutation, issue_code
) -> None:
    value = json.loads(native_settings_bytes())
    endpoint = value["settings"][0]["groupLayers"]["children"][0]["children"][0][
        "endPoint"
    ]
    if mutation == "selected":
        value["selectedSetting"] = "missing"
    elif mutation == "protocol":
        endpoint["type"] = "future-map-service"
    elif mutation == "extent":
        endpoint["layer"]["extent"]["maxx"] = "100"
    elif mutation == "service_fragment":
        endpoint["url"] = "https://idecyl.jcyl.es/geoserver/urbanismo/wms#unsafe"
    elif mutation == "wmc_url":
        value["settings"][0]["wmcUrl"] = "../default.xml"
    else:
        value["settings"][0]["groupLayers"]["children"][0]["name"] += "\u0000"
    document = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    analysis = analyze_siur_settings(document, baseline=baseline_for(document))

    assert analysis.definition is None
    assert any(issue.code == issue_code for issue in analysis.unresolved)


def test_identities_do_not_depend_on_titles_or_order() -> None:
    first_document = settings_bytes()
    renamed_document = settings_bytes(
        group_title="Urbanismo",
        layer_title="Suelo vigente",
    )
    first = analyze_siur_settings(
        first_document,
        baseline=baseline(first_document),
    )
    renamed = analyze_siur_settings(
        renamed_document,
        baseline=baseline(renamed_document),
    )

    assert [node.source_key for node in first.nodes] == [
        node.source_key for node in renamed.nodes
    ]
    assert first.services[0].source_key == renamed.services[0].source_key
    assert first.raw_sha256 != renamed.raw_sha256

    first_external = settings_bytes(
        service_url="https://maps.example.org/services/wms",
    )
    rotated_external = settings_bytes(
        service_url="https://maps-backup.example.org/services/wms",
    )
    first_endpoint = analyze_siur_settings(
        first_external,
        baseline=baseline(first_external),
    )
    rotated_endpoint = analyze_siur_settings(
        rotated_external,
        baseline=baseline(rotated_external),
    )
    assert first_endpoint.services[0].source_key == (
        rotated_endpoint.services[0].source_key
    )


def test_unknown_fields_and_missing_baseline_block_instead_of_guessing() -> None:
    document = settings_bytes(extra={"futureRendererOptions": {"x": 1}})

    unreviewed = analyze_siur_settings(document)
    reviewed = analyze_siur_settings(document, baseline=baseline(document))

    assert unreviewed.definition is None
    assert any(issue.code == "baseline_required" for issue in unreviewed.unresolved)
    assert reviewed.definition is None
    assert [issue.path for issue in reviewed.unresolved] == [
        "$.layerGroups[0].layers[0].futureRendererOptions"
    ]
    with pytest.raises(SiurSettingsPromotionError):
        reviewed.require_definition()


def test_counts_without_an_approved_raw_hash_never_promote() -> None:
    document = settings_bytes()
    reviewed = baseline(document)
    unpinned = SiurSettingsBaseline(
        top_level_groups=1,
        groups=1,
        layers=1,
        services=1,
        layer_keys=reviewed.layer_keys,
    )

    analysis = analyze_siur_settings(document, baseline=unpinned)

    assert analysis.definition is None
    assert {issue.code for issue in analysis.unresolved} == {
        "approved_hash_required"
    }


def test_counts_and_hash_without_layer_manifest_never_promote() -> None:
    document = settings_bytes()
    unpinned = SiurSettingsBaseline(
        top_level_groups=1,
        groups=1,
        layers=1,
        services=1,
        raw_sha256=hashlib.sha256(document).hexdigest(),
    )

    analysis = analyze_siur_settings(document, baseline=unpinned)

    assert analysis.definition is None
    assert {issue.code for issue in analysis.unresolved} == {
        "approved_layer_manifest_required"
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/wms",
        "https://[::1]/wms",
        "http://localhost/wms",
        "https://service.internal/wms",
        "https://user:secret@example.org/wms",
    ],
)
def test_unsafe_service_urls_are_never_normalized_into_the_catalog(url) -> None:
    document = settings_bytes(service_url=url)

    analysis = analyze_siur_settings(
        document,
        baseline=baseline(document),
    )

    assert analysis.definition is None
    assert any(
        issue.code == "invalid_service_url" for issue in analysis.unresolved
    )
    assert url not in " ".join(analysis.blocking_issues)


def test_baseline_mismatches_are_exhaustive() -> None:
    analysis = analyze_siur_settings(
        settings_bytes(),
        baseline=baseline(
            settings_bytes(),
            top_level_groups=12,
            layers=224,
            services=7,
        ),
    )

    assert analysis.definition is None
    assert {issue.code for issue in analysis.unresolved} == {
        "top_groups_mismatch",
        "layers_mismatch",
        "services_mismatch",
    }


@pytest.mark.parametrize(
    "document, limits, message",
    [
        (b"{}", SiurSettingsLimits(max_bytes=1), "size"),
        (
            b'{"groups":[{"id":"a","children":[]}]}',
            SiurSettingsLimits(max_depth=2),
            "deep",
        ),
        (
            b'{"groups":[]}',
            SiurSettingsLimits(max_nodes=1),
            "too many",
        ),
        (
            b'{"title":"abcd"}',
            SiurSettingsLimits(max_string_chars=3),
            "oversized string",
        ),
        (b'{"groups":[],"groups":[]}', SiurSettingsLimits(), "duplicate"),
        (b'{"value":NaN}', SiurSettingsLimits(), "strict UTF-8 JSON"),
    ],
)
def test_json_input_limits_are_enforced(document, limits, message) -> None:
    with pytest.raises(SiurSettingsError, match=message):
        analyze_siur_settings(document, limits=limits)
