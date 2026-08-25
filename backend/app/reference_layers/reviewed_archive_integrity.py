"""Hash-bound invariants and baseline observations for reviewed ZIP feeds."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping
import zipfile

from app.reference_layers.safe_download import HTTPSDownloadResult


INTEGRITY_SPEC_SCHEMA = "reference-reviewed-archive-integrity/v1"
INTEGRITY_OBSERVATION_SCHEMA = (
    "reference-reviewed-archive-integrity-observation/v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ALLOWED_COMPRESSION = {
    "stored": zipfile.ZIP_STORED,
    "deflate": zipfile.ZIP_DEFLATED,
}
_MAX_ENTRIES = 4_096
_MAX_NAME_BYTES = 4_096


class ReviewedArchiveIntegrityError(RuntimeError):
    """A reviewed archive profile or observation is malformed."""


def configured_reviewed_archive_integrity(
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], str] | None:
    """Return a fresh validated invariant profile and semantic digest."""

    raw = config.get("reviewed_archive_integrity")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "response_constraints",
        "archive_constraints",
        "spec_sha256",
    }:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive integrity profile shape is invalid"
        )
    if raw.get("schema_version") != INTEGRITY_SPEC_SCHEMA:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive integrity schema is invalid"
        )
    response = raw.get("response_constraints")
    archive = raw.get("archive_constraints")
    _validate_response_constraints(response)
    _validate_archive_constraints(archive)
    semantic = {
        "schema_version": INTEGRITY_SPEC_SCHEMA,
        "response_constraints": response,
        "archive_constraints": archive,
    }
    digest = canonical_json_sha256(semantic)
    if raw.get("spec_sha256") != digest:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive integrity hash is invalid"
        )
    return json.loads(json.dumps(semantic)), digest


def inspect_reviewed_archive(
    path: Path,
    *,
    config: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate ZIP structure while allowing legitimate dataset byte changes."""

    configured = configured_reviewed_archive_integrity(config)
    if configured is None:
        return None
    semantic, spec_sha256 = configured
    constraints = semantic["archive_constraints"]
    try:
        with zipfile.ZipFile(Path(path)) as archive:
            entries = archive.infolist()
            observed = [_entry_projection(item) for item in entries]
            _validate_observed_entries(observed, constraints)
            license_member = constraints["license_member"]
            license_limit = constraints["license_max_uncompressed_bytes"]
            info = archive.getinfo(license_member)
            if info.file_size > license_limit:
                raise ReviewedArchiveIntegrityError(
                    "reviewed archive license exceeds its byte limit"
                )
            license_sha256 = _member_sha256(
                archive,
                info,
                maximum_bytes=license_limit,
            )
    except ReviewedArchiveIntegrityError:
        raise
    except (
        KeyError,
        OSError,
        RuntimeError,
        UnicodeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive central directory is unreadable"
        ) from error
    if license_sha256 not in constraints["license_sha256_allowlist"]:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive license content changed"
        )
    return {
        "schema_version": INTEGRITY_OBSERVATION_SCHEMA,
        "passed": True,
        "spec_sha256": spec_sha256,
        "response": None,
        "entries": observed,
        "entries_sha256": canonical_json_sha256(observed),
        "license_sha256": license_sha256,
    }


def validate_reviewed_archive_response(
    config: Mapping[str, Any],
    result: HTTPSDownloadResult,
) -> dict[str, Any] | None:
    """Validate live HTTP bounds without pinning mutable validators."""

    configured = configured_reviewed_archive_integrity(config)
    if configured is None:
        return None
    semantic, spec_sha256 = configured
    constraints = semantic["response_constraints"]
    if (
        constraints["require_etag"]
        and (not isinstance(result.etag, str) or not result.etag)
    ) or (
        constraints["require_last_modified"]
        and (
            not isinstance(result.last_modified, str)
            or not result.last_modified
        )
    ):
        raise ReviewedArchiveIntegrityError(
            "reviewed archive response validators are missing"
        )
    if result.last_modified is not None:
        try:
            parsed = parsedate_to_datetime(result.last_modified)
        except (TypeError, ValueError, OverflowError) as error:
            raise ReviewedArchiveIntegrityError(
                "reviewed archive Last-Modified is invalid"
            ) from error
        if parsed is None or parsed.tzinfo is None:
            raise ReviewedArchiveIntegrityError(
                "reviewed archive Last-Modified is invalid"
            )
    if result.not_modified:
        observed = {
            "not_modified": True,
            "content_length": 0,
            "content_type": None,
            "etag": result.etag,
            "last_modified": result.last_modified,
        }
    else:
        if (
            result.content_type != constraints["content_type"]
            or result.size_bytes > constraints["max_content_length"]
        ):
            raise ReviewedArchiveIntegrityError(
                "reviewed archive HTTP bounds changed"
            )
        observed = {
            "not_modified": False,
            "content_length": result.size_bytes,
            "content_type": result.content_type,
            "etag": result.etag,
            "last_modified": result.last_modified,
        }
    return {
        "schema_version": INTEGRITY_OBSERVATION_SCHEMA,
        "passed": True,
        "spec_sha256": spec_sha256,
        "response": observed,
        "entries": None,
        "entries_sha256": None,
        "license_sha256": None,
    }


def canonical_json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive evidence is not canonical JSON"
        ) from error
    if len(encoded) > 4 * 1024 * 1024:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive evidence is oversized"
        )
    return hashlib.sha256(encoded).hexdigest()


def _validate_response_constraints(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "content_type",
        "max_content_length",
        "require_etag",
        "require_last_modified",
    }:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive response constraints are invalid"
        )
    maximum = value.get("max_content_length")
    if (
        value.get("content_type") != "application/x-zip-compressed"
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 22 <= maximum <= 20 * 1024 * 1024 * 1024
        or value.get("require_etag") is not True
        or value.get("require_last_modified") is not True
    ):
        raise ReviewedArchiveIntegrityError(
            "reviewed archive response constraints are invalid"
        )


def _validate_archive_constraints(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "max_entries",
        "max_uncompressed_bytes",
        "required_members",
        "license_member",
        "license_max_uncompressed_bytes",
        "license_sha256_allowlist",
    }:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive constraints are invalid"
        )
    max_entries = value.get("max_entries")
    max_uncompressed = value.get("max_uncompressed_bytes")
    required = value.get("required_members")
    license_member = value.get("license_member")
    license_maximum = value.get("license_max_uncompressed_bytes")
    allowlist = value.get("license_sha256_allowlist")
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or not 2 <= max_entries <= _MAX_ENTRIES
        or isinstance(max_uncompressed, bool)
        or not isinstance(max_uncompressed, int)
        or not 100 <= max_uncompressed <= 40 * 1024 * 1024 * 1024
        or not isinstance(required, list)
        or not 2 <= len(required) <= max_entries
        or required != sorted(set(required))
        or any(not _safe_member_name(item) for item in required)
        or not isinstance(license_member, str)
        or license_member not in required
        or isinstance(license_maximum, bool)
        or not isinstance(license_maximum, int)
        or not 100 <= license_maximum <= 1024 * 1024
        or not isinstance(allowlist, list)
        or not 1 <= len(allowlist) <= 16
        or allowlist != sorted(set(allowlist))
        or any(
            not isinstance(item, str)
            or _SHA256_RE.fullmatch(item) is None
            for item in allowlist
        )
    ):
        raise ReviewedArchiveIntegrityError(
            "reviewed archive constraints are invalid"
        )


def _validate_observed_entries(
    entries: list[dict[str, Any]],
    constraints: Mapping[str, Any],
) -> None:
    if not entries or len(entries) > constraints["max_entries"]:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive entry count exceeds its bound"
        )
    names = [item["name"] for item in entries]
    if (
        len(names) != len(set(names))
        or len({item.casefold() for item in names}) != len(names)
        or not set(constraints["required_members"]).issubset(names)
        or sum(item["uncompressed_bytes"] for item in entries)
        > constraints["max_uncompressed_bytes"]
    ):
        raise ReviewedArchiveIntegrityError(
            "reviewed archive members changed outside their bounds"
        )


def _entry_projection(entry: zipfile.ZipInfo) -> dict[str, Any]:
    unix_mode = (entry.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if (
        entry.is_dir()
        or not _safe_member_name(entry.filename)
        or entry.flag_bits & 0x1
        or file_type == stat.S_IFLNK
        or (
            file_type
            and file_type not in {stat.S_IFREG, stat.S_IFDIR}
        )
        or entry.file_size < 0
        or entry.compress_size < 0
        or (
            entry.file_size > 0
            and (
                entry.compress_size == 0
                or entry.file_size
                > max(entry.compress_size, 1) * 1_000
            )
        )
    ):
        raise ReviewedArchiveIntegrityError(
            "reviewed archive contains an unsafe entry"
        )
    compression = next(
        (
            name
            for name, method in _ALLOWED_COMPRESSION.items()
            if method == entry.compress_type
        ),
        None,
    )
    if compression is None:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive compression is unsupported"
        )
    return {
        "name": entry.filename,
        "crc32": f"{entry.CRC:08x}",
        "compressed_bytes": entry.compress_size,
        "uncompressed_bytes": entry.file_size,
        "compression": compression,
    }


def _member_sha256(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    *,
    maximum_bytes: int,
) -> str:
    digest = hashlib.sha256()
    total = 0
    with archive.open(member) as source:
        while chunk := source.read(64 * 1024):
            total += len(chunk)
            if total > maximum_bytes or total > member.file_size:
                raise ReviewedArchiveIntegrityError(
                    "reviewed archive license exceeds its declared bounds"
                )
            digest.update(chunk)
    if total != member.file_size:
        raise ReviewedArchiveIntegrityError(
            "reviewed archive license is truncated"
        )
    return digest.hexdigest()


def _safe_member_name(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return False
    pure = PurePosixPath(value)
    return (
        len(encoded) <= _MAX_NAME_BYTES
        and "\\" not in value
        and not pure.is_absolute()
        and all(part not in {"", ".", ".."} for part in pure.parts)
    )
