"""Bounded offline probes for candidate reference-layer sources.

Network acquisition is intentionally separate.  A downloader supplies bytes
from the candidate's already validated endpoint and these parsers prove that
the exact collection is advertised before any source is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Literal
from xml.etree import ElementTree

from app.reference_layers.source_discovery import SourceCandidate

MAX_PROBE_BYTES = 8 * 1024 * 1024
MAX_XML_ELEMENTS = 100_000
MAX_XML_TEXT_BYTES = 4 * 1024 * 1024
MAX_JSON_NODES = 100_000
MAX_JSON_DEPTH = 48
MAX_COLLECTIONS = 20_000
MAX_XML_DEPTH = 64
_XML_ENCODING_RE = re.compile(
    br"<\?xml[^>]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

FingerprintQuality = Literal["strong", "weak", "none"]


class SourceProbeError(ValueError):
    """The response is malformed, unsafe or too large to use as evidence."""


@dataclass(frozen=True)
class SourceProbe:
    available: bool
    protocol: str
    requested_name: str
    canonical_name: str | None
    service_version: str | None
    fingerprint_sha256: str | None
    fingerprint_quality: FingerprintQuality
    metadata: dict[str, Any]
    reason: str | None = None


def probe_candidate_document(
    candidate: SourceCandidate,
    document: bytes,
) -> SourceProbe:
    """Probe one downloaded metadata document for an acquisition candidate."""

    if candidate.protocol == "wfs":
        return _probe_ogc_xml(candidate, document, kind="wfs")
    if candidate.protocol == "wcs":
        return _probe_ogc_xml(candidate, document, kind="wcs")
    if candidate.protocol == "wmts":
        return _probe_ogc_xml(candidate, document, kind="wmts")
    if candidate.protocol == "wms_tiles":
        return _probe_ogc_xml(candidate, document, kind="wms")
    if candidate.protocol == "arcgis_rest":
        return _probe_arcgis(candidate, document)
    if candidate.protocol == "ogc_api_features":
        return _probe_ogc_api(candidate, document)
    if candidate.protocol in {"xyz", "download", "atom", "local"}:
        return SourceProbe(
            available=True,
            protocol=candidate.protocol,
            requested_name=candidate.remote_name,
            canonical_name=candidate.remote_name,
            service_version=None,
            fingerprint_sha256=None,
            fingerprint_quality="none",
            metadata={},
            reason=None,
        )
    raise SourceProbeError(f"unsupported probe protocol: {candidate.protocol}")


def _probe_ogc_xml(
    candidate: SourceCandidate,
    document: bytes,
    *,
    kind: Literal["wfs", "wcs", "wmts", "wms"],
) -> SourceProbe:
    root = _safe_xml(document)
    root_name = _local_name(root.tag)
    expected_roots = {
        "wfs": {"WFS_Capabilities"},
        "wcs": {"Capabilities", "WCS_Capabilities"},
        "wmts": {"Capabilities"},
        "wms": {"WMS_Capabilities", "WMT_MS_Capabilities"},
    }[kind]
    if root_name not in expected_roots:
        raise SourceProbeError(f"response is not {kind.upper()} capabilities")
    version = _clean_text(root.get("version"), 32)

    if kind == "wfs":
        entries = _named_entries(root, containers={"FeatureType"})
    elif kind == "wcs":
        entries = _named_entries(
            root,
            containers={"CoverageSummary", "CoverageOfferingBrief"},
            identifiers={"CoverageId", "Identifier", "name"},
        )
    elif kind == "wmts":
        entries = _wmts_layers(root)
    else:
        entries = _named_entries(root, containers={"Layer"})

    selected = _select_entry(entries, candidate.remote_name)
    if selected is None:
        return _unavailable(candidate, version, "collection is not advertised")
    normalized = {
        "protocol": kind,
        "service_version": version,
        "collection": selected,
    }
    return SourceProbe(
        available=True,
        protocol=candidate.protocol,
        requested_name=candidate.remote_name,
        canonical_name=selected["name"],
        service_version=version,
        fingerprint_sha256=_canonical_sha256(normalized),
        fingerprint_quality="weak",
        metadata=selected,
    )


def _probe_arcgis(
    candidate: SourceCandidate,
    document: bytes,
) -> SourceProbe:
    root = _safe_json(document)
    if not isinstance(root, dict) or "error" in root:
        raise SourceProbeError("response is not ArcGIS service metadata")
    raw_layers = root.get("layers")
    if not isinstance(raw_layers, list) or len(raw_layers) > MAX_COLLECTIONS:
        raise SourceProbeError("ArcGIS metadata has no bounded layer list")
    entries: list[dict[str, Any]] = []
    for item in raw_layers:
        if not isinstance(item, dict):
            raise SourceProbeError("ArcGIS layer metadata is malformed")
        identifier = item.get("id")
        name = _clean_text(item.get("name"), 500)
        if not isinstance(identifier, int) or identifier < 0 or name is None:
            raise SourceProbeError("ArcGIS layer identity is malformed")
        entry: dict[str, Any] = {"id": identifier, "name": name}
        if isinstance(item.get("type"), str):
            entry["type"] = _clean_text(item["type"], 100)
        entries.append(entry)
    selected = next(
        (
            item
            for item in entries
            if str(item["id"]) == candidate.remote_name
            or item["name"].casefold() == candidate.remote_name.casefold()
        ),
        None,
    )
    version = _number_text(root.get("currentVersion"))
    if selected is None:
        return _unavailable(candidate, version, "ArcGIS layer is not advertised")
    last_edit = _arcgis_last_edit(root, selected)
    metadata = {
        **selected,
        "capabilities": _clean_text(root.get("capabilities"), 500),
        "last_edit_epoch_ms": last_edit,
    }
    normalized = {
        "protocol": "arcgis_rest",
        "service_version": version,
        "collection": metadata,
    }
    return SourceProbe(
        available=True,
        protocol=candidate.protocol,
        requested_name=candidate.remote_name,
        canonical_name=str(selected["id"]),
        service_version=version,
        fingerprint_sha256=_canonical_sha256(normalized),
        fingerprint_quality="strong" if last_edit is not None else "weak",
        metadata=metadata,
    )


def _probe_ogc_api(
    candidate: SourceCandidate,
    document: bytes,
) -> SourceProbe:
    root = _safe_json(document)
    if not isinstance(root, dict) or not isinstance(root.get("collections"), list):
        raise SourceProbeError("response is not an OGC API collections document")
    if len(root["collections"]) > MAX_COLLECTIONS:
        raise SourceProbeError("OGC API advertises too many collections")
    entries: list[dict[str, Any]] = []
    for collection in root["collections"]:
        if not isinstance(collection, dict):
            raise SourceProbeError("OGC API collection is malformed")
        identifier = _clean_text(collection.get("id"), 500)
        if identifier is None:
            raise SourceProbeError("OGC API collection has no valid id")
        entry = {"name": identifier}
        title = _clean_text(collection.get("title"), 500)
        if title is not None:
            entry["title"] = title
        if isinstance(collection.get("extent"), dict):
            entry["extent"] = collection["extent"]
        entries.append(entry)
    selected = _select_entry(entries, candidate.remote_name)
    if selected is None:
        return _unavailable(candidate, None, "collection is not advertised")
    normalized = {"protocol": "ogc_api_features", "collection": selected}
    return SourceProbe(
        available=True,
        protocol=candidate.protocol,
        requested_name=candidate.remote_name,
        canonical_name=selected["name"],
        service_version=None,
        fingerprint_sha256=_canonical_sha256(normalized),
        fingerprint_quality="weak",
        metadata=selected,
    )


def _safe_xml(document: bytes) -> ElementTree.Element:
    _bounded_bytes(document)
    lowered = document.lower()
    encoding = _XML_ENCODING_RE.search(document[:512])
    if (
        b"<!doctype" in lowered
        or b"<!entity" in lowered
        or b"\x00" in document
        or document.startswith((b"\xff\xfe", b"\xfe\xff"))
        or (
            encoding is not None
            and encoding.group(1).lower() not in {b"utf-8", b"utf8"}
        )
    ):
        raise SourceProbeError("DTD, entities and NUL bytes are forbidden")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise SourceProbeError("capabilities XML is malformed") from exc
    elements = list(root.iter())
    if len(elements) > MAX_XML_ELEMENTS:
        raise SourceProbeError("capabilities contains too many elements")
    stack = [(root, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_XML_DEPTH:
            raise SourceProbeError("capabilities XML is too deep")
        stack.extend((child, depth + 1) for child in item)
    text_bytes = sum(
        len(value.encode("utf-8"))
        for item in elements
        for value in (item.text or "", item.tail or "")
    )
    if text_bytes > MAX_XML_TEXT_BYTES:
        raise SourceProbeError("capabilities text is too large")
    return root


def _safe_json(document: bytes) -> Any:
    _bounded_bytes(document)

    def unique_object(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SourceProbeError("metadata is not strict UTF-8 JSON") from exc
    stack = [(value, 0)]
    seen = 0
    while stack:
        item, depth = stack.pop()
        seen += 1
        if seen > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise SourceProbeError("metadata JSON is too complex")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise SourceProbeError("metadata contains a non-finite number")
    return value


def _bounded_bytes(document: bytes) -> None:
    if not isinstance(document, bytes) or not 0 < len(document) <= MAX_PROBE_BYTES:
        raise SourceProbeError("probe size is outside the allowed range")


def _named_entries(
    root: ElementTree.Element,
    *,
    containers: set[str],
    identifiers: set[str] = {"Name"},
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    names_seen: set[str] = set()
    for element in root.iter():
        if _local_name(element.tag) not in containers:
            continue
        name = next(
            (
                _clean_text(child.text, 500)
                for child in element
                if _local_name(child.tag) in identifiers
                and _clean_text(child.text, 500) is not None
            ),
            None,
        )
        if name is None:
            continue
        if name in names_seen:
            raise SourceProbeError("capabilities contains duplicate collections")
        names_seen.add(name)
        entry: dict[str, Any] = {"name": name}
        crs = sorted(
            {
                value
                for child in element
                if _local_name(child.tag)
                in {"DefaultCRS", "DefaultSRS", "SRS", "SupportedCRS"}
                for value in [_clean_text(child.text, 500)]
                if value is not None
            }
        )
        if crs:
            entry["crs"] = crs
        result.append(entry)
        if len(result) > MAX_COLLECTIONS:
            raise SourceProbeError("capabilities advertises too many collections")
    return result


def _wmts_layers(root: ElementTree.Element) -> list[dict[str, Any]]:
    entries = _named_entries(
        root,
        containers={"Layer"},
        identifiers={"Identifier"},
    )
    return entries


def _select_entry(
    entries: list[dict[str, Any]],
    requested: str,
) -> dict[str, Any] | None:
    exact = [item for item in entries if item["name"] == requested]
    if len(exact) == 1:
        return exact[0]
    local_name = requested.rsplit(":", 1)[-1].casefold()
    local = [
        item
        for item in entries
        if item["name"].rsplit(":", 1)[-1].casefold() == local_name
    ]
    return local[0] if len(local) == 1 else None


def _unavailable(
    candidate: SourceCandidate,
    version: str | None,
    reason: str,
) -> SourceProbe:
    return SourceProbe(
        available=False,
        protocol=candidate.protocol,
        requested_name=candidate.remote_name,
        canonical_name=None,
        service_version=version,
        fingerprint_sha256=None,
        fingerprint_quality="none",
        metadata={},
        reason=reason,
    )


def _arcgis_last_edit(
    root: dict[str, Any],
    selected: dict[str, Any],
) -> int | None:
    candidates = [root.get("editingInfo")]
    layer_details = root.get("layerDetails")
    if isinstance(layer_details, dict):
        candidates.append(layer_details.get(str(selected["id"])))
    for item in candidates:
        if isinstance(item, dict):
            value = item.get("lastEditDate")
            if isinstance(value, int) and value >= 0:
                return value
    return None


def _number_text(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            return str(value)
    return None


def _clean_text(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > max_chars
        or any(ord(character) < 32 for character in normalized)
    ):
        return None
    return normalized


def _canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
