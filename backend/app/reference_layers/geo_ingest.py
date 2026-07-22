"""Normalize acquired geodata into immutable local delivery assets."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    StoredReferenceBlob,
)
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.local_tile_archive import (
    InvalidLocalTileArchiveError,
    validate_tile_image,
)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$", re.ASCII)
_CONNECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$", re.ASCII)
_LAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$", re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_EPSG_WKT_RE = re.compile(
    r'(?:ID\["EPSG",\s*|AUTHORITY\["EPSG",\s*")(?P<code>[1-9][0-9]{2,6})',
    re.ASCII,
)
_ALLOWED_BINARIES = {
    "gdalinfo": "/usr/bin/gdalinfo",
    "gdal_translate": "/usr/bin/gdal_translate",
    "ogr2ogr": "/usr/bin/ogr2ogr",
}
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RASTER_PIXELS = 1_000_000_000_000
MAX_TILE_COUNT = 10_000_000


class GeoIngestError(RuntimeError):
    """Local geodata could not be normalized or validated safely."""


@dataclass(frozen=True)
class GeoCommandResult:
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class GeoDatabaseTarget:
    host: str
    port: int
    database: str
    username: str
    password: str | None

    @classmethod
    def from_url(cls, database_url: str) -> "GeoDatabaseTarget":
        try:
            value = make_url(database_url)
        except Exception as error:
            raise GeoIngestError("geodata database URL is invalid") from error
        if not value.drivername.startswith("postgresql"):
            raise GeoIngestError("geodata database must be PostgreSQL")
        host = value.host or ""
        database = value.database or ""
        username = value.username or ""
        port = value.port or 5432
        if (
            _CONNECTION_NAME_RE.fullmatch(host) is None
            or _CONNECTION_NAME_RE.fullmatch(database) is None
            or _CONNECTION_NAME_RE.fullmatch(username) is None
            or not 1 <= port <= 65535
            or value.query
            or (value.password is not None and "\x00" in value.password)
        ):
            raise GeoIngestError("geodata database URL contains unsafe fields")
        return cls(host, port, database, username, value.password)

    @property
    def ogr_connection(self) -> str:
        return (
            f"PG:host={self.host} port={self.port} "
            f"dbname={self.database} user={self.username}"
        )

    def command_environment(self) -> dict[str, str]:
        result = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PGCONNECT_TIMEOUT": "10",
        }
        if self.password is not None:
            result["PGPASSWORD"] = self.password
        return result


@dataclass(frozen=True)
class VectorIngestResult:
    table_name: str
    storage_key: str
    content_sha256: str
    feature_count: int
    crs: str
    bounds_json: dict[str, float]
    geometry_type: str
    validation_json: dict[str, Any]


@dataclass(frozen=True)
class RasterInspection:
    width: int
    height: int
    band_count: int
    crs: str
    bounds_json: dict[str, float]
    driver: str
    is_cog: bool


@dataclass(frozen=True)
class RasterIngestResult:
    blob: StoredReferenceBlob
    inspection: RasterInspection
    validation_json: dict[str, Any]


@dataclass(frozen=True)
class TileArchiveInspection:
    tile_count: int
    min_zoom: int
    max_zoom: int
    image_format: str
    bounds_json: dict[str, float]
    validation_json: dict[str, Any]


CommandRunner = Callable[
    [Sequence[str], Mapping[str, str], int],
    GeoCommandResult,
]


def run_geo_command(
    argv: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> GeoCommandResult:
    """Execute one allowlisted GDAL command without a shell."""

    if not argv or argv[0] not in _ALLOWED_BINARIES.values():
        raise GeoIngestError("geodata command is not allowlisted")
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 86_400:
        raise GeoIngestError("geodata command timeout is invalid")
    if any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise GeoIngestError("geodata command argument is invalid")
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GeoIngestError("geodata command could not complete") from error
    if (
        len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
        or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise GeoIngestError("geodata command output exceeded its limit")
    if completed.returncode != 0:
        raise GeoIngestError("geodata command rejected the local artifact")
    return GeoCommandResult(completed.stdout, completed.stderr)


def versioned_vector_table_name(
    *,
    provider_key: str,
    layer_id: int,
    run_id: int,
    input_sha256: str,
) -> str:
    if (
        not isinstance(provider_key, str)
        or not provider_key
        or len(provider_key) > 64
        or isinstance(layer_id, bool)
        or not isinstance(layer_id, int)
        or layer_id < 1
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id < 1
        or _SHA256_RE.fullmatch(input_sha256) is None
    ):
        raise GeoIngestError("vector table identity is invalid")
    provider_hash = hashlib.sha256(provider_key.encode()).hexdigest()[:8]
    value = f"m_{provider_hash}_l{layer_id}_r{run_id}_{input_sha256[:10]}"
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise GeoIngestError("vector table identity is too large")
    return value


def ingest_vector_artifact(
    db: Session,
    *,
    database: GeoDatabaseTarget,
    source_path: Path,
    input_sha256: str,
    provider_key: str,
    layer_id: int,
    run_id: int,
    input_layer: str | None = None,
    minimum_features: int = 1,
    timeout_seconds: int = 3600,
    runner: CommandRunner = run_geo_command,
) -> VectorIngestResult:
    """Import one immutable vector snapshot into a versioned PostGIS table."""

    path = _regular_file(source_path)
    if input_layer is not None and (
        _LAYER_NAME_RE.fullmatch(input_layer) is None or input_layer.startswith("-")
    ):
        raise GeoIngestError("input vector layer name is invalid")
    if (
        isinstance(minimum_features, bool)
        or not isinstance(minimum_features, int)
        or not 0 <= minimum_features <= 1_000_000_000
    ):
        raise GeoIngestError("minimum vector feature count is invalid")
    table_name = versioned_vector_table_name(
        provider_key=provider_key,
        layer_id=layer_id,
        run_id=run_id,
        input_sha256=input_sha256,
    )
    storage_key = f"reference_data.{table_name}"
    if db.execute(
        text("SELECT to_regclass(:key)"),
        {"key": storage_key},
    ).scalar_one() is not None:
        raise GeoIngestError("versioned vector table already exists")

    db.execute(text("CREATE SCHEMA IF NOT EXISTS reference_data"))
    db.commit()
    argv = [
        _ALLOWED_BINARIES["ogr2ogr"],
        "-f",
        "PostgreSQL",
        "-nln",
        storage_key,
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-t_srs",
        "EPSG:3857",
        "-makevalid",
        "-lco",
        "GEOMETRY_NAME=geom",
        "-lco",
        "FID=source_fid",
        database.ogr_connection,
        str(path),
    ]
    if input_layer is not None:
        argv.append(input_layer)
    try:
        runner(argv, database.command_environment(), timeout_seconds)
        row = db.execute(
            text(
                f"""
                SELECT
                    count(*)::bigint AS feature_count,
                    count(*) FILTER (WHERE geom IS NULL)::bigint AS null_count,
                    count(*) FILTER (
                        WHERE geom IS NOT NULL AND NOT ST_IsValid(geom)
                    )::bigint AS invalid_count,
                    count(DISTINCT ST_SRID(geom)) FILTER (
                        WHERE geom IS NOT NULL
                    )::integer AS srid_count,
                    min(ST_SRID(geom)) FILTER (
                        WHERE geom IS NOT NULL
                    )::integer AS srid,
                    min(GeometryType(geom)) FILTER (
                        WHERE geom IS NOT NULL
                    ) AS geometry_type,
                    max(GeometryType(geom)) FILTER (
                        WHERE geom IS NOT NULL
                    ) AS max_geometry_type,
                    ST_Extent(ST_Transform(geom, 4326))::text AS extent
                FROM reference_data.{table_name}
                """
            )
        ).mappings().one()
        feature_count = int(row["feature_count"])
        if feature_count < minimum_features:
            raise GeoIngestError("vector feature count is below its minimum")
        if row["null_count"] or row["invalid_count"]:
            raise GeoIngestError("vector table contains invalid geometries")
        if row["srid_count"] != 1 or row["srid"] != 3857:
            raise GeoIngestError("vector table CRS is inconsistent")
        if row["geometry_type"] != row["max_geometry_type"]:
            raise GeoIngestError("vector table mixes geometry families")
        bounds = _postgis_extent(row["extent"])
        _install_table_guards(db, table_name)
        transform_identity = {
            "schema_version": "reference-vector-normalization-v1",
            "input_sha256": input_sha256,
            "table": storage_key,
            "target_crs": "EPSG:3857",
            "make_valid": True,
            "feature_count": feature_count,
            "geometry_type": row["geometry_type"],
            "bounds": bounds,
        }
        content_sha256 = canonical_json_sha256(transform_identity)
        validation = {
            "passed": True,
            "kind": "vector",
            "checks": {
                "feature_count": feature_count,
                "minimum_features": minimum_features,
                "null_geometries": int(row["null_count"]),
                "invalid_geometries": int(row["invalid_count"]),
                "srid": int(row["srid"]),
                "geometry_type": row["geometry_type"],
                "immutable_guards": True,
            },
        }
        db.commit()
        return VectorIngestResult(
            table_name=table_name,
            storage_key=storage_key,
            content_sha256=content_sha256,
            feature_count=feature_count,
            crs="EPSG:3857",
            bounds_json=bounds,
            geometry_type=str(row["geometry_type"]),
            validation_json=validation,
        )
    except Exception:
        db.rollback()
        _drop_unpublished_table(db, table_name)
        raise


def ingest_raster_artifact(
    store: ReferenceBlobStore,
    *,
    source_path: Path,
    timeout_seconds: int = 3600,
    max_output_bytes: int = 20 * 1024 * 1024 * 1024,
    runner: CommandRunner = run_geo_command,
) -> RasterIngestResult:
    """Convert a local raster snapshot to a validated content-addressed COG."""

    source = _regular_file(source_path)
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    }
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1 <= max_output_bytes <= store.max_blob_bytes
    ):
        raise GeoIngestError("normalized raster byte limit is invalid")
    original = _inspect_raster(
        source,
        runner=runner,
        environment=environment,
        timeout=timeout_seconds,
    )
    with tempfile.TemporaryDirectory(prefix="reference-cog-") as directory:
        output = Path(directory, "normalized.tif")
        runner(
            [
                _ALLOWED_BINARIES["gdal_translate"],
                "-of",
                "COG",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "BIGTIFF=IF_SAFER",
                "-co",
                "OVERVIEWS=AUTO",
                "-co",
                "RESAMPLING=AVERAGE",
                str(source),
                str(output),
            ],
            environment,
            timeout_seconds,
        )
        inspection = _inspect_raster(
            output,
            runner=runner,
            environment=environment,
            timeout=timeout_seconds,
            require_cog=True,
        )
        if (
            inspection.width != original.width
            or inspection.height != original.height
            or inspection.band_count != original.band_count
            or inspection.crs != original.crs
            or inspection.bounds_json != original.bounds_json
        ):
            raise GeoIngestError("COG conversion changed raster coverage")
        try:
            output_size = output.stat().st_size
        except OSError as error:
            raise GeoIngestError("normalized raster output is unavailable") from error
        if output_size > max_output_bytes:
            raise GeoIngestError("normalized raster exceeds its byte limit")
        with output.open("rb") as stream:
            blob = store.put_stream(stream, max_bytes=max_output_bytes)
    validation = {
        "passed": True,
        "kind": "raster",
        "checks": {
            "driver": inspection.driver,
            "cloud_optimized": inspection.is_cog,
            "width": inspection.width,
            "height": inspection.height,
            "band_count": inspection.band_count,
            "crs": inspection.crs,
        },
    }
    return RasterIngestResult(blob, inspection, validation)


def inspect_tile_archive(
    store: ReferenceBlobStore,
    *,
    storage_key: str,
    archive_sha256: str,
    expected_tile_count: int | None = None,
    max_tiles: int = MAX_TILE_COUNT,
) -> TileArchiveInspection:
    """Validate every image and coordinate in an immutable MBTiles archive."""

    if _SHA256_RE.fullmatch(archive_sha256) is None:
        raise GeoIngestError("tile archive digest is invalid")
    if (
        isinstance(max_tiles, bool)
        or not isinstance(max_tiles, int)
        or not 1 <= max_tiles <= MAX_TILE_COUNT
    ):
        raise GeoIngestError("tile archive limit is invalid")
    if expected_tile_count is not None and (
        isinstance(expected_tile_count, bool)
        or not isinstance(expected_tile_count, int)
        or not 1 <= expected_tile_count <= max_tiles
    ):
        raise GeoIngestError("expected tile count is invalid")
    path = store.resolve_blob(storage_key)
    if _sha256_file(path) != archive_sha256:
        raise GeoIngestError("tile archive bytes do not match their digest")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        _require_mbtiles_schema(connection)
        metadata_rows = connection.execute(
            "SELECT name, value FROM metadata ORDER BY name"
        ).fetchall()
        metadata: dict[str, str] = {}
        for name, value in metadata_rows:
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or name in metadata
                or len(name) > 128
                or len(value) > 4096
            ):
                raise GeoIngestError("tile archive metadata is malformed")
            metadata[name] = value
        image_format = metadata.get("format", "").casefold()
        if image_format == "jpeg":
            image_format = "jpg"
        if image_format not in {"png", "jpg"}:
            raise GeoIngestError("tile archive image format is unsupported")
        try:
            min_zoom = int(metadata["minzoom"])
            max_zoom = int(metadata["maxzoom"])
        except (KeyError, TypeError, ValueError) as error:
            raise GeoIngestError("tile archive zoom metadata is invalid") from error
        if not 0 <= min_zoom <= max_zoom <= 22:
            raise GeoIngestError("tile archive zoom range is invalid")
        bounds = _metadata_bounds(metadata.get("bounds"))
        total = int(connection.execute("SELECT count(*) FROM tiles").fetchone()[0])
        if not 1 <= total <= max_tiles:
            raise GeoIngestError("tile archive count is outside its limit")
        if expected_tile_count is not None and total != expected_tile_count:
            raise GeoIngestError("tile archive coverage is incomplete")
        duplicate = connection.execute(
            "SELECT 1 FROM tiles GROUP BY zoom_level, tile_column, tile_row "
            "HAVING count(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise GeoIngestError("tile archive contains duplicate coordinates")
        seen = 0
        for zoom, column, row, body in connection.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
            "ORDER BY zoom_level, tile_column, tile_row"
        ):
            if (
                not isinstance(zoom, int)
                or not isinstance(column, int)
                or not isinstance(row, int)
                or not min_zoom <= zoom <= max_zoom
                or not 0 <= column < 2**zoom
                or not 0 <= row < 2**zoom
                or not isinstance(body, bytes)
            ):
                raise GeoIngestError("tile archive coordinate is invalid")
            validate_tile_image(body, image_format)
            seen += 1
        if seen != total:
            raise GeoIngestError("tile archive changed during validation")
    except (sqlite3.Error, InvalidLocalTileArchiveError) as error:
        raise GeoIngestError("tile archive is invalid") from error
    finally:
        if connection is not None:
            connection.close()
    validation = {
        "passed": True,
        "kind": "tiles",
        "checks": {
            "tile_count": total,
            "expected_tile_count": expected_tile_count,
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "format": image_format,
            "all_images_valid": True,
            "unique_coordinates": True,
        },
    }
    return TileArchiveInspection(
        tile_count=total,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        image_format=image_format,
        bounds_json=bounds,
        validation_json=validation,
    )


def _inspect_raster(
    path: Path,
    *,
    runner: CommandRunner,
    environment: Mapping[str, str],
    timeout: int,
    require_cog: bool = False,
) -> RasterInspection:
    result = runner(
        [_ALLOWED_BINARIES["gdalinfo"], "-json", str(path)],
        environment,
        timeout,
    )
    try:
        payload = json.loads(
            result.stdout.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise GeoIngestError("gdalinfo did not return strict JSON") from error
    if not isinstance(payload, dict):
        raise GeoIngestError("gdalinfo response is malformed")
    size = payload.get("size")
    bands = payload.get("bands")
    driver = payload.get("driverShortName")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in size)
        or not 1 <= size[0]
        or not 1 <= size[1]
        or size[0] * size[1] > MAX_RASTER_PIXELS
        or not isinstance(bands, list)
        or not 1 <= len(bands) <= 64
        or not isinstance(driver, str)
        or not driver
    ):
        raise GeoIngestError("raster dimensions, bands or driver are invalid")
    coordinate_system = payload.get("coordinateSystem")
    wkt = coordinate_system.get("wkt") if isinstance(coordinate_system, dict) else None
    epsg_codes = _EPSG_WKT_RE.findall(wkt) if isinstance(wkt, str) else []
    if not epsg_codes:
        raise GeoIngestError("raster CRS has no unambiguous EPSG identifier")
    bounds = _wgs84_extent(payload.get("wgs84Extent"))
    metadata = payload.get("metadata")
    image_structure = metadata.get("IMAGE_STRUCTURE") if isinstance(metadata, dict) else None
    is_cog = (
        isinstance(image_structure, dict)
        and image_structure.get("LAYOUT") == "COG"
    )
    if require_cog and (driver != "GTiff" or not is_cog):
        raise GeoIngestError("normalized raster is not a cloud-optimized GeoTIFF")
    return RasterInspection(
        width=size[0],
        height=size[1],
        band_count=len(bands),
        crs=f"EPSG:{epsg_codes[-1]}",
        bounds_json=bounds,
        driver=driver,
        is_cog=is_cog,
    )


def _install_table_guards(db: Session, table_name: str) -> None:
    db.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION reference_data.reject_immutable_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'immutable reference data table'
                    USING ERRCODE = '55000';
            END;
            $$
            """
        )
    )
    db.execute(
        text(
            f"""
            CREATE TRIGGER {table_name}_immutable_rows
            BEFORE INSERT OR UPDATE OR DELETE ON reference_data.{table_name}
            FOR EACH STATEMENT
            EXECUTE FUNCTION reference_data.reject_immutable_mutation()
            """
        )
    )
    db.execute(
        text(
            f"""
            CREATE TRIGGER {table_name}_immutable_truncate
            BEFORE TRUNCATE ON reference_data.{table_name}
            FOR EACH STATEMENT
            EXECUTE FUNCTION reference_data.reject_immutable_mutation()
            """
        )
    )


def _require_mbtiles_schema(connection: sqlite3.Connection) -> None:
    objects = dict(
        connection.execute(
            "SELECT name, type FROM sqlite_schema "
            "WHERE name IN ('metadata', 'tiles')"
        ).fetchall()
    )
    if objects != {"metadata": "table", "tiles": "table"}:
        raise GeoIngestError("tile archive schema is unsupported")
    metadata_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(metadata)")
    }
    tile_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(tiles)")
    }
    if not {"name", "value"} <= metadata_columns or not {
        "zoom_level",
        "tile_column",
        "tile_row",
        "tile_data",
    } <= tile_columns:
        raise GeoIngestError("tile archive columns are incomplete")


def _drop_unpublished_table(db: Session, table_name: str) -> None:
    if _IDENTIFIER_RE.fullmatch(table_name) is None:
        return
    try:
        db.execute(text(f"DROP TABLE IF EXISTS reference_data.{table_name}"))
        db.commit()
    except Exception:
        db.rollback()


def _regular_file(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise GeoIngestError("local geodata artifact must not be a symlink")
    try:
        value = candidate.resolve(strict=True)
        metadata = value.stat()
    except (OSError, RuntimeError) as error:
        raise GeoIngestError("local geodata artifact is unavailable") from error
    if not value.is_file() or value.is_symlink() or metadata.st_size <= 0:
        raise GeoIngestError("local geodata artifact is not a regular file")
    return value


def _unique_object(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise GeoIngestError("could not verify local geodata bytes") from error
    return digest.hexdigest()


def _postgis_extent(value: Any) -> dict[str, float]:
    if not isinstance(value, str):
        raise GeoIngestError("vector extent is unavailable")
    match = re.fullmatch(
        r"BOX\((-?[0-9]+(?:\.[0-9]+)?) (-?[0-9]+(?:\.[0-9]+)?),"
        r"(-?[0-9]+(?:\.[0-9]+)?) (-?[0-9]+(?:\.[0-9]+)?)\)",
        value,
        re.ASCII,
    )
    if match is None:
        raise GeoIngestError("vector extent is malformed")
    return _validated_bounds(*(float(item) for item in match.groups()))


def _metadata_bounds(value: Any) -> dict[str, float]:
    if not isinstance(value, str):
        raise GeoIngestError("tile archive bounds are unavailable")
    parts = value.split(",")
    if len(parts) != 4:
        raise GeoIngestError("tile archive bounds are invalid")
    try:
        numbers = [float(item) for item in parts]
    except ValueError as error:
        raise GeoIngestError("tile archive bounds are invalid") from error
    return _validated_bounds(*numbers)


def _wgs84_extent(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        raise GeoIngestError("raster WGS84 extent is unavailable")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise GeoIngestError("raster WGS84 extent is malformed")
    points: list[tuple[float, float]] = []
    for ring in coordinates:
        if not isinstance(ring, list):
            raise GeoIngestError("raster WGS84 extent is malformed")
        for point in ring:
            if (
                not isinstance(point, list)
                or len(point) < 2
                or isinstance(point[0], bool)
                or isinstance(point[1], bool)
                or not isinstance(point[0], (int, float))
                or not isinstance(point[1], (int, float))
            ):
                raise GeoIngestError("raster WGS84 extent is malformed")
            points.append((float(point[0]), float(point[1])))
    if not points or len(points) > 10_000:
        raise GeoIngestError("raster WGS84 extent is malformed")
    return _validated_bounds(
        min(item[0] for item in points),
        min(item[1] for item in points),
        max(item[0] for item in points),
        max(item[1] for item in points),
    )


def _validated_bounds(
    west: float,
    south: float,
    east: float,
    north: float,
) -> dict[str, float]:
    if (
        not -180 <= west < east <= 180
        or not -90 <= south < north <= 90
    ):
        raise GeoIngestError("geodata bounds are outside WGS84")
    return {"west": west, "south": south, "east": east, "north": north}
