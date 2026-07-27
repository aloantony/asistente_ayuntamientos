"""Hash-bound IDECyL style evidence embedded in reviewed ZIP feeds.

The classification manifests deliberately describe acquisition routes, while
this independent resource records only exact style correspondences observed
inside complete local ZIP captures.  A correspondence is exposed only after
the manifest, catalog identity, archive identity, ZIP member identity and SLD
identity all agree.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping

from app.reference_layers.reviewed_archive_styles import (
    ReviewedArchiveStyleError,
    reviewed_archive_style_specs,
)


MANIFEST_SCHEMA = "siur-idecyl-archive-style-evidence/v1"
MANIFEST_RESOURCE = "evidence/idecyl_archive_styles/manifest-v1.json"
MANIFEST_SHA256 = (
    "6d85301514210dced9a54855bc12c204"
    "b7e287c5898ee6715121bc14f198c6a4"
)
MAX_MANIFEST_BYTES = 128 * 1024

CLASSIFICATION_MANIFEST_RESOURCE = (
    "evidence/idecyl_exact/decision-manifest-v3.json"
)
CLASSIFICATION_MANIFEST_SHA256 = (
    "201deb9372cc1201bf1645ec8249cdf9"
    "fdac69784915d71492a0db7b36a01eb5"
)
SETTINGS_SNAPSHOT_SHA256 = (
    "950eb0dbfb9226a39257ca61d27ef815"
    "353bcad76d00c5c18348489480ba53fc"
)
SETTINGS_SNAPSHOT_BYTES = 451_232
WMC_SNAPSHOT_SHA256 = (
    "9c3571179e8f489daa9b9c4531d99d0e"
    "0e612be03b1e269fa38908e72e6aaeb8"
)
WMC_SNAPSHOT_BYTES = 26_739

_EXPECTED_LAYER_IDS = (98, 137, 151, 197, 213, 225, 230, 268)
_EXPECTED_ARCHIVE_SHA256 = {
    98: "b4a48ed9c0e5c3c2ec3c95bd9c53cdfbba5583886ec8ac412b872f8a6312a6f2",
    137: "f61f68d2e1e0ecdfbd8ccf9731df87c56f76ef12b5e5fe62b75f9f2f5e657b00",
    151: "6954734b0497dbe7a8c7020ebdbf34b319fc98af114d4fb7caa880727a541b6e",
    197: "ca8e05499b02065120bd8b5f4f4f62777918c06d12178ad42cba198b58ff5d3e",
    213: "20053369969861e9c1e9f3f196c7a010bf38e6fc389c9724545f0322e3e23279",
    225: "54da041825d04aff7bd9cba3d15cf4c0165bb5dac25a7ecac68f303a49964716",
    230: "20053369969861e9c1e9f3f196c7a010bf38e6fc389c9724545f0322e3e23279",
    268: "36a0af1a234321707bbbb3722b7aefcac9228872366f9851e0d1acfd51faa538",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_CRC32_RE = re.compile(r"^[0-9a-f]{8}$", re.ASCII)
_STYLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,254}$", re.ASCII)
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:]{1,500}$", re.ASCII)


class IDECyLArchiveStyleEvidenceError(RuntimeError):
    """The committed archive-style evidence is altered or inconsistent."""


def load_idecyl_archive_style_evidence(
    manifest_body: bytes,
    *,
    archive_sources: Mapping[int, Mapping[str, Any]],
    catalog_sources: Mapping[int, Mapping[str, Any]],
    expected_manifest_sha256: str = MANIFEST_SHA256,
) -> dict[int, dict[str, Any]]:
    """Validate and project exact archive styles for the classification loader."""

    if (
        not isinstance(manifest_body, bytes)
        or not 0 < len(manifest_body) <= MAX_MANIFEST_BYTES
        or not isinstance(expected_manifest_sha256, str)
        or _SHA256_RE.fullmatch(expected_manifest_sha256) is None
        or hashlib.sha256(manifest_body).hexdigest()
        != expected_manifest_sha256
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style manifest failed its local digest"
        )
    manifest = _json_object(manifest_body)
    canonical = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if (
        manifest_body != canonical
        or set(manifest) != {"schema", "capture", "layers"}
        or manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("capture") != _expected_capture()
        or not isinstance(manifest.get("layers"), list)
        or len(manifest["layers"]) != len(_EXPECTED_LAYER_IDS)
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style manifest shape is invalid"
        )

    result: dict[int, dict[str, Any]] = {}
    for raw_layer in manifest["layers"]:
        layer_id, projected = _layer_evidence(
            raw_layer,
            archive_sources=archive_sources,
            catalog_sources=catalog_sources,
        )
        if layer_id in result:
            raise IDECyLArchiveStyleEvidenceError(
                "IDECyL archive style layer identity is duplicated"
            )
        result[layer_id] = projected
    if tuple(result) != _EXPECTED_LAYER_IDS:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style layers are incomplete or not canonical"
        )

    shared = (result[213]["archive_capture"], result[230]["archive_capture"])
    if shared[0] != shared[1]:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL shared archive style capture changed"
        )
    return deepcopy(result)


def _expected_capture() -> dict[str, Any]:
    return {
        "captured_on": "2026-07-27",
        "catalog_capture": {
            "settings_sha256": SETTINGS_SNAPSHOT_SHA256,
            "settings_size_bytes": SETTINGS_SNAPSHOT_BYTES,
            "wmc_sha256": WMC_SNAPSHOT_SHA256,
            "wmc_size_bytes": WMC_SNAPSHOT_BYTES,
            "selection_rule": (
                "native_settings_then_exact_wmc_augmentation_v1"
            ),
        },
        "classification_manifest": {
            "resource": CLASSIFICATION_MANIFEST_RESOURCE,
            "sha256": CLASSIFICATION_MANIFEST_SHA256,
        },
        "authorization_effect": (
            "none_without_persisted_human_mirror_review"
        ),
    }


def _layer_evidence(
    value: Any,
    *,
    archive_sources: Mapping[int, Mapping[str, Any]],
    catalog_sources: Mapping[int, Mapping[str, Any]],
) -> tuple[int, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {
        "audit_layer_id",
        "catalog_identity",
        "source_identity",
        "archive_capture",
        "catalog_styles",
        "style_coverage",
        "styles",
    }:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style layer shape is invalid"
        )
    layer_id = value.get("audit_layer_id")
    if (
        isinstance(layer_id, bool)
        or not isinstance(layer_id, int)
        or layer_id not in _EXPECTED_LAYER_IDS
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style layer identity is invalid"
        )
    archive_source = archive_sources.get(layer_id)
    catalog_source = catalog_sources.get(layer_id)
    if archive_source is None or catalog_source is None:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style source identity is missing"
        )

    expected_catalog_identity = {
        "catalog_layer_source_key": catalog_source.get(
            "catalog_layer_source_key"
        ),
        "catalog_endpoint_url": catalog_source.get("catalog_endpoint_url"),
        "catalog_remote_name": catalog_source.get("catalog_remote_name"),
    }
    expected_source_identity = {
        "selected_endpoint_url": archive_source.get(
            "selected_endpoint_url"
        ),
        "selected_remote_name": archive_source.get(
            "selected_remote_name"
        ),
        "archive_member": archive_source.get("archive_member"),
        "input_layer": archive_source.get("input_layer"),
    }
    if (
        value.get("catalog_identity") != expected_catalog_identity
        or value.get("source_identity") != expected_source_identity
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style source identity changed"
        )

    archive_capture = value.get("archive_capture")
    baseline = archive_source.get("audit_capture")
    response = (
        baseline.get("baseline_response")
        if isinstance(baseline, dict)
        else None
    )
    if (
        not isinstance(archive_capture, dict)
        or set(archive_capture)
        != {
            "archive_sha256",
            "content_length",
            "etag",
            "last_modified",
            "v3_capture_kind",
            "v3_capture_length",
            "v3_capture_range_start",
            "v3_capture_sha256",
        }
        or not isinstance(response, dict)
        or archive_capture.get("archive_sha256")
        != _EXPECTED_ARCHIVE_SHA256[layer_id]
        or archive_capture.get("content_length")
        != response.get("content_length")
        or archive_capture.get("etag") != response.get("etag")
        or not _strong_etag(archive_capture.get("etag"))
        or archive_capture.get("last_modified")
        != response.get("last_modified")
        or archive_capture.get("v3_capture_kind")
        != baseline.get("capture_kind")
        or archive_capture.get("v3_capture_length")
        != baseline.get("tail_length")
        or archive_capture.get("v3_capture_range_start")
        != baseline.get("tail_range_start")
        or archive_capture.get("v3_capture_sha256")
        != baseline.get("tail_sha256")
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style capture identity changed"
        )

    catalog_styles = _catalog_styles(value.get("catalog_styles"))
    raw_styles = value.get("styles")
    if not isinstance(raw_styles, list) or not raw_styles:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style list is empty"
        )
    baseline_entries = baseline.get("baseline_entries")
    if not isinstance(baseline_entries, list):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive member baseline is missing"
        )
    entries = {
        item.get("name"): item
        for item in baseline_entries
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }

    projected_styles: list[dict[str, Any]] = []
    for raw_style in raw_styles:
        projected_styles.append(
            _style_evidence(
                raw_style,
                catalog_styles=catalog_styles,
                archive_entries=entries,
            )
        )
    projected_styles.sort(
        key=lambda item: item["catalog_style_source_key"]
    )
    if projected_styles != [
        _style_evidence(
            item,
            catalog_styles=catalog_styles,
            archive_entries=entries,
        )
        for item in raw_styles
    ]:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive styles are not canonical"
        )
    try:
        reviewed_archive_style_specs(
            {
                "data_format": archive_source.get("data_format"),
                "archive_styles": projected_styles,
            },
            protocol="download",
            target_kind="vector",
            selected_layer_name=str(
                archive_source.get("selected_remote_name") or ""
            ),
        )
    except ReviewedArchiveStyleError as error:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style projection is invalid"
        ) from error

    included_keys = {
        item["catalog_style_source_key"] for item in projected_styles
    }
    catalog_keys = {item["catalog_style_source_key"] for item in catalog_styles}
    missing_keys = sorted(catalog_keys - included_keys)
    coverage = value.get("style_coverage")
    if (
        included_keys - catalog_keys
        or not isinstance(coverage, dict)
        or set(coverage) != {"complete", "missing_catalog_style_source_keys"}
        or coverage.get("complete") is not (not missing_keys)
        or coverage.get("missing_catalog_style_source_keys") != missing_keys
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style coverage is invalid"
        )
    return layer_id, {
        "archive_capture": deepcopy(archive_capture),
        "archive_styles": projected_styles,
        "catalog_styles": catalog_styles,
        "style_coverage": deepcopy(coverage),
    }


def _catalog_styles(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL catalog style inventory is invalid"
        )
    result: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "catalog_style_source_key",
            "remote_name",
            "is_default",
        }:
            raise IDECyLArchiveStyleEvidenceError(
                "IDECyL catalog style identity is invalid"
            )
        source_key = item.get("catalog_style_source_key")
        remote_name = item.get("remote_name")
        identity = (str(source_key), str(remote_name))
        if (
            not isinstance(source_key, str)
            or _STYLE_KEY_RE.fullmatch(source_key) is None
            or not isinstance(remote_name, str)
            or _REMOTE_NAME_RE.fullmatch(remote_name) is None
            or not isinstance(item.get("is_default"), bool)
            or identity in identities
        ):
            raise IDECyLArchiveStyleEvidenceError(
                "IDECyL catalog style identity is invalid"
            )
        identities.add(identity)
        result.append(deepcopy(item))
    if (
        result
        != sorted(
            result,
            key=lambda item: item["catalog_style_source_key"],
        )
        or sum(item["is_default"] for item in result) != 1
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL catalog styles are ambiguous or not canonical"
        )
    return result


def _style_evidence(
    value: Any,
    *,
    catalog_styles: list[dict[str, Any]],
    archive_entries: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "catalog_style",
        "archive_member",
        "sld",
    }:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style evidence shape is invalid"
        )
    catalog_style = value.get("catalog_style")
    if (
        not isinstance(catalog_style, dict)
        or set(catalog_style)
        != {
            "catalog_style_source_key",
            "remote_name",
            "is_default",
        }
        or catalog_style not in catalog_styles
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style catalog mapping is invalid"
        )
    member = value.get("archive_member")
    if not isinstance(member, dict) or set(member) != {
        "name",
        "sha256",
        "size_bytes",
        "crc32",
    }:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style member identity is invalid"
        )
    name = member.get("name")
    baseline = archive_entries.get(name) if isinstance(name, str) else None
    if (
        not isinstance(name, str)
        or not name.casefold().endswith(".sld")
        or "/" in name
        or "\\" in name
        or not isinstance(baseline, Mapping)
        or _SHA256_RE.fullmatch(str(member.get("sha256"))) is None
        or member.get("size_bytes") != baseline.get("uncompressed_bytes")
        or member.get("crc32") != baseline.get("crc32")
        or _CRC32_RE.fullmatch(str(member.get("crc32"))) is None
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style member identity changed"
        )
    sld = value.get("sld")
    if (
        not isinstance(sld, dict)
        or set(sld) != {"named_layer", "user_style", "self_contained"}
        or not isinstance(sld.get("named_layer"), str)
        or not sld["named_layer"]
        or not isinstance(sld.get("user_style"), str)
        or not sld["user_style"]
        or sld.get("self_contained") is not True
    ):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive SLD identity is invalid"
        )
    return {
        "catalog_style_source_key": catalog_style[
            "catalog_style_source_key"
        ],
        "remote_name": catalog_style["remote_name"],
        "is_default": catalog_style["is_default"],
        "archive_member": name,
        "sha256": member["sha256"],
        "size_bytes": member["size_bytes"],
        "crc32": member["crc32"],
        "sld_layer_name": sld["named_layer"],
        "sld_style_name": sld["user_style"],
    }


def _strong_etag(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 3 <= len(value) <= 4_096
        and value.startswith('"')
        and value.endswith('"')
        and not value.startswith('W/"')
        and all(
            ord(character) >= 32 and ord(character) != 127
            for character in value
        )
    )


def _json_object(body: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise IDECyLArchiveStyleEvidenceError(
                    "IDECyL archive style manifest has duplicate keys"
                )
            value[key] = item
        return value

    try:
        decoded = body.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=object_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(item)
            ),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style manifest is not strict UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise IDECyLArchiveStyleEvidenceError(
            "IDECyL archive style manifest is not an object"
        )
    return value
