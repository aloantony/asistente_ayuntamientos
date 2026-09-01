from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.siur_wmc import (
    SiurWmcError,
    augment_catalog_with_wmc_evidence,
    compare_wmc_to_catalog,
    parse_wmc_evidence,
)


FIXTURE = Path(__file__).parent / "fixtures" / "siur_context.xml"
EXPECTED_SHA256 = (
    "9c3571179e8f489daa9b9c4531d99d0e0e612be03b1e269fa38908e72e6aaeb8"
)


def exact_fixture() -> bytes:
    # The downloaded WMC has no final newline. Keeping the source fixture as a
    # normal text file should not make its evidence hash platform-dependent.
    return FIXTURE.read_bytes().rstrip(b"\r\n")


def matching_catalog() -> ReferenceCatalogDefinition:
    evidence = parse_wmc_evidence(exact_fixture())
    service_urls = {
        layer.service_key: layer.service_url for layer in evidence.layers
    }
    services = tuple(
        ReferenceServiceDefinition(
            source_key=key,
            title=key,
            upstream_protocol="wms",
            base_url=url,
            version="1.1.1",
        )
        for key, url in sorted(service_urls.items())
    )
    layers = tuple(
        ReferenceLayerDefinition(
            source_key=layer.source_key,
            node_type="layer",
            title=layer.title,
            service_key=layer.service_key,
            remote_name=layer.remote_name,
            role="overlay",
            renderer="raster_tile",
            delivery_mode="proxy",
            style_name=layer.selected_style_key,
            styles=tuple(
                ReferenceLayerStyleDefinition(
                    source_key=style.source_key,
                    title=style.title,
                    legend_url=style.legend_url,
                    sort_order=style_index,
                    is_default=style.selected,
                )
                for style_index, style in enumerate(layer.styles)
            ),
        )
        for layer in evidence.layers
    )
    extra = ReferenceLayerDefinition(
        source_key="layer:wms:idecyl:urbanismo:future",
        node_type="layer",
        title="Capa adicional del catálogo completo",
        service_key="service:wms:idecyl:urbanismo",
        remote_name="urbanismo:future",
        role="overlay",
        renderer="raster_tile",
        delivery_mode="proxy",
    )
    return ReferenceCatalogDefinition(
        provider_key="siur",
        source_url=(
            "https://idecyl.jcyl.es/siur/assets/settings/settings.json"
        ),
        raw_catalog={"fixture": "complete-catalog"},
        services=services,
        layers=layers + (extra,),
    )


def test_exact_download_is_parsed_as_partial_wmc_evidence() -> None:
    evidence = parse_wmc_evidence(exact_fixture())

    assert evidence.content_sha256 == EXPECTED_SHA256
    assert evidence.title == (
        "Sistema de Información Urbanística de Castilla y León"
    )
    assert evidence.bounds.crs == "EPSG:25830"
    assert len(evidence.layers) == 11
    assert evidence.service_count == 3
    assert evidence.style_count == 28
    assert evidence.selected_style_count == 11
    assert evidence.metadata_count == 10
    assert {layer.crs for layer in evidence.layers} == {"EPSG:25830"}
    assert [layer.opacity for layer in evidence.layers if layer.opacity < 1] == [
        Decimal("0.8"),
        Decimal("0.8"),
        Decimal("0.75"),
    ]
    assert sum(layer.visible for layer in evidence.layers) == 8
    assert all(layer.queryable for layer in evidence.layers)
    assert evidence.normalized()["content_sha256"] == EXPECTED_SHA256


def test_wmc_is_a_probe_and_does_not_reject_extra_catalog_layers() -> None:
    evidence = parse_wmc_evidence(exact_fixture())
    definition = matching_catalog()

    report = compare_wmc_to_catalog(evidence, definition)

    assert report.blocking_issues == ()
    assert len(report.matched_layers) == 11
    assert len(report.matched_styles) == 28
    assert len(definition.layers) == 12


def test_wmc_enriches_only_matching_entries_and_never_adds_layers() -> None:
    evidence = parse_wmc_evidence(exact_fixture())
    complete = matching_catalog()
    bare = replace(
        complete,
        services=tuple(
            replace(service, version=None) for service in complete.services
        ),
        layers=tuple(
            replace(layer, style_name=None, styles=())
            for layer in complete.layers
        ),
    )

    enriched = augment_catalog_with_wmc_evidence(bare, evidence)
    report = compare_wmc_to_catalog(evidence, enriched)

    assert report.blocking_issues == ()
    assert len(enriched.layers) == len(bare.layers) == 12
    assert sum(len(layer.styles) for layer in enriched.layers) == 28
    assert all(service.version == "1.1.1" for service in enriched.services)
    assert all(
        layer.queryable
        for layer in enriched.layers
        if layer.remote_name != "urbanismo:future"
    )
    extra = next(
        layer for layer in enriched.layers if layer.remote_name == "urbanismo:future"
    )
    assert extra.styles == ()


def test_wmc_merges_workspace_prefixed_style_alias_without_a_duplicate() -> None:
    evidence = parse_wmc_evidence(exact_fixture())
    definition = matching_catalog()
    target = next(
        layer
        for layer in definition.layers
        if layer.remote_name == "plau_cyl_clasificacion"
    )
    assert len(target.styles) == 1
    observed_style = target.styles[0]
    assert observed_style.source_key.startswith("urbanismo:")
    local_key = observed_style.source_key.split(":", 1)[1]
    settings_style = replace(
        observed_style,
        source_key=local_key,
        remote_name=local_key,
    )
    settings_layer = replace(
        target,
        style_name=local_key,
        styles=(settings_style,),
    )
    native_like = replace(
        definition,
        layers=tuple(
            settings_layer if layer.source_key == target.source_key else layer
            for layer in definition.layers
        ),
    )

    enriched = augment_catalog_with_wmc_evidence(native_like, evidence)
    report = compare_wmc_to_catalog(evidence, enriched)

    assert report.blocking_issues == ()
    enriched_layer = next(
        layer
        for layer in enriched.layers
        if layer.source_key == target.source_key
    )
    assert enriched_layer.style_name == local_key
    assert len(enriched_layer.styles) == 1
    assert enriched_layer.styles[0].source_key == local_key
    assert enriched_layer.styles[0].remote_name == observed_style.source_key
    assert enriched_layer.styles[0].is_default is True


def test_wmc_style_alias_matching_requires_a_one_to_one_mapping() -> None:
    evidence = parse_wmc_evidence(exact_fixture())
    definition = matching_catalog()
    target = next(
        layer
        for layer in evidence.layers
        if layer.remote_name == "plau_cyl_clasificacion"
    )
    selected = target.styles[0]
    duplicate_alias = replace(
        selected,
        source_key=f"alias:{selected.source_key.rsplit(':', 1)[-1]}",
        remote_name=f"alias:{selected.remote_name.rsplit(':', 1)[-1]}",
        selected=False,
    )
    collided_layer = replace(target, styles=(selected, duplicate_alias))
    collided_evidence = replace(
        evidence,
        layers=tuple(
            collided_layer if layer.source_key == target.source_key else layer
            for layer in evidence.layers
        ),
    )

    report = compare_wmc_to_catalog(collided_evidence, definition)

    collisions = [
        identity
        for identity in report.unmatched_styles
        if identity.startswith(f"{target.source_key}|")
    ]
    assert len(collisions) == 2
    assert target.source_key in report.selected_style_mismatches
    assert report.blocking_issues


def test_wmc_probe_blocks_missing_or_ambiguous_observed_elements() -> None:
    evidence = parse_wmc_evidence(exact_fixture())
    definition = matching_catalog()
    first = definition.layers[0]
    without_first = replace(definition, layers=definition.layers[1:])

    missing_report = compare_wmc_to_catalog(evidence, without_first)
    assert missing_report.unmatched_layers == (evidence.layers[0].source_key,)
    assert missing_report.blocking_issues

    duplicate = replace(
        first,
        source_key=f"{first.source_key}:duplicate",
    )
    ambiguous = replace(definition, layers=definition.layers + (duplicate,))
    ambiguous_report = compare_wmc_to_catalog(evidence, ambiguous)
    assert ambiguous_report.ambiguous_layers == (evidence.layers[0].source_key,)

    first_without_style = replace(first, styles=first.styles[:-1])
    missing_style = replace(
        definition,
        layers=(first_without_style,) + definition.layers[1:],
    )
    style_report = compare_wmc_to_catalog(evidence, missing_style)
    assert style_report.unmatched_styles

    wrong_selected = replace(
        first,
        style_name=first.styles[-1].source_key,
        styles=tuple(
            replace(
                style,
                is_default=style.source_key == first.styles[-1].source_key,
            )
            for style in first.styles
        ),
    )
    selected_report = compare_wmc_to_catalog(
        evidence,
        replace(definition, layers=(wrong_selected,) + definition.layers[1:]),
    )
    assert selected_report.selected_style_mismatches == (
        evidence.layers[0].source_key,
    )


def test_wmc_probe_requires_siur_and_the_observed_service_endpoint() -> None:
    evidence = parse_wmc_evidence(exact_fixture())
    definition = matching_catalog()

    provider_report = compare_wmc_to_catalog(
        evidence,
        replace(definition, provider_key="other"),
    )
    assert provider_report.provider_mismatch is True
    assert provider_report.blocking_issues

    changed_service = replace(
        definition.services[0],
        base_url="https://idecyl.jcyl.es/geoserver/otro/wms",
    )
    service_report = compare_wmc_to_catalog(
        evidence,
        replace(
            definition,
            services=(changed_service,) + definition.services[1:],
        ),
    )
    assert changed_service.source_key in service_report.service_mismatches

    changed_version = replace(definition.services[0], version="1.3.0")
    version_report = compare_wmc_to_catalog(
        evidence,
        replace(
            definition,
            services=(changed_version,) + definition.services[1:],
        ),
    )
    assert changed_version.source_key in version_report.service_mismatches

    changed_protocol = replace(
        definition.services[0],
        upstream_protocol="wfs",
    )
    protocol_report = compare_wmc_to_catalog(
        evidence,
        replace(
            definition,
            services=(changed_protocol,) + definition.services[1:],
        ),
    )
    assert changed_protocol.source_key in protocol_report.service_mismatches


@pytest.mark.parametrize(
    "document, expected",
    [
        (
            b'<!DOCTYPE ViewContext [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<ViewContext xmlns="http://www.opengis.net/context">&xxe;'
            b"</ViewContext>",
            "DTD and entity declarations are forbidden",
        ),
        (
            b'<ViewContext version="1.1.0"><General /></ViewContext>',
            "Only OGC WMC 1.1.0 is supported",
        ),
        (b"x" * (512 * 1024 + 1), "WMC size is outside the allowed range"),
    ],
)
def test_wmc_parser_rejects_unsafe_or_unsupported_xml(
    document: bytes,
    expected: str,
) -> None:
    with pytest.raises(SiurWmcError, match=expected):
        parse_wmc_evidence(document)


def test_wmc_parser_rejects_upstream_urls_outside_the_fixed_allowlist() -> None:
    unsafe = exact_fixture().replace(
        b"https://idecyl.jcyl.es/",
        b"https://attacker.example/",
    )

    with pytest.raises(SiurWmcError, match="outside the SIUR allowlist"):
        parse_wmc_evidence(unsafe)


def test_wmc_parser_rejects_utf16_before_doctype_or_entity_processing() -> None:
    utf16 = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<!DOCTYPE ViewContext [<!ENTITY bypass "expanded">]>'
        '<ViewContext version="1.1.0" '
        'xmlns="http://www.opengis.net/context">&bypass;</ViewContext>'
    ).encode("utf-16")

    with pytest.raises(SiurWmcError, match="must use UTF-8 encoding"):
        parse_wmc_evidence(utf16)


def test_wmc_with_two_current_styles_selects_none_instead_of_guessing() -> None:
    # SIUR publishes plau_cyl_planes_parciales with two styles marked current.
    # The document stays parseable evidence, but it must stop asserting a
    # default for that layer rather than pick one of the two. See ADR-055.
    document = exact_fixture()
    marker = b'<Style>'
    assert document.count(b'current="1"') == 11
    ambiguous = document.replace(marker, b'<Style current="1">', 1)

    evidence = parse_wmc_evidence(ambiguous)

    assert len(evidence.layers) == 11
    ambiguous_layers = [
        layer
        for layer in evidence.layers
        if not any(style.selected for style in layer.styles)
    ]
    assert len(ambiguous_layers) == 1
    assert evidence.selected_style_count == 10
