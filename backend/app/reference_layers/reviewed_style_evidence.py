"""Hash-bound local evidence for reviewed cartographic style adaptations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import re
from typing import Any


EVIDENCE_SCHEMA = "siur-reviewed-official-mvt-style/v1"
MITECO_WATER_DIRECTORY_URL = (
    "https://www.miteco.gob.es/es/cartografia-y-sig/ide/"
    "directorio_datos_servicios/agua.html"
)
MAX_EVIDENCE_BYTES = 64 * 1024
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$", re.ASCII)


class ReviewedStyleEvidenceError(RuntimeError):
    """A committed style source document is missing or does not match review."""


@dataclass(frozen=True)
class _MitecoStyleSpec:
    filename: str
    official_url: str
    archive_timestamp: str
    archive_payload_sha256: str
    archive_cdx_digest: str
    local_sha256: str
    source_key: str
    source_layer: str
    style_id: str


@dataclass(frozen=True)
class ReviewedMitecoStyleWatchTarget:
    """Exact live URL and committed baseline for one reviewed adaptation."""

    profile: str
    official_url: str
    baseline_raw_sha256: str
    baseline_semantic_sha256: str
    baseline_size_bytes: int


@dataclass(frozen=True)
class ObservedMitecoStyleDocument:
    """Strict, bounded identity of one live official style response."""

    raw_sha256: str
    semantic_sha256: str
    size_bytes: int
    matches_vendored_bytes: bool
    matches_vendored_semantics: bool


_MITECO_STYLE_SPECS = {
    "miteco-flood-q10-ogc-api-features-v1": _MitecoStyleSpec(
        filename="ZI_LaminasQ10.json",
        official_url=(
            "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
            "ZI_LaminasQ10.json"
        ),
        archive_timestamp="20260511115207",
        archive_payload_sha256=(
            "99f7a8c01015ae5cc3983c333e2e76ca"
            "30a20502c693fcd25ff6ca878fd99f5b"
        ),
        archive_cdx_digest="6FBSC7FL7AUKGTWVGIYGXQCXWWIAEC5Q",
        local_sha256=(
            "5966ace7d1012605f2bf68264f8533fc"
            "73a0a91d4bf08b9b3fba6245d1e77c07"
        ),
        source_key="zi",
        source_layer="ZI_LaminasQ10",
        style_id="ZI_LaminasQ10",
    ),
    "miteco-flood-q50-ogc-api-features-v1": _MitecoStyleSpec(
        filename="ZI_LaminasQ50.json",
        official_url=(
            "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
            "ZI_LaminasQ50.json"
        ),
        archive_timestamp="20260511121530",
        archive_payload_sha256=(
            "0e753d758c49eded5da62147e8d1329f"
            "29359d2864379fa87396ac07fd042868"
        ),
        archive_cdx_digest="QZRDMVWC7URISIFD36X5YLX2NRYEVQSF",
        local_sha256=(
            "e7d8abd91b0432ede69d2ac2066842d3"
            "e961907ebed6884f629cf7c5d6dca193"
        ),
        source_key="zi",
        source_layer="ZI_LaminasQ50",
        style_id="ZI_LaminasQ50",
    ),
    "miteco-flood-q100-ogc-api-features-v1": _MitecoStyleSpec(
        filename="ZI_LaminasQ100.json",
        official_url=(
            "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
            "ZI_LaminasQ100.json"
        ),
        archive_timestamp="20260511114538",
        archive_payload_sha256=(
            "5d971fcab264052457976e82d63c8e73"
            "92e04b259f0c9944f3271c541fccb592"
        ),
        archive_cdx_digest="SFQDC262FDYJRIKGDEXCSYLV33TAELW5",
        local_sha256=(
            "fb4946a17b7bae54d90ef22212586c53"
            "3fdded5265e30fc901caf2f56d9d37fd"
        ),
        source_key="zi",
        source_layer="ZI_LaminasQ100",
        style_id="ZI_LaminasQ100",
    ),
    "miteco-flood-q500-ogc-api-features-v1": _MitecoStyleSpec(
        filename="ZI_LaminasQ500.json",
        official_url=(
            "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
            "ZI_LaminasQ500.json"
        ),
        archive_timestamp="20260511120957",
        archive_payload_sha256=(
            "dfce3a142a2b773762e1381a89a13dcb"
            "5523b4c9f35dd5d04d4d2138435bb28f"
        ),
        archive_cdx_digest="RXZY5N2JZTP22MTX42HWD3SD62P5R27L",
        local_sha256=(
            "cd1202d3ab3cb98efd688521134a53b72"
            "b95863a4a2a792bfc32f80aa07d5744"
        ),
        source_key="zi",
        source_layer="ZI_LaminasQ500",
        style_id="ZI_LaminasQ500",
    ),
    "miteco-flood-zfp-ogc-api-features-v1": _MitecoStyleSpec(
        filename="ZI_LaminasZFP.json",
        official_url=(
            "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
            "ZI_LaminasZFP.json"
        ),
        archive_timestamp="20260511114801",
        archive_payload_sha256=(
            "18ec64cf71afd41ab622365b0ca67e98f"
            "a54417903d1a616e3b868f8371b3e55"
        ),
        archive_cdx_digest="U6MDWAXDI7ITEIWTC44CI5T3OPE4PTIS",
        local_sha256=(
            "19dab6437a2d650505333d440cf2cc299"
            "6ba055f257c48d0d31921947fc9af8d"
        ),
        source_key="mvt_source",
        source_layer="ZI_LaminasZFP",
        style_id="zona_flujo_preferente_fill",
    ),
}


def reviewed_miteco_mvt_style_reference(
    profile: str,
) -> dict[str, Any] | None:
    """Return a strict projection of one committed official MVT document."""

    spec = _MITECO_STYLE_SPECS.get(profile)
    if spec is None:
        return None
    body = _reviewed_resource_body(spec)
    document = _strict_json_object(body)
    fill_color, outline_color = _validate_miteco_document(
        document,
        spec,
    )
    capture_url = (
        f"https://web.archive.org/web/{spec.archive_timestamp}id_/"
        f"{spec.official_url}"
    )
    return {
        "schema": EVIDENCE_SCHEMA,
        "source_kind": "archived-official-mvt-json",
        "url": spec.official_url,
        "official_directory_url": MITECO_WATER_DIRECTORY_URL,
        "archive_capture_url": capture_url,
        "archive_timestamp": spec.archive_timestamp,
        "archive_payload_sha256": spec.archive_payload_sha256,
        "archive_cdx_digest": spec.archive_cdx_digest,
        "local_evidence_resource": (
            f"evidence/miteco_mvt/{spec.filename}"
        ),
        "local_evidence_sha256": spec.local_sha256,
        "source_layer": spec.source_layer,
        "style_id": spec.style_id,
        "adaptation_status": "adaptation_required",
        "fill_color": fill_color,
        "outline_color": outline_color,
    }


def reviewed_miteco_style_watch_target(
    profile: str,
) -> ReviewedMitecoStyleWatchTarget | None:
    """Return the only live style URL accepted for a reviewed profile."""

    spec = _MITECO_STYLE_SPECS.get(profile)
    if spec is None:
        return None
    body = _reviewed_resource_body(spec)
    document = _strict_json_object(body)
    _validate_json_limits(document)
    semantic_sha256 = _canonical_json_sha256(document)
    return ReviewedMitecoStyleWatchTarget(
        profile=profile,
        official_url=spec.official_url,
        baseline_raw_sha256=spec.local_sha256,
        baseline_semantic_sha256=semantic_sha256,
        baseline_size_bytes=len(body),
    )


def observe_miteco_style_document(
    profile: str,
    body: bytes,
) -> ObservedMitecoStyleDocument:
    """Parse a live response without treating changed colors as reviewed.

    A changed but syntactically safe document is deliberately reduced to
    hashes only.  Its cartographic values are never projected into the local
    recipe until a later code review updates the committed evidence.
    """

    target = reviewed_miteco_style_watch_target(profile)
    if target is None:
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style profile is not configured"
        )
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_EVIDENCE_BYTES:
        raise ReviewedStyleEvidenceError(
            "observed MITECO style exceeds its byte limit"
        )
    document = _strict_json_object(body)
    _validate_json_limits(document)
    raw_sha256 = hashlib.sha256(body).hexdigest()
    semantic_sha256 = _canonical_json_sha256(document)
    return ObservedMitecoStyleDocument(
        raw_sha256=raw_sha256,
        semantic_sha256=semantic_sha256,
        size_bytes=len(body),
        matches_vendored_bytes=(
            raw_sha256 == target.baseline_raw_sha256
        ),
        matches_vendored_semantics=(
            semantic_sha256 == target.baseline_semantic_sha256
        ),
    )


def _reviewed_resource_body(spec: _MitecoStyleSpec) -> bytes:
    resource = (
        files("app.reference_layers")
        .joinpath("evidence")
        .joinpath("miteco_mvt")
        .joinpath(spec.filename)
    )
    try:
        body = resource.read_bytes()
    except (OSError, FileNotFoundError) as error:
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style evidence is unavailable"
        ) from error
    if (
        not 1 <= len(body) <= MAX_EVIDENCE_BYTES
        or hashlib.sha256(body).hexdigest() != spec.local_sha256
    ):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style evidence failed its local digest"
        )
    return body


def _strict_json_object(body: bytes) -> dict[str, Any]:
    if body.startswith(b"\xef\xbb\xbf"):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style evidence has a UTF-8 BOM"
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
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style evidence is not strict JSON"
        ) from error
    if not isinstance(value, dict):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style evidence root is invalid"
        )
    return value


def _validate_json_limits(value: Any) -> None:
    remaining = 10_000
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ReviewedStyleEvidenceError(
                "observed MITECO style exceeds its structural limits"
            )
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 1_024:
                    raise ReviewedStyleEvidenceError(
                        "observed MITECO style key is invalid"
                    )
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and len(item) > 8_192:
            raise ReviewedStyleEvidenceError(
                "observed MITECO style string exceeds its limit"
            )
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ReviewedStyleEvidenceError(
                "observed MITECO style value is invalid"
            )


def _canonical_json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ReviewedStyleEvidenceError(
            "observed MITECO style is not canonical JSON"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


def _validate_miteco_document(
    document: dict[str, Any],
    spec: _MitecoStyleSpec,
) -> tuple[str, str]:
    if (
        set(document) != {"version", "sources", "layers"}
        or document.get("version") != 8
    ):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style document shape is invalid"
        )
    sources = document.get("sources")
    layers = document.get("layers")
    if (
        not isinstance(sources, dict)
        or set(sources) != {spec.source_key}
        or not isinstance(layers, list)
        or len(layers) != 1
    ):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style source identity is invalid"
        )
    source = sources[spec.source_key]
    layer = layers[0]
    expected_tile_url = (
        "https://wmts.mapama.gob.es/sig/tiles/"
        f"{spec.source_layer}/{{z}}/{{x}}/{{y}}"
    )
    if (
        not isinstance(source, dict)
        or set(source)
        != {
            "type",
            "tiles",
            "minzoom",
            "maxzoom",
            "minZoom",
            "maxZoom",
        }
        or source.get("type") != "vector"
        or source.get("tiles") != [expected_tile_url]
        or source.get("minzoom") != 4
        or source.get("maxzoom") != 19
        or source.get("minZoom") != 4
        or source.get("maxZoom") != 19
        or not isinstance(layer, dict)
        or set(layer)
        != {"id", "type", "source", "source-layer", "paint"}
        or layer.get("id") != spec.style_id
        or layer.get("type") != "fill"
        or layer.get("source") != spec.source_key
        or layer.get("source-layer") != spec.source_layer
    ):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style semantics are invalid"
        )
    paint = layer.get("paint")
    if (
        not isinstance(paint, dict)
        or set(paint) != {"fill-color", "fill-outline-color"}
    ):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style paint is invalid"
        )
    fill_color = paint.get("fill-color")
    outline_color = paint.get("fill-outline-color")
    if (
        not isinstance(fill_color, str)
        or _COLOR_RE.fullmatch(fill_color) is None
        or not isinstance(outline_color, str)
        or _COLOR_RE.fullmatch(outline_color) is None
    ):
        raise ReviewedStyleEvidenceError(
            "reviewed MITECO style colors are invalid"
        )
    return fill_color, outline_color


def _without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewedStyleEvidenceError(
                "reviewed MITECO style contains duplicate JSON keys"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ReviewedStyleEvidenceError(
        f"reviewed MITECO style contains invalid constant {value}"
    )
