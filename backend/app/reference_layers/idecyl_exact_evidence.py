"""Hash-bound official metadata for exact IDECyL acquisition sources.

The committed records are evidence inputs, not mirror authorizations.  A
separate persisted human review remains mandatory before acquisition or
promotion.  This module only exposes an allowlisted source when the local
manifest, the curated XML bundle and the relevant ISO 19139 record all match
their reviewed identities.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree


EVIDENCE_SCHEMA = "siur-idecyl-exact-source-evidence/v1"
MANIFEST_SCHEMA = "siur-idecyl-exact-evidence-manifest/v1"
RECORDS_SCHEMA = "siur-idecyl-exact-metadata-records/v1"
MANIFEST_RESOURCE = "evidence/idecyl_exact/manifest-v1.json"
RECORDS_RESOURCE = "evidence/idecyl_exact/records-20260727.xml"
MANIFEST_SHA256 = (
    "5b60099dfbe8000e286b1da30491e846"
    "73a8691baea64c32aa4f284af56cbf07"
)
RECORDS_SHA256 = (
    "9e7eb47a169eb22303267378cd762357a"
    "be0c97137267e37e6e80c4f720e07d5"
)
SOURCE_SNAPSHOT_SHA256 = (
    "ed7b6ea8256313aa1977e6cad89bd069"
    "4d752bf519a55705d5c562ecf5180a56"
)
AUDIT_SHA256 = (
    "86f9d379d0c775473bf7bea53dfa59f9"
    "b400b73474b6822b0d5ec6d469b4e275"
)
OFFICIAL_CATALOG_URL = (
    "https://idecyl.jcyl.es/geonetwork/srv/spa/csw"
    "?service=CSW&request=GetCapabilities&version=2.0.2"
)
MAX_MANIFEST_BYTES = 128 * 1024
MAX_RECORDS_BYTES = 2 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_FID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$", re.ASCII)
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,500}$", re.ASCII)
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$", re.ASCII)
_LAYER_SOURCE_KEY_RE = re.compile(
    r"^layer:siur:[0-9a-f]{64}$",
    re.ASCII,
)
_RECORD_BLOCK_RE = re.compile(
    rb"    <gmd:MD_Metadata\b.*?    </gmd:MD_Metadata>\r?\n",
    re.DOTALL,
)
_GMD = "http://www.isotc211.org/2005/gmd"
_GCO = "http://www.isotc211.org/2005/gco"
_NS = {"gmd": _GMD, "gco": _GCO}
_XSI = "http://www.w3.org/2001/XMLSchema-instance"


class IDECyLExactEvidenceError(RuntimeError):
    """Committed IDECyL evidence is missing, altered or semantically invalid."""


@dataclass(frozen=True)
class ReviewedIDECyLExactSource:
    """One exact, evidence-bound WFS source selected for a SIUR layer."""

    profile: str
    catalog_layer_source_key: str
    catalog_endpoint_url: str
    catalog_remote_name: str
    protocol: str
    target_kind: str
    endpoint_url: str
    remote_name: str
    sync_strategy: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _OnlineResource:
    protocol: str
    name: str
    description: str
    url: str


@dataclass(frozen=True)
class _ParsedRecord:
    fid: str
    title: str
    raw_sha256: str
    terms_urls: tuple[str, ...]
    use_constraint_codes: tuple[str, ...]
    access_constraint_codes: tuple[str, ...]
    attribution_statements: tuple[str, ...]
    online_resources: tuple[_OnlineResource, ...]


@dataclass(frozen=True)
class _EvidencePackage:
    sources: tuple[ReviewedIDECyLExactSource, ...]
    sources_by_identity: MappingProxyType


def is_idecyl_geoserver_catalog_endpoint(endpoint_url: str) -> bool:
    """Return whether an already-canonical URL is an IDECyL WMS catalog."""

    try:
        parts = urlsplit(endpoint_url)
    except (TypeError, ValueError):
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == "idecyl.jcyl.es"
        and parts.username is None
        and parts.password is None
        and parts.port in {None, 443}
        and parts.query == ""
        and parts.fragment == ""
        and re.fullmatch(
            r"/geoserver/[A-Za-z0-9_.-]+/(?:wms|ows)/?",
            parts.path,
            re.ASCII,
        )
        is not None
    )


def reviewed_idecyl_exact_source(
    *,
    catalog_layer_source_key: str,
    catalog_endpoint_url: str,
    catalog_remote_name: str,
) -> ReviewedIDECyLExactSource | None:
    """Return fresh evidence only for one exact committed allowlist identity."""

    package = _committed_evidence_package()
    source = package.sources_by_identity.get(
        (
            catalog_layer_source_key,
            catalog_endpoint_url,
            catalog_remote_name,
        )
    )
    return _fresh_source(source) if source is not None else None


def idecyl_exact_source_inventory() -> tuple[ReviewedIDECyLExactSource, ...]:
    """Return fresh projections of all 31 evidence-bound source definitions."""

    return tuple(
        _fresh_source(source)
        for source in _committed_evidence_package().sources
    )


@lru_cache(maxsize=1)
def _committed_evidence_package() -> _EvidencePackage:
    return _load_evidence_package(
        _resource_body(MANIFEST_RESOURCE, MAX_MANIFEST_BYTES),
        _resource_body(RECORDS_RESOURCE, MAX_RECORDS_BYTES),
        expected_manifest_sha256=MANIFEST_SHA256,
    )


def _load_evidence_package(
    manifest_body: bytes,
    records_body: bytes,
    *,
    expected_manifest_sha256: str,
) -> _EvidencePackage:
    """Validate exact bytes and all legal/source bindings.

    The explicit expected digest parameter is useful to test semantic
    validation after rebuilding a syntactically valid but altered package.
    Production always supplies the hardcoded committed manifest digest.
    """

    _bounded_bytes(
        manifest_body,
        maximum=MAX_MANIFEST_BYTES,
        message="IDECyL evidence manifest size is invalid",
    )
    _bounded_bytes(
        records_body,
        maximum=MAX_RECORDS_BYTES,
        message="IDECyL metadata bundle size is invalid",
    )
    if (
        _SHA256_RE.fullmatch(expected_manifest_sha256) is None
        or hashlib.sha256(manifest_body).hexdigest()
        != expected_manifest_sha256
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL evidence manifest failed its local digest"
        )
    manifest = _strict_json_object(manifest_body)
    _exact_keys(
        manifest,
        {"schema", "capture", "records", "sources"},
        "IDECyL evidence manifest shape is invalid",
    )
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence manifest schema is invalid"
        )
    capture = _capture(manifest["capture"], records_body)
    parsed_records = _parse_records_bundle(records_body)
    reviewed_records = _reviewed_records(
        manifest["records"],
        parsed_records,
    )
    sources = _reviewed_sources(
        manifest["sources"],
        reviewed_records,
        capture,
    )
    by_identity: dict[
        tuple[str, str, str],
        ReviewedIDECyLExactSource,
    ] = {}
    for source in sources:
        identity = (
            source.catalog_layer_source_key,
            source.catalog_endpoint_url,
            source.catalog_remote_name,
        )
        if identity in by_identity:
            raise IDECyLExactEvidenceError(
                "IDECyL evidence contains a duplicate source identity"
            )
        by_identity[identity] = source
    if len(by_identity) != 31:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence does not cover exactly 31 sources"
        )
    return _EvidencePackage(
        sources=tuple(sources),
        sources_by_identity=MappingProxyType(by_identity),
    )


def _capture(value: Any, records_body: bytes) -> dict[str, Any]:
    capture = _dict(value, "IDECyL capture evidence is invalid")
    _exact_keys(
        capture,
        {
            "captured_at",
            "official_catalog_url",
            "source_snapshot_sha256",
            "source_record_count",
            "curated_record_count",
            "curated_layer_count",
            "audit_sha256",
            "bundle_resource",
            "bundle_sha256",
        },
        "IDECyL capture evidence shape is invalid",
    )
    expected = {
        "captured_at": "2026-07-27T01:55:14",
        "official_catalog_url": OFFICIAL_CATALOG_URL,
        "source_snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
        "source_record_count": 197,
        "curated_record_count": 30,
        "curated_layer_count": 31,
        "audit_sha256": AUDIT_SHA256,
        "bundle_resource": RECORDS_RESOURCE,
        "bundle_sha256": RECORDS_SHA256,
    }
    if capture != expected:
        raise IDECyLExactEvidenceError(
            "IDECyL capture evidence identity is invalid"
        )
    if hashlib.sha256(records_body).hexdigest() != capture["bundle_sha256"]:
        raise IDECyLExactEvidenceError(
            "IDECyL metadata bundle failed its local digest"
        )
    return capture


def _parse_records_bundle(body: bytes) -> dict[str, _ParsedRecord]:
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise IDECyLExactEvidenceError(
            "IDECyL metadata bundle contains forbidden XML declarations"
        )
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, RecursionError) as error:
        raise IDECyLExactEvidenceError(
            "IDECyL metadata bundle is not valid XML"
        ) from error
    if (
        root.tag != "idecylExactMetadataRecords"
        or root.attrib
        != {
            "schemaVersion": RECORDS_SCHEMA,
            "sourceSnapshotSha256": SOURCE_SNAPSHOT_SHA256,
        }
        or len(root) != 30
        or any(child.tag != f"{{{_GMD}}}MD_Metadata" for child in root)
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL metadata bundle identity is invalid"
        )
    blocks = _RECORD_BLOCK_RE.findall(body)
    if len(blocks) != 30:
        raise IDECyLExactEvidenceError(
            "IDECyL metadata bundle record framing is invalid"
        )
    result: dict[str, _ParsedRecord] = {}
    for block in blocks:
        record = _parse_record_block(block)
        key = record.fid.casefold()
        if key in result:
            raise IDECyLExactEvidenceError(
                "IDECyL metadata bundle contains a duplicate FID"
            )
        result[key] = record
    return result


def _parse_record_block(block: bytes) -> _ParsedRecord:
    wrapped = (
        f'<root xmlns:xsi="{_XSI}">'.encode("ascii")
        + block
        + b"</root>"
    )
    try:
        record = ElementTree.fromstring(wrapped)[0]
    except (ElementTree.ParseError, RecursionError, IndexError) as error:
        raise IDECyLExactEvidenceError(
            "IDECyL metadata record is invalid"
        ) from error
    fid = _element_text(
        record,
        "./gmd:fileIdentifier/gco:CharacterString",
    )
    title = _element_text(
        record,
        ".//gmd:citation//gmd:title/gco:CharacterString",
    )
    if (
        _FID_RE.fullmatch(fid) is None
        or not title
        or len(title) > 2_000
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL metadata record identity is invalid"
        )
    terms = _unique_element_texts(
        record,
        ".//gmd:resourceConstraints//"
        "gmd:useLimitation/gco:CharacterString",
    )
    attribution = _unique_element_texts(
        record,
        ".//gmd:resourceConstraints//"
        "gmd:otherConstraints/gco:CharacterString",
    )
    use_codes = _restriction_codes(
        record,
        ".//gmd:resourceConstraints//"
        "gmd:useConstraints/gmd:MD_RestrictionCode",
    )
    access_codes = _restriction_codes(
        record,
        ".//gmd:resourceConstraints//"
        "gmd:accessConstraints/gmd:MD_RestrictionCode",
    )
    resources: list[_OnlineResource] = []
    for online in record.findall(
        ".//gmd:onLine/gmd:CI_OnlineResource",
        _NS,
    ):
        url = _optional_element_text(online, "./gmd:linkage/gmd:URL")
        if not url:
            continue
        resources.append(
            _OnlineResource(
                protocol=_optional_element_text(
                    online,
                    "./gmd:protocol/gco:CharacterString",
                ),
                name=_optional_element_text(
                    online,
                    "./gmd:name/gco:CharacterString",
                ),
                description=_optional_element_text(
                    online,
                    "./gmd:description/gco:CharacterString",
                ),
                url=url,
            )
        )
    if not resources:
        raise IDECyLExactEvidenceError(
            "IDECyL metadata record has no online resources"
        )
    return _ParsedRecord(
        fid=fid,
        title=title,
        raw_sha256=hashlib.sha256(block).hexdigest(),
        terms_urls=terms,
        use_constraint_codes=use_codes,
        access_constraint_codes=access_codes,
        attribution_statements=attribution,
        online_resources=tuple(resources),
    )


def _reviewed_records(
    raw_records: Any,
    parsed_records: dict[str, _ParsedRecord],
) -> dict[str, tuple[_ParsedRecord, dict[str, Any]]]:
    if not isinstance(raw_records, list) or len(raw_records) != 30:
        raise IDECyLExactEvidenceError(
            "IDECyL reviewed record inventory is invalid"
        )
    result: dict[str, tuple[_ParsedRecord, dict[str, Any]]] = {}
    for value in raw_records:
        item = _dict(value, "IDECyL reviewed record is invalid")
        _exact_keys(
            item,
            {
                "fid",
                "title",
                "record_raw_sha256",
                "terms_urls",
                "use_constraint_codes",
                "access_constraint_codes",
                "attribution_statements",
                "attribution_labels",
            },
            "IDECyL reviewed record shape is invalid",
        )
        fid = _text(item["fid"], maximum=200)
        parsed = parsed_records.get(fid.casefold())
        if parsed is None or parsed.fid != fid:
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed record FID is unavailable"
            )
        labels = _text_list(
            item["attribution_labels"],
            minimum=1,
            maximum=3,
            item_maximum=200,
        )
        expected = {
            "fid": parsed.fid,
            "title": parsed.title,
            "record_raw_sha256": parsed.raw_sha256,
            "terms_urls": list(parsed.terms_urls),
            "use_constraint_codes": list(parsed.use_constraint_codes),
            "access_constraint_codes": list(
                parsed.access_constraint_codes
            ),
            "attribution_statements": list(
                parsed.attribution_statements
            ),
            "attribution_labels": labels,
        }
        if item != expected:
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed record does not match its XML evidence"
            )
        if (
            _SHA256_RE.fullmatch(parsed.raw_sha256) is None
            or parsed.use_constraint_codes != ("license",)
            or parsed.access_constraint_codes
            != ("otherRestrictions",)
            or not parsed.terms_urls
            or not parsed.attribution_statements
            or not _has_reuse_grant(parsed.attribution_statements)
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed record lacks exact reuse evidence"
            )
        for url in parsed.terms_urls:
            _legal_reference_url(url)
        _validate_attribution_labels(
            labels,
            parsed.attribution_statements,
        )
        if fid.casefold() in result:
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed records contain a duplicate FID"
            )
        result[fid.casefold()] = (parsed, item)
    if set(result) != set(parsed_records):
        raise IDECyLExactEvidenceError(
            "IDECyL reviewed record inventory is incomplete"
        )
    return result


def _reviewed_sources(
    raw_sources: Any,
    records: dict[str, tuple[_ParsedRecord, dict[str, Any]]],
    capture: dict[str, Any],
) -> list[ReviewedIDECyLExactSource]:
    if not isinstance(raw_sources, list) or len(raw_sources) != 31:
        raise IDECyLExactEvidenceError(
            "IDECyL reviewed source inventory is invalid"
        )
    result: list[ReviewedIDECyLExactSource] = []
    layer_ids: set[int] = set()
    profiles: set[str] = set()
    for value in raw_sources:
        item = _dict(value, "IDECyL reviewed source is invalid")
        _exact_keys(
            item,
            {
                "profile",
                "audit_layer_id",
                "catalog_layer_source_key",
                "catalog_endpoint_url",
                "catalog_remote_name",
                "selected_protocol",
                "selected_endpoint_url",
                "selected_remote_name",
                "selected_target_kind",
                "selected_sync_strategy",
                "metadata_fid",
                "metadata_url",
                "audit_proposed_acquisition",
                "acquisition_safety",
            },
            "IDECyL reviewed source shape is invalid",
        )
        profile = _text(item["profile"], maximum=255)
        source_key = _text(
            item["catalog_layer_source_key"],
            maximum=128,
        )
        catalog_endpoint = _https_service_url(
            _text(item["catalog_endpoint_url"], maximum=8_192)
        )
        catalog_remote = _text(
            item["catalog_remote_name"],
            maximum=500,
        )
        selected_endpoint = _https_service_url(
            _text(item["selected_endpoint_url"], maximum=8_192)
        )
        selected_remote = _text(
            item["selected_remote_name"],
            maximum=500,
        )
        layer_id = item["audit_layer_id"]
        if (
            _PROFILE_RE.fullmatch(profile) is None
            or _LAYER_SOURCE_KEY_RE.fullmatch(source_key) is None
            or _REMOTE_NAME_RE.fullmatch(catalog_remote) is None
            or _REMOTE_NAME_RE.fullmatch(selected_remote) is None
            or not isinstance(layer_id, int)
            or isinstance(layer_id, bool)
            or layer_id < 1
            or layer_id in layer_ids
            or profile in profiles
            or not is_idecyl_geoserver_catalog_endpoint(
                catalog_endpoint
            )
            or item["selected_protocol"] != "wfs"
            or item["selected_target_kind"] != "vector"
            or item["selected_sync_strategy"] != "paged_snapshot"
            or item["acquisition_safety"]
            != "bounded_exact_wfs_with_live_capabilities_probe"
            or catalog_remote != selected_remote
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed source identity is invalid"
            )
        expected_wfs = _derived_wfs_endpoint(catalog_endpoint)
        if selected_endpoint != expected_wfs:
            raise IDECyLExactEvidenceError(
                "IDECyL selected WFS is not the catalog workspace WFS"
            )
        fid = _text(item["metadata_fid"], maximum=200)
        record_pair = records.get(fid.casefold())
        if record_pair is None or record_pair[0].fid != fid:
            raise IDECyLExactEvidenceError(
                "IDECyL source references an unavailable metadata FID"
            )
        record, reviewed_record = record_pair
        expected_metadata_url = (
            "https://idecyl.jcyl.es/geonetwork/srv/api/records/"
            f"{fid}/formatters/xml"
        )
        if item["metadata_url"] != expected_metadata_url:
            raise IDECyLExactEvidenceError(
                "IDECyL source metadata URL does not match its FID"
            )
        proposed = _proposed_acquisition(
            item["audit_proposed_acquisition"],
            catalog_remote,
        )
        proposed["metadata_link_present"] = (
            _validate_record_source_binding(
                record,
                catalog_endpoint=catalog_endpoint,
                catalog_remote=catalog_remote,
                selected_wfs=selected_endpoint,
                proposed=proposed,
            )
        )
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "profile": profile,
            "source_kind": "exact-official-idecyl-dataset",
            "catalog_layer_source_key": source_key,
            "catalog_endpoint_url": catalog_endpoint,
            "catalog_remote_name": catalog_remote,
            "selected_protocol": "wfs",
            "selected_endpoint_url": selected_endpoint,
            "selected_remote_name": selected_remote,
            "target_kind": "vector",
            "sync_strategy": "paged_snapshot",
            "acquisition_safety": item["acquisition_safety"],
            "live_probe_required": True,
            "content_parity_required_before_promotion": True,
            "official_metadata": {
                "fid": fid,
                "url": expected_metadata_url,
                "title": record.title,
                "captured_at": capture["captured_at"],
                "official_catalog_url": capture[
                    "official_catalog_url"
                ],
                "source_snapshot_sha256": capture[
                    "source_snapshot_sha256"
                ],
                "local_bundle_resource": capture["bundle_resource"],
                "local_bundle_sha256": capture["bundle_sha256"],
                "record_raw_sha256": record.raw_sha256,
                "terms_urls": list(record.terms_urls),
                "use_constraint_codes": list(
                    record.use_constraint_codes
                ),
                "access_constraint_codes": list(
                    record.access_constraint_codes
                ),
                "attribution_statements": list(
                    record.attribution_statements
                ),
                "attribution_labels": list(
                    reviewed_record["attribution_labels"]
                ),
            },
            "audit_proposed_distribution": deepcopy(proposed),
            "authorization_effect": (
                "none_without_persisted_human_mirror_review"
            ),
        }
        result.append(
            ReviewedIDECyLExactSource(
                profile=profile,
                catalog_layer_source_key=source_key,
                catalog_endpoint_url=catalog_endpoint,
                catalog_remote_name=catalog_remote,
                protocol="wfs",
                target_kind="vector",
                endpoint_url=selected_endpoint,
                remote_name=selected_remote,
                sync_strategy="paged_snapshot",
                evidence=evidence,
            )
        )
        layer_ids.add(layer_id)
        profiles.add(profile)
    return result


def _proposed_acquisition(
    value: Any,
    expected_remote: str,
) -> dict[str, Any]:
    item = _dict(value, "IDECyL proposed acquisition is invalid")
    _exact_keys(
        item,
        {
            "kind",
            "url",
            "format_or_selection",
            "remote_layer_key",
        },
        "IDECyL proposed acquisition shape is invalid",
    )
    kind = item["kind"]
    if kind not in {
        "download_archive",
        "directory_download",
        "wfs_exact",
    }:
        raise IDECyLExactEvidenceError(
            "IDECyL proposed acquisition kind is invalid"
        )
    url = _text(item["url"], maximum=8_192)
    _reference_url(url)
    description = _text(
        item["format_or_selection"],
        maximum=1_000,
    )
    if (
        item["remote_layer_key"] != expected_remote
        or not description
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL proposed acquisition identity is invalid"
        )
    return {
        "kind": kind,
        "url": url,
        "format_or_selection": description,
        "remote_layer_key": expected_remote,
        "activated": False,
        "selection_reason": (
            "evidence_only_current_source_uses_bounded_wfs"
        ),
    }


def _validate_record_source_binding(
    record: _ParsedRecord,
    *,
    catalog_endpoint: str,
    catalog_remote: str,
    selected_wfs: str,
    proposed: dict[str, Any],
) -> bool:
    wms_resources = [
        resource
        for resource in record.online_resources
        if resource.protocol.casefold() == "ogc:wms"
    ]
    if not any(
        _service_base(resource.url) == catalog_endpoint
        for resource in wms_resources
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL metadata does not identify the catalog WMS"
        )
    if not any(
        _resource_references_remote(resource, catalog_remote)
        for resource in record.online_resources
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL metadata does not identify the catalog layer"
        )
    if selected_wfs != _derived_wfs_endpoint(catalog_endpoint):
        raise IDECyLExactEvidenceError(
            "IDECyL metadata source WFS binding is invalid"
        )
    proposed_url = proposed["url"]
    if proposed["kind"] in {
        "download_archive",
        "directory_download",
    }:
        if proposed_url not in {
            resource.url for resource in record.online_resources
        }:
            raise IDECyLExactEvidenceError(
                "IDECyL official distribution URL is absent from metadata "
                f"for {record.fid}"
            )
        return True
    return any(
        _service_base(resource.url) == proposed_url
        and _wfs_resource_matches_remote(
            resource.url,
            catalog_remote,
        )
        for resource in record.online_resources
    )


def _resource_references_remote(
    resource: _OnlineResource,
    remote_name: str,
) -> bool:
    if resource.name == remote_name:
        return True
    decoded = unquote(resource.url)
    return (
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(remote_name)}"
            r"(?![A-Za-z0-9_])",
            decoded,
            re.ASCII,
        )
        is not None
    )


def _wfs_resource_matches_remote(url: str, remote_name: str) -> bool:
    try:
        query = parse_qs(
            urlsplit(url).query,
            keep_blank_values=True,
        )
    except (TypeError, ValueError):
        return False
    lowered = {key.casefold(): values for key, values in query.items()}
    service = lowered.get("service", [])
    request = lowered.get("request", [])
    type_names = (
        lowered.get("typename", [])
        + lowered.get("typenames", [])
    )
    return (
        any(value.casefold() == "wfs" for value in service)
        and any(value.casefold() == "getfeature" for value in request)
        and any(
            value.rsplit(":", 1)[-1] == remote_name
            for value in type_names
        )
    )


def _derived_wfs_endpoint(catalog_endpoint: str) -> str:
    parts = urlsplit(catalog_endpoint)
    matched = re.fullmatch(
        r"(?P<prefix>/geoserver/[A-Za-z0-9_.-]+)/(?:wms|ows)/?",
        parts.path,
        re.ASCII,
    )
    if matched is None:
        raise IDECyLExactEvidenceError(
            "IDECyL catalog endpoint workspace is invalid"
        )
    return urlunsplit(
        (
            "https",
            "idecyl.jcyl.es",
            matched.group("prefix") + "/wfs",
            "",
            "",
        )
    )


def _service_base(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.casefold() != "https"
            or parts.hostname is None
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.fragment
        ):
            return None
        return urlunsplit(
            (
                "https",
                parts.hostname.encode("idna").decode("ascii").lower(),
                parts.path.rstrip("/") or "/",
                "",
                "",
            )
        )
    except (TypeError, ValueError, UnicodeError):
        return None


def _https_service_url(value: str) -> str:
    normalized = _service_base(value)
    if normalized is None or urlsplit(value).query:
        raise IDECyLExactEvidenceError(
            "IDECyL service URL is not credential-free HTTPS"
        )
    return normalized


def _reference_url(value: str) -> None:
    try:
        parts = urlsplit(value)
    except (TypeError, ValueError) as error:
        raise IDECyLExactEvidenceError(
            "IDECyL reference URL is invalid"
        ) from error
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL reference URL is invalid"
        )


def _legal_reference_url(value: str) -> None:
    _reference_url(value)
    if len(value) > 8_192:
        raise IDECyLExactEvidenceError(
            "IDECyL legal reference URL is too long"
        )


def _has_reuse_grant(statements: tuple[str, ...]) -> bool:
    normalized = " ".join(statements).casefold()
    return (
        "uso libre y gratuito" in normalized
        or "cc by" in normalized
        or "cc-by" in normalized
    )


def _validate_attribution_labels(
    labels: list[str],
    statements: tuple[str, ...],
) -> None:
    normalized = " ".join(statements).casefold()
    expected: list[str] = []
    if "junta de castilla y león" in normalized:
        expected.append("Junta de Castilla y León")
    if (
        "instituto geográfico nacional" in normalized
        or "ign.es" in normalized
        or "bdlje" in normalized
    ):
        expected.append("Instituto Geográfico Nacional / BDLJE")
    if labels != expected:
        raise IDECyLExactEvidenceError(
            "IDECyL attribution labels do not match official metadata"
        )


def _fresh_source(
    source: ReviewedIDECyLExactSource,
) -> ReviewedIDECyLExactSource:
    return ReviewedIDECyLExactSource(
        profile=source.profile,
        catalog_layer_source_key=source.catalog_layer_source_key,
        catalog_endpoint_url=source.catalog_endpoint_url,
        catalog_remote_name=source.catalog_remote_name,
        protocol=source.protocol,
        target_kind=source.target_kind,
        endpoint_url=source.endpoint_url,
        remote_name=source.remote_name,
        sync_strategy=source.sync_strategy,
        evidence=deepcopy(source.evidence),
    )


def _resource_body(resource_path: str, maximum: int) -> bytes:
    resource = files("app.reference_layers")
    for component in resource_path.split("/"):
        resource = resource.joinpath(component)
    try:
        body = resource.read_bytes()
    except (OSError, FileNotFoundError) as error:
        raise IDECyLExactEvidenceError(
            "committed IDECyL evidence is unavailable"
        ) from error
    _bounded_bytes(
        body,
        maximum=maximum,
        message="committed IDECyL evidence size is invalid",
    )
    return body


def _strict_json_object(body: bytes) -> dict[str, Any]:
    if body.startswith(b"\xef\xbb\xbf"):
        raise IDECyLExactEvidenceError(
            "IDECyL evidence manifest has a UTF-8 BOM"
        )
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence manifest is not strict JSON"
        ) from error
    return _dict(value, "IDECyL evidence manifest root is invalid")


def _without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise IDECyLExactEvidenceError(
                "IDECyL evidence manifest contains duplicate keys"
            )
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise IDECyLExactEvidenceError(
        f"IDECyL evidence manifest contains {value}"
    )


def _bounded_bytes(value: Any, *, maximum: int, message: str) -> None:
    if (
        not isinstance(value, bytes)
        or not 1 <= len(value) <= maximum
    ):
        raise IDECyLExactEvidenceError(message)


def _dict(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IDECyLExactEvidenceError(message)
    return value


def _exact_keys(
    value: dict[str, Any],
    expected: set[str],
    message: str,
) -> None:
    if set(value) != expected:
        raise IDECyLExactEvidenceError(message)


def _text(value: Any, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL evidence text is invalid"
        )
    return value


def _text_list(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    item_maximum: int,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence text list is invalid"
        )
    result = [_text(item, maximum=item_maximum) for item in value]
    if len(set(result)) != len(result):
        raise IDECyLExactEvidenceError(
            "IDECyL evidence text list contains duplicates"
        )
    return result


def _element_text(
    root: ElementTree.Element,
    path: str,
) -> str:
    value = _optional_element_text(root, path)
    if not value:
        raise IDECyLExactEvidenceError(
            "IDECyL metadata required text is missing"
        )
    return value


def _optional_element_text(
    root: ElementTree.Element,
    path: str,
) -> str:
    value = root.findtext(path, default="", namespaces=_NS)
    return value.strip() if isinstance(value, str) else ""


def _unique_element_texts(
    root: ElementTree.Element,
    path: str,
) -> tuple[str, ...]:
    result: list[str] = []
    for element in root.findall(path, _NS):
        value = (element.text or "").strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


def _restriction_codes(
    root: ElementTree.Element,
    path: str,
) -> tuple[str, ...]:
    result: list[str] = []
    for element in root.findall(path, _NS):
        value = (element.get("codeListValue") or "").strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)
