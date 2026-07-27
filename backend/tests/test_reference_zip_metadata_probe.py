from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import struct
import warnings
import zipfile

import pytest
from sqlalchemy import func, select

import app.reference_layers.zip_metadata_probe as zip_metadata_probe
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.idecyl_exact_evidence import (
    idecyl_exact_source_inventory,
)
from app.reference_layers.models import (
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceSourceArtifact,
    ReferenceSyncRun,
)
from app.reference_layers.safe_download import (
    HTTPSHeadResult,
    HTTPSRangeResult,
    UnsafeDownloadURLError,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
)
from app.reference_layers.zip_metadata_probe import (
    PROBE_MANIFEST_SCHEMA,
    ZipMetadataProbeError,
    ZipMetadataProbeLimits,
    ZipProbeResource,
    _probe_one_resource,
    build_sigpac_zip_metadata_probe_plan,
    metadata_probe_source_definition,
    probe_reviewed_zip_metadata,
    write_hash_bound_probe_manifest,
)
from support_reference_mirror_authorization import authorize_mirror_source


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 27, 16, tzinfo=timezone.utc)
METADATA_ONLY = {
    "metadata_probe": True,
    "dataset_download": False,
    "local_storage": False,
    "local_service": False,
    "bulk_tile_seed": False,
}


def _reviewed(layer_id: int = 123):
    return next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == layer_id
    )


def _plan(layer_id: int = 123):
    year = "2024" if layer_id == 123 else "2022"
    return build_sigpac_zip_metadata_probe_plan(
        _reviewed(layer_id),
        root_index=(FIXTURES / f"sigpac-{year}-root.html").read_bytes(),
        province_index=(FIXTURES / f"sigpac-{year}-provinces.html").read_bytes(),
    )


def _zip32(*names: str) -> bytes:
    sink = BytesIO()
    with zipfile.ZipFile(
        sink,
        mode="w",
        compression=zipfile.ZIP_STORED,
    ) as archive:
        for name in names or ("recintos.shp", "recintos.dbf"):
            archive.writestr(name, b"fixture")
    return sink.getvalue()


def _central_member(
    name: str,
    *,
    compressed_size: int,
    uncompressed_size: int,
    zip64: bool,
) -> bytes:
    encoded = name.encode()
    extra = b""
    stored_compressed = compressed_size
    stored_uncompressed = uncompressed_size
    stored_offset = 0
    stored_disk = 0
    if zip64:
        payload = struct.pack(
            "<QQQI",
            uncompressed_size,
            compressed_size,
            0,
            0,
        )
        extra = struct.pack("<HH", 0x0001, len(payload)) + payload
        stored_compressed = 0xFFFFFFFF
        stored_uncompressed = 0xFFFFFFFF
        stored_offset = 0xFFFFFFFF
        stored_disk = 0xFFFF
    return (
        struct.pack(
            "<4s6H3L5H2L",
            b"PK\x01\x02",
            45,
            45 if zip64 else 20,
            0x0800,
            0,
            0,
            0,
            0x12345678,
            stored_compressed,
            stored_uncompressed,
            len(encoded),
            len(extra),
            0,
            stored_disk,
            0,
            0,
            stored_offset,
        )
        + encoded
        + extra
    )


def _zip64() -> bytes:
    prefix = b"local-payload"
    central = _central_member(
        "recintos.shp",
        compressed_size=1,
        uncompressed_size=1,
        zip64=True,
    )
    directory_offset = len(prefix)
    zip64_offset = directory_offset + len(central)
    zip64_eocd = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        1,
        1,
        len(central),
        directory_offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1)
    eocd = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    return prefix + central + zip64_eocd + locator + eocd


class MemoryRangeClient:
    def __init__(
        self,
        bodies: dict[str, bytes],
        *,
        short_range: bool = False,
    ) -> None:
        self.bodies = bodies
        self.short_range = short_range
        self.requests: list[tuple[str, str, int | None, int | None]] = []

    def head(self, url: str, *, accept: str | None = None) -> HTTPSHeadResult:
        self.requests.append(("HEAD", url, None, None))
        body = self.bodies[url]
        return HTTPSHeadResult(
            source_url=url,
            final_url=url,
            status_code=200,
            content_type="application/zip",
            content_length=len(body),
            accept_ranges="bytes",
            etag='"fixture-v1"',
            last_modified="Mon, 27 Jul 2026 12:00:00 GMT",
            redirects=0,
            redirect_chain=(url,),
        )

    def download_range(
        self,
        url: str,
        sink,
        *,
        start: int,
        end: int,
        if_match: str,
        accept: str | None = None,
    ) -> HTTPSRangeResult:
        self.requests.append(("GET", url, start, end))
        body = self.bodies[url][start : end + 1]
        if self.short_range:
            body = body[:-1]
        sink.write(body)
        return HTTPSRangeResult(
            source_url=url,
            final_url=url,
            status_code=206,
            content_type="application/zip",
            content_length=end - start + 1,
            object_size=len(self.bodies[url]),
            range_start=start,
            range_end=end,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            etag=if_match,
            last_modified="Mon, 27 Jul 2026 12:00:00 GMT",
            redirects=0,
            redirect_chain=(url,),
        )


def _seed_probe_source(db, plan, *, provider_key: str):
    reviewed = _reviewed(plan.audit_layer_id)
    definition = ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://catalog.example.test/siur.json",
        raw_catalog={"revision": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service",
                title="IDECyL",
                upstream_protocol="wms",
                base_url=reviewed.catalog_endpoint_url,
                attribution=None,
                license_status="pending",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key=reviewed.catalog_layer_source_key,
                node_type="layer",
                title="SIGPAC",
                service_key="service",
                remote_name=reviewed.catalog_remote_name,
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
            ),
        ),
        retrieved_at=NOW,
    )
    apply_catalog_definition(db, definition)
    layer = db.scalar(
        select(ReferenceLayer).where(ReferenceLayer.provider_key == provider_key)
    )
    expected = metadata_probe_source_definition(plan)
    source = ReferenceLayerSource(
        provider_key=provider_key,
        layer_id=layer.id,
        source_key=f"probe:{plan.sha256[:32]}",
        protocol=expected["protocol"],
        target_kind=expected["target_kind"],
        endpoint_url=expected["endpoint_url"],
        remote_name=expected["remote_name"],
        sync_strategy=expected["sync_strategy"],
        priority=expected["priority"],
        config_json=expected["config"],
        definition_sha256=canonical_json_sha256(expected),
        enabled=False,
        is_primary=False,
    )
    db.add(source)
    db.commit()
    return source


@pytest.mark.parametrize("layer_id", [123, 166])
def test_local_indexes_build_exact_nine_resource_plan_but_keep_restricted(
    layer_id,
) -> None:
    reviewed = _reviewed(layer_id)
    plan = _plan(layer_id)

    assert reviewed.local_service_status == "restricted"
    assert len(plan.resources) == 9
    assert [item.resource_key for item in plan.resources] == [
        "avila",
        "burgos",
        "leon",
        "palencia",
        "salamanca",
        "segovia",
        "soria",
        "valladolid",
        "zamora",
    ]
    assert all(
        item.url.startswith(plan.province_directory_url) for item in plan.resources
    )

    service = ReferenceServiceDefinition(
        source_key="service",
        title="IDECyL",
        upstream_protocol="wms",
        base_url=reviewed.catalog_endpoint_url,
        attribution=None,
        license_status="pending",
    )
    layer = ReferenceLayerDefinition(
        source_key=reviewed.catalog_layer_source_key,
        node_type="layer",
        title="SIGPAC",
        service_key="service",
        remote_name=reviewed.catalog_remote_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
    )
    with pytest.raises(SourceDiscoveryError) as captured:
        acquisition_candidates(service, layer)
    assert captured.value.code == "idecyl_local_service_restricted"


def test_changed_or_extended_html_index_is_rejected() -> None:
    province = (
        (FIXTURES / "sigpac-2024-provinces.html")
        .read_bytes()
        .replace(
            b"</table>",
            b'<a href="EXTRA.zip">EXTRA.zip</a></table>',
        )
    )

    with pytest.raises(ZipMetadataProbeError) as captured:
        build_sigpac_zip_metadata_probe_plan(
            _reviewed(123),
            root_index=(FIXTURES / "sigpac-2024-root.html").read_bytes(),
            province_index=province,
        )

    assert captured.value.code == "directory_index_changed"


def test_resource_url_rejects_ssrf_shapes_before_network() -> None:
    with pytest.raises(UnsafeDownloadURLError):
        ZipProbeResource(
            resource_key="unsafe",
            url="https://127.0.0.1/private.zip",
        )


@pytest.mark.parametrize(
    ("archive", "expected_zip64"),
    [(_zip32(), False), (_zip64(), True)],
)
def test_zip32_and_zip64_central_directories_are_inventory_only(
    archive,
    expected_zip64,
) -> None:
    resource = ZipProbeResource(
        resource_key="avila",
        url="https://files.example.test/AVILA.zip",
    )
    client = MemoryRangeClient({resource.url: archive})

    observed = _probe_one_resource(
        client,
        resource,
        limits=ZipMetadataProbeLimits(),
    )

    assert observed["zip64"] is expected_zip64
    assert observed["member_count"] >= 1
    assert observed["members"][0]["name"] in {
        "recintos.dbf",
        "recintos.shp",
    }
    assert all(request[0] in {"HEAD", "GET"} for request in client.requests)


@pytest.mark.parametrize(
    ("archive", "limits", "expected_code"),
    [
        (_zip32("../escape.shp"), ZipMetadataProbeLimits(), "zip_path_traversal"),
        (
            _central_member(
                "recintos.shp",
                compressed_size=1,
                uncompressed_size=10_000,
                zip64=False,
            ),
            ZipMetadataProbeLimits(max_compression_ratio=10),
            "zip_bomb",
        ),
    ],
)
def test_unsafe_member_inventory_is_rejected(
    archive,
    limits,
    expected_code,
) -> None:
    if not archive.endswith(b"PK\x05\x06" + b"\0" * 18):
        if archive.startswith(b"PK\x01\x02"):
            central = archive
            archive = central + struct.pack(
                "<4s4H2LH",
                b"PK\x05\x06",
                0,
                0,
                1,
                1,
                len(central),
                0,
                0,
            )
    resource = ZipProbeResource(
        resource_key="avila",
        url="https://files.example.test/AVILA.zip",
    )

    with pytest.raises(ZipMetadataProbeError) as captured:
        _probe_one_resource(
            MemoryRangeClient({resource.url: archive}),
            resource,
            limits=limits,
        )

    assert captured.value.code == expected_code


def test_duplicate_and_truncated_member_evidence_is_rejected() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        duplicate = _zip32("recintos.shp", "recintos.shp")
    resource = ZipProbeResource(
        resource_key="avila",
        url="https://files.example.test/AVILA.zip",
    )
    with pytest.raises(ZipMetadataProbeError) as duplicate_error:
        _probe_one_resource(
            MemoryRangeClient({resource.url: duplicate}),
            resource,
            limits=ZipMetadataProbeLimits(),
        )
    assert duplicate_error.value.code == "zip_duplicate_member"

    with pytest.raises(ZipMetadataProbeError) as truncated_error:
        _probe_one_resource(
            MemoryRangeClient(
                {resource.url: _zip32()},
                short_range=True,
            ),
            resource,
            limits=ZipMetadataProbeLimits(),
        )
    assert truncated_error.value.code == "range_evidence_invalid"


def test_probe_requires_persisted_metadata_only_review_and_hashes_manifest(
    db,
    tmp_path,
) -> None:
    plan = _plan(123)
    source = _seed_probe_source(
        db,
        plan,
        provider_key="zip-probe-metadata-only",
    )
    bodies = {item.url: _zip32() for item in plan.resources}

    with pytest.raises(ZipMetadataProbeError) as missing:
        probe_reviewed_zip_metadata(
            db,
            source=source,
            plan=plan,
            captured_at=NOW,
            client=MemoryRangeClient(bodies),
        )
    assert missing.value.code == "mirror_authorization_missing"

    authorize_mirror_source(
        db,
        source,
        attribution=None,
        allowed_origins=["https://ftp.itacyl.es"],
        permission_overrides=METADATA_ONLY,
    )
    before = {
        model: db.scalar(select(func.count()).select_from(model))
        for model in (
            ReferenceSourceArtifact,
            ReferenceSyncRun,
            ReferenceDeliveryVersion,
        )
    }
    evidence = probe_reviewed_zip_metadata(
        db,
        source=source,
        plan=plan,
        captured_at=NOW,
        client=MemoryRangeClient(bodies),
    )
    after = {
        model: db.scalar(select(func.count()).select_from(model)) for model in before
    }

    assert evidence.manifest["schema"] == PROBE_MANIFEST_SCHEMA
    assert evidence.manifest["status"] == ("evidence_only_restricted_until_versioned")
    assert evidence.manifest["aggregate"]["resource_count"] == 9
    assert canonical_json_sha256(evidence.manifest) == evidence.manifest_sha256
    assert after == before
    output = tmp_path / "sigpac-probe.json"
    write_hash_bound_probe_manifest(output, evidence)
    assert output.read_bytes() == evidence.canonical_bytes()
    with pytest.raises(ZipMetadataProbeError) as overwrite:
        write_hash_bound_probe_manifest(output, evidence)
    assert overwrite.value.code == "output_exists"


def test_probe_rejects_review_with_download_or_storage_permission(db) -> None:
    plan = _plan(166)
    source = _seed_probe_source(
        db,
        plan,
        provider_key="zip-probe-overbroad-review",
    )
    authorize_mirror_source(
        db,
        source,
        allowed_origins=["https://ftp.itacyl.es"],
    )

    with pytest.raises(ZipMetadataProbeError) as captured:
        probe_reviewed_zip_metadata(
            db,
            source=source,
            plan=plan,
            captured_at=NOW,
            client=MemoryRangeClient({item.url: _zip32() for item in plan.resources}),
        )

    assert captured.value.code == "metadata_only_authorization_required"


def test_probe_revalidates_persisted_review_after_the_ninth_resource(db) -> None:
    plan = _plan(123)
    source = _seed_probe_source(
        db,
        plan,
        provider_key="zip-probe-review-race",
    )
    first = authorize_mirror_source(
        db,
        source,
        attribution=None,
        allowed_origins=["https://ftp.itacyl.es"],
        permission_overrides=METADATA_ONLY,
    )

    class MutatingClient(MemoryRangeClient):
        changed = False
        zamora_ranges = 0

        def download_range(self, url, sink, **kwargs):
            result = super().download_range(url, sink, **kwargs)
            if url.endswith("/ZAMORA.zip"):
                self.zamora_ranges += 1
            if self.zamora_ranges == 2 and not self.changed:
                self.changed = True
                authorize_mirror_source(
                    db,
                    source,
                    reviewed_at=NOW,
                    supersedes_review_sha256=first.review_sha256,
                    attribution=None,
                    allowed_origins=["https://ftp.itacyl.es"],
                    permission_overrides=METADATA_ONLY,
                )
            return result

    client = MutatingClient({item.url: _zip32() for item in plan.resources})
    with pytest.raises(ZipMetadataProbeError) as captured:
        probe_reviewed_zip_metadata(
            db,
            source=source,
            plan=plan,
            captured_at=NOW,
            client=client,
        )

    assert client.changed is True
    assert captured.value.code == "authorization_changed"


def test_operator_entrypoint_dry_run_and_hash_confirmed_apply(
    db,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    plan = _plan(166)
    source = _seed_probe_source(
        db,
        plan,
        provider_key="zip-probe-cli",
    )
    authorize_mirror_source(
        db,
        source,
        attribution=None,
        allowed_origins=["https://ftp.itacyl.es"],
        permission_overrides=METADATA_ONLY,
    )
    root = FIXTURES / "sigpac-2022-root.html"
    provinces = FIXTURES / "sigpac-2022-provinces.html"
    output = tmp_path / "sigpac-2022-evidence.json"
    monkeypatch.setattr(
        zip_metadata_probe,
        "register_all_models",
        lambda: None,
    )
    monkeypatch.setattr(
        zip_metadata_probe,
        "SessionLocal",
        lambda: nullcontext(db),
    )

    assert (
        zip_metadata_probe.main(
            [
                "--source-id",
                str(source.id),
                "--root-index",
                str(root),
                "--province-index",
                str(provinces),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["network_performed"] is False
    assert dry_run["delivery_mutation_performed"] is False
    assert dry_run["probe_plan_sha256"] == plan.sha256
    assert dry_run["resource_count"] == 9
    assert not output.exists()

    evidence = probe_reviewed_zip_metadata(
        db,
        source=source,
        plan=plan,
        captured_at=NOW,
        client=MemoryRangeClient({item.url: _zip32() for item in plan.resources}),
    )
    monkeypatch.setattr(
        zip_metadata_probe,
        "probe_reviewed_zip_metadata",
        lambda *_args, **_kwargs: evidence,
    )
    assert (
        zip_metadata_probe.main(
            [
                "--source-id",
                str(source.id),
                "--root-index",
                str(root),
                "--province-index",
                str(provinces),
                "--output",
                str(output),
                "--apply",
                "--expected-plan-sha256",
                plan.sha256,
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply"
    assert applied["network_performed"] is True
    assert applied["delivery_mutation_performed"] is False
    assert applied["manifest_sha256"] == evidence.manifest_sha256
    assert output.read_bytes() == evidence.canonical_bytes()
