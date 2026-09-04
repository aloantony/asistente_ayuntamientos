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
MAX_WMTS_MATRIX_SETS = 2_000
MAX_WMTS_TILE_MATRICES = 64
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
        matrix_sets = _wmts_matrix_sets(root)
        definitions = {item["identifier"]: item for item in matrix_sets}
        for entry in entries:
            linked = entry.get("tile_matrix_sets", [])
            missing = [identifier for identifier in linked if identifier not in definitions]
            if missing:
                raise SourceProbeError(
                    "WMTS layer links a tile-matrix set without a definition"
                )
            entry["tile_matrix_set_definitions"] = [
                definitions[identifier]
                for identifier in linked
            ]
    else:
        entries = _wms_layers(root)

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
    entries: list[dict[str, Any]] = []
    names_seen: set[str] = set()
    for element in root.iter():
        if _local_name(element.tag) != "Layer":
            continue
        name = _first_child_text(element, {"Identifier"}, 500)
        if name is None:
            continue
        if name in names_seen:
            raise SourceProbeError("capabilities contains duplicate collections")
        names_seen.add(name)

        entry: dict[str, Any] = {"name": name}
        formats = _direct_child_texts(element, {"Format"}, 200)
        if formats:
            entry["formats"] = formats

        styles: list[dict[str, Any]] = []
        matrix_sets: list[str] = []
        matrix_limits: dict[str, list[dict[str, Any]]] = {}
        resources: list[dict[str, Any]] = []
        for child in element:
            child_name = _local_name(child.tag)
            if child_name == "Style":
                identifier = _first_child_text(child, {"Identifier"}, 500)
                if identifier is None:
                    raise SourceProbeError("WMTS style has no valid identifier")
                style: dict[str, Any] = {"name": identifier}
                if str(child.get("isDefault", "")).casefold() in {"true", "1"}:
                    style["default"] = True
                styles.append(style)
            elif child_name == "TileMatrixSetLink":
                identifier = _first_child_text(child, {"TileMatrixSet"}, 500)
                if identifier is None:
                    raise SourceProbeError("WMTS matrix-set link is malformed")
                try:
                    limits = _wmts_matrix_limits(child)
                except SourceProbeError:
                    # Some public WMTS documents contain an invalid limit in one
                    # optional CRS while advertising a valid WebMercator link. A
                    # malformed link is unusable and therefore omitted entirely;
                    # it must not make unrelated, valid links unavailable.
                    continue
                matrix_sets.append(identifier)
                if limits:
                    matrix_limits[identifier] = limits
            elif child_name == "ResourceURL":
                template = _clean_text(child.get("template"), 8192)
                resource_type = _clean_text(child.get("resourceType"), 100)
                resource_format = _clean_text(child.get("format"), 200)
                if template is None or resource_type is None:
                    raise SourceProbeError("WMTS resource URL is malformed")
                resource: dict[str, Any] = {
                    "template": template,
                    "resource_type": resource_type,
                }
                if resource_format is not None:
                    resource["format"] = resource_format
                resources.append(resource)
        if styles:
            style_names = [item["name"] for item in styles]
            if len(style_names) != len(set(style_names)):
                raise SourceProbeError("WMTS layer repeats a style identifier")
            entry["styles"] = styles
        if matrix_sets:
            if len(matrix_sets) != len(set(matrix_sets)):
                raise SourceProbeError("WMTS layer repeats a matrix-set link")
            entry["tile_matrix_sets"] = matrix_sets
            if matrix_limits:
                entry["tile_matrix_set_limits"] = matrix_limits
        if resources:
            entry["resource_urls"] = resources
        entries.append(entry)
        if len(entries) > MAX_COLLECTIONS:
            raise SourceProbeError("capabilities advertises too many collections")
    return entries


def _wms_layers(root: ElementTree.Element) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    get_map_formats: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) == "GetMap":
            get_map_formats = _direct_child_texts(element, {"Format"}, 200)
            break
    if not get_map_formats:
        raise SourceProbeError("WMS capabilities do not advertise a GetMap format")

    def visit(
        element: ElementTree.Element,
        inherited_crs: tuple[str, ...],
        inherited_styles: tuple[str, ...],
    ) -> None:
        direct_crs = [
            token
            for value in _direct_child_texts(element, {"CRS", "SRS"}, 500)
            for token in value.split()
        ]
        crs = tuple(dict.fromkeys((*inherited_crs, *direct_crs)))
        direct_styles = [
            style_name
            for child in element
            if _local_name(child.tag) == "Style"
            for style_name in [_first_child_text(child, {"Name"}, 500)]
            if style_name is not None
        ]
        styles = tuple(dict.fromkeys((*inherited_styles, *direct_styles)))
        name = _first_child_text(element, {"Name"}, 500)
        if name is not None:
            if name in seen:
                raise SourceProbeError("capabilities contains duplicate collections")
            seen.add(name)
            entry: dict[str, Any] = {"name": name}
            if crs:
                entry["crs"] = sorted(crs)
            entry["formats"] = get_map_formats
            if styles:
                entry["styles"] = list(styles)
            result.append(entry)
            if len(result) > MAX_COLLECTIONS:
                raise SourceProbeError("capabilities advertises too many collections")
        for child in element:
            if _local_name(child.tag) == "Layer":
                visit(child, crs, styles)

    for element in root.iter():
        if _local_name(element.tag) == "Capability":
            for child in element:
                if _local_name(child.tag) == "Layer":
                    visit(child, (), ())
            break
    return result


def _wmts_matrix_sets(root: ElementTree.Element) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for element in root.iter():
        if _local_name(element.tag) != "TileMatrixSet":
            continue
        identifier = _first_child_text(element, {"Identifier"}, 500)
        supported_crs = _first_child_text(element, {"SupportedCRS"}, 1000)
        matrices = [
            child for child in element if _local_name(child.tag) == "TileMatrix"
        ]
        if identifier is None and supported_crs is None and not matrices:
            continue  # This is the text-only element inside a layer link.
        if identifier is None or supported_crs is None or not matrices:
            raise SourceProbeError("WMTS tile-matrix set is incomplete")
        if identifier in identifiers:
            raise SourceProbeError("WMTS repeats a tile-matrix set identifier")
        identifiers.add(identifier)
        if len(matrices) > MAX_WMTS_TILE_MATRICES:
            raise SourceProbeError("WMTS tile-matrix set contains too many matrices")
        parsed_matrices = [_wmts_tile_matrix(item) for item in matrices]
        matrix_identifiers = [item["identifier"] for item in parsed_matrices]
        if len(matrix_identifiers) != len(set(matrix_identifiers)):
            raise SourceProbeError("WMTS tile-matrix set repeats a matrix identifier")
        definition: dict[str, Any] = {
            "identifier": identifier,
            "supported_crs": supported_crs,
            "tile_matrices": parsed_matrices,
        }
        well_known = _first_child_text(element, {"WellKnownScaleSet"}, 1000)
        if well_known is not None:
            definition["well_known_scale_set"] = well_known
        result.append(definition)
        if len(result) > MAX_WMTS_MATRIX_SETS:
            raise SourceProbeError("WMTS advertises too many tile-matrix sets")
    return result


def _wmts_tile_matrix(element: ElementTree.Element) -> dict[str, Any]:
    identifier = _first_child_text(element, {"Identifier"}, 500)
    scale = _positive_float_text(
        _first_child_text(element, {"ScaleDenominator"}, 100),
        "scale denominator",
    )
    top_left_text = _first_child_text(element, {"TopLeftCorner"}, 200)
    if top_left_text is None:
        raise SourceProbeError("WMTS tile matrix has no top-left corner")
    try:
        top_left = [float(value) for value in top_left_text.split()]
    except ValueError as exc:
        raise SourceProbeError("WMTS top-left corner is malformed") from exc
    if len(top_left) != 2 or any(not math.isfinite(value) for value in top_left):
        raise SourceProbeError("WMTS top-left corner is malformed")
    if identifier is None:
        raise SourceProbeError("WMTS tile matrix has no identifier")
    return {
        "identifier": identifier,
        "scale_denominator": scale,
        "top_left_corner": top_left,
        "tile_width": _positive_int_text(
            _first_child_text(element, {"TileWidth"}, 20),
            "tile width",
            maximum=4096,
        ),
        "tile_height": _positive_int_text(
            _first_child_text(element, {"TileHeight"}, 20),
            "tile height",
            maximum=4096,
        ),
        "matrix_width": _positive_int_text(
            _first_child_text(element, {"MatrixWidth"}, 20),
            "matrix width",
            maximum=2**31,
        ),
        "matrix_height": _positive_int_text(
            _first_child_text(element, {"MatrixHeight"}, 20),
            "matrix height",
            maximum=2**31,
        ),
    }


def _wmts_matrix_limits(link: ElementTree.Element) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in link.iter():
        if _local_name(element.tag) != "TileMatrixLimits":
            continue
        identifier = _first_child_text(element, {"TileMatrix"}, 500)
        if identifier is None or identifier in seen:
            raise SourceProbeError("WMTS tile-matrix limits are malformed")
        seen.add(identifier)
        minimum_row = _non_negative_int_text(
            _first_child_text(element, {"MinTileRow"}, 20),
            "minimum tile row",
        )
        maximum_row = _non_negative_int_text(
            _first_child_text(element, {"MaxTileRow"}, 20),
            "maximum tile row",
        )
        minimum_col = _non_negative_int_text(
            _first_child_text(element, {"MinTileCol"}, 20),
            "minimum tile column",
        )
        maximum_col = _non_negative_int_text(
            _first_child_text(element, {"MaxTileCol"}, 20),
            "maximum tile column",
        )
        if minimum_row > maximum_row or minimum_col > maximum_col:
            raise SourceProbeError("WMTS tile-matrix limits are inverted")
        result.append(
            {
                "tile_matrix": identifier,
                "min_tile_row": minimum_row,
                "max_tile_row": maximum_row,
                "min_tile_col": minimum_col,
                "max_tile_col": maximum_col,
            }
        )
        if len(result) > MAX_WMTS_TILE_MATRICES:
            raise SourceProbeError("WMTS contains too many tile-matrix limits")
    return result


def _positive_float_text(value: str | None, name: str) -> float:
    try:
        result = float(value) if value is not None else math.nan
    except ValueError as exc:
        raise SourceProbeError(f"WMTS {name} is malformed") from exc
    if not math.isfinite(result) or result <= 0:
        raise SourceProbeError(f"WMTS {name} is malformed")
    return result


def _positive_int_text(
    value: str | None,
    name: str,
    *,
    maximum: int,
) -> int:
    result = _non_negative_int_text(value, name)
    if result <= 0 or result > maximum:
        raise SourceProbeError(f"WMTS {name} is outside its allowed range")
    return result


def _non_negative_int_text(value: str | None, name: str) -> int:
    if value is None or not value.isdecimal():
        raise SourceProbeError(f"WMTS {name} is malformed")
    result = int(value)
    if result > 2**31:
        raise SourceProbeError(f"WMTS {name} is outside its allowed range")
    return result


def _first_child_text(
    element: ElementTree.Element,
    names: set[str],
    max_chars: int,
) -> str | None:
    return next(
        (
            value
            for child in element
            if _local_name(child.tag) in names
            for value in [_clean_text(child.text, max_chars)]
            if value is not None
        ),
        None,
    )


def _direct_child_texts(
    element: ElementTree.Element,
    names: set[str],
    max_chars: int,
) -> list[str]:
    """Los valores admitidos de un elemento, sin repetir y en su orden.

    Aquí sólo se leen listas de «lo que este servicio admite»: formatos de
    imagen y sistemas de referencia. Repetir uno no dice nada distinto de
    declararlo una vez, y el propio código ya las trataba como conjuntos unas
    líneas más abajo (`dict.fromkeys`). Rechazar el documento por un duplicado
    era incoherente con eso y tenía un coste real: el PNOA del IGN declara
    `EPSG:32631` dos veces entre sus veintiún sistemas, y por esa redundancia
    la ortofoto entera quedaba fuera del espejo.

    La duplicación que sí importa —dos colecciones con el mismo nombre, que
    haría ambiguo a qué capa nos referimos— se sigue rechazando aparte.
    """

    values = [
        value
        for child in element
        if _local_name(child.tag) in names
        for value in [_clean_text(child.text, max_chars)]
        if value is not None
    ]
    return list(dict.fromkeys(values))


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
