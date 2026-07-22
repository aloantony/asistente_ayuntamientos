"""Read-only delivery of versioned MBTiles archives from local storage."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import struct
import zlib

from app.reference_layers.wms_proxy import TILE_SIZE, tile_bbox

MAX_TILE_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_BLOB_KEY = re.compile(
    r"^blobs/sha256/(?P<prefix>[0-9a-f]{2})/(?P<sha>[0-9a-f]{64})$",
    re.ASCII,
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class LocalTileArchiveError(Exception):
    """Base failure opening or reading a local tile archive."""


class UnsafeLocalTileArchiveError(LocalTileArchiveError):
    """The storage key or physical file is not an immutable local blob."""


class InvalidLocalTileArchiveError(LocalTileArchiveError):
    """The archive schema, metadata or image payload is invalid."""


class LocalTileNotFoundError(LocalTileArchiveError):
    """The requested coordinate is valid but absent from this archive."""


@dataclass(frozen=True)
class LocalTileArchiveResponse:
    body: bytes
    content_type: str
    etag: str


class LocalTileArchiveRenderer:
    """Serve a single image from a content-addressed MBTiles file.

    The database is opened for every request using SQLite's immutable read-only
    mode.  Ingestion performs the expensive whole-archive validation; delivery
    still verifies schema shape, cardinality, bytes and image dimensions.
    """

    def __init__(self, storage_root: Path | str) -> None:
        try:
            root = Path(storage_root).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UnsafeLocalTileArchiveError(
                "local tile storage root is unavailable"
            ) from exc
        if not root.is_dir():
            raise UnsafeLocalTileArchiveError(
                "local tile storage root is not a directory"
            )
        self._storage_root = root

    def render_tile(
        self,
        *,
        storage_key: str,
        archive_sha256: str,
        z: int,
        x: int,
        y: int,
    ) -> LocalTileArchiveResponse:
        tile_bbox(z, x, y)
        path = self._resolve_blob(storage_key, archive_sha256)
        tms_y = (2**z - 1) - y
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro&immutable=1",
                uri=True,
                timeout=2.0,
            )
            connection.execute("PRAGMA query_only = ON")
            archive_format = self._archive_format(connection)
            rows = connection.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? "
                "LIMIT 2",
                (z, x, tms_y),
            ).fetchall()
        except sqlite3.Error as exc:
            raise InvalidLocalTileArchiveError(
                "local tile archive cannot be read"
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        if not rows:
            raise LocalTileNotFoundError("local tile is not present")
        if len(rows) != 1:
            raise InvalidLocalTileArchiveError(
                "local tile archive contains duplicate coordinates"
            )
        body = rows[0][0]
        if not isinstance(body, bytes) or not 0 < len(body) <= MAX_TILE_BYTES:
            raise InvalidLocalTileArchiveError("local tile bytes are invalid")
        content_type = _validate_image(body, archive_format)
        digest = hashlib.sha256(body).hexdigest()
        return LocalTileArchiveResponse(
            body=body,
            content_type=content_type,
            etag=f'"{archive_sha256[:16]}-{digest}"',
        )

    def _resolve_blob(self, storage_key: str, archive_sha256: str) -> Path:
        if not isinstance(storage_key, str) or not isinstance(archive_sha256, str):
            raise UnsafeLocalTileArchiveError("invalid local tile blob identity")
        match = _BLOB_KEY.fullmatch(storage_key)
        if (
            match is None
            or _SHA256.fullmatch(archive_sha256) is None
            or match.group("sha") != archive_sha256
            or match.group("prefix") != archive_sha256[:2]
            or PurePosixPath(storage_key).as_posix() != storage_key
        ):
            raise UnsafeLocalTileArchiveError("invalid local tile blob identity")
        candidate = self._storage_root.joinpath(*PurePosixPath(storage_key).parts)
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UnsafeLocalTileArchiveError(
                "local tile blob is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or resolved.parent != candidate.parent.resolve(strict=True)
            or self._storage_root not in resolved.parents
        ):
            raise UnsafeLocalTileArchiveError("local tile blob is unsafe")
        return resolved

    @staticmethod
    def _archive_format(connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT value FROM metadata WHERE name = 'format' LIMIT 2"
        ).fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            raise InvalidLocalTileArchiveError(
                "local tile archive has no unique format metadata"
            )
        value = rows[0][0].strip().casefold()
        if value not in {"png", "jpg", "jpeg"}:
            raise InvalidLocalTileArchiveError(
                "local tile archive format is unsupported"
            )
        return "jpg" if value == "jpeg" else value


def _validate_image(body: bytes, archive_format: str) -> str:
    if archive_format == "png":
        if not body.startswith(_PNG_SIGNATURE):
            raise InvalidLocalTileArchiveError(
                "local tile does not match PNG metadata"
            )
        width, height = _png_dimensions(body)
        content_type = "image/png"
    else:
        if not body.startswith(b"\xff\xd8") or not body.endswith(b"\xff\xd9"):
            raise InvalidLocalTileArchiveError(
                "local tile does not match JPEG metadata"
            )
        width, height = _jpeg_dimensions(body)
        content_type = "image/jpeg"
    if (width, height) != (TILE_SIZE, TILE_SIZE):
        raise InvalidLocalTileArchiveError(
            "local tile dimensions are not 256 by 256"
        )
    return content_type


def _png_dimensions(body: bytes) -> tuple[int, int]:
    if (
        len(body) < 33
        or body[8:12] != b"\x00\x00\x00\r"
        or body[12:16] != b"IHDR"
    ):
        raise InvalidLocalTileArchiveError("local PNG header is invalid")
    chunk = body[12:29]
    expected_crc = struct.unpack(">I", body[29:33])[0]
    if zlib.crc32(chunk) & 0xFFFFFFFF != expected_crc:
        raise InvalidLocalTileArchiveError("local PNG header is invalid")
    return struct.unpack(">II", body[16:24])


def _jpeg_dimensions(body: bytes) -> tuple[int, int]:
    offset = 2
    while offset + 4 <= len(body):
        if body[offset] != 0xFF:
            raise InvalidLocalTileArchiveError("local JPEG is malformed")
        while offset < len(body) and body[offset] == 0xFF:
            offset += 1
        if offset >= len(body):
            break
        marker = body[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(body):
            break
        length = int.from_bytes(body[offset : offset + 2], "big")
        if length < 2 or offset + length > len(body):
            raise InvalidLocalTileArchiveError("local JPEG is malformed")
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if length < 7:
                raise InvalidLocalTileArchiveError("local JPEG SOF is malformed")
            height = int.from_bytes(body[offset + 3 : offset + 5], "big")
            width = int.from_bytes(body[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    raise InvalidLocalTileArchiveError("local JPEG has no dimensions")
