from io import BytesIO
import sqlite3
import struct
import zlib

import pytest
from PIL import Image

from app.reference_layers.local_tile_archive import (
    InvalidLocalTileArchiveError,
    LocalTileArchiveRenderer,
    LocalTileNotFoundError,
    UnsafeLocalTileArchiveError,
)


SHA = "ab" + "1" * 62
KEY = f"blobs/sha256/ab/{SHA}"


def png(width: int = 256, height: int = 256) -> bytes:
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = b"IHDR" + ihdr_data
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr_data))
        + ihdr
        + struct.pack(">I", zlib.crc32(ihdr) & 0xFFFFFFFF)
    )


def jpeg(width: int = 256, height: int = 256) -> bytes:
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


def archive(tmp_path, *, image=None, image_format="png", duplicate=False):
    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    connection.execute(
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB)"
    )
    connection.execute("INSERT INTO metadata VALUES ('format', ?)", (image_format,))
    connection.execute("INSERT INTO metadata VALUES ('maxzoom', '3')")
    if image is not None:
        connection.execute("INSERT INTO tiles VALUES (3, 2, 6, ?)", (image,))
        if duplicate:
            connection.execute("INSERT INTO tiles VALUES (3, 2, 6, ?)", (image,))
    connection.commit()
    connection.close()
    return path


def test_reads_xyz_coordinate_from_tms_archive(tmp_path) -> None:
    image = png()
    archive(tmp_path, image=image)

    response = LocalTileArchiveRenderer(tmp_path).render_tile(
        storage_key=KEY,
        archive_sha256=SHA,
        z=3,
        x=2,
        y=1,
    )

    assert response.body == image
    assert response.content_type == "image/png"
    assert SHA[:16] in response.etag


def test_jpeg_archive_is_delivered_with_its_real_content_type(tmp_path) -> None:
    image = jpeg()
    archive(tmp_path, image=image, image_format="jpeg")

    response = LocalTileArchiveRenderer(tmp_path).render_tile(
        storage_key=KEY,
        archive_sha256=SHA,
        z=3,
        x=2,
        y=1,
    )

    assert response.body == image
    assert response.content_type == "image/jpeg"


def test_zoom_above_native_archive_is_rendered_from_local_parent(tmp_path) -> None:
    source = Image.new("RGB", (256, 256), (255, 0, 0))
    output = BytesIO()
    source.save(output, format="PNG")
    archive(tmp_path, image=output.getvalue())

    response = LocalTileArchiveRenderer(tmp_path).render_tile(
        storage_key=KEY,
        archive_sha256=SHA,
        z=5,
        x=9,
        y=6,
    )

    assert response.content_type == "image/png"
    with Image.open(BytesIO(response.body)) as rendered:
        rendered.load()
        assert rendered.size == (256, 256)
        assert rendered.getpixel((128, 128)) == (255, 0, 0)


def test_overzoom_never_falls_back_to_network_or_an_unbounded_ancestor(
    tmp_path,
) -> None:
    source = Image.new("RGB", (256, 256), (0, 0, 0))
    output = BytesIO()
    source.save(output, format="PNG")
    archive(tmp_path, image=output.getvalue())

    with pytest.raises(LocalTileNotFoundError):
        LocalTileArchiveRenderer(tmp_path).render_tile(
            storage_key=KEY,
            archive_sha256=SHA,
            z=12,
            x=2 << 9,
            y=1 << 9,
        )


def test_missing_coordinate_is_distinct_from_corrupt_archive(tmp_path) -> None:
    archive(tmp_path)

    with pytest.raises(LocalTileNotFoundError):
        LocalTileArchiveRenderer(tmp_path).render_tile(
            storage_key=KEY,
            archive_sha256=SHA,
            z=3,
            x=2,
            y=1,
        )


@pytest.mark.parametrize(
    ("key", "digest"),
    [
        ("../archive.mbtiles", SHA),
        (KEY, "c" * 64),
        (f"blobs/sha256/ff/{SHA}", SHA),
        (f"blobs/sha256/ab/{SHA}/extra", SHA),
    ],
)
def test_storage_key_cannot_escape_or_misidentify_blob(tmp_path, key, digest) -> None:
    archive(tmp_path, image=png())

    with pytest.raises(UnsafeLocalTileArchiveError):
        LocalTileArchiveRenderer(tmp_path).render_tile(
            storage_key=key,
            archive_sha256=digest,
            z=3,
            x=2,
            y=1,
        )


def test_symlink_blob_is_rejected(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"not sqlite")
    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    with pytest.raises(UnsafeLocalTileArchiveError):
        LocalTileArchiveRenderer(tmp_path).render_tile(
            storage_key=KEY,
            archive_sha256=SHA,
            z=3,
            x=2,
            y=1,
        )


@pytest.mark.parametrize(
    ("image", "image_format"),
    [
        (png(512, 256), "png"),
        (png(), "jpg"),
        (b"not an image", "png"),
    ],
)
def test_invalid_image_or_dimensions_are_rejected(
    tmp_path,
    image,
    image_format,
) -> None:
    archive(tmp_path, image=image, image_format=image_format)

    with pytest.raises(InvalidLocalTileArchiveError):
        LocalTileArchiveRenderer(tmp_path).render_tile(
            storage_key=KEY,
            archive_sha256=SHA,
            z=3,
            x=2,
            y=1,
        )


def test_duplicate_coordinate_is_rejected(tmp_path) -> None:
    archive(tmp_path, image=png(), duplicate=True)

    with pytest.raises(InvalidLocalTileArchiveError):
        LocalTileArchiveRenderer(tmp_path).render_tile(
            storage_key=KEY,
            archive_sha256=SHA,
            z=3,
            x=2,
            y=1,
        )


def test_invalid_xyz_coordinate_is_rejected_before_sqlite(tmp_path) -> None:
    archive(tmp_path, image=png())

    with pytest.raises(ValueError):
        LocalTileArchiveRenderer(tmp_path).render_tile(
            storage_key=KEY,
            archive_sha256=SHA,
            z=3,
            x=8,
            y=1,
        )
