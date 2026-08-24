"""Hash-bound source-content parity shared by acquisition and promotion."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping


PARITY_SPEC_SCHEMA = "reference-source-content-parity-spec/v1"
ACQUISITION_GATE_SCHEMA = "reference-source-content-parity-acquisition/v1"
PROMOTION_GATE_SCHEMA = "reference-source-content-parity-promotion/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_EXPECTED_KEYS = {
    "archive_sha256",
    "archive_size_bytes",
    "archive_entry_count",
    "archive_uncompressed_bytes",
    "archive_member",
    "archive_member_crc32",
    "archive_member_size_bytes",
    "archive_member_sha256",
    "feature_layer",
    "feature_layers",
    "feature_count",
    "geometry_type",
    "crs",
    "declared_bounds",
    "geometry_bounds",
    "empty_geometry_count",
    "data_schema_sha256",
    "sample_sha256",
    "content_identity_sha256",
}


class SourceContentParityError(RuntimeError):
    """Parity configuration or persisted evidence is malformed."""


def configured_parity_spec(
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], str] | None:
    raw = config.get("source_content_parity")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "expected",
        "spec_sha256",
    }:
        raise SourceContentParityError(
            "source-content parity specification shape is invalid"
        )
    expected = raw.get("expected")
    if (
        raw.get("schema_version") != PARITY_SPEC_SCHEMA
        or not isinstance(expected, dict)
        or set(expected) != _EXPECTED_KEYS
    ):
        raise SourceContentParityError(
            "source-content parity specification is invalid"
        )
    _validate_expected(expected)
    semantic = {
        "schema_version": PARITY_SPEC_SCHEMA,
        "expected": expected,
    }
    digest = canonical_json_sha256(semantic)
    if raw.get("spec_sha256") != digest:
        raise SourceContentParityError(
            "source-content parity specification hash is invalid"
        )
    return json.loads(json.dumps(expected)), digest


def evaluate_acquisition_parity(
    config: Mapping[str, Any],
    inspection: Mapping[str, Any],
) -> dict[str, Any] | None:
    configured = configured_parity_spec(config)
    if configured is None:
        return None
    expected, spec_sha256 = configured
    observed = _inspection_projection(inspection)
    checks = {
        key: {
            "passed": observed[key] == expected[key],
            "expected": expected[key],
            "observed": observed[key],
        }
        for key in sorted(_EXPECTED_KEYS)
    }
    failed = [
        key
        for key, check in checks.items()
        if check["passed"] is not True
    ]
    return {
        "schema_version": ACQUISITION_GATE_SCHEMA,
        "passed": not failed,
        "spec_sha256": spec_sha256,
        "observation_sha256": canonical_json_sha256(observed),
        "failed_checks": failed,
        "checks": checks,
    }


def build_promotion_parity_gate(
    *,
    config: Mapping[str, Any],
    source_definition_sha256: str,
    input_artifact_id: int,
    input_artifact_sha256: str,
    input_artifact_metadata: Mapping[str, Any],
    delivery_kind: str,
    feature_count: int | None,
) -> dict[str, Any] | None:
    configured = configured_parity_spec(config)
    if configured is None:
        return None
    expected, spec_sha256 = configured
    acquisition = input_artifact_metadata.get("source_content_parity")
    if not valid_acquisition_gate(config, acquisition):
        raise SourceContentParityError(
            "input artifact lacks its exact acquisition parity gate"
        )
    checks = {
        "source_definition": {
            "passed": _valid_sha256(source_definition_sha256),
        },
        "input_artifact": {
            "passed": (
                isinstance(input_artifact_id, int)
                and not isinstance(input_artifact_id, bool)
                and input_artifact_id > 0
                and _valid_sha256(input_artifact_sha256)
            ),
        },
        "delivery_kind": {
            "passed": delivery_kind == "vector",
            "expected": "vector",
            "observed": delivery_kind,
        },
        "feature_count": {
            "passed": feature_count == expected["feature_count"],
            "expected": expected["feature_count"],
            "observed": feature_count,
        },
    }
    if any(check["passed"] is not True for check in checks.values()):
        raise SourceContentParityError(
            "prepared delivery differs from its source-content parity"
        )
    return {
        "schema_version": PROMOTION_GATE_SCHEMA,
        "passed": True,
        "source_definition_sha256": source_definition_sha256,
        "spec_sha256": spec_sha256,
        "input_artifact_id": input_artifact_id,
        "input_artifact_sha256": input_artifact_sha256,
        "acquisition_gate_sha256": canonical_json_sha256(acquisition),
        "checks": checks,
    }


def valid_acquisition_gate(
    config: Mapping[str, Any],
    gate: Any,
) -> bool:
    try:
        configured = configured_parity_spec(config)
        if configured is None or not isinstance(gate, dict):
            return False
        expected, spec_sha256 = configured
        if (
            set(gate)
            != {
                "schema_version",
                "passed",
                "spec_sha256",
                "observation_sha256",
                "failed_checks",
                "checks",
            }
            or gate.get("schema_version") != ACQUISITION_GATE_SCHEMA
            or gate.get("passed") is not True
            or gate.get("spec_sha256") != spec_sha256
            or not _valid_sha256(gate.get("observation_sha256"))
            or gate.get("failed_checks") != []
        ):
            return False
        checks = gate.get("checks")
        if not isinstance(checks, dict) or set(checks) != _EXPECTED_KEYS:
            return False
        if not all(
            isinstance(checks[key], dict)
            and set(checks[key]) == {"passed", "expected", "observed"}
            and checks[key].get("passed") is True
            and checks[key].get("expected") == expected[key]
            and checks[key].get("observed") == expected[key]
            for key in _EXPECTED_KEYS
        ):
            return False
        observed = {
            key: checks[key]["observed"]
            for key in sorted(_EXPECTED_KEYS)
        }
        return gate.get("observation_sha256") == canonical_json_sha256(
            observed
        )
    except (SourceContentParityError, TypeError, ValueError, RecursionError):
        return False


def valid_promotion_parity_gate(
    *,
    config: Mapping[str, Any],
    gate: Any,
    source_definition_sha256: str,
    input_artifact_id: int,
    input_artifact_sha256: str,
    input_artifact_metadata: Mapping[str, Any],
    delivery_kind: str,
    feature_count: int | None,
) -> bool:
    try:
        expected = build_promotion_parity_gate(
            config=config,
            source_definition_sha256=source_definition_sha256,
            input_artifact_id=input_artifact_id,
            input_artifact_sha256=input_artifact_sha256,
            input_artifact_metadata=input_artifact_metadata,
            delivery_kind=delivery_kind,
            feature_count=feature_count,
        )
        return expected is not None and gate == expected
    except (SourceContentParityError, TypeError, ValueError, RecursionError):
        return False


def canonical_json_sha256(value: Any) -> str:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise SourceContentParityError(
            "source-content parity evidence is not canonical JSON"
        ) from error
    if len(body) > 4 * 1024 * 1024:
        raise SourceContentParityError(
            "source-content parity evidence is oversized"
        )
    return hashlib.sha256(body).hexdigest()


def _inspection_projection(
    inspection: Mapping[str, Any],
) -> dict[str, Any]:
    sample = inspection.get("sample")
    return {
        "archive_sha256": inspection.get("archive_sha256"),
        "archive_size_bytes": inspection.get("archive_size_bytes"),
        "archive_entry_count": inspection.get("archive_entry_count"),
        "archive_uncompressed_bytes": inspection.get(
            "archive_uncompressed_bytes"
        ),
        "archive_member": inspection.get("archive_member"),
        "archive_member_crc32": inspection.get("archive_member_crc32"),
        "archive_member_size_bytes": inspection.get(
            "archive_member_size_bytes"
        ),
        "archive_member_sha256": inspection.get("archive_member_sha256"),
        "feature_layer": inspection.get("feature_layer"),
        "feature_layers": inspection.get("feature_layers"),
        "feature_count": inspection.get("feature_count"),
        "geometry_type": inspection.get("geometry_type"),
        "crs": inspection.get("crs"),
        "declared_bounds": inspection.get("declared_bounds"),
        "geometry_bounds": inspection.get("geometry_bounds"),
        "empty_geometry_count": inspection.get("empty_geometry_count"),
        "data_schema_sha256": inspection.get("data_schema_sha256"),
        "sample_sha256": (
            sample.get("sha256")
            if isinstance(sample, Mapping)
            else None
        ),
        "content_identity_sha256": inspection.get(
            "content_identity_sha256"
        ),
    }


def _validate_expected(value: dict[str, Any]) -> None:
    for name in (
        "archive_sha256",
        "archive_member_sha256",
        "data_schema_sha256",
        "sample_sha256",
        "content_identity_sha256",
    ):
        if not _valid_sha256(value.get(name)):
            raise SourceContentParityError(
                "source-content parity hash is invalid"
            )
    crc32 = value.get("archive_member_crc32")
    if (
        not isinstance(crc32, str)
        or re.fullmatch(r"[0-9a-f]{8}", crc32, re.ASCII) is None
    ):
        raise SourceContentParityError(
            "source-content parity CRC32 is invalid"
        )
    for name in ("archive_member", "feature_layer", "geometry_type", "crs"):
        item = value.get(name)
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 4_096
            or any(ord(character) < 32 for character in item)
        ):
            raise SourceContentParityError(
                "source-content parity text is invalid"
            )
    member = PurePosixPath(value["archive_member"])
    if (
        "\\" in value["archive_member"]
        or member.is_absolute()
        or any(part in {"", ".", ".."} for part in member.parts)
        or member.suffix.casefold() != ".gpkg"
    ):
        raise SourceContentParityError(
            "source-content parity archive member is invalid"
        )
    layers = value.get("feature_layers")
    if (
        not isinstance(layers, list)
        or layers != [value["feature_layer"]]
    ):
        raise SourceContentParityError(
            "source-content parity feature layers are invalid"
        )
    for name in (
        "archive_size_bytes",
        "archive_entry_count",
        "archive_uncompressed_bytes",
        "archive_member_size_bytes",
        "feature_count",
    ):
        item = value.get(name)
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 1
        ):
            raise SourceContentParityError(
                "source-content parity count is invalid"
            )
    empty = value.get("empty_geometry_count")
    if (
        isinstance(empty, bool)
        or not isinstance(empty, int)
        or empty < 0
    ):
        raise SourceContentParityError(
            "source-content parity empty count is invalid"
        )
    if (
        value["archive_size_bytes"] < 22
        or value["archive_entry_count"] > 4_096
        or value["archive_member_size_bytes"]
        > value["archive_uncompressed_bytes"]
        or empty > value["feature_count"]
    ):
        raise SourceContentParityError(
            "source-content parity counts are inconsistent"
        )
    _validate_bounds(value.get("declared_bounds"))
    _validate_bounds(value.get("geometry_bounds"))


def _validate_bounds(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "west",
        "south",
        "east",
        "north",
    }:
        raise SourceContentParityError(
            "source-content parity bounds are invalid"
        )
    west = value["west"]
    south = value["south"]
    east = value["east"]
    north = value["north"]
    if (
        any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in (west, south, east, north)
        )
        or float(west) >= float(east)
        or float(south) >= float(north)
    ):
        raise SourceContentParityError(
            "source-content parity bounds are invalid"
        )


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _SHA256_RE.fullmatch(value) is not None
    )
