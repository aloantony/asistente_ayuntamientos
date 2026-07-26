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
) -> str:
    return canonical_json_sha256(
        {
            "schema_version": "reference-vector-input-manifest-v1",
            "artifacts": [
                {
                    "ordinal": 0,
                    "input_sha256": input_sha256,
                    "input_driver": input_driver,
                    "input_layer": input_layer,
                    "size_bytes": path.stat().st_size,
                }
            ],
        }
    )


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
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_sha256 = _vector_manifest_sha256(source, source_sha256)
    table_name = versioned_vector_table_name(
        provider_key="siur",
        layer_id=90,
        run_id=91,
        input_sha256=manifest_sha256,
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
        table_name = storage_key.split(".", 1)[1]
        imported_snapshots.append(Path(argv[-1]).read_bytes())
        assert "-unsetFid" in argv
        if "-append" not in argv:
            assert "-update" not in argv
            db.execute(
                text(
                    f"""
                    CREATE TABLE reference_data.{table_name} (
                        source_fid bigserial PRIMARY KEY,
                        geom geometry(MultiPolygon, 3857) NOT NULL
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
                INSERT INTO reference_data.{table_name} (geom)
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
        table_name = storage_key.split(".", 1)[1]
        if "-append" not in argv:
            db.execute(
                text(
                    f"""
                    CREATE TABLE reference_data.{table_name} (
                        source_fid bigserial PRIMARY KEY,
                        geom geometry(MultiPolygon, 3857) NOT NULL
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
                INSERT INTO reference_data.{table_name} (geom)
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
                CREATE TABLE reference_data.{imported_table} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857) NOT NULL
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data.{imported_table} (geom)
                VALUES (ST_Multi(ST_GeomFromText(
                    'POLYGON((0 0,1000 0,1000 1000,0 1000,0 0))',
                    3857
                )))
                """
            )
        )
        db.commit()
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
        {"name": f"reference_data.{imported_table}"},
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
        db.execute(
            text(
                f"""
                CREATE TABLE reference_data.{table_name} (
                    source_fid bigserial PRIMARY KEY,
                    geom geometry(MultiPolygon, 3857) NOT NULL
                )
                """
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data.{table_name} (geom)
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
    assert second.validation_json["checks"]["idempotent_reuse"] is True
    assert second.validation_json["checks"]["artifact_feature_counts"] is None


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
        assert db.execute(
            text("SELECT to_regclass(:name)"),
            {"name": f"reference_data.{table_name}"},
        ).scalar_one() is None
        db.execute(
            text(
                f"CREATE TABLE reference_data.{table_name} ("
                "source_fid bigserial PRIMARY KEY, "
                "geom geometry(MultiPolygon, 3857) NOT NULL)"
            )
        )
        db.execute(
            text(
                f"""
                INSERT INTO reference_data.{table_name} (geom)
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
    assert result.validation_json["checks"]["idempotent_reuse"] is False


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
