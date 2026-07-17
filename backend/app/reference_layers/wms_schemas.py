from __future__ import annotations

import json
from math import isfinite
from typing import Any

MAX_IDENTIFY_DEPTH = 20
MAX_IDENTIFY_NODES = 20_000
MAX_IDENTIFY_STRING_CHARS = 20_000
MAX_IDENTIFY_KEY_CHARS = 256


class InvalidFeatureInfoError(ValueError):
    pass


def parse_feature_collection(
    body: bytes,
    *,
    max_features: int,
) -> dict[str, Any]:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise InvalidFeatureInfoError("invalid feature information") from error
    if not isinstance(value, dict) or value.get("type") != "FeatureCollection":
        raise InvalidFeatureInfoError("invalid feature collection")
    features = value.get("features")
    if not isinstance(features, list) or len(features) > max_features:
        raise InvalidFeatureInfoError("invalid feature collection")
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise InvalidFeatureInfoError("invalid feature")
        if not isinstance(feature.get("properties"), dict):
            raise InvalidFeatureInfoError("invalid feature properties")
        geometry = feature.get("geometry")
        if geometry is not None and not isinstance(geometry, dict):
            raise InvalidFeatureInfoError("invalid feature geometry")
    _validate_json_limits(value)
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidFeatureInfoError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise InvalidFeatureInfoError(f"invalid JSON number: {value}")


def _validate_json_limits(root: Any) -> None:
    nodes = 0
    pending: list[tuple[Any, int]] = [(root, 1)]
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > MAX_IDENTIFY_NODES or depth > MAX_IDENTIFY_DEPTH:
            raise InvalidFeatureInfoError("feature information is too complex")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > MAX_IDENTIFY_KEY_CHARS:
                    raise InvalidFeatureInfoError("invalid feature key")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if len(value) > MAX_IDENTIFY_STRING_CHARS:
                raise InvalidFeatureInfoError("feature string is too long")
        elif isinstance(value, bool) or value is None:
            continue
        elif isinstance(value, int):
            continue
        elif isinstance(value, float):
            if not isfinite(value):
                raise InvalidFeatureInfoError("invalid feature number")
        else:
            raise InvalidFeatureInfoError("invalid feature value")
