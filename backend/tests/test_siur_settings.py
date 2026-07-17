import hashlib
import json

import pytest

from app.reference_layers.siur_settings import (
    SiurSettingsBaseline,
    SiurSettingsError,
    SiurSettingsLimits,
    SiurSettingsPromotionError,
    analyze_siur_settings,
)


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
    assert layer.styles[0].is_default is True


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
