"""Fail-closed identities for reviewed SLD members embedded in ZIP feeds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any, Mapping


MAX_REVIEWED_ARCHIVE_STYLES = 256
MAX_REVIEWED_ARCHIVE_STYLE_BYTES = 8 * 1024 * 1024

LEGACY_ARCHIVE_STYLE_KEYS = frozenset(
    {
        "catalog_style_source_key",
        "remote_name",
        "archive_member",
        "sha256",
    }
)
EXACT_ARCHIVE_STYLE_KEYS = frozenset(
    {
        "catalog_style_source_key",
        "remote_name",
        "is_default",
        "archive_member",
        "sha256",
        "size_bytes",
        "crc32",
        "sld_layer_name",
        "sld_style_name",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_CRC32_RE = re.compile(r"^[0-9a-f]{8}$", re.ASCII)
_STYLE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:]{1,255}$", re.ASCII)
_STYLE_SOURCE_KEY_RE = re.compile(
    r"^[a-z0-9][a-z0-9_.:/-]{0,254}$",
    re.ASCII,
)


class ReviewedArchiveStyleError(ValueError):
    """An embedded-style declaration is ambiguous or incomplete."""


@dataclass(frozen=True)
class ReviewedArchiveStyleSpec:
    catalog_style_source_key: str
    remote_name: str
    is_default: bool | None
    archive_member: str
    sha256: str
    size_bytes: int | None
    crc32: str | None
    sld_layer_name: str
    sld_style_name: str

    @property
    def has_exact_member_evidence(self) -> bool:
        return (
            self.is_default is not None
            and self.size_bytes is not None
            and self.crc32 is not None
        )


def reviewed_archive_style_specs(
    config: Mapping[str, Any],
    *,
    protocol: str,
    target_kind: str,
    selected_layer_name: str,
) -> tuple[ReviewedArchiveStyleSpec, ...]:
    """Validate the effective source config before any network request."""

    raw_styles = config.get("archive_styles")
    if raw_styles is None:
        return ()
    if (
        protocol != "download"
        or target_kind != "vector"
        or config.get("data_format")
        not in {"geopackage-zip", "shapefile-zip"}
        or not isinstance(raw_styles, list)
        or not 1 <= len(raw_styles) <= MAX_REVIEWED_ARCHIVE_STYLES
        or not _safe_sld_identity(selected_layer_name)
    ):
        raise ReviewedArchiveStyleError(
            "reviewed archive style configuration is invalid"
        )

    key_sets = {
        frozenset(raw) if isinstance(raw, dict) else frozenset()
        for raw in raw_styles
    }
    if key_sets == {LEGACY_ARCHIVE_STYLE_KEYS}:
        exact = False
    elif key_sets == {EXACT_ARCHIVE_STYLE_KEYS}:
        exact = True
    else:
        raise ReviewedArchiveStyleError(
            "reviewed archive style evidence is incomplete or mixed"
        )

    specs: list[ReviewedArchiveStyleSpec] = []
    source_keys: set[str] = set()
    remote_names: set[str] = set()
    members: set[str] = set()
    content_digests: set[str] = set()
    for raw in raw_styles:
        source_key = raw.get("catalog_style_source_key")
        remote_name = raw.get("remote_name")
        member = raw.get("archive_member")
        sha256 = raw.get("sha256")
        is_default = raw.get("is_default") if exact else None
        size_bytes = raw.get("size_bytes") if exact else None
        crc32 = raw.get("crc32") if exact else None
        sld_layer_name = (
            raw.get("sld_layer_name") if exact else selected_layer_name
        )
        sld_style_name = (
            raw.get("sld_style_name") if exact else remote_name
        )
        if (
            not isinstance(source_key, str)
            or _STYLE_SOURCE_KEY_RE.fullmatch(source_key) is None
            or source_key in source_keys
            or not isinstance(remote_name, str)
            or _STYLE_NAME_RE.fullmatch(remote_name) is None
            or remote_name in remote_names
            or not _safe_archive_member_name(member)
            or PurePosixPath(member).suffix.casefold() != ".sld"
            or member in members
            or not isinstance(sha256, str)
            or _SHA256_RE.fullmatch(sha256) is None
            or sha256 in content_digests
            or not _safe_sld_identity(sld_layer_name)
            or not _safe_sld_identity(sld_style_name)
            or (
                exact
                and (
                    not isinstance(is_default, bool)
                    or isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or not 1
                    <= size_bytes
                    <= MAX_REVIEWED_ARCHIVE_STYLE_BYTES
                    or not isinstance(crc32, str)
                    or _CRC32_RE.fullmatch(crc32) is None
                )
            )
        ):
            raise ReviewedArchiveStyleError(
                "reviewed archive style identity is invalid"
            )
        source_keys.add(source_key)
        remote_names.add(remote_name)
        members.add(member)
        content_digests.add(sha256)
        specs.append(
            ReviewedArchiveStyleSpec(
                catalog_style_source_key=source_key,
                remote_name=remote_name,
                is_default=is_default,
                archive_member=member,
                sha256=sha256,
                size_bytes=size_bytes,
                crc32=crc32,
                sld_layer_name=sld_layer_name,
                sld_style_name=sld_style_name,
            )
        )

    if specs != sorted(
        specs,
        key=lambda item: item.catalog_style_source_key,
    ):
        raise ReviewedArchiveStyleError(
            "reviewed archive styles are not canonical"
        )
    if exact and sum(item.is_default is True for item in specs) != 1:
        raise ReviewedArchiveStyleError(
            "reviewed archive style default is ambiguous"
        )
    return tuple(specs)


def _safe_archive_member_name(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return False
    pure = PurePosixPath(value)
    return (
        len(encoded) <= 4_096
        and not pure.is_absolute()
        and pure.as_posix() == value
        and all(part not in {"", ".", ".."} for part in pure.parts)
        and all(
            ord(character) >= 32 and ord(character) != 127
            for character in value
        )
    )


def _safe_sld_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 1_000
        and all(
            ord(character) >= 32 and ord(character) != 127
            for character in value
        )
    )
