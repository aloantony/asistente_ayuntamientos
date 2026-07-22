import json
from pathlib import Path
import sqlite3
import struct
import zlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.geo_ingest import (
    GeoCommandResult,
    GeoDatabaseTarget,
    GeoIngestError,
    ingest_raster_artifact,
    ingest_vector_artifact,
    inspect_tile_archive,
    versioned_vector_table_name,
)


def _png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 256, 256, 8, 6, 0, 0, 0)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\x00\x00\x00\xff" * 256 for _ in range(256))
    return (
        signature
        + chunk(b"IHDR", ihdr_data)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _raster_info(*, cog: bool) -> bytes:
    return json.dumps(
        {
            "driverShortName": "GTiff",
            "size": [100, 200],
            "bands": [{"band": 1}],
            "coordinateSystem": {
                "wkt": (
                    'PROJCRS["ETRS89 / UTM zone 30N",'
                    'BASEGEOGCRS["ETRS89",ID["EPSG",4258]],'
                    'ID["EPSG",25830]]'
                )
            },
            "wgs84Extent": {
                "type": "Polygon",
                "coordinates": [
                    [[-7, 40], [-1, 40], [-1, 44], [-7, 44], [-7, 40]]
                ],
            },
            "metadata": {"IMAGE_STRUCTURE": {"LAYOUT": "COG" if cog else ""}},
        },
        separators=(",", ":"),
    ).encode()


def test_database_target_and_versioned_name_are_strict() -> None:
    target = GeoDatabaseTarget.from_url(
        "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
    )
    assert "secret" not in target.ogr_connection
    assert target.command_environment()["PGPASSWORD"] == "secret"
    assert versioned_vector_table_name(
        provider_key="siur",
        layer_id=12,
        run_id=34,
        input_sha256="a" * 64,
    ).startswith("m_")
    with pytest.raises(GeoIngestError):
        GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:secret@127.0.0.1/app?options=-csearch_path%3Dpublic"
        )


def test_vector_ingest_validates_table_and_installs_guards(db, tmp_path) -> None:
    source = Path(tmp_path, "source.geojson")
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=12,
        run_id=34,
        input_sha256="a" * 64,
    )

    def runner(argv, environment, timeout):
        assert argv[0] == "/usr/bin/ogr2ogr"
        assert "secret" not in " ".join(argv)
        assert environment["PGPASSWORD"] == "secret"
        assert timeout == 30
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data.{table_name} (
                    source_fid bigint PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857) NOT NULL
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data.{table_name} (source_fid, geom)
                VALUES (
                    1,
                    ST_Multi(ST_GeomFromText(
                        'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                        3857
                    ))
                )
                """
            )
        )
        return GeoCommandResult(b"", b"")

    result = ingest_vector_artifact(
        db,
        database=GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
        ),
        source_path=source,
        input_sha256="a" * 64,
        provider_key="siur",
        layer_id=12,
        run_id=34,
        timeout_seconds=30,
        runner=runner,
    )

    assert result.feature_count == 1
    assert result.crs == "EPSG:3857"
    assert result.storage_key == f"reference_data.{table_name}"
    assert result.validation_json["passed"] is True
    with pytest.raises(DBAPIError, match="immutable reference data table"):
        with db.begin_nested():
            db.execute(
                text(
                    f"INSERT INTO reference_data.{table_name} "
                    "(source_fid, geom) VALUES (2, NULL)"
                )
            )


def test_rejected_vector_import_drops_only_its_unpublished_table(db, tmp_path) -> None:
    source = Path(tmp_path, "source.geojson")
    source.write_bytes(b"{}")
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=90,
        run_id=91,
        input_sha256="b" * 64,
    )

    def runner(argv, environment, timeout):
        db.execute(
            text(
                f"CREATE TABLE reference_data.{table_name} "
                "(source_fid bigint, geom geometry(MultiPolygon, 3857))"
            )
        )
        return GeoCommandResult(b"", b"")

    with pytest.raises(GeoIngestError, match="feature count"):
        ingest_vector_artifact(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:app@127.0.0.1:5432/app"
            ),
            source_path=source,
            input_sha256="b" * 64,
            provider_key="siur",
            layer_id=90,
            run_id=91,
            runner=runner,
        )
    assert db.execute(
        text("SELECT to_regclass(:name)"),
        {"name": f"reference_data.{table_name}"},
    ).scalar_one() is None


def test_raster_ingest_creates_content_addressed_cog(tmp_path) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"source-raster")
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)

    def runner(argv, environment, timeout):
        if argv[0] == "/usr/bin/gdal_translate":
            Path(argv[-1]).write_bytes(b"normalized-cog")
            return GeoCommandResult(b"", b"")
        return GeoCommandResult(_raster_info(cog=Path(argv[-1]).name == "normalized.tif"), b"")

    result = ingest_raster_artifact(
        store,
        source_path=source,
        max_output_bytes=1024 * 1024,
        runner=runner,
    )

    assert result.inspection.is_cog is True
    assert result.inspection.crs == "EPSG:25830"
    assert store.resolve_blob(result.blob.storage_key).read_bytes() == b"normalized-cog"
    assert result.validation_json["passed"] is True
    store.close()


def _mbtiles(path: Path, *, duplicate: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    connection.execute(
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB)"
    )
    connection.executemany(
        "INSERT INTO metadata (name, value) VALUES (?, ?)",
        [
            ("format", "png"),
            ("minzoom", "0"),
            ("maxzoom", "0"),
            ("bounds", "-180,-85,180,85"),
        ],
    )
    connection.execute("INSERT INTO tiles VALUES (0, 0, 0, ?)", (_png(),))
    if duplicate:
        connection.execute("INSERT INTO tiles VALUES (0, 0, 0, ?)", (_png(),))
    connection.commit()
    connection.close()


def test_tile_archive_validation_checks_every_tile_and_coverage(tmp_path) -> None:
    archive = Path(tmp_path, "tiles.mbtiles")
    _mbtiles(archive)
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)
    with archive.open("rb") as stream:
        blob = store.put_stream(stream)

    result = inspect_tile_archive(
        store,
        storage_key=blob.storage_key,
        archive_sha256=blob.sha256,
        expected_tile_count=1,
    )

    assert result.tile_count == 1
    assert result.image_format == "png"
    assert result.validation_json["checks"]["all_images_valid"] is True
    with pytest.raises(GeoIngestError, match="coverage"):
        inspect_tile_archive(
            store,
            storage_key=blob.storage_key,
            archive_sha256=blob.sha256,
            expected_tile_count=2,
        )
    store.close()


def test_tile_archive_rejects_duplicate_coordinates(tmp_path) -> None:
    archive = Path(tmp_path, "duplicate.mbtiles")
    _mbtiles(archive, duplicate=True)
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)
    with archive.open("rb") as stream:
        blob = store.put_stream(stream)
    with pytest.raises(GeoIngestError, match="duplicate"):
        inspect_tile_archive(
            store,
            storage_key=blob.storage_key,
            archive_sha256=blob.sha256,
        )
    store.close()
