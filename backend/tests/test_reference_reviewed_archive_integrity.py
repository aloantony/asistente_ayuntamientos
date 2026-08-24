from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from app.reference_layers.acquisition import (
    AcquisitionLimits,
    ReferenceAcquisitionPipeline,
    source_candidate_definition_sha256,
)
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.reviewed_archive_integrity import (
    INTEGRITY_SPEC_SCHEMA,
    ReviewedArchiveIntegrityError,
    configured_reviewed_archive_integrity,
    inspect_reviewed_archive,
    validate_reviewed_archive_response,
)
from app.reference_layers.safe_download import HTTPSDownloadResult
from app.reference_layers.source_discovery import SourceCandidate


LICENSE = (
    b"Reviewed IGCYL-NC fixture: acceptance, attribution and "
    b"conditional commercial license."
)


def _canonical_sha256(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _config() -> dict:
    profile = {
        "schema_version": INTEGRITY_SPEC_SCHEMA,
        "response_constraints": {
            "content_type": "application/x-zip-compressed",
            "max_content_length": 2 * 1024 * 1024,
            "require_etag": True,
            "require_last_modified": True,
        },
        "archive_constraints": {
            "max_entries": 8,
            "max_uncompressed_bytes": 4 * 1024 * 1024,
            "required_members": [
                "Licencia-IGCYL.txt",
                "roads.gpkg",
            ],
            "license_member": "Licencia-IGCYL.txt",
            "license_max_uncompressed_bytes": 64 * 1024,
            "license_sha256_allowlist": [
                hashlib.sha256(LICENSE).hexdigest()
            ],
        },
    }
    profile["spec_sha256"] = _canonical_sha256(profile)
    return {"reviewed_archive_integrity": profile}


def _archive(
    path: Path,
    *,
    dataset: bytes = b"dataset-v1",
    license_body: bytes = LICENSE,
) -> None:
    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("roads.gpkg", dataset)
        archive.writestr("Licencia-IGCYL.txt", license_body)


def _response(
    *,
    size_bytes: int,
    etag: str,
) -> HTTPSDownloadResult:
    return HTTPSDownloadResult(
        source_url="https://opendata.jcyl.es/roads.zip",
        final_url="https://opendata.jcyl.es/roads.zip",
        status_code=200,
        not_modified=False,
        content_type="application/x-zip-compressed",
        size_bytes=size_bytes,
        sha256="a" * 64,
        etag=etag,
        last_modified="Tue, 28 Jul 2026 08:00:00 GMT",
        redirects=0,
        redirect_chain=("https://opendata.jcyl.es/roads.zip",),
    )


class _Download:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __call__(self, _policy):
        return self

    def download(
        self,
        url,
        sink,
        *,
        etag=None,
        last_modified=None,
        accept=None,
    ) -> HTTPSDownloadResult:
        del etag, last_modified
        assert accept == "application/x-zip-compressed"
        sink.write(self.body)
        return HTTPSDownloadResult(
            source_url=url,
            final_url=url,
            status_code=200,
            not_modified=False,
            content_type="application/x-zip-compressed",
            size_bytes=len(self.body),
            sha256=hashlib.sha256(self.body).hexdigest(),
            etag='"shapefile-v2"',
            last_modified="Mon, 27 Jul 2026 08:00:00 GMT",
            redirects=0,
            redirect_chain=(url,),
        )


def _shapefile_candidate(config: dict) -> SourceCandidate:
    draft = SourceCandidate(
        protocol="download",
        target_kind="vector",
        endpoint_url="https://opendata.jcyl.es/hydro.zip",
        remote_name="hydro",
        sync_strategy="conditional_get",
        priority=5,
        config=config,
        source_key="source:reviewed-shapefile",
        definition_sha256="0" * 64,
    )
    return replace(
        draft,
        definition_sha256=source_candidate_definition_sha256(draft),
    )


def test_live_dataset_bytes_and_http_validators_may_change_within_bounds(
    tmp_path: Path,
) -> None:
    config = _config()
    first = tmp_path / "first.zip"
    changed = tmp_path / "changed.zip"
    _archive(first)
    _archive(changed, dataset=b"dataset-v2-with-more-bytes")

    first_observation = inspect_reviewed_archive(first, config=config)
    changed_observation = inspect_reviewed_archive(changed, config=config)
    response = validate_reviewed_archive_response(
        config,
        _response(size_bytes=changed.stat().st_size, etag='"v2"'),
    )

    assert first_observation["passed"] is True
    assert changed_observation["passed"] is True
    assert (
        first_observation["entries_sha256"]
        != changed_observation["entries_sha256"]
    )
    assert response["response"]["etag"] == '"v2"'


def test_pipeline_accepts_exact_reviewed_shapefile_set(tmp_path: Path) -> None:
    path = tmp_path / "hydro.zip"
    members = [
        "Licencia-IGCYL.txt",
        "hy.hidro_cyl_cursos.dbf",
        "hy.hidro_cyl_cursos.prj",
        "hy.hidro_cyl_cursos.shp",
        "hy.hidro_cyl_cursos.shx",
    ]
    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("Licencia-IGCYL.txt", LICENSE)
        for member in members[1:]:
            archive.writestr(member, f"live-{member}".encode())
    config = _config()
    profile = config["reviewed_archive_integrity"]
    profile["archive_constraints"]["required_members"] = members
    semantic = {
        key: value
        for key, value in profile.items()
        if key != "spec_sha256"
    }
    profile["spec_sha256"] = _canonical_sha256(semantic)
    config.update(
        {
            "media_type": "application/x-zip-compressed",
            "data_format": "shapefile-zip",
            "archive_member": "hy.hidro_cyl_cursos.shp",
            "input_layer": "hy.hidro_cyl_cursos",
        }
    )
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        result = ReferenceAcquisitionPipeline(
            store,
            limits=AcquisitionLimits(
                max_probe_bytes=256 * 1024,
                max_page_bytes=512 * 1024,
                max_dataset_bytes=2 * 1024 * 1024,
                max_total_bytes=4 * 1024 * 1024,
                page_size=100,
                max_pages=2,
                max_features=100,
                timeout_seconds=10,
                idle_timeout_seconds=2,
            ),
            downloader_factory=_Download(path.read_bytes()),
        ).acquire(_shapefile_candidate(config))
    finally:
        store.close()

    dataset = next(item for item in result.artifacts if item.role == "input")
    assert dataset.metadata["archive_member"] == (
        "hy.hidro_cyl_cursos.shp"
    )
    assert dataset.metadata["input_layer"] == "hy.hidro_cyl_cursos"
    assert dataset.metadata["reviewed_archive_integrity"]["passed"] is True


def test_license_or_required_member_drift_fails_closed(
    tmp_path: Path,
) -> None:
    config = _config()
    changed_license = tmp_path / "changed-license.zip"
    missing_dataset = tmp_path / "missing-dataset.zip"
    _archive(changed_license, license_body=b"different terms")
    with zipfile.ZipFile(
        missing_dataset,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("Licencia-IGCYL.txt", LICENSE)

    with pytest.raises(
        ReviewedArchiveIntegrityError,
        match="license content changed",
    ):
        inspect_reviewed_archive(changed_license, config=config)
    with pytest.raises(
        ReviewedArchiveIntegrityError,
        match="members changed",
    ):
        inspect_reviewed_archive(missing_dataset, config=config)


def test_profile_hash_and_response_bounds_fail_closed(tmp_path: Path) -> None:
    config = _config()
    tampered = json.loads(json.dumps(config))
    tampered["reviewed_archive_integrity"]["response_constraints"][
        "max_content_length"
    ] += 1

    with pytest.raises(
        ReviewedArchiveIntegrityError,
        match="hash is invalid",
    ):
        configured_reviewed_archive_integrity(tampered)
    with pytest.raises(
        ReviewedArchiveIntegrityError,
        match="HTTP bounds changed",
    ):
        validate_reviewed_archive_response(
            config,
            _response(
                size_bytes=3 * 1024 * 1024,
                etag='"oversized"',
            ),
        )
