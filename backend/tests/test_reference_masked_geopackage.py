from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3

import pytest

import app.reference_layers.masked_geopackage as masked_geopackage_module
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.geo_ingest import GeoCommandResult
from app.reference_layers.masked_geopackage import (
    MaskedGeoPackageError,
    derive_masked_geopackage,
    parse_masked_geopackage_spec,
    validate_reviewed_mask,
)


CELL_IDS = (
    "CRS3035RES100000mN2000000E2800000",
    "CRS3035RES100000mN2100000E2800000",
)


def _identifier_sha256(values: tuple[str, ...]) -> str:
    return hashlib.sha256(
        ("\n".join(sorted(values)) + "\n").encode("utf-8")
    ).hexdigest()


def _mask_document(*, name: str = "Castilla y León") -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "codnut2": "ES41",
                    "nameunit": name,
                    "nationallevelname": "Comunidad autónoma",
                },
                "id": 1124753,
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [
                            [
                                [-7.0, 40.0],
                                [-1.0, 40.0],
                                [-1.0, 44.0],
                                [-7.0, 44.0],
                                [-7.0, 40.0],
                            ]
                        ]
                    ],
                },
            }
        ],
        "numberMatched": 1,
        "numberReturned": 1,
        "links": [{"rel": "self", "href": "https://ign.example/items"}],
        "timeStamp": "2026-07-27T01:00:00Z",
    }


def _mask_identity_sha256(document: dict) -> str:
    feature = document["features"][0]
    identity = {
        "schema": "ign-administrative-mask-geometry/v1",
        "type": "Feature",
        "id": str(feature["id"]),
        "properties": {
            key: feature["properties"][key]
            for key in sorted(feature["properties"])
        },
        "geometry": feature["geometry"],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _spec_config(document: dict) -> dict:
    return {
        "vector_transform": {
            "schema": "reference-masked-geopackage/v1",
            "mask_url": "https://api-features.ign.es/cyl.json",
            "mask_media_type": "application/json",
            "mask_max_bytes": 1024 * 1024,
            "mask_identity_sha256": _mask_identity_sha256(document),
            "source_layer": "grid_100km_surf",
            "output_layer": "grid_100km_surf_cyl",
            "selected_fields": ["GRD_ID", "X_LLC", "Y_LLC"],
            "identifier_field": "GRD_ID",
            "cell_size_meters": 100_000,
            "expected_feature_count": len(CELL_IDS),
            "expected_identifier_sha256": _identifier_sha256(CELL_IDS),
            "source_crs": "EPSG:3035",
            "mask_target_crs": "EPSG:3035",
            "predicate": "intersects",
            "geometry_mode": "preserve-whole-source-features",
        }
    }


def _write_core_tables(
    connection: sqlite3.Connection,
) -> None:
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
        INSERT INTO gpkg_spatial_ref_sys VALUES (
            'ETRS89-extended / LAEA Europe',
            3035,
            'EPSG',
            3035,
            'PROJCS["ETRS89-extended / LAEA Europe",AUTHORITY["EPSG","3035"]]',
            ''
        );
        """
    )


def _write_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        _write_core_tables(connection)
        connection.executescript(
            """
            CREATE TABLE grid_100km_surf (
                fid INTEGER PRIMARY KEY,
                geom POLYGON,
                GRD_ID TEXT,
                X_LLC MEDIUMINT,
                Y_LLC MEDIUMINT,
                TOT_P_2021 REAL
            );
            INSERT INTO gpkg_contents VALUES (
                'grid_100km_surf',
                'features',
                'grid_100km_surf',
                '',
                '2025-07-03T00:00:00.000Z',
                2800000,
                2000000,
                2900000,
                2200000,
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
        for index, identifier in enumerate(CELL_IDS, start=1):
            connection.execute(
                "INSERT INTO grid_100km_surf VALUES (?, ?, ?, ?, ?, ?)",
                (
                    index,
                    b"source-geometry",
                    identifier,
                    2_800_000,
                    2_000_000 + index * 100_000,
                    999.0,
                ),
            )


def _add_mask_layer(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE __reference_spatial_mask (
                fid INTEGER PRIMARY KEY,
                geom MULTIPOLYGON
            );
            INSERT INTO gpkg_contents VALUES (
                '__reference_spatial_mask',
                'features',
                '__reference_spatial_mask',
                '',
                '2026-07-27T00:00:00.000Z',
                2800000,
                2000000,
                2900000,
                2200000,
                3035
            );
            INSERT INTO gpkg_geometry_columns VALUES (
                '__reference_spatial_mask',
                'geom',
                'MULTIPOLYGON',
                3035,
                0,
                0
            );
            INSERT INTO __reference_spatial_mask VALUES (1, X'00');
            """
        )


def _write_output(
    path: Path,
    identifiers: tuple[str, ...],
    *,
    last_change: str = "2026-07-27T00:00:00.000Z",
) -> None:
    with sqlite3.connect(path) as connection:
        _write_core_tables(connection)
        connection.executescript(
            """
            CREATE TABLE grid_100km_surf_cyl (
                fid INTEGER PRIMARY KEY,
                geom POLYGON,
                GRD_ID TEXT,
                X_LLC MEDIUMINT,
                Y_LLC MEDIUMINT
            );
            INSERT INTO gpkg_contents VALUES (
                'grid_100km_surf_cyl',
                'features',
                'grid_100km_surf_cyl',
                '',
                '2026-07-27T00:00:00.000Z',
                2800000,
                2000000,
                2900000,
                2200000,
                3035
            );
            INSERT INTO gpkg_geometry_columns VALUES (
                'grid_100km_surf_cyl',
                'geom',
                'POLYGON',
                3035,
                0,
                0
            );
            """
        )
        for index, identifier in enumerate(identifiers, start=1):
            connection.execute(
                "INSERT INTO grid_100km_surf_cyl VALUES (?, ?, ?, ?, ?)",
                (
                    index,
                    b"derived-geometry",
                    identifier,
                    2_800_000,
                    2_000_000 + (index - 1) * 100_000,
                ),
            )
        connection.execute(
            "UPDATE gpkg_contents SET last_change = ?",
            (last_change,),
        )


def _geometry_validation_payload(
    invalid_geometry_count: int = 0,
) -> bytes:
    return json.dumps(
        {
            "driverShortName": "GPKG",
            "layers": [
                {
                    "name": "SELECT",
                    "featureCount": 1,
                    "features": [
                        {
                            "properties": {
                                "invalid_geometry_count": (
                                    invalid_geometry_count
                                )
                            }
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def test_reviewed_mask_identity_ignores_volatile_response_metadata(
    tmp_path: Path,
) -> None:
    document = _mask_document()
    spec = parse_masked_geopackage_spec(_spec_config(document))
    assert spec is not None
    first = tmp_path / "first.json"
    first.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    repeated_document = {
        **document,
        "timeStamp": "2026-07-28T01:00:00Z",
        "links": [{"rel": "self", "href": "https://ign.example/repeated"}],
    }
    repeated = tmp_path / "repeated.json"
    repeated.write_text(
        json.dumps(repeated_document, ensure_ascii=False),
        encoding="utf-8",
    )

    first_identity = validate_reviewed_mask(
        first,
        first.stat().st_size,
        spec,
    )
    repeated_identity = validate_reviewed_mask(
        repeated,
        repeated.stat().st_size,
        spec,
    )

    assert first_identity.sha256 == repeated_identity.sha256
    assert first_identity.feature_id == "1124753"


def test_transient_workspace_cleanup_failure_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with ReferenceBlobStore(tmp_path / "workspace") as workspace:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                masked_geopackage_module.shutil,
                "rmtree",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("injected cleanup failure")
                ),
            )
            with pytest.raises(MaskedGeoPackageError) as error:
                with masked_geopackage_module._masked_geopackage_workspace(
                    workspace
                ):
                    pass

        assert error.value.code == (
            "masked_geopackage_derivation_invalid"
        )
        with masked_geopackage_module._masked_geopackage_workspace(
            workspace
        ):
            pass
        assert list(
            (workspace.root / "workspaces").iterdir()
        ) == []


def test_reviewed_mask_rejects_changed_feature_semantics(
    tmp_path: Path,
) -> None:
    document = _mask_document()
    spec = parse_masked_geopackage_spec(_spec_config(document))
    assert spec is not None
    changed = _mask_document(name="Castilla-La Mancha")
    path = tmp_path / "changed.json"
    path.write_text(
        json.dumps(changed, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(MaskedGeoPackageError) as error:
        validate_reviewed_mask(path, path.stat().st_size, spec)

    assert error.value.code == "spatial_mask_changed"


def test_transform_config_cannot_select_population_fields() -> None:
    document = _mask_document()
    config = _spec_config(document)
    config["vector_transform"]["selected_fields"].append("TOT_P_2021")

    with pytest.raises(MaskedGeoPackageError) as error:
        parse_masked_geopackage_spec(config)

    assert error.value.code == "masked_geopackage_config_invalid"


def test_derivation_rejects_spoofed_epsg_3035_definition(
    tmp_path: Path,
) -> None:
    document = _mask_document()
    spec = parse_masked_geopackage_spec(_spec_config(document))
    assert spec is not None
    source_path = tmp_path / "source.gpkg"
    _write_source(source_path)
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            "UPDATE gpkg_spatial_ref_sys "
            "SET definition = ? WHERE srs_id = 3035",
            (
                'GEOGCS["WGS 84",AUTHORITY["EPSG","4326"]]',
            ),
        )
        connection.commit()
    mask_body = json.dumps(document).encode("utf-8")
    mask_path = tmp_path / "mask.json"
    mask_path.write_bytes(mask_body)
    mask_identity = validate_reviewed_mask(
        mask_path,
        len(mask_body),
        spec,
    )

    with (
        ReferenceBlobStore(tmp_path / "store") as store,
        ReferenceBlobStore(tmp_path / "workspace") as workspace,
    ):
        source_blob = store.put_stream(
            io.BytesIO(source_path.read_bytes())
        )
        mask_blob = store.put_stream(io.BytesIO(mask_body))
        with pytest.raises(MaskedGeoPackageError) as error:
            derive_masked_geopackage(
                store,
                workspace_store=workspace,
                source_blob=source_blob,
                mask_blob=mask_blob,
                mask_identity=mask_identity,
                spec=spec,
                max_output_bytes=1024 * 1024,
                timeout_seconds=30,
                runner=lambda *_args: pytest.fail(
                    "invalid CRS must fail before GDAL"
                ),
            )

    assert error.value.code == (
        "masked_geopackage_derivation_invalid"
    )


def test_derivation_preserves_whole_cells_and_excludes_population(
    tmp_path: Path,
) -> None:
    document = _mask_document()
    spec = parse_masked_geopackage_spec(_spec_config(document))
    assert spec is not None
    source_path = tmp_path / "source.gpkg"
    _write_source(source_path)
    mask_body = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    mask_path = tmp_path / "mask.json"
    mask_path.write_bytes(mask_body)
    mask_identity = validate_reviewed_mask(
        mask_path,
        len(mask_body),
        spec,
    )
    commands: list[list[str]] = []
    output_runs = 0

    def runner(argv, environment, timeout):
        nonlocal output_runs
        commands.append(list(argv))
        assert timeout == 30
        assert "GDAL_HTTP_PROXY" not in environment
        if argv[0] == "/usr/bin/ogrinfo":
            sql = argv[argv.index("-sql") + 1]
            assert "ST_IsValid" in sql
            assert "CRS3035RES100000mN" in sql
            assert '"X_LLC" IS NULL' in sql
            assert 'typeof("X_LLC")' in sql
            assert 'CAST("X_LLC" AS INTEGER)' in sql
            return GeoCommandResult(
                _geometry_validation_payload(),
                b"",
            )
        if "-update" in argv:
            _add_mask_layer(Path(argv[-2]))
        else:
            assert "ST_Intersects" in argv[argv.index("-sql") + 1]
            assert "TOT_P_2021" not in argv[argv.index("-sql") + 1]
            assert argv[argv.index("-a_srs") + 1] == "EPSG:3035"
            output_runs += 1
            _write_output(
                Path(argv[-2]),
                CELL_IDS,
                last_change=(
                    f"2026-07-27T00:00:0{output_runs}.000Z"
                ),
            )
        return GeoCommandResult(b"", b"")

    with (
        ReferenceBlobStore(tmp_path / "store") as store,
        ReferenceBlobStore(tmp_path / "workspace") as workspace,
    ):
        orphan = (
            workspace.root
            / "workspaces"
            / f"masked-geopackage-{'a' * 32}"
        )
        orphan.mkdir(parents=True)
        (orphan / "source.gpkg").write_bytes(b"crash residue")
        source_blob = store.put_stream(
            io.BytesIO(source_path.read_bytes())
        )
        mask_blob = store.put_stream(io.BytesIO(mask_body))
        result = derive_masked_geopackage(
            store,
            workspace_store=workspace,
            source_blob=source_blob,
            mask_blob=mask_blob,
            mask_identity=mask_identity,
            spec=spec,
            max_output_bytes=1024 * 1024,
            timeout_seconds=30,
            runner=runner,
        )
        repeated = derive_masked_geopackage(
            store,
            workspace_store=workspace,
            source_blob=source_blob,
            mask_blob=mask_blob,
            mask_identity=mask_identity,
            spec=spec,
            max_output_bytes=1024 * 1024,
            timeout_seconds=30,
            runner=runner,
        )

        assert result.feature_count == 2
        assert result.identifier_sha256 == _identifier_sha256(CELL_IDS)
        assert result.validation["whole_source_features_preserved"] is True
        assert result.validation["population_fields_excluded"] is True
        assert result.validation["selected_fields"] == [
            "GRD_ID",
            "X_LLC",
            "Y_LLC",
        ]
        assert result.validation["normalized_last_change"] == (
            "1970-01-01T00:00:00.000Z"
        )
        assert result.validation["canonical_grid_geometry"] is True
        assert result.validation["identifier_coordinate_binding"] is True
        assert repeated.blob.sha256 == result.blob.sha256
        assert store.resolve_blob(result.blob.storage_key).is_file()
        assert list((store.root / "staging").iterdir()) == []
        assert list(
            (workspace.root / "workspaces").iterdir()
        ) == []
        assert len(commands) == 6


def test_derivation_rejects_identifier_parity_drift(
    tmp_path: Path,
) -> None:
    document = _mask_document()
    spec = parse_masked_geopackage_spec(_spec_config(document))
    assert spec is not None
    source_path = tmp_path / "source.gpkg"
    _write_source(source_path)
    mask_body = json.dumps(document).encode("utf-8")
    mask_path = tmp_path / "mask.json"
    mask_path.write_bytes(mask_body)
    mask_identity = validate_reviewed_mask(
        mask_path,
        len(mask_body),
        spec,
    )

    def runner(argv, _environment, _timeout):
        if argv[0] == "/usr/bin/ogrinfo":
            return GeoCommandResult(
                _geometry_validation_payload(),
                b"",
            )
        if "-update" in argv:
            _add_mask_layer(Path(argv[-2]))
        else:
            _write_output(Path(argv[-2]), ("unexpected-cell",))
        return GeoCommandResult(b"", b"")

    with (
        ReferenceBlobStore(tmp_path / "store") as store,
        ReferenceBlobStore(tmp_path / "workspace") as workspace,
    ):
        source_blob = store.put_stream(
            io.BytesIO(source_path.read_bytes())
        )
        mask_blob = store.put_stream(io.BytesIO(mask_body))
        with pytest.raises(MaskedGeoPackageError) as error:
            derive_masked_geopackage(
                store,
                workspace_store=workspace,
                source_blob=source_blob,
                mask_blob=mask_blob,
                mask_identity=mask_identity,
                spec=spec,
                max_output_bytes=1024 * 1024,
                timeout_seconds=30,
                runner=runner,
            )

    assert error.value.code == "masked_geopackage_parity_failed"


def test_derivation_rejects_noncanonical_cell_geometry(
    tmp_path: Path,
) -> None:
    document = _mask_document()
    spec = parse_masked_geopackage_spec(_spec_config(document))
    assert spec is not None
    source_path = tmp_path / "source.gpkg"
    _write_source(source_path)
    mask_body = json.dumps(document).encode("utf-8")
    mask_path = tmp_path / "mask.json"
    mask_path.write_bytes(mask_body)
    mask_identity = validate_reviewed_mask(
        mask_path,
        len(mask_body),
        spec,
    )

    def runner(argv, _environment, _timeout):
        if argv[0] == "/usr/bin/ogrinfo":
            return GeoCommandResult(
                _geometry_validation_payload(1),
                b"",
            )
        if "-update" in argv:
            _add_mask_layer(Path(argv[-2]))
        else:
            _write_output(Path(argv[-2]), CELL_IDS)
        return GeoCommandResult(b"", b"")

    with (
        ReferenceBlobStore(tmp_path / "store") as store,
        ReferenceBlobStore(tmp_path / "workspace") as workspace,
    ):
        source_blob = store.put_stream(
            io.BytesIO(source_path.read_bytes())
        )
        mask_blob = store.put_stream(io.BytesIO(mask_body))
        with pytest.raises(MaskedGeoPackageError) as error:
            derive_masked_geopackage(
                store,
                workspace_store=workspace,
                source_blob=source_blob,
                mask_blob=mask_blob,
                mask_identity=mask_identity,
                spec=spec,
                max_output_bytes=1024 * 1024,
                timeout_seconds=30,
                runner=runner,
            )

    assert error.value.code == "masked_geopackage_parity_failed"


@pytest.mark.parametrize(
    "invalid_x",
    [None, "not-a-coordinate", 2_800_000.5, 2_700_000],
)
def test_derivation_rejects_invalid_identifier_coordinate_binding(
    tmp_path: Path,
    invalid_x,
) -> None:
    document = _mask_document()
    spec = parse_masked_geopackage_spec(_spec_config(document))
    assert spec is not None
    source_path = tmp_path / "source.gpkg"
    _write_source(source_path)
    mask_body = json.dumps(document).encode("utf-8")
    mask_path = tmp_path / "mask.json"
    mask_path.write_bytes(mask_body)
    mask_identity = validate_reviewed_mask(
        mask_path,
        len(mask_body),
        spec,
    )

    def runner(argv, _environment, _timeout):
        if argv[0] == "/usr/bin/ogrinfo":
            return GeoCommandResult(
                _geometry_validation_payload(),
                b"",
            )
        if "-update" in argv:
            _add_mask_layer(Path(argv[-2]))
        else:
            output = Path(argv[-2])
            _write_output(output, CELL_IDS)
            with sqlite3.connect(output) as connection:
                connection.execute(
                    "UPDATE grid_100km_surf_cyl "
                    "SET X_LLC = ? WHERE fid = 1",
                    (invalid_x,),
                )
                connection.commit()
        return GeoCommandResult(b"", b"")

    with (
        ReferenceBlobStore(tmp_path / "store") as store,
        ReferenceBlobStore(tmp_path / "workspace") as workspace,
    ):
        source_blob = store.put_stream(
            io.BytesIO(source_path.read_bytes())
        )
        mask_blob = store.put_stream(io.BytesIO(mask_body))
        with pytest.raises(MaskedGeoPackageError) as error:
            derive_masked_geopackage(
                store,
                workspace_store=workspace,
                source_blob=source_blob,
                mask_blob=mask_blob,
                mask_identity=mask_identity,
                spec=spec,
                max_output_bytes=1024 * 1024,
                timeout_seconds=30,
                runner=runner,
            )

    assert error.value.code == "masked_geopackage_parity_failed"
