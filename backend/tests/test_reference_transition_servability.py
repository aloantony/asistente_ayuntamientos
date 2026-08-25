import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.geoserver_admin import (
    GeoServerLayerSmokeError,
    LayerSmokeResult,
)
from app.reference_layers.models import (
    ReferenceDeliveryAsset,
    ReferenceDeliveryVersion,
)
from app.reference_layers.transition_servability import (
    DeliveryTransitionServabilityError,
    verify_delivery_transition_servability,
)
from test_reference_geo_ingest import _mbtiles


class _AssetSession:
    def __init__(self, assets):
        self.assets = tuple(assets)

    def scalars(self, _statement):
        return iter(self.assets)


class _HealthyGeoServer:
    def __init__(self):
        self.calls = []

    def health(self):
        return SimpleNamespace(version="test")

    def smoke_layer(self, **arguments):
        self.calls.append(arguments)
        return LayerSmokeResult(
            layer_name=arguments["layer_name"],
            style_name=arguments["style_name"],
            image_sha256="a" * 64,
            image_bytes=128,
            image_content_type="image/png",
            z=arguments["z"],
            x=arguments["x"],
            y=arguments["y"],
        )


class _MissingGeoServerResource(_HealthyGeoServer):
    def smoke_layer(self, **arguments):
        raise GeoServerLayerSmokeError(
            "local GeoServer layer is not published"
        )


def _raster_delivery(store):
    blob = store.put_stream(io.BytesIO(b"immutable-raster-cog"))
    version = ReferenceDeliveryVersion(
        id=41,
        delivery_kind="raster",
        content_sha256=blob.sha256,
        validation_json={
            "schema_version": "reference-delivery-validation/v1",
            "passed": True,
            "kind": "raster",
            "checks": {},
        },
    )
    asset = ReferenceDeliveryAsset(
        id=73,
        version_id=version.id,
        asset_key="primary",
        asset_kind="raster_cog",
        is_primary=True,
        storage_backend="filesystem",
        storage_key=blob.storage_key,
        media_type="image/tiff",
        sha256=blob.sha256,
        size_bytes=blob.size_bytes,
        metadata_json={
            "renderer": "geoserver",
            "layer_name": "l41_raster_v_abcdef123456",
            "default_style_name": None,
            "styles": {},
            "identify_available": False,
            "legend_available": False,
        },
    )
    return version, asset


def test_raster_transition_rehashes_blob_and_smokes_direct_renderer(
    tmp_path,
) -> None:
    with ReferenceBlobStore(tmp_path / "reference-data") as store:
        version, asset = _raster_delivery(store)
        geoserver = _HealthyGeoServer()

        result = verify_delivery_transition_servability(
            store,
            _AssetSession((asset,)),
            version,
            geoserver=geoserver,
        )

    assert result.version_id == version.id
    assert result.primary_asset_id == asset.id
    assert result.verified_filesystem_asset_ids == (asset.id,)
    assert result.renderer == "geoserver"
    assert result.render_transport == "direct_geoserver_wms"
    assert result.rendered_style_names == (None,)
    assert len(geoserver.calls) == 1
    assert geoserver.calls[0]["layer_name"] == result.resource_name


@pytest.mark.parametrize("tamper_kind", ("delete", "corrupt"))
def test_raster_transition_fails_closed_for_missing_or_corrupt_blob(
    tmp_path,
    tamper_kind,
) -> None:
    with ReferenceBlobStore(tmp_path / "reference-data") as store:
        version, asset = _raster_delivery(store)
        path = store.resolve_blob(asset.storage_key)
        if tamper_kind == "delete":
            path.unlink()
        else:
            body = path.read_bytes()
            path.write_bytes(bytes([body[0] ^ 1]) + body[1:])

        with pytest.raises(
            DeliveryTransitionServabilityError,
            match="filesystem blob",
        ):
            verify_delivery_transition_servability(
                store,
                _AssetSession((asset,)),
                version,
                geoserver=_HealthyGeoServer(),
            )


def test_raster_transition_fails_closed_when_geoserver_resource_is_absent(
    tmp_path,
) -> None:
    with ReferenceBlobStore(tmp_path / "reference-data") as store:
        version, asset = _raster_delivery(store)

        with pytest.raises(
            DeliveryTransitionServabilityError,
            match="resource or direct render smoke",
        ):
            verify_delivery_transition_servability(
                store,
                _AssetSession((asset,)),
                version,
                geoserver=_MissingGeoServerResource(),
            )


def test_tile_transition_fully_inspects_and_renders_each_archive(
    tmp_path,
) -> None:
    archive = Path(tmp_path, "tiles.mbtiles")
    coordinate_sha256 = _mbtiles(archive)
    with ReferenceBlobStore(
        tmp_path / "reference-data",
        max_blob_bytes=1024 * 1024,
    ) as store:
        with archive.open("rb") as source:
            blob = store.put_stream(source)
        version = ReferenceDeliveryVersion(
            id=91,
            delivery_kind="tiles",
            content_sha256=blob.sha256,
            validation_json={
                "schema_version": "reference-delivery-validation/v1",
                "passed": True,
                "kind": "tiles",
                "checks": {},
            },
        )
        asset = ReferenceDeliveryAsset(
            id=92,
            version_id=version.id,
            asset_key="primary",
            asset_kind="tile_archive",
            is_primary=True,
            storage_backend="filesystem",
            storage_key=blob.storage_key,
            media_type="application/vnd.mapbox.mbtiles",
            sha256=blob.sha256,
            size_bytes=blob.size_bytes,
            metadata_json={
                "renderer": "tile_archive",
                "expected_tile_count": 1,
                "expected_coordinate_sha256": coordinate_sha256,
                "min_zoom": 0,
                "max_zoom": 0,
                "image_format": "png",
                "smoke_coordinate": [0, 0, 0],
            },
        )

        result = verify_delivery_transition_servability(
            store,
            _AssetSession((asset,)),
            version,
        )

    assert result.renderer == "tile_archive"
    assert result.render_transport == "local_tile_archive"
    assert result.rendered_tile_asset_ids == (asset.id,)
    assert result.verified_filesystem_asset_ids == (asset.id,)
