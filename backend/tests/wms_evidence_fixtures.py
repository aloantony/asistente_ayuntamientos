import json

from app.reference_layers.delivery_evidence import (
    apply_delivery_evidence,
    build_delivery_evidence_plan,
    parse_license_review,
)
from app.reference_layers.wms_capabilities import parse_wms_capabilities


def make_capabilities_xml(
    *,
    version: str = "1.3.0",
    endpoint: str = "https://idecyl.jcyl.es/geoserver/urbanismo/wms",
    feature_info_endpoint: str | None = None,
    legend_endpoint: str | None = None,
    remote_name: str = "plau_cyl_clasificacion",
    crs: tuple[str, ...] = ("EPSG:25830", "EPSG:3857"),
    queryable: bool = True,
    styles: tuple[str, ...] = (
        "plau_cyl_clasificacion_color",
        "plau_cyl_clasificacion_trama",
    ),
    map_formats: tuple[str, ...] = ("image/png",),
    legend_formats: tuple[str, ...] = ("image/png",),
    feature_info_formats: tuple[str, ...] = ("application/json",),
) -> bytes:
    feature_endpoint = feature_info_endpoint or endpoint
    resolved_legend_endpoint = legend_endpoint or endpoint
    namespace = (
        ' xmlns="http://www.opengis.net/wms"'
        if version == "1.3.0"
        else ""
    )
    root = "WMS_Capabilities" if version == "1.3.0" else "WMT_MS_Capabilities"
    crs_tag = "CRS" if version == "1.3.0" else "SRS"
    style_xml = "".join(
        f"<Style><Name>{style}</Name><Title>{style}</Title></Style>"
        for style in styles
    )
    map_format_xml = "".join(
        f"<Format>{value}</Format>" for value in map_formats
    )
    legend_format_xml = "".join(
        f"<Format>{value}</Format>" for value in legend_formats
    )
    feature_format_xml = "".join(
        f"<Format>{value}</Format>" for value in feature_info_formats
    )
    feature_info_xml = ""
    if feature_info_formats:
        feature_info_xml = f"""
        <GetFeatureInfo>
          {feature_format_xml}
          <DCPType><HTTP><Get><OnlineResource
            xlink:href="{feature_endpoint}?"/></Get></HTTP></DCPType>
        </GetFeatureInfo>
        """
    legend_xml = ""
    if legend_formats:
        legend_xml = f"""
        <GetLegendGraphic>
          {legend_format_xml}
          <DCPType><HTTP><Get><OnlineResource
            xlink:href="{resolved_legend_endpoint}?"/></Get></HTTP></DCPType>
        </GetLegendGraphic>
        """
    crs_xml = "".join(f"<{crs_tag}>{value}</{crs_tag}>" for value in crs)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} version="{version}"{namespace}
 xmlns:xlink="http://www.w3.org/1999/xlink">
  <Service><Name>WMS</Name><Title>Synthetic SIUR fixture</Title></Service>
  <Capability>
    <Request>
      <GetMap>
        {map_format_xml}
        <DCPType><HTTP><Get><OnlineResource
          xlink:href="{endpoint}?"/></Get></HTTP></DCPType>
      </GetMap>
      {legend_xml}
      {feature_info_xml}
    </Request>
    <Layer queryable="{1 if queryable else 0}">
      <Title>Synthetic parent</Title>
      {crs_xml}
      {style_xml}
      <Layer>
        <Name>{remote_name}</Name>
        <Title>Synthetic layer</Title>
      </Layer>
    </Layer>
  </Capability>
</{root}>""".encode()


def make_license_review_document(
    service_key: str,
    *,
    decision: str = "approved",
    allow_proxy: bool = True,
    allow_cache: bool = True,
    reviewer: str = "Synthetic Test Reviewer",
    reviewed_at: str = "2026-07-17T12:30:00Z",
    supersedes_review_sha256: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "schema_version": "siur-license-review-v1",
            "provider_key": "siur",
            "service_key": service_key,
            "decision": decision,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "supersedes_review_sha256": supersedes_review_sha256,
            "license_name": "Synthetic test license",
            "license_url": "https://example.test/synthetic-license",
            "license_terms": (
                "Synthetic test-only review. This is not an approval of the "
                "real SIUR license."
            ),
            "allow_proxy": allow_proxy,
            "allow_cache": allow_cache,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def apply_synthetic_delivery_evidence(
    db,
    layer,
    *,
    capabilities_xml: bytes | None = None,
    license_document: bytes | None = None,
    **capabilities_options,
):
    capabilities = parse_wms_capabilities(
        capabilities_xml
        if capabilities_xml is not None
        else make_capabilities_xml(**capabilities_options)
    )
    review = parse_license_review(
        license_document
        if license_document is not None
        else make_license_review_document(layer.service.source_key)
    )
    plan = build_delivery_evidence_plan(db, capabilities, review)
    result, applied_plan = apply_delivery_evidence(
        db,
        capabilities,
        review,
        expected_plan_sha256=plan.plan_sha256,
    )
    return result, applied_plan, capabilities, review
