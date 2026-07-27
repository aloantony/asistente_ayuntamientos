import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import sys
import zlib
import zipfile

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.geo_ingest import (
    GeoCommandResult,
    GeoDatabaseTarget,
    GeoIngestError,
    ingest_raster_artifact,
    ingest_vector_artifact,
    ingest_vector_artifacts,
    inspect_tile_archive,
    run_geo_command,
    verify_vector_delivery_table,
    versioned_vector_table_name,
)
from app.reference_layers.geoserver_admin import VERSIONED_NAME


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


def _raster_info(
    *,
    cog: bool,
    size: tuple[int, int] = (100, 200),
    driver: str = "GTiff",
    band_types: tuple[str, ...] = ("Byte",),
    nodata_values: tuple[float | None, ...] | None = None,
    pixel_size: tuple[float, float] = (10.0, 10.0),
    overview_sizes: tuple[tuple[int, int], ...] = (),
) -> bytes:
    nodata_values = nodata_values or tuple(None for _ in band_types)
    bands = []
    for ordinal, (band_type, nodata) in enumerate(
        zip(band_types, nodata_values, strict=True),
        start=1,
    ):
        band = {
            "band": ordinal,
            "type": band_type,
            "overviews": [{"size": list(item)} for item in overview_sizes],
        }
        if nodata is not None:
            band["noDataValue"] = nodata
        bands.append(band)
    return json.dumps(
        {
            "driverShortName": driver,
            "size": list(size),
            "bands": bands,
            "geoTransform": [
                500_000.0,
                pixel_size[0],
                0.0,
                4_600_000.0,
                0.0,
                -pixel_size[1],
            ],
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


def _vector_manifest_sha256(
    path: Path,
    input_sha256: str,
    *,
    input_driver: str = "GeoJSON",
    input_layer: str | None = None,
    archive_member: str | None = None,
) -> str:
    artifact = {
        "ordinal": 0,
        "input_sha256": input_sha256,
        "input_driver": input_driver,
        "input_layer": input_layer,
        "size_bytes": path.stat().st_size,
    }
    if archive_member is not None:
        artifact["archive_member"] = archive_member
    return canonical_json_sha256(
        {
            "schema_version": "reference-vector-input-manifest-v1",
            "artifacts": [artifact],
        }
    )


def _reviewed_geopackage_zip(tmp_path: Path) -> tuple[Path, str]:
    member = "reviewed.gpkg"
    package = Path(tmp_path, member)
    geometry = (
        b"GP"
        + bytes((0, 3))
        + struct.pack("<i4d", 25830, 0.0, 10.0, 0.0, 10.0)
        + struct.pack("<BI", 1, 2)
        + struct.pack("<I", 2)
        + struct.pack("<4d", 0.0, 0.0, 10.0, 10.0)
    )
    with sqlite3.connect(package) as connection:
        connection.executescript(
            """
            PRAGMA application_id = 1196444487;
            CREATE TABLE gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            );
            CREATE TABLE gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL,
                min_x DOUBLE,
                min_y DOUBLE,
                max_x DOUBLE,
                max_y DOUBLE,
                srs_id INTEGER
            );
            CREATE TABLE gpkg_geometry_columns (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL,
                z TINYINT NOT NULL,
                m TINYINT NOT NULL,
                PRIMARY KEY (table_name, column_name)
            );
            CREATE TABLE reviewed (
                id INTEGER PRIMARY KEY,
                geometry MULTICURVE,
                label TEXT
            );
            INSERT INTO gpkg_spatial_ref_sys VALUES (
                'ETRS89 / UTM zone 30N',
                25830,
                'EPSG',
                25830,
                'EPSG:25830',
                ''
            );
            INSERT INTO gpkg_contents VALUES (
                'reviewed',
                'features',
                'reviewed',
                '',
                '2026-07-27T00:00:00.000Z',
                0,
                0,
                10,
                10,
                25830
            );
            INSERT INTO gpkg_geometry_columns VALUES (
                'reviewed',
                'geometry',
                'MULTICURVE',
                25830,
                0,
                0
            );
            """
        )
        connection.execute(
            "INSERT INTO reviewed (id, geometry, label) VALUES (?, ?, ?)",
            (1, geometry, "one"),
        )
    archive = Path(tmp_path, "reviewed.zip")
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as target:
        target.write(package, member)
    return archive, member


def test_database_target_and_versioned_name_are_strict() -> None:
    target = GeoDatabaseTarget.from_url(
        "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
    )
    assert "secret" not in target.ogr_connection
    assert target.command_environment()["PGPASSWORD"] == "secret"
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=12,
        run_id=34,
        input_sha256="a" * 64,
    )
    assert table_name.startswith("m_")
    assert "_v_" in table_name
    assert VERSIONED_NAME.fullmatch(table_name)
    assert VERSIONED_NAME.fullmatch(
        versioned_vector_table_name(
            provider_key="siur",
            layer_id=2**63 - 1,
            run_id=2**63 - 1,
            input_sha256="b" * 64,
        )
    )
    with pytest.raises(GeoIngestError):
        GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:secret@127.0.0.1/app?options=-csearch_path%3Dpublic"
        )


def test_geo_command_rejects_environment_injection_and_bounds_output(
    monkeypatch,
) -> None:
    from app.reference_layers import geo_ingest

    target = GeoDatabaseTarget.from_url(
        "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
    )
    environment = target.command_environment()
    environment["LD_PRELOAD"] = "/tmp/untrusted.so"
    with pytest.raises(GeoIngestError, match="environment is invalid"):
        run_geo_command(
            ["/usr/bin/gdalinfo", "--version"],
            environment,
            10,
        )

    monkeypatch.setitem(
        geo_ingest._ALLOWED_BINARIES,
        "gdalinfo",
        sys.executable,
    )
    with pytest.raises(GeoIngestError, match="output exceeded"):
        run_geo_command(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.write('x'*(8*1024*1024+1))",
            ],
            target.command_environment(),
            10,
        )


def test_vector_ingest_validates_table_and_installs_guards(db, tmp_path) -> None:
    source = Path(tmp_path, "source.geojson")
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_sha256 = _vector_manifest_sha256(source, source_sha256)
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=12,
        run_id=34,
        input_sha256=manifest_sha256,
    )

    def runner(argv, environment, timeout):
        assert argv[0] == "/usr/bin/ogr2ogr"
        assert argv[1:3] == ["-if", "GeoJSON"]
        assert "-makevalid" not in argv
        assert "secret" not in " ".join(argv)
        assert environment["PGPASSWORD"] == "secret"
        assert environment["PROJ_NETWORK"] == "OFF"
        assert environment["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"
        source_argument = Path(argv[-1])
        assert source_argument != source
        assert source_argument.read_bytes() == source.read_bytes()
        source.write_bytes(b"{}")
        assert source_argument.read_bytes() == b'{"type":"FeatureCollection","features":[]}'
        assert timeout == 30
        staging_key = argv[argv.index("-nln") + 1]
        assert staging_key.startswith(
            "reference_data_staging."
            f"s_v_{table_name.rsplit('_v_', 1)[1]}_"
        )
        staging_table = staging_key.split(".", 1)[1]
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data_staging.{staging_table} (
                    source_fid bigint PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857)
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{staging_table}
                    (source_fid, geom)
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
        input_sha256=source_sha256,
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
    verified = verify_vector_delivery_table(
        db,
        storage_key=result.storage_key,
        content_sha256=result.content_sha256,
        validation_json=result.validation_json,
        expected_feature_count=result.feature_count,
    )
    assert verified.content_sha256 == result.content_sha256
    assert verified.feature_count == result.feature_count
    with pytest.raises(DBAPIError, match="immutable reference data table"):
        with db.begin_nested():
            db.execute(
                text(
                    f"INSERT INTO reference_data.{table_name} "
                    "(source_fid, geom) VALUES (2, NULL)"
                )
            )


def test_vector_delivery_verification_fails_closed_when_relation_is_absent(
    db,
) -> None:
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=812,
        run_id=913,
        input_sha256="f" * 64,
    )

    with pytest.raises(
        GeoIngestError,
        match="absent or not a table",
    ):
        verify_vector_delivery_table(
            db,
            storage_key=f"reference_data.{table_name}",
            content_sha256="e" * 64,
            validation_json={
                "schema_version": "reference-delivery-validation/v1",
                "passed": True,
                "kind": "vector",
                "checks": {},
            },
            expected_feature_count=1,
        )


def test_vector_ingest_accepts_revalidated_geopackage(
    db,
    tmp_path,
) -> None:
    source = Path(tmp_path, "grid_100km_surf.gpkg")
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            PRAGMA application_id = 1196444487;
            CREATE TABLE gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            );
            CREATE TABLE gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL,
                min_x DOUBLE,
                min_y DOUBLE,
                max_x DOUBLE,
                max_y DOUBLE,
                srs_id INTEGER
            );
            CREATE TABLE gpkg_geometry_columns (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL,
                z TINYINT NOT NULL,
                m TINYINT NOT NULL,
                PRIMARY KEY (table_name, column_name)
            );
            CREATE TABLE grid_100km_surf (
                fid INTEGER PRIMARY KEY,
                geom BLOB
            );
            INSERT INTO gpkg_spatial_ref_sys VALUES (
                'ETRS89-extended / LAEA Europe',
                3035,
                'EPSG',
                3035,
                'EPSG:3035',
                ''
            );
            INSERT INTO gpkg_contents VALUES (
                'grid_100km_surf',
                'features',
                'grid_100km_surf',
                '',
                '2025-07-03T00:00:00.000Z',
                NULL,
                NULL,
                NULL,
                NULL,
                3035
            );
            INSERT INTO gpkg_geometry_columns VALUES (
                'grid_100km_surf',
                'geom',
                'POLYGON',
                3035,
                0,
                0
            );
            """
        )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    def runner(argv, environment, timeout):
        del environment, timeout
        assert argv[1:3] == ["-if", "GPKG"]
        assert argv[-1] == "grid_100km_surf"
        snapshot = Path(argv[-2])
        assert snapshot != source
        assert snapshot.read_bytes() == source.read_bytes()
        staging_table = argv[argv.index("-nln") + 1].split(".", 1)[1]
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data_staging.{staging_table} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857)
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{staging_table} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
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
        input_sha256=source_sha256,
        provider_key="siur",
        layer_id=100,
        run_id=101,
        input_layer="grid_100km_surf",
        runner=runner,
    )

    assert result.feature_count == 1
    assert result.validation_json["checks"]["input_manifest"] == [
        {
            "ordinal": 0,
            "input_sha256": source_sha256,
            "input_driver": "GPKG",
            "input_layer": "grid_100km_surf",
            "size_bytes": source.stat().st_size,
        }
    ]


def test_vector_ingest_accepts_one_reviewed_geopackage_zip_member(
    db,
    tmp_path,
) -> None:
    source, member = _reviewed_geopackage_zip(tmp_path)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_sha256 = _vector_manifest_sha256(
        source,
        source_sha256,
        input_driver="GPKGZIP",
        input_layer="reviewed",
        archive_member=member,
    )
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=104,
        run_id=105,
        input_sha256=manifest_sha256,
    )

    def runner(argv, environment, timeout):
        del environment, timeout
        assert argv[1:3] == ["-if", "GPKG"]
        assert argv[-1] == "reviewed"
        assert argv[-2].startswith("/vsizip/")
        assert argv[-2].endswith(f"/{member}")
        assert source.as_posix() not in argv[-2]
        staging_table = argv[argv.index("-nln") + 1].split(".", 1)[1]
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data_staging.{staging_table} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiLineString, 3857)
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{staging_table} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'LINESTRING(0 0,1000 1000)',
                    3857
                )))
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
        input_sha256=source_sha256,
        provider_key="siur",
        layer_id=104,
        run_id=105,
        input_layer="reviewed",
        input_driver="GPKGZIP",
        archive_member=member,
        runner=runner,
    )

    assert result.storage_key == f"reference_data.{table_name}"
    assert result.feature_count == 1
    assert result.validation_json["checks"]["input_manifest"] == [
        {
            "ordinal": 0,
            "input_sha256": source_sha256,
            "input_driver": "GPKGZIP",
            "input_layer": "reviewed",
            "archive_member": member,
            "size_bytes": source.stat().st_size,
        }
    ]


def test_vector_ingest_rejects_wrong_geopackage_zip_member_before_gdal(
    db,
    tmp_path,
) -> None:
    source, _member = _reviewed_geopackage_zip(tmp_path)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return GeoCommandResult(b"", b"")

    with pytest.raises(
        GeoIngestError,
        match="unique reviewed member",
    ):
        ingest_vector_artifact(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
            ),
            source_path=source,
            input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            provider_key="siur",
            layer_id=106,
            run_id=107,
            input_layer="reviewed",
            input_driver="GPKGZIP",
            archive_member="wrong.gpkg",
            runner=runner,
        )
    assert calls == 0


def test_vector_ingest_rejects_spoofed_geopackage_before_gdal(
    db,
    tmp_path,
) -> None:
    source = Path(tmp_path, "spoofed.gpkg")
    source.write_bytes(b"SQLite format 3\x00" + b"\x00" * 256)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return GeoCommandResult(b"", b"")

    with pytest.raises(
        GeoIngestError,
        match="GeoPackage could not be inspected",
    ):
        ingest_vector_artifact(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
            ),
            source_path=source,
            input_sha256=source_sha256,
            provider_key="siur",
            layer_id=102,
            run_id=103,
            runner=runner,
        )
    assert calls == 0


def test_rejected_vector_import_drops_only_its_unpublished_table(db, tmp_path) -> None:
    source = Path(tmp_path, "source.geojson")
    source.write_bytes(b"{}")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_sha256 = _vector_manifest_sha256(source, source_sha256)
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=90,
        run_id=91,
        input_sha256=manifest_sha256,
    )

    def runner(argv, environment, timeout):
        staging_table = argv[argv.index("-nln") + 1].split(".", 1)[1]
        db.execute(
            text(
                f"CREATE TABLE reference_data_staging.{staging_table} "
                "(source_fid bigint, geom geometry(MultiPolygon, 3857))"
            )
        )
        return GeoCommandResult(b"", b"")

    with pytest.raises(GeoIngestError, match="no usable geometry"):
        ingest_vector_artifact(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:app@127.0.0.1:5432/app"
            ),
            source_path=source,
            input_sha256=source_sha256,
            provider_key="siur",
            layer_id=90,
            run_id=91,
            runner=runner,
        )
    assert db.execute(
        text("SELECT to_regclass(:name)"),
        {"name": f"reference_data.{table_name}"},
    ).scalar_one() is None


def test_vector_ingest_appends_ordered_pages_with_regenerated_fids(db, tmp_path) -> None:
    pages = [
        Path(tmp_path, "page-1.geojson"),
        Path(tmp_path, "page-2.geojson"),
    ]
    pages[0].write_bytes(b'{"type":"FeatureCollection","features":[],"page":1}')
    pages[1].write_bytes(b'{"type":"FeatureCollection","features":[],"page":2}')
    digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in pages]
    imported_snapshots: list[bytes] = []

    def runner(argv, environment, timeout):
        storage_key = argv[argv.index("-nln") + 1]
        assert storage_key.startswith("reference_data_staging.")
        table_name = storage_key.split(".", 1)[1]
        imported_snapshots.append(Path(argv[-1]).read_bytes())
        assert "-unsetFid" in argv
        assert "-makevalid" not in argv
        if "-append" not in argv:
            assert "-update" not in argv
            db.execute(
                text(
                    f"""
                    CREATE TABLE reference_data_staging.{table_name} (
                        source_fid bigserial PRIMARY KEY,
                        geom geometry(MultiPolygon, 3857)
                    )
                    """
                )
            )
        else:
            assert "-update" in argv
            assert "-lco" not in argv
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{table_name} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        return GeoCommandResult(b"", b"")

    result = ingest_vector_artifacts(
        db,
        database=GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
        ),
        artifacts=[(pages[0], digests[0]), (pages[1], digests[1])],
        provider_key="siur",
        layer_id=501,
        run_id=502,
        runner=runner,
    )

    assert imported_snapshots == [path.read_bytes() for path in pages]
    assert result.feature_count == 2
    checks = result.validation_json["checks"]
    assert checks["artifact_count"] == 2
    assert checks["artifact_feature_counts"] == [1, 1]
    assert [item["input_sha256"] for item in checks["input_manifest"]] == digests
    assert checks["source_fids_regenerated"] is True


def test_vector_ingest_persists_quantified_pre_and_post_repair_evidence(
    db,
    tmp_path,
) -> None:
    pages = [
        Path(tmp_path, "geometry-page-1.geojson"),
        Path(tmp_path, "geometry-page-2.geojson"),
    ]
    for ordinal, path in enumerate(pages):
        path.write_bytes(
            (
                '{"type":"FeatureCollection","features":[],'
                f'"page":{ordinal + 1}}}'
            ).encode()
        )
    digests = [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in pages
    ]
    calls = 0

    stale_final = versioned_vector_table_name(
        provider_key="siur",
        layer_id=998,
        run_id=999,
        input_sha256="b" * 64,
    )
    stale_staging = (
        f"s_v_{stale_final.rsplit('_v_', 1)[1]}_{'a' * 16}"
    )
    db.execute(text("CREATE SCHEMA IF NOT EXISTS reference_data_staging"))
    db.execute(
        text(
            f"CREATE TABLE reference_data_staging.{stale_staging} "
            "(orphaned boolean)"
        )
    )
    db.commit()

    def runner(argv, environment, timeout):
        nonlocal calls
        del environment, timeout
        calls += 1
        assert "-makevalid" not in argv
        storage_key = argv[argv.index("-nln") + 1]
        assert storage_key.startswith("reference_data_staging.")
        staging_table = storage_key.split(".", 1)[1]
        if "-append" not in argv:
            db.execute(
                text(
                    f"""
                    CREATE TABLE reference_data_staging.{staging_table} (
                        source_fid bigserial PRIMARY KEY,
                        label text NOT NULL,
                        geom geometry(MultiPolygon, 3857)
                    )
                    """
                )
            )
            db.execute(
                text(
                    f"""
                    INSERT INTO reference_data_staging.{staging_table}
                        (label, geom)
                    VALUES
                        (
                            'multipart',
                            ST_GeomFromText(
                                'MULTIPOLYGON('
                                '((0 0,1000 0,1000 1000,0 1000,0 0)),'
                                '((2000 0,3000 0,3000 1000,2000 1000,2000 0))'
                                ')',
                                3857
                            )
                        ),
                        (
                            'invalid-bow-tie',
                            ST_Multi(ST_GeomFromText(
                                'POLYGON(('
                                '4000 0,5000 1000,4000 1000,5000 0,4000 0'
                                '))',
                                3857
                            ))
                        ),
                        ('null', NULL),
                        (
                            'empty',
                            ST_GeomFromText('MULTIPOLYGON EMPTY', 3857)
                        )
                    """
                )
            )
        else:
            db.execute(
                text(
                    f"""
                    INSERT INTO reference_data_staging.{staging_table}
                        (label, geom)
                    VALUES (
                        'second-page',
                        ST_Multi(ST_GeomFromText(
                            'POLYGON(('
                            '6000 0,7000 0,7000 1000,6000 1000,6000 0'
                            '))',
                            3857
                        ))
                    )
                    """
                )
            )
        return GeoCommandResult(b"", b"")

    arguments = {
        "database": GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:app@127.0.0.1:5432/app"
        ),
        "artifacts": list(zip(pages, digests, strict=True)),
        "provider_key": "siur",
        "layer_id": 901,
        "run_id": 902,
        "runner": runner,
    }
    first = ingest_vector_artifacts(db, **arguments)
    checks = first.validation_json["checks"]
    evidence = checks["geometry_evidence"]
    source = evidence["source"]
    normalization = evidence["normalization"]
    result = evidence["result"]

    assert calls == 2
    assert checks["geometry_evidence_sha256"] == canonical_json_sha256(
        evidence
    )
    assert source["total"] == {
        "feature_count": 5,
        "source_fid_count": 5,
        "max_source_fid": 5,
        "null_geometry_count": 1,
        "empty_geometry_count": 1,
        "invalid_geometry_count": 1,
        "valid_geometry_count": 2,
        "multi_geometry_count": 3,
        "multiple_part_geometry_count": 1,
        "geometry_part_count": 4,
        "srid_count": 1,
        "srid": 3857,
        "geometry_types": ["MULTIPOLYGON"],
    }
    assert [
        {
            key: artifact[key]
            for key in (
                "feature_count",
                "null_geometry_count",
                "empty_geometry_count",
                "invalid_geometry_count",
            )
        }
        for artifact in source["artifacts"]
    ] == [
        {
            "feature_count": 4,
            "null_geometry_count": 1,
            "empty_geometry_count": 1,
            "invalid_geometry_count": 1,
        },
        {
            "feature_count": 1,
            "null_geometry_count": 0,
            "empty_geometry_count": 0,
            "invalid_geometry_count": 0,
        },
    ]
    assert normalization == {
        "algorithm": (
            "postgis-st-makevalid-collectionextract-promote-to-multi/v1"
        ),
        "target_crs": "EPSG:3857",
        "geometry_family": "polygon",
        "target_geometry_type": "MULTIPOLYGON",
        "repaired_geometry_count": 1,
        "retained_valid_geometry_count": 2,
        "discarded_null_geometry_count": 1,
        "discarded_empty_geometry_count": 1,
        "discarded_unrepairable_geometry_count": 0,
        "discarded_geometry_count": 2,
        "result_feature_count": 3,
    }
    assert result["feature_count"] == 3
    assert result["source_fid_count"] == 3
    assert result["null_geometry_count"] == 0
    assert result["empty_geometry_count"] == 0
    assert result["invalid_geometry_count"] == 0
    assert result["valid_geometry_count"] == 3
    assert result["geometry_type"] == "MULTIPOLYGON"
    assert result["geometry_family"] == "polygon"
    assert result["srid"] == 3857
    assert result["multiple_part_geometry_count"] == 2

    columns = db.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'reference_data'
              AND table_name = :table_name
            ORDER BY ordinal_position
            """
        ),
        {"table_name": first.table_name},
    ).scalars().all()
    assert columns == ["source_fid", "label", "geom"]
    assert db.execute(
        text(
            f"SELECT label FROM {first.storage_key} "
            "ORDER BY source_fid"
        )
    ).scalars().all() == ["multipart", "invalid-bow-tie", "second-page"]
    table_attestation = json.loads(
        db.execute(
            text(
                "SELECT obj_description(to_regclass(:key), 'pg_class')"
            ),
            {"key": first.storage_key},
        ).scalar_one()
    )
    attestation_core = {
        key: value
        for key, value in table_attestation.items()
        if key != "attestation_sha256"
    }
    assert table_attestation["attestation_sha256"] == canonical_json_sha256(
        attestation_core
    )
    assert table_attestation["content_sha256"] == first.content_sha256
    assert table_attestation["geometry_evidence_sha256"] == checks[
        "geometry_evidence_sha256"
    ]
    assert table_attestation["validation_sha256"] == canonical_json_sha256(
        first.validation_json
    )
    assert db.execute(
        text(
            "SELECT to_regclass(:key)"
        ),
        {"key": f"reference_data_staging.{stale_staging}"},
    ).scalar_one() is None
    assert db.execute(
        text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'reference_data_staging'
              AND relation.relkind IN ('r', 'p')
            """
        )
    ).scalar_one() == 0

    second = ingest_vector_artifacts(db, **arguments)
    assert calls == 2
    assert second.content_sha256 == first.content_sha256
    assert second.validation_json == first.validation_json
    assert second.validation_json["checks"]["artifact_feature_counts"] == [4, 1]


def test_vector_ingest_with_real_ogr_uses_isolated_staging_and_repairs(
    engine,
    tmp_path,
) -> None:
    if not Path("/usr/bin/ogr2ogr").is_file():
        pytest.skip("requires the current GDAL-enabled backend image")
    source = Path(tmp_path, "real-ogr.geojson")
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"label": "valid-multipart"},
                        "geometry": {
                            "type": "MultiPolygon",
                            "coordinates": [
                                [
                                    [
                                        [-4.0, 42.0],
                                        [-3.99, 42.0],
                                        [-3.99, 42.01],
                                        [-4.0, 42.01],
                                        [-4.0, 42.0],
                                    ]
                                ],
                                [
                                    [
                                        [-3.98, 42.0],
                                        [-3.97, 42.0],
                                        [-3.97, 42.01],
                                        [-3.98, 42.01],
                                        [-3.98, 42.0],
                                    ]
                                ],
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"label": "invalid-bow-tie"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-3.96, 42.0],
                                    [-3.95, 42.01],
                                    [-3.96, 42.01],
                                    [-3.95, 42.0],
                                    [-3.96, 42.0],
                                ]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"label": "null"},
                        "geometry": None,
                    },
                    {
                        "type": "Feature",
                        "properties": {"label": "empty"},
                        "geometry": {
                            "type": "MultiPolygon",
                            "coordinates": [],
                        },
                    },
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    expected_table = versioned_vector_table_name(
        provider_key="siur",
        layer_id=951,
        run_id=952,
        input_sha256=_vector_manifest_sha256(source, source_sha256),
    )
    staging_prefix = f"s_v_{expected_table.rsplit('_v_', 1)[1]}_"
    database_url = engine.url.render_as_string(hide_password=False)
    result = None
    with Session(engine) as real_db:
        try:
            result = ingest_vector_artifact(
                real_db,
                database=GeoDatabaseTarget.from_url(database_url),
                source_path=source,
                input_sha256=source_sha256,
                provider_key="siur",
                layer_id=951,
                run_id=952,
            )
            evidence = result.validation_json["checks"][
                "geometry_evidence"
            ]
            source_total = evidence["source"]["total"]
            normalization = evidence["normalization"]
            assert source_total["feature_count"] == 4
            assert (
                source_total["null_geometry_count"]
                + source_total["empty_geometry_count"]
                == 2
            )
            assert source_total["invalid_geometry_count"] == 1
            assert normalization["repaired_geometry_count"] == 1
            assert normalization["discarded_geometry_count"] == 2
            assert result.feature_count == 2
            assert real_db.execute(
                text(
                    f"SELECT count(*) FROM {result.storage_key} "
                    "WHERE geom IS NULL OR ST_IsEmpty(geom) "
                    "OR NOT ST_IsValid(geom)"
                )
            ).scalar_one() == 0
            assert real_db.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'reference_data_staging'
                      AND relation.relkind IN ('r', 'p')
                    """
                )
            ).scalar_one() == 0
        finally:
            real_db.rollback()
            staging_tables = real_db.execute(
                text(
                    """
                    SELECT relation.relname
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'reference_data_staging'
                      AND relation.relkind IN ('r', 'p')
                      AND relation.relname LIKE :prefix
                    """
                ),
                {"prefix": f"{staging_prefix}%"},
            ).scalars().all()
            for staging_table in staging_tables:
                assert staging_table.startswith(staging_prefix)
                real_db.execute(
                    text(
                        "DROP TABLE IF EXISTS "
                        f"reference_data_staging.{staging_table}"
                    )
                )
            real_db.execute(
                text(
                    "DROP TABLE IF EXISTS "
                    f"reference_data.{expected_table}"
                )
            )
            real_db.commit()


def test_cadastral_gml_zip_ingest_appends_in_order_with_unique_fids(
    db,
    tmp_path,
) -> None:
    archives = [
        Path(tmp_path, "05001.zip"),
        Path(tmp_path, "09001.zip"),
    ]
    members = [
        "A.ES.SDGC.CP.05001.cadastralparcel.gml",
        "A.ES.SDGC.CP.09001.cadastralparcel.gml",
    ]
    for archive_path, member in zip(archives, members, strict=True):
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                member,
                b"""<wfs:FeatureCollection
 xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:cp="http://inspire.ec.europa.eu/schemas/cp/4.0">
 <wfs:member><cp:CadastralParcel /></wfs:member>
</wfs:FeatureCollection>""",
            )
    digests = [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in archives
    ]
    imported_members: list[str] = []

    def runner(argv, environment, timeout):
        del environment, timeout
        assert argv[1:3] == ["-if", "GML"]
        assert "-unsetFid" in argv
        source_argument = next(
            item for item in argv if item.startswith("/vsizip/")
        )
        imported_members.append(source_argument.rsplit("/", 1)[-1])
        storage_key = argv[argv.index("-nln") + 1]
        assert storage_key.startswith("reference_data_staging.")
        table_name = storage_key.split(".", 1)[1]
        if "-append" not in argv:
            db.execute(
                text(
                    f"""
                    CREATE TABLE reference_data_staging.{table_name} (
                        source_fid bigserial PRIMARY KEY,
                        geom geometry(MultiPolygon, 3857)
                    )
                    """
                )
            )
        else:
            assert "-update" in argv
            assert "-lco" not in argv
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{table_name} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        return GeoCommandResult(b"", b"")

    result = ingest_vector_artifacts(
        db,
        database=GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
        ),
        artifacts=[
            (archives[0], digests[0], "CadastralParcel"),
            (archives[1], digests[1], "CadastralParcel"),
        ],
        provider_key="siur",
        layer_id=511,
        run_id=512,
        input_driver="GMLZIP",
        runner=runner,
    )

    assert imported_members == members
    assert result.feature_count == 2
    checks = result.validation_json["checks"]
    assert checks["artifact_feature_counts"] == [1, 1]
    assert checks["source_fids_regenerated"] is True
    assert {
        item["input_driver"] for item in checks["input_manifest"]
    } == {"GMLZIP"}


def test_cadastral_gml_zip_is_revalidated_before_gdal(db, tmp_path) -> None:
    archive_path = Path(tmp_path, "unsafe.zip")
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "A.ES.SDGC.CP.05001.cadastralparcel.gml",
            b"<!DOCTYPE unsafe><FeatureCollection><CadastralParcel/>",
        )
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return GeoCommandResult(b"", b"")

    with pytest.raises(GeoIngestError, match="member is invalid"):
        ingest_vector_artifact(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
            ),
            source_path=archive_path,
            input_sha256=digest,
            provider_key="siur",
            layer_id=521,
            run_id=522,
            input_layer="CadastralParcel",
            input_driver="GMLZIP",
            runner=runner,
        )
    assert calls == 0


def test_vector_multipage_failure_drops_the_whole_unpublished_table(db, tmp_path) -> None:
    pages = [Path(tmp_path, "page-a.geojson"), Path(tmp_path, "page-b.geojson")]
    for index, path in enumerate(pages):
        path.write_bytes(
            f'{{"type":"FeatureCollection","features":[],"page":{index}}}'.encode()
        )
    artifacts = [
        (path, hashlib.sha256(path.read_bytes()).hexdigest()) for path in pages
    ]
    imported_table: str | None = None
    final_table = versioned_vector_table_name(
        provider_key="siur",
        layer_id=601,
        run_id=602,
        input_sha256=canonical_json_sha256(
            {
                "schema_version": "reference-vector-input-manifest-v1",
                "artifacts": [
                    {
                        "ordinal": ordinal,
                        "input_sha256": digest,
                        "input_driver": "GeoJSON",
                        "input_layer": None,
                        "size_bytes": path.stat().st_size,
                    }
                    for ordinal, (path, digest) in enumerate(artifacts)
                ],
            }
        ),
    )
    calls = 0

    def runner(argv, environment, timeout):
        nonlocal imported_table, calls
        calls += 1
        storage_key = argv[argv.index("-nln") + 1]
        imported_table = storage_key.split(".", 1)[1]
        if calls == 2:
            raise GeoIngestError("simulated append rejection")
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data_staging.{imported_table} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857)
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{imported_table} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        return GeoCommandResult(b"", b"")

    with pytest.raises(GeoIngestError, match="simulated append"):
        ingest_vector_artifacts(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:app@127.0.0.1:5432/app"
            ),
            artifacts=artifacts,
            provider_key="siur",
            layer_id=601,
            run_id=602,
            runner=runner,
        )
    assert calls == 2
    assert imported_table is not None
    assert db.execute(
        text("SELECT to_regclass(:name)"),
        {"name": f"reference_data_staging.{imported_table}"},
    ).scalar_one() is None
    assert db.execute(
        text("SELECT to_regclass(:name)"),
        {"name": f"reference_data.{final_table}"},
    ).scalar_one() is None


def test_vector_failure_after_normalization_cleans_final_and_staging(
    db,
    tmp_path,
    monkeypatch,
) -> None:
    from app.reference_layers import geo_ingest

    source = Path(tmp_path, "normalize-then-fail.geojson")
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_sha256 = _vector_manifest_sha256(source, source_sha256)
    final_table = versioned_vector_table_name(
        provider_key="siur",
        layer_id=611,
        run_id=612,
        input_sha256=manifest_sha256,
    )
    staging_table: str | None = None

    def runner(argv, environment, timeout):
        nonlocal staging_table
        del environment, timeout
        staging_table = argv[argv.index("-nln") + 1].split(".", 1)[1]
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data_staging.{staging_table} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857)
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{staging_table} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        return GeoCommandResult(b"", b"")

    def reject_attestation(*_args, **_kwargs):
        raise GeoIngestError("simulated attestation failure")

    monkeypatch.setattr(
        geo_ingest,
        "_persist_vector_table_attestation",
        reject_attestation,
    )
    with pytest.raises(GeoIngestError, match="attestation failure"):
        ingest_vector_artifact(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:app@127.0.0.1:5432/app"
            ),
            source_path=source,
            input_sha256=source_sha256,
            provider_key="siur",
            layer_id=611,
            run_id=612,
            runner=runner,
        )

    assert staging_table is not None
    assert db.execute(
        text("SELECT to_regclass(:name)"),
        {"name": f"reference_data_staging.{staging_table}"},
    ).scalar_one() is None
    assert db.execute(
        text("SELECT to_regclass(:name)"),
        {"name": f"reference_data.{final_table}"},
    ).scalar_one() is None


def test_vector_ingest_revalidates_identical_immutable_table_on_retry(db, tmp_path) -> None:
    source = Path(tmp_path, "retry.geojson")
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = 0

    def runner(argv, environment, timeout):
        nonlocal calls
        calls += 1
        storage_key = argv[argv.index("-nln") + 1]
        table_name = storage_key.split(".", 1)[1]
        assert storage_key.startswith("reference_data_staging.")
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data_staging.{table_name} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857)
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{table_name} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        return GeoCommandResult(b"", b"")

    arguments = {
        "database": GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:app@127.0.0.1:5432/app"
        ),
        "source_path": source,
        "input_sha256": source_sha256,
        "provider_key": "siur",
        "layer_id": 701,
        "run_id": 702,
        "runner": runner,
    }
    first = ingest_vector_artifact(db, **arguments)
    second = ingest_vector_artifact(db, **arguments)

    assert calls == 1
    assert second.storage_key == first.storage_key
    assert second.content_sha256 == first.content_sha256
    assert second.validation_json == first.validation_json
    assert second.validation_json["checks"]["idempotent_reuse"] is True
    assert second.validation_json["checks"]["artifact_feature_counts"] == [1]


def test_vector_reuse_rejects_tampered_persisted_attestation(db, tmp_path) -> None:
    source = Path(tmp_path, "tampered-attestation.geojson")
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = 0

    def runner(argv, environment, timeout):
        nonlocal calls
        del environment, timeout
        calls += 1
        staging_table = argv[argv.index("-nln") + 1].split(".", 1)[1]
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data_staging.{staging_table} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857)
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{staging_table} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        return GeoCommandResult(b"", b"")

    arguments = {
        "database": GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:app@127.0.0.1:5432/app"
        ),
        "source_path": source,
        "input_sha256": source_sha256,
        "provider_key": "siur",
        "layer_id": 711,
        "run_id": 712,
        "runner": runner,
    }
    first = ingest_vector_artifact(db, **arguments)
    db.connection().exec_driver_sql(
        f"COMMENT ON TABLE {first.storage_key} "
        "IS '{\"schema_version\":\"tampered\"}'"
    )
    db.commit()

    with pytest.raises(GeoIngestError, match="attestation is malformed"):
        ingest_vector_artifact(db, **arguments)
    assert calls == 1
    assert db.execute(
        text("SELECT to_regclass(:name)"),
        {"name": first.storage_key},
    ).scalar_one() == first.storage_key


def test_vector_ingest_rebuilds_partial_table_without_immutable_guards(db, tmp_path) -> None:
    source = Path(tmp_path, "resume.geojson")
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_sha256 = _vector_manifest_sha256(source, source_sha256)
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=801,
        run_id=802,
        input_sha256=manifest_sha256,
    )
    db.execute(text("CREATE SCHEMA IF NOT EXISTS reference_data"))
    db.execute(
        text(
            f"CREATE TABLE reference_data.{table_name} ("
            "source_fid bigserial PRIMARY KEY, "
            "geom geometry(MultiPolygon, 3857) NOT NULL)"
        )
    )
    db.commit()
    calls = 0

    def runner(argv, environment, timeout):
        nonlocal calls
        calls += 1
        staging_key = argv[argv.index("-nln") + 1]
        staging_table = staging_key.split(".", 1)[1]
        assert staging_key.startswith("reference_data_staging.")
        assert db.execute(
            text("SELECT to_regclass(:name)"),
            {"name": f"reference_data.{table_name}"},
        ).scalar_one() is None
        db.execute(
            text(
                f"CREATE TABLE reference_data_staging.{staging_table} ("
                "source_fid bigserial PRIMARY KEY, "
                "geom geometry(MultiPolygon, 3857))"
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data_staging.{staging_table} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        return GeoCommandResult(b"", b"")

    result = ingest_vector_artifact(
        db,
        database=GeoDatabaseTarget.from_url(
            "postgresql+psycopg://app:app@127.0.0.1:5432/app"
        ),
        source_path=source,
        input_sha256=source_sha256,
        provider_key="siur",
        layer_id=801,
        run_id=802,
        runner=runner,
    )

    assert calls == 1
    assert result.table_name == table_name
    assert result.validation_json["checks"]["idempotent_reuse"] is True


def test_raster_ingest_creates_content_addressed_cog(tmp_path) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"II*\x00source-raster")
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)

    def runner(argv, environment, timeout):
        if argv[0] == "/usr/bin/gdal_translate":
            assert Path(argv[-1]).is_relative_to(store.root / "staging")
            assert environment["GDAL_PAM_ENABLED"] == "NO"
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
    assert result.inspection.band_types == ("Byte",)
    assert result.inspection.nodata_values == (None,)
    assert result.inspection.pixel_size_x == 10.0
    assert result.inspection.pixel_size_y == 10.0
    assert result.inspection.overview_sizes == ()
    assert store.resolve_blob(result.blob.storage_key).read_bytes() == b"normalized-cog"
    assert result.validation_json["passed"] is True
    assert result.validation_json["checks"]["nodata_preserved"] is True
    assert result.validation_json["checks"]["resolution_preserved"] is True
    assert result.validation_json["checks"]["overviews_validated"] is True
    store.close()


def test_geotiff_zip_ingest_uses_only_the_unique_archived_raster(
    tmp_path,
) -> None:
    source = Path(tmp_path, "erosion.zip")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "EroPotNiveles_41.tiff",
            b"II+\x00" + b"\x00" * 124,
        )
        archive.writestr(
            "EroPotNiveles_41.tiff.aux.xml",
            b"<PAMDataset/>",
        )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=1024 * 1024,
    )
    inspected_sources: list[str] = []

    def runner(argv, environment, timeout):
        del timeout
        if argv[0] == "/usr/bin/gdal_translate":
            assert argv[-2].startswith("/vsizip/")
            assert argv[-2].endswith("/EroPotNiveles_41.tiff")
            assert environment["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"
            Path(argv[-1]).write_bytes(b"normalized-cog")
            return GeoCommandResult(b"", b"")
        inspected_sources.append(argv[-1])
        return GeoCommandResult(
            _raster_info(
                cog=Path(argv[-1]).name == "normalized.tif",
            ),
            b"",
        )

    result = ingest_raster_artifact(
        store,
        source_path=source,
        input_sha256=source_sha256,
        input_driver="GTiffZIP",
        max_output_bytes=1024 * 1024,
        runner=runner,
    )

    assert inspected_sources[0].startswith("/vsizip/")
    assert inspected_sources[0].endswith("/EroPotNiveles_41.tiff")
    assert result.validation_json["checks"]["input_driver"] == "GTiffZIP"
    assert result.validation_json["checks"]["input_sha256"] == source_sha256
    store.close()


def test_geotiff_zip_rejects_ambiguous_rasters_before_gdal(tmp_path) -> None:
    source = Path(tmp_path, "ambiguous.zip")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("one.tif", b"II*\x00" + b"\x00" * 124)
        archive.writestr("two.tiff", b"MM\x00*" + b"\x00" * 124)
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=1024 * 1024,
    )
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        return GeoCommandResult(b"", b"")

    with pytest.raises(GeoIngestError, match="no unique raster member"):
        ingest_raster_artifact(
            store,
            source_path=source,
            input_driver="GTiffZIP",
            max_output_bytes=1024 * 1024,
            runner=runner,
        )
    assert calls == 0
    store.close()


def test_raster_ingest_validates_band_nodata_resolution_and_overviews(
    tmp_path,
) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"II*\x00source-raster")
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=64 * 1024 * 1024,
    )

    def runner(argv, environment, timeout):
        del environment, timeout
        if argv[0] == "/usr/bin/gdal_translate":
            Path(argv[-1]).write_bytes(b"normalized-cog")
            return GeoCommandResult(b"", b"")
        normalized = Path(argv[-1]).name == "normalized.tif"
        return GeoCommandResult(
            _raster_info(
                cog=normalized,
                size=(2048, 1024),
                band_types=("UInt16", "UInt16"),
                nodata_values=(-9999, -9999),
                pixel_size=(5.0, 5.0),
                overview_sizes=(
                    ((1024, 512), (512, 256)) if normalized else ()
                ),
            ),
            b"",
        )

    result = ingest_raster_artifact(
        store,
        source_path=source,
        max_output_bytes=64 * 1024 * 1024,
        runner=runner,
    )

    assert result.inspection.band_types == ("UInt16", "UInt16")
    assert result.inspection.nodata_values == (-9999.0, -9999.0)
    assert result.inspection.overview_sizes == ((1024, 512), (512, 256))
    assert result.validation_json["checks"]["overview_count"] == 2
    store.close()


@pytest.mark.parametrize(
    ("normalized_nodata", "normalized_pixel_size"),
    [
        ((None,), (10.0, 10.0)),
        ((-9999,), (20.0, 10.0)),
    ],
)
def test_raster_ingest_rejects_nodata_or_resolution_drift(
    tmp_path,
    normalized_nodata,
    normalized_pixel_size,
) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"II*\x00source-raster")
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=1024 * 1024,
    )

    def runner(argv, environment, timeout):
        del environment, timeout
        if argv[0] == "/usr/bin/gdal_translate":
            Path(argv[-1]).write_bytes(b"normalized-cog")
            return GeoCommandResult(b"", b"")
        normalized = Path(argv[-1]).name == "normalized.tif"
        return GeoCommandResult(
            _raster_info(
                cog=normalized,
                nodata_values=(
                    normalized_nodata if normalized else (-9999,)
                ),
                pixel_size=(
                    normalized_pixel_size if normalized else (10.0, 10.0)
                ),
            ),
            b"",
        )

    with pytest.raises(GeoIngestError, match="changed raster semantics"):
        ingest_raster_artifact(
            store,
            source_path=source,
            max_output_bytes=1024 * 1024,
            runner=runner,
        )
    store.close()


def test_large_normalized_raster_requires_internal_overviews(tmp_path) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"II*\x00source-raster")
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=16 * 1024 * 1024,
    )

    def runner(argv, environment, timeout):
        del environment, timeout
        if argv[0] == "/usr/bin/gdal_translate":
            Path(argv[-1]).write_bytes(b"normalized-cog")
            return GeoCommandResult(b"", b"")
        return GeoCommandResult(
            _raster_info(
                cog=Path(argv[-1]).name == "normalized.tif",
                size=(1024, 1024),
            ),
            b"",
        )

    with pytest.raises(GeoIngestError, match="missing internal overviews"):
        ingest_raster_artifact(
            store,
            source_path=source,
            max_output_bytes=16 * 1024 * 1024,
            runner=runner,
        )
    store.close()


def _coordinate_sha256(coordinates: list[tuple[int, int, int]]) -> str:
    digest = hashlib.sha256()
    for zoom, column, xyz_row in sorted(coordinates):
        digest.update(f"{zoom}/{column}/{xyz_row}\n".encode("ascii"))
    return digest.hexdigest()


def _mbtiles(
    path: Path,
    *,
    unique_index: bool = True,
    body: bytes | None = None,
    coordinate_metadata: str | None = None,
    coordinates: list[tuple[int, int, int]] | None = None,
) -> str:
    coordinates = coordinates or [(0, 0, 0)]
    coordinate_sha256 = coordinate_metadata or _coordinate_sha256(coordinates)
    min_zoom = min(item[0] for item in coordinates)
    max_zoom = max(item[0] for item in coordinates)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    if unique_index:
        connection.execute(
            "CREATE TABLE tiles ("
            "zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL, "
            "tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL, "
            "PRIMARY KEY (zoom_level, tile_column, tile_row)"
            ") WITHOUT ROWID"
        )
    else:
        connection.execute(
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
            "tile_row INTEGER, tile_data BLOB)"
        )
    connection.executemany(
        "INSERT INTO metadata (name, value) VALUES (?, ?)",
        [
            ("name", "Test reference tiles"),
            ("type", "overlay"),
            ("version", "1.3"),
            ("description", "Validated local reference tiles"),
            ("format", "png"),
            ("minzoom", str(min_zoom)),
            ("maxzoom", str(max_zoom)),
            ("bounds", "-180,-85,180,85"),
            ("scheme", "tms"),
            ("coordinate_sha256", coordinate_sha256),
            ("coordinate_hash_schema", "xyz-z-x-y-newline-v1"),
            ("source_definition_sha256", "a" * 64),
        ],
    )
    for zoom, column, xyz_row in reversed(coordinates):
        tms_row = (2**zoom - 1) - xyz_row
        connection.execute(
            "INSERT INTO tiles VALUES (?, ?, ?, ?)",
            (zoom, column, tms_row, body or _png()),
        )
    if unique_index:
        connection.execute(
            "CREATE UNIQUE INDEX tile_index "
            "ON tiles (zoom_level, tile_column, tile_row)"
        )
    connection.commit()
    connection.close()
    return coordinate_sha256


def test_tile_archive_validation_checks_every_tile_and_coverage(tmp_path) -> None:
    archive = Path(tmp_path, "tiles.mbtiles")
    coordinate_sha256 = _mbtiles(archive)
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)
    with archive.open("rb") as stream:
        blob = store.put_stream(stream)

    result = inspect_tile_archive(
        store,
        storage_key=blob.storage_key,
        archive_sha256=blob.sha256,
        expected_tile_count=1,
        expected_coordinate_sha256=coordinate_sha256,
    )

    assert result.tile_count == 1
    assert result.image_format == "png"
    assert result.coordinate_sha256 == coordinate_sha256
    assert result.validation_json["checks"]["all_images_valid"] is True
    with pytest.raises(GeoIngestError, match="coverage"):
        inspect_tile_archive(
            store,
            storage_key=blob.storage_key,
            archive_sha256=blob.sha256,
            expected_tile_count=2,
        )
    store.close()


def test_tile_archive_requires_canonical_unique_coordinate_index(tmp_path) -> None:
    archive = Path(tmp_path, "unindexed.mbtiles")
    _mbtiles(archive, unique_index=False)
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)
    with archive.open("rb") as stream:
        blob = store.put_stream(stream)
    with pytest.raises(GeoIngestError, match="canonical unique index"):
        inspect_tile_archive(
            store,
            storage_key=blob.storage_key,
            archive_sha256=blob.sha256,
        )
    store.close()


def test_vector_ingest_rejects_digest_mismatch_before_database_or_gdal(
    db,
    tmp_path,
) -> None:
    source = Path(tmp_path, "source.geojson")
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    calls = 0

    def runner(argv, environment, timeout):
        nonlocal calls
        calls += 1
        return GeoCommandResult(b"", b"")

    with pytest.raises(GeoIngestError, match="do not match"):
        ingest_vector_artifact(
            db,
            database=GeoDatabaseTarget.from_url(
                "postgresql+psycopg://app:app@127.0.0.1:5432/app"
            ),
            source_path=source,
            input_sha256="f" * 64,
            provider_key="siur",
            layer_id=301,
            run_id=302,
            runner=runner,
        )
    assert calls == 0


def test_raster_ingest_rejects_non_allowlisted_or_spoofed_driver(tmp_path) -> None:
    source = Path(tmp_path, "source.png")
    source.write_bytes(_png())
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)
    with pytest.raises(GeoIngestError, match="not allowlisted"):
        ingest_raster_artifact(store, source_path=source)
    with pytest.raises(GeoIngestError, match="does not match GeoTIFF"):
        ingest_raster_artifact(
            store,
            source_path=source,
            input_driver="GTiff",
            max_output_bytes=1024 * 1024,
        )
    store.close()


def test_raster_preflight_rejects_pixel_bombs_before_translation(tmp_path) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"II*\x00source-raster")
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=1024 * 1024 * 1024,
    )
    calls: list[str] = []

    def runner(argv, environment, timeout):
        calls.append(argv[0])
        return GeoCommandResult(
            _raster_info(cog=False, size=(500_000, 500_000)),
            b"",
        )

    with pytest.raises(GeoIngestError, match="dimensions, bands or driver"):
        ingest_raster_artifact(
            store,
            source_path=source,
            max_output_bytes=1024 * 1024 * 1024,
            runner=runner,
        )
    assert calls == ["/usr/bin/gdalinfo"]
    store.close()


def test_reviewed_ines_pixel_limit_accepts_real_raster_dimensions(
    tmp_path,
) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"II*\x00source-raster")
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=10 * 1024 * 1024 * 1024,
    )

    def runner(argv, environment, timeout):
        del argv, environment, timeout
        return GeoCommandResult(
            _raster_info(
                cog=False,
                size=(45_644, 34_891),
                band_types=("Int32",),
                nodata_values=(2_147_483_647,),
                pixel_size=(25.0, 25.0),
            ),
            b"",
        )

    with pytest.raises(GeoIngestError, match="dimensions, bands or driver"):
        ingest_raster_artifact(
            store,
            source_path=source,
            max_output_bytes=1024 * 1024 * 1024,
            runner=runner,
        )
    with pytest.raises(GeoIngestError, match="worst-case normalized size"):
        ingest_raster_artifact(
            store,
            source_path=source,
            max_pixels=1_600_000_000,
            max_output_bytes=1024 * 1024 * 1024,
            runner=runner,
        )
    store.close()


def test_raster_preflight_reserves_two_staging_copies_against_quota(tmp_path) -> None:
    source = Path(tmp_path, "source.tif")
    source.write_bytes(b"II*\x00source-raster")
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=600_000,
        quota_bytes=600_000,
    )
    calls: list[str] = []

    def runner(argv, environment, timeout):
        calls.append(argv[0])
        return GeoCommandResult(
            _raster_info(cog=False, size=(400, 400)),
            b"",
        )

    with pytest.raises(GeoIngestError, match="storage cannot stage"):
        ingest_raster_artifact(
            store,
            source_path=source,
            max_output_bytes=600_000,
            runner=runner,
        )
    assert calls == ["/usr/bin/gdalinfo"]
    store.close()


def test_tile_archive_rejects_wrong_coordinate_set_and_truncated_image(tmp_path) -> None:
    archive = Path(tmp_path, "tiles.mbtiles")
    _mbtiles(archive)
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)
    with archive.open("rb") as stream:
        blob = store.put_stream(stream)
    with pytest.raises(GeoIngestError, match="coordinate coverage"):
        inspect_tile_archive(
            store,
            storage_key=blob.storage_key,
            archive_sha256=blob.sha256,
            expected_tile_count=1,
            expected_coordinate_sha256="f" * 64,
        )

    truncated_archive = Path(tmp_path, "truncated.mbtiles")
    _mbtiles(truncated_archive, body=_png()[:-12])
    with truncated_archive.open("rb") as stream:
        truncated_blob = store.put_stream(stream)
    with pytest.raises(GeoIngestError, match="decoded safely"):
        inspect_tile_archive(
            store,
            storage_key=truncated_blob.storage_key,
            archive_sha256=truncated_blob.sha256,
        )
    store.close()


def test_tile_archive_hashes_tms_rows_in_canonical_xyz_order(tmp_path) -> None:
    archive = Path(tmp_path, "coverage.mbtiles")
    coordinates = [(1, 1, 1), (0, 0, 0), (1, 0, 1), (1, 0, 0)]
    coordinate_sha256 = _mbtiles(archive, coordinates=coordinates)
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=4 * 1024 * 1024)
    with archive.open("rb") as stream:
        blob = store.put_stream(stream)

    result = inspect_tile_archive(
        store,
        storage_key=blob.storage_key,
        archive_sha256=blob.sha256,
        expected_tile_count=len(coordinates),
        expected_coordinate_sha256=coordinate_sha256,
    )

    assert result.coordinate_sha256 == _coordinate_sha256(coordinates)
    assert result.min_zoom == 0
    assert result.max_zoom == 1
    store.close()
