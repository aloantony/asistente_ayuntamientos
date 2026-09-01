"""Closed administrative client for publishing immutable local map resources.

Only backend jobs may construct this client.  Hosts and REST paths are never
accepted from callers: the endpoint is the dedicated numeric-loopback
GeoServer instance and every catalog identifier is validated before a path is
built.  Existing immutable resources are checked, never overwritten.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import math
import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree
import zipfile

from PIL import Image, UnidentifiedImageError
from pydantic import SecretStr

from app.core.config import settings
from app.reference_layers.local_geoserver import (
    LocalGeoServerError,
    LocalGeoServerRenderer,
    UnsafeLocalGeoServerConfigurationError,
    validate_local_geoserver_base_url,
)
from app.reference_layers.wms_schemas import parse_feature_collection

JSON_CONTENT_TYPE = "application/json"
XML_CONTENT_TYPE = "application/xml"
SLD_CONTENT_TYPE = "application/vnd.ogc.sld+xml"
JSON_CONTENT_TYPES = frozenset({JSON_CONTENT_TYPE, "application/geo+json"})
XML_CONTENT_TYPES = frozenset({XML_CONTENT_TYPE, "text/xml"})
SLD_CONTENT_TYPES = frozenset(
    {SLD_CONTENT_TYPE, "application/xml", "text/xml"}
)
MAX_JSON_BYTES = 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_SLD_BYTES = 1024 * 1024
MAX_STYLE_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_STYLE_PACKAGE_RESOURCES = 512
MAX_ERROR_BYTES = 16 * 1024
MAX_REQUEST_TARGET_BYTES = 4096
MAX_CONTENT_LENGTH_DIGITS = 20
MAX_SMOKE_TILE_CANDIDATES = 64
MAX_SMOKE_ZOOM_CANDIDATES = 3
MAX_WEB_MERCATOR_ZOOM = 24
SMOKE_TARGET_TILE_COUNT = 16
SMOKE_COLOR_TOLERANCE = 24
# OGC 0.28 mm portrayal pixel at the EPSG:3857 zoom-zero resolution.
WEB_MERCATOR_SCALE_DENOMINATOR_Z0 = 559_082_264.0287178
WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066
GEOSERVER_ARTIFACT_ROOT = PurePosixPath("/mnt/reference_artifacts")
GEOWEBCACHE_TILE_BLOB_STORE_ID = "siur-tile-cache-v3"
GEOWEBCACHE_TILE_BLOB_STORE_DIRECTORY = "/var/lib/geowebcache"

RESOURCE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", re.ASCII)
TECHNICAL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", re.ASCII)
VERSIONED_NAME = re.compile(
    r"^[a-z][a-z0-9_]{0,52}_v_[a-z0-9]{6,32}$",
    re.ASCII,
)
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
STYLE_PACKAGE_RESOURCE = re.compile(
    r"^resources/(?P<sha>[0-9a-f]{64})\."
    r"(?P<extension>png|jpg|gif|svg|webp)$",
    re.ASCII,
)
SRS_NAME = re.compile(r"^EPSG:[1-9][0-9]{2,6}$", re.ASCII)
PROJECTION_POLICIES = frozenset(
    {"NONE", "FORCE_DECLARED", "REPROJECT_TO_DECLARED"}
)


class UnsafeGeoServerAdminConfigurationError(ValueError):
    """Administrative credentials or a fixed local dependency are unsafe."""


class InvalidGeoServerPublicationError(ValueError):
    """A publication description contains an unsafe or mutable identifier."""


class GeoServerAdminError(Exception):
    """Base class for closed GeoServer administration failures."""


class GeoServerAdminAuthenticationError(GeoServerAdminError):
    """GeoServer rejected the configured backend-only credentials."""


class GeoServerAdminUnavailableError(GeoServerAdminError):
    """GeoServer could not complete an administrative request."""


class GeoServerAdminResponseError(GeoServerAdminError):
    """GeoServer returned a malformed or unsafe administrative response."""


class GeoServerAdminConflictError(GeoServerAdminError):
    """An immutable name already exists with different configuration."""


class GeoServerLayerSmokeError(GeoServerAdminError):
    """A published layer did not pass a local catalog/render smoke test."""


@dataclass(frozen=True)
class PublicationResult:
    kind: Literal[
        "workspace",
        "postgis_datastore",
        "versioned_table",
        "geotiff_store",
        "geotiff_coverage",
        "style",
        "layer_style",
    ]
    name: str
    created: bool


@dataclass(frozen=True)
class GeoServerHealth:
    version: str


@dataclass(frozen=True)
class GeoWebCacheDiskQuota:
    enabled: bool
    quota_bytes: int
    quota_value: int
    quota_units: Literal["MiB", "GiB", "TiB"]
    cleanup_frequency: int
    cleanup_units: Literal["SECONDS", "MINUTES", "HOURS", "DAYS"]
    max_concurrent_cleanups: int
    expiration_policy: Literal["LRU", "LFU"]


@dataclass(frozen=True)
class GeoWebCacheQuotaHealth:
    healthy: bool
    monitor_enabled: bool
    monitor_running: bool
    scheduled_cleanup_active: bool
    provider_error: bool
    store_class: str | None
    dialect_class: str | None
    global_used_bytes: int | None


@dataclass(frozen=True)
class GeoWebCacheFileBlobStore:
    id: str
    enabled: bool
    default: bool
    base_directory: str
    file_system_block_size: int
    path_generator_type: Literal["DEFAULT", "TMS", "SLIPPY"]


@dataclass(frozen=True)
class LayerSmokeResult:
    layer_name: str
    style_name: str | None
    image_sha256: str
    image_bytes: int
    image_content_type: str
    z: int
    x: int
    y: int
    legend_sha256: str | None = None
    legend_bytes: int | None = None
    legend_content_type: str | None = None
    identify_sha256: str | None = None
    identify_bytes: int | None = None
    identify_content_type: str | None = None
    identify_feature_count: int | None = None
    pixel_x: int | None = None
    pixel_y: int | None = None
    visible_pixel_count: int = 0
    coverage_required: bool = False
    attempted_tile_count: int = 1
    expected_style_colors: tuple[str, ...] = ()
    matched_style_color: str | None = None


@dataclass(frozen=True)
class _AdminResponse:
    status: int
    body: bytes
    content_type: str | None


ConnectionFactory = Callable[[str, int, float], http.client.HTTPConnection]


class GeoServerAdminClient:
    """Idempotent publisher for the fixed local GeoServer catalog.

    The object intentionally has no public generic HTTP method.  This prevents
    database values, browser input, or future API parameters from selecting a
    host, REST path, arbitrary file URL, or destructive operation.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        workspace: str | None = None,
        timeout_seconds: float | None = None,
        admin_user: str | None = None,
        admin_password: SecretStr | None = None,
        postgis_host: str | None = None,
        postgis_port: int | None = None,
        postgis_database: str | None = None,
        postgis_user: str | None = None,
        postgis_password: SecretStr | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        try:
            self._endpoint = validate_local_geoserver_base_url(
                settings.local_geoserver_base_url
                if base_url is None
                else base_url
            )
        except UnsafeLocalGeoServerConfigurationError as error:
            raise UnsafeGeoServerAdminConfigurationError(
                "invalid local GeoServer administrative endpoint"
            ) from error
        self._workspace = _validate_resource_name(
            settings.local_geoserver_workspace if workspace is None else workspace,
            label="workspace",
            configuration=True,
        )
        configured_timeout = (
            settings.local_geoserver_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if (
            isinstance(configured_timeout, bool)
            or not isinstance(configured_timeout, (int, float))
            or not isfinite(float(configured_timeout))
            or not 0.1 <= float(configured_timeout) <= 30.0
        ):
            raise UnsafeGeoServerAdminConfigurationError(
                "invalid local GeoServer administrative timeout"
            )
        self._timeout_seconds = float(configured_timeout)

        configured_user = (
            settings.geoserver_admin_user if admin_user is None else admin_user
        )
        configured_password = (
            settings.geoserver_admin_password
            if admin_password is None
            else admin_password
        )
        self._authorization = _basic_authorization(
            configured_user,
            configured_password,
        )

        self._postgis_host = _validate_postgis_host(
            settings.local_geoserver_postgis_host
            if postgis_host is None
            else postgis_host
        )
        configured_port = (
            settings.local_geoserver_postgis_port
            if postgis_port is None
            else postgis_port
        )
        if (
            isinstance(configured_port, bool)
            or not isinstance(configured_port, int)
            or not 1 <= configured_port <= 65535
        ):
            raise UnsafeGeoServerAdminConfigurationError(
                "invalid local GeoServer PostGIS port"
            )
        self._postgis_port = configured_port
        self._postgis_database = _validate_connection_name(
            settings.local_geoserver_postgis_database
            if postgis_database is None
            else postgis_database,
            "PostGIS database",
        )
        self._postgis_user = _validate_connection_name(
            settings.local_geoserver_postgis_user
            if postgis_user is None
            else postgis_user,
            "PostGIS user",
        )
        configured_postgis_password = (
            settings.local_geoserver_postgis_password
            if postgis_password is None
            else postgis_password
        )
        if configured_postgis_password is not None and not isinstance(
            configured_postgis_password, SecretStr
        ):
            raise UnsafeGeoServerAdminConfigurationError(
                "invalid local GeoServer PostGIS credentials"
            )
        if configured_postgis_password is not None:
            _validate_secret(configured_postgis_password, "PostGIS credentials")
        self._postgis_password = configured_postgis_password
        self._connection_factory = connection_factory or _http_connection
        self._rest_root = f"{self._endpoint.path}/rest"
        self._gwc_rest_root = f"{self._endpoint.path}/gwc/rest"

    def __repr__(self) -> str:
        return (
            "GeoServerAdminClient("
            f"endpoint={self.endpoint_url!r}, workspace={self._workspace!r}, "
            "credentials=***, postgis_credentials=***)"
        )

    @property
    def endpoint_url(self) -> str:
        return (
            f"http://127.0.0.1:{self._endpoint.port}"
            f"{self._endpoint.path}"
        )

    def health(self) -> GeoServerHealth:
        payload = self._get_json("/about/version.json")
        if payload is None:
            raise GeoServerAdminResponseError(
                "local GeoServer version resource is missing"
            )
        about = payload.get("about")
        if not isinstance(about, Mapping):
            raise GeoServerAdminResponseError(
                "invalid local GeoServer version response"
            )
        resources = about.get("resource")
        if isinstance(resources, Mapping):
            candidates = [resources]
        elif isinstance(resources, list):
            candidates = resources
        else:
            raise GeoServerAdminResponseError(
                "invalid local GeoServer version response"
            )
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            if candidate.get("@name") != "GeoServer":
                continue
            version = candidate.get("Version")
            if isinstance(version, str) and 0 < len(version.strip()) <= 128:
                return GeoServerHealth(version=version.strip())
        raise GeoServerAdminResponseError(
            "invalid local GeoServer version response"
        )

    def read_geowebcache_disk_quota(self) -> GeoWebCacheDiskQuota:
        """Read the fixed GeoWebCache global disk-quota configuration."""

        # GeoServer 3.0.0 / GeoWebCache 2.0.0 returns an XML body even from
        # ``diskquota.json`` while advertising application/json.  The fixed
        # XML representation is therefore the only response accepted here.
        payload = self._get_xml(
            "/diskquota.xml",
            request_root=self._gwc_rest_root,
        )
        return _parse_geowebcache_disk_quota(payload)

    def read_geowebcache_quota_health(self) -> GeoWebCacheQuotaHealth:
        """Read the version-pinned extension's real monitor/store health."""

        payload = self._get_json(
            "/siur/gwc-quota-health",
            request_root=self._rest_root,
        )
        if payload is None:  # pragma: no cover - GET does not allow 404.
            raise GeoServerAdminResponseError(
                "GeoWebCache quota health resource is missing"
            )
        return _parse_geowebcache_quota_health(payload)

    def read_geowebcache_file_blob_stores(
        self,
    ) -> tuple[GeoWebCacheFileBlobStore, ...]:
        """Read every configured standard FileBlobStore through fixed GWC REST.

        GeoWebCache's anonymous legacy default is intentionally absent from
        this resource. Any configured non-file implementation is rejected:
        this closed client cannot prove its default or storage semantics.
        """

        listing = self._get_xml(
            "/blobstores.xml",
            request_root=self._gwc_rest_root,
        )
        names = _parse_geowebcache_blob_store_names(listing)
        stores: list[GeoWebCacheFileBlobStore] = []
        for name in names:
            payload = self._get_xml(
                f"/blobstores/{_segment(name)}.xml",
                request_root=self._gwc_rest_root,
            )
            store = _parse_geowebcache_file_blob_store(payload)
            if store.id != name:
                raise GeoServerAdminResponseError(
                    "GeoWebCache blob store list and resource differ"
                )
            stores.append(store)
        return tuple(sorted(stores, key=lambda item: item.id))

    def ensure_geowebcache_tile_blob_store(
        self,
        *,
        file_system_block_size: int,
        allow_file_system_block_size_migration: bool = False,
    ) -> GeoWebCacheFileBlobStore:
        """Create or verify the one fixed default tile-only FileBlobStore.

        A differing store with the reserved id, or any other configured
        blobstore, fails before PUT. The upsert endpoint is used only after
        proving the complete list is empty, then the complete list and exact
        resource are re-read. The sole mutation of existing configuration is
        an explicitly authorized block-size-only migration; its caller must
        first prove the reconstructible tile volume is empty.
        """

        if not isinstance(allow_file_system_block_size_migration, bool):
            raise InvalidGeoServerPublicationError(
                "GeoWebCache block-size migration flag is invalid"
            )
        expected = expected_geowebcache_tile_blob_store(
            file_system_block_size=file_system_block_size,
        )
        before = self.read_geowebcache_file_blob_stores()
        current = _validate_geowebcache_tile_blob_store_set(
            before,
            expected=expected,
            require_present=False,
            allow_file_system_block_size_migration=(
                allow_file_system_block_size_migration
            ),
        )
        if current == expected:
            return current

        self._put_xml(
            f"/blobstores/{_segment(expected.id)}.xml",
            _serialize_geowebcache_file_blob_store(expected),
            request_root=self._gwc_rest_root,
            expected_status=201 if current is None else 200,
        )
        after = self.read_geowebcache_file_blob_stores()
        current = _validate_geowebcache_tile_blob_store_set(
            after,
            expected=expected,
            require_present=True,
        )
        if current is None:  # pragma: no cover - guarded by require_present.
            raise GeoServerAdminConflictError(
                "GeoWebCache tile blob store is missing after configuration"
            )
        return current

    def ensure_workspace(self) -> PublicationResult:
        name = self._workspace
        get_path = (
            f"/workspaces/{_segment(name)}.json?quietOnNotFound=true"
        )
        existing = self._get_json(get_path, allow_not_found=True)
        if existing is not None:
            _validate_workspace_payload(existing, name)
            return PublicationResult("workspace", name, False)

        status = self._post_json(
            "/workspaces",
            {"workspace": {"name": name}},
        )
        if status == 201:
            return PublicationResult("workspace", name, True)
        existing = self._get_json(get_path, allow_not_found=True)
        if existing is None:
            raise GeoServerAdminConflictError(
                "local GeoServer workspace creation conflicted"
            )
        _validate_workspace_payload(existing, name)
        return PublicationResult("workspace", name, False)

    def ensure_postgis_datastore(
        self,
        *,
        datastore_name: str,
        schema_name: str,
    ) -> PublicationResult:
        datastore = _validate_resource_name(datastore_name, "datastore")
        schema = _validate_technical_name(schema_name, "PostGIS schema")
        password = self._require_postgis_password()
        self.ensure_workspace()
        get_path = (
            f"/workspaces/{_segment(self._workspace)}/datastores/"
            f"{_segment(datastore)}.json?quietOnNotFound=true"
        )
        expected_parameters = {
            "dbtype": "postgis",
            "host": self._postgis_host,
            "port": str(self._postgis_port),
            "database": self._postgis_database,
            "schema": schema,
            "user": self._postgis_user,
        }
        existing = self._get_json(get_path, allow_not_found=True)
        if existing is not None:
            _validate_datastore_payload(
                existing,
                name=datastore,
                expected_parameters=expected_parameters,
            )
            return PublicationResult("postgis_datastore", datastore, False)

        entries = [
            {"@key": key, "$": value}
            for key, value in expected_parameters.items()
        ]
        entries.append({"@key": "passwd", "$": password})
        status = self._post_json(
            f"/workspaces/{_segment(self._workspace)}/datastores",
            {
                "dataStore": {
                    "name": datastore,
                    "type": "PostGIS",
                    "enabled": True,
                    "workspace": {"name": self._workspace},
                    "connectionParameters": {"entry": entries},
                }
            },
        )
        if status == 201:
            return PublicationResult("postgis_datastore", datastore, True)
        existing = self._get_json(get_path, allow_not_found=True)
        if existing is None:
            raise GeoServerAdminConflictError(
                "local GeoServer datastore creation conflicted"
            )
        _validate_datastore_payload(
            existing,
            name=datastore,
            expected_parameters=expected_parameters,
        )
        return PublicationResult("postgis_datastore", datastore, False)

    def publish_versioned_table(
        self,
        *,
        datastore_name: str,
        schema_name: str,
        table_name: str,
        layer_name: str,
        title: str,
        declared_srs: str,
        projection_policy: str = "REPROJECT_TO_DECLARED",
    ) -> PublicationResult:
        datastore = _validate_resource_name(datastore_name, "datastore")
        schema = _validate_technical_name(schema_name, "PostGIS schema")
        table = _validate_versioned_name(table_name, "versioned table")
        layer = _validate_versioned_name(layer_name, "versioned layer")
        display_title = _validate_title(title)
        srs = _validate_srs(declared_srs)
        policy = _validate_projection_policy(projection_policy)
        self.ensure_postgis_datastore(
            datastore_name=datastore,
            schema_name=schema,
        )

        get_path = (
            f"/workspaces/{_segment(self._workspace)}/datastores/"
            f"{_segment(datastore)}/featuretypes/{_segment(layer)}.json"
            "?quietOnNotFound=true"
        )
        expected = {
            "name": layer,
            "nativeName": table,
            "title": display_title,
            "enabled": True,
            "srs": srs,
            "projectionPolicy": policy,
        }
        existing = self._get_json(get_path, allow_not_found=True)
        if existing is not None:
            _validate_feature_type_payload(existing, expected)
            return PublicationResult("versioned_table", layer, False)

        status = self._post_json(
            f"/workspaces/{_segment(self._workspace)}/datastores/"
            f"{_segment(datastore)}/featuretypes",
            {"featureType": expected},
        )
        if status == 201:
            return PublicationResult("versioned_table", layer, True)
        existing = self._get_json(get_path, allow_not_found=True)
        if existing is None:
            raise GeoServerAdminConflictError(
                "local GeoServer feature type creation conflicted"
            )
        _validate_feature_type_payload(existing, expected)
        return PublicationResult("versioned_table", layer, False)

    def publish_local_geotiff(
        self,
        *,
        store_name: str,
        layer_name: str,
        artifact_relative_path: str,
        title: str,
        native_name: str | None = None,
    ) -> tuple[PublicationResult, PublicationResult]:
        store = _validate_versioned_name(store_name, "GeoTIFF store")
        layer = _validate_versioned_name(layer_name, "GeoTIFF layer")
        relative_path = _validate_artifact_relative_path(artifact_relative_path)
        display_title = _validate_title(title)
        inferred_native_name = relative_path.stem
        native = _validate_resource_name(
            inferred_native_name if native_name is None else native_name,
            "GeoTIFF native name",
        )
        artifact_uri = f"file:{GEOSERVER_ARTIFACT_ROOT / relative_path}"
        self.ensure_workspace()

        store_get_path = (
            f"/workspaces/{_segment(self._workspace)}/coveragestores/"
            f"{_segment(store)}.json?quietOnNotFound=true"
        )
        existing_store = self._get_json(store_get_path, allow_not_found=True)
        if existing_store is not None:
            _validate_coverage_store_payload(
                existing_store,
                name=store,
                artifact_uri=artifact_uri,
            )
            store_result = PublicationResult("geotiff_store", store, False)
        else:
            status = self._post_json(
                f"/workspaces/{_segment(self._workspace)}/coveragestores",
                {
                    "coverageStore": {
                        "name": store,
                        "workspace": {"name": self._workspace},
                        "enabled": True,
                        "type": "GeoTIFF",
                        "url": artifact_uri,
                    }
                },
            )
            if status == 201:
                store_result = PublicationResult("geotiff_store", store, True)
            else:
                existing_store = self._get_json(
                    store_get_path,
                    allow_not_found=True,
                )
                if existing_store is None:
                    raise GeoServerAdminConflictError(
                        "local GeoServer coverage store creation conflicted"
                    )
                _validate_coverage_store_payload(
                    existing_store,
                    name=store,
                    artifact_uri=artifact_uri,
                )
                store_result = PublicationResult("geotiff_store", store, False)

        coverage_get_path = (
            f"/workspaces/{_segment(self._workspace)}/coveragestores/"
            f"{_segment(store)}/coverages/{_segment(layer)}.json"
            "?quietOnNotFound=true"
        )
        expected_coverage = {
            "name": layer,
            "nativeName": native,
            "title": display_title,
            "enabled": True,
        }
        existing_coverage = self._get_json(
            coverage_get_path,
            allow_not_found=True,
        )
        if existing_coverage is not None:
            _validate_coverage_payload(existing_coverage, expected_coverage)
            coverage_result = PublicationResult(
                "geotiff_coverage",
                layer,
                False,
            )
        else:
            status = self._post_json(
                f"/workspaces/{_segment(self._workspace)}/coveragestores/"
                f"{_segment(store)}/coverages",
                {"coverage": expected_coverage},
            )
            if status == 201:
                coverage_result = PublicationResult(
                    "geotiff_coverage",
                    layer,
                    True,
                )
            else:
                existing_coverage = self._get_json(
                    coverage_get_path,
                    allow_not_found=True,
                )
                if existing_coverage is None:
                    raise GeoServerAdminConflictError(
                        "local GeoServer coverage creation conflicted"
                    )
                _validate_coverage_payload(existing_coverage, expected_coverage)
                coverage_result = PublicationResult(
                    "geotiff_coverage",
                    layer,
                    False,
                )
        return store_result, coverage_result

    def publish_immutable_sld(
        self,
        *,
        style_name: str,
        sld: bytes | str,
    ) -> PublicationResult:
        style = _validate_versioned_name(style_name, "immutable style")
        body = _validate_sld(sld)
        self.ensure_workspace()
        get_path = (
            f"/workspaces/{_segment(self._workspace)}/styles/"
            f"{_segment(style)}.sld?quietOnNotFound=true"
        )
        existing = self._get_sld(get_path, allow_not_found=True)
        if existing is not None:
            _assert_same_bytes(existing, body, "style")
            return PublicationResult("style", style, False)

        status = self._post_raw(
            f"/workspaces/{_segment(self._workspace)}/styles?"
            + urlencode({"name": style, "raw": "true"}),
            body=body,
            content_type=SLD_CONTENT_TYPE,
        )
        if status == 201:
            return PublicationResult("style", style, True)
        existing = self._get_sld(get_path, allow_not_found=True)
        if existing is None:
            raise GeoServerAdminConflictError(
                "local GeoServer style creation conflicted"
            )
        _assert_same_bytes(existing, body, "style")
        return PublicationResult("style", style, False)

    def publish_immutable_sld_package(
        self,
        *,
        style_name: str,
        package: bytes,
        expected_sld_sha256: str,
    ) -> PublicationResult:
        """Publish one closed SLD ZIP with content-addressed local graphics."""

        style = _validate_versioned_name(style_name, "immutable style")
        body, sld = _validate_sld_package(
            package,
            expected_sld_sha256=expected_sld_sha256,
        )
        self.ensure_workspace()
        get_path = (
            f"/workspaces/{_segment(self._workspace)}/styles/"
            f"{_segment(style)}.sld?quietOnNotFound=true"
        )
        existing = self._get_sld(get_path, allow_not_found=True)
        if existing is not None:
            _assert_same_bytes(existing, sld, "style")
            return PublicationResult("style", style, False)

        status = self._post_raw(
            f"/workspaces/{_segment(self._workspace)}/styles?"
            + urlencode({"name": style, "raw": "true"}),
            body=body,
            content_type="application/zip",
            max_body_bytes=MAX_STYLE_PACKAGE_BYTES,
        )
        if status == 201:
            return PublicationResult("style", style, True)
        existing = self._get_sld(get_path, allow_not_found=True)
        if existing is None:
            raise GeoServerAdminConflictError(
                "local GeoServer style package creation conflicted"
            )
        _assert_same_bytes(existing, sld, "style")
        return PublicationResult("style", style, False)

    def ensure_layer_style(
        self,
        *,
        layer_name: str,
        style_name: str,
    ) -> PublicationResult:
        """Associate an immutable workspace style with a versioned layer.

        GeoServer keeps publishing a generic default style when a feature type
        or coverage is first created.  A workspace SLD must also be present in
        the layer's advertised style set before the qualified style can be
        selected reliably through WMS.  This method only appends that
        association; it never replaces the layer's default style or edits an
        existing style.
        """

        layer = _validate_versioned_name(layer_name, "versioned layer")
        style = _validate_versioned_name(style_name, "immutable style")
        self.ensure_workspace()

        existing_style = self._get_sld(
            f"/workspaces/{_segment(self._workspace)}/styles/"
            f"{_segment(style)}.sld?quietOnNotFound=true",
            allow_not_found=True,
        )
        if existing_style is None:
            raise GeoServerAdminConflictError(
                "local GeoServer immutable style is not published"
            )

        qualified_layer = (
            f"{_segment(self._workspace)}:{_segment(layer)}"
        )
        layer_payload = self._get_json(
            f"/layers/{qualified_layer}.json?quietOnNotFound=true",
            allow_not_found=True,
        )
        if layer_payload is None:
            raise GeoServerAdminConflictError(
                "local GeoServer versioned layer is not published"
            )
        _validate_publication_layer_payload(layer_payload, layer)

        styles_path = f"/layers/{qualified_layer}/styles.json"
        existing_styles = self._get_json(styles_path)
        if existing_styles is None:  # pragma: no cover - required by typing
            raise GeoServerAdminResponseError(
                "invalid local GeoServer layer styles response"
            )
        if _layer_styles_contain(
            existing_styles,
            style,
            layer_name=layer,
            workspace=self._workspace,
            rest_root=self._rest_root,
        ):
            return PublicationResult("layer_style", style, False)

        status = self._post_json(
            f"/layers/{qualified_layer}/styles",
            {
                "style": {
                    # StyleController resolves workspace styles by qualified
                    # catalog name, even though each style's stored name is
                    # unqualified.
                    "name": f"{self._workspace}:{style}",
                }
            },
            # GeoServer's layer-style controller negotiates an empty response
            # and returns 406 when the generic JSON helper requests JSON.
            # The resource is re-read and validated below, so accepting any
            # response representation does not weaken the closed client.
            accept="*/*",
        )
        if status == 201:
            return PublicationResult("layer_style", style, True)

        # GeoServer may report an already-created style association as 403 or
        # 409.  Only accept it as a benign race after an authenticated read
        # proves that the exact immutable style is now associated.
        existing_styles = self._get_json(styles_path)
        if existing_styles is None:  # pragma: no cover - required by typing
            raise GeoServerAdminResponseError(
                "invalid local GeoServer layer styles response"
            )
        if not _layer_styles_contain(
            existing_styles,
            style,
            layer_name=layer,
            workspace=self._workspace,
            rest_root=self._rest_root,
        ):
            raise GeoServerAdminConflictError(
                "local GeoServer layer style association conflicted"
            )
        return PublicationResult("layer_style", style, False)

    def smoke_layer(
        self,
        *,
        layer_name: str,
        style_name: str | None,
        legend_available: bool = False,
        identify_available: bool = False,
        z: int = 0,
        x: int = 0,
        y: int = 0,
        pixel_x: int = 128,
        pixel_y: int = 128,
        bounds: Mapping[str, float] | None = None,
        zoom_candidates: tuple[int, ...] | None = None,
        coverage_required: bool = False,
        progress_callback: Callable[[], None] | None = None,
        expected_style_colors: tuple[str, ...] = (),
    ) -> LayerSmokeResult:
        layer = _validate_versioned_name(layer_name, "versioned layer")
        style = (
            None
            if style_name is None
            else _validate_versioned_name(style_name, "immutable style")
        )
        if not isinstance(coverage_required, bool):
            raise InvalidGeoServerPublicationError(
                "local GeoServer smoke coverage flag is invalid"
            )
        if progress_callback is not None and not callable(progress_callback):
            raise InvalidGeoServerPublicationError(
                "local GeoServer smoke progress callback is invalid"
            )
        expected_colors = _validate_smoke_colors(expected_style_colors)
        if bounds is None:
            if zoom_candidates is not None:
                raise InvalidGeoServerPublicationError(
                    "local GeoServer smoke zooms require bounds"
                )
            coordinates = ((z, x, y),)
        else:
            if zoom_candidates is None:
                zoom_candidates = style_smoke_zoom_candidates(
                    bounds=bounds,
                )
            selected_coordinates: list[tuple[int, int, int]] = []
            for zoom in _validate_smoke_zooms(zoom_candidates):
                remaining = (
                    MAX_SMOKE_TILE_CANDIDATES
                    - len(selected_coordinates)
                )
                if remaining == 0:
                    break
                selected_coordinates.extend(
                    _representative_tile_coordinates(
                        bounds,
                        zoom,
                        limit=remaining,
                    )
                )
            coordinates = tuple(selected_coordinates)
            if not coordinates:
                raise InvalidGeoServerPublicationError(
                    "local GeoServer smoke coverage is empty"
                )
        payload = self._get_json(
            f"/layers/{_segment(self._workspace)}:{_segment(layer)}.json"
            "?quietOnNotFound=true",
            allow_not_found=True,
        )
        if payload is None:
            raise GeoServerLayerSmokeError(
                "local GeoServer layer is not published"
            )
        _validate_layer_payload(payload, layer)
        renderer = LocalGeoServerRenderer(
            base_url=self.endpoint_url,
            workspace=self._workspace,
            timeout_seconds=self._timeout_seconds,
            connection_factory=self._connection_factory,
        )
        response = None
        visible_pixel_count = 0
        matched_style_color = None
        attempted_tile_count = 0
        selected_z = z
        selected_x = x
        selected_y = y
        try:
            for selected_z, selected_x, selected_y in coordinates:
                if progress_callback is not None:
                    progress_callback()
                response = renderer.render_tile(
                    layer_name=layer,
                    style_name=style,
                    z=selected_z,
                    x=selected_x,
                    y=selected_y,
                )
                attempted_tile_count += 1
                (
                    visible_pixel_count,
                    matched_style_color,
                ) = _analyze_smoke_png(
                    response.body,
                    expected_colors=expected_colors,
                )
                if not coverage_required or (
                    visible_pixel_count > 0
                    and (
                        not expected_colors
                        or matched_style_color is not None
                    )
                ):
                    break
            if response is None or (
                coverage_required
                and (
                    visible_pixel_count == 0
                    or (
                        expected_colors
                        and matched_style_color is None
                    )
                )
            ):
                raise GeoServerLayerSmokeError(
                    "local GeoServer layer did not render required coverage "
                    "and style"
                )
            legend_response = (
                renderer.render_legend(
                    layer_name=layer,
                    style_name=style,
                )
                if legend_available
                else None
            )
            identify_response = (
                renderer.get_feature_info(
                    layer_name=layer,
                    style_name=style,
                    z=selected_z,
                    x=selected_x,
                    y=selected_y,
                    pixel_x=pixel_x,
                    pixel_y=pixel_y,
                    feature_count=1,
                )
                if identify_available
                else None
            )
        except LocalGeoServerError as error:
            raise GeoServerLayerSmokeError(
                "local GeoServer layer operation smoke failed"
            ) from error
        identify_feature_count = None
        if identify_response is not None:
            feature_collection = parse_feature_collection(
                identify_response.body,
                max_features=1,
            )
            identify_feature_count = len(feature_collection["features"])
        return LayerSmokeResult(
            layer_name=layer,
            style_name=style,
            image_sha256=hashlib.sha256(response.body).hexdigest(),
            image_bytes=len(response.body),
            image_content_type=response.content_type,
            z=selected_z,
            x=selected_x,
            y=selected_y,
            legend_sha256=(
                hashlib.sha256(legend_response.body).hexdigest()
                if legend_response is not None
                else None
            ),
            legend_bytes=(
                len(legend_response.body)
                if legend_response is not None
                else None
            ),
            legend_content_type=(
                legend_response.content_type
                if legend_response is not None
                else None
            ),
            identify_sha256=(
                hashlib.sha256(identify_response.body).hexdigest()
                if identify_response is not None
                else None
            ),
            identify_bytes=(
                len(identify_response.body)
                if identify_response is not None
                else None
            ),
            identify_content_type=(
                identify_response.content_type
                if identify_response is not None
                else None
            ),
            identify_feature_count=identify_feature_count,
            pixel_x=pixel_x if identify_response is not None else None,
            pixel_y=pixel_y if identify_response is not None else None,
            visible_pixel_count=visible_pixel_count,
            coverage_required=coverage_required,
            attempted_tile_count=attempted_tile_count,
            expected_style_colors=expected_colors,
            matched_style_color=matched_style_color,
        )

    def _require_postgis_password(self) -> str:
        if self._postgis_password is None:
            raise UnsafeGeoServerAdminConfigurationError(
                "local GeoServer PostGIS credentials are not configured"
            )
        return self._postgis_password.get_secret_value()

    def _get_json(
        self,
        path: str,
        *,
        allow_not_found: bool = False,
        request_root: str | None = None,
    ) -> dict[str, Any] | None:
        expected = frozenset({200, 404} if allow_not_found else {200})
        response = self._request(
            "GET",
            path,
            accept=JSON_CONTENT_TYPE,
            expected_statuses=expected,
            max_response_bytes=MAX_JSON_BYTES,
            request_root=request_root,
        )
        if response.status == 404:
            return None
        if response.content_type not in JSON_CONTENT_TYPES:
            raise GeoServerAdminResponseError(
                "unexpected local GeoServer JSON content type"
            )
        try:
            payload = json.loads(
                response.body,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except GeoServerAdminResponseError:
            raise
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as error:
            raise GeoServerAdminResponseError(
                "invalid local GeoServer JSON response"
            ) from error
        if not isinstance(payload, dict):
            raise GeoServerAdminResponseError(
                "invalid local GeoServer JSON response"
            )
        return payload

    def _get_sld(
        self,
        path: str,
        *,
        allow_not_found: bool,
    ) -> bytes | None:
        response = self._request(
            "GET",
            path,
            accept=SLD_CONTENT_TYPE,
            expected_statuses=frozenset(
                {200, 404} if allow_not_found else {200}
            ),
            max_response_bytes=MAX_SLD_BYTES,
        )
        if response.status == 404:
            return None
        if response.content_type not in SLD_CONTENT_TYPES:
            raise GeoServerAdminResponseError(
                "unexpected local GeoServer style content type"
            )
        if not response.body:
            raise GeoServerAdminResponseError(
                "empty local GeoServer style response"
            )
        return response.body

    def _get_xml(
        self,
        path: str,
        *,
        request_root: str,
    ) -> bytes:
        response = self._request(
            "GET",
            path,
            accept=XML_CONTENT_TYPE,
            expected_statuses=frozenset({200}),
            max_response_bytes=MAX_XML_BYTES,
            request_root=request_root,
        )
        if response.content_type not in XML_CONTENT_TYPES:
            raise GeoServerAdminResponseError(
                "unexpected local GeoServer XML content type"
            )
        if not response.body:
            raise GeoServerAdminResponseError(
                "empty local GeoServer XML response"
            )
        return response.body

    def _post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        accept: str = JSON_CONTENT_TYPE,
    ) -> int:
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return self._post_raw(
            path,
            body=body,
            content_type=JSON_CONTENT_TYPE,
            accept=accept,
        )

    def _post_raw(
        self,
        path: str,
        *,
        body: bytes,
        content_type: str,
        accept: str = JSON_CONTENT_TYPE,
        max_body_bytes: int = MAX_SLD_BYTES,
    ) -> int:
        if (
            not body
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 1
            or len(body) > max_body_bytes
        ):
            raise InvalidGeoServerPublicationError(
                "local GeoServer publication body is invalid"
            )
        response = self._request(
            "POST",
            path,
            body=body,
            content_type=content_type,
            accept=accept,
            # GeoServer uses 403 for some duplicate catalog creations (styles
            # in particular) and 409 for others.  Callers always re-read and
            # validate the exact resource before accepting either as a race.
            expected_statuses=frozenset({201, 403, 409}),
            max_response_bytes=MAX_ERROR_BYTES,
        )
        return response.status

    def _put_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        request_root: str,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if not body or len(body) > MAX_JSON_BYTES:
            raise InvalidGeoServerPublicationError(
                "local GeoServer administration body is invalid"
            )
        self._request(
            "PUT",
            path,
            body=body,
            content_type=JSON_CONTENT_TYPE,
            accept=JSON_CONTENT_TYPE,
            expected_statuses=frozenset({200}),
            max_response_bytes=MAX_ERROR_BYTES,
            request_root=request_root,
        )

    def _put_xml(
        self,
        path: str,
        body: bytes,
        *,
        request_root: str,
        expected_status: Literal[200, 201] = 200,
    ) -> None:
        if not body or len(body) > MAX_XML_BYTES:
            raise InvalidGeoServerPublicationError(
                "local GeoServer administration body is invalid"
            )
        self._request(
            "PUT",
            path,
            body=body,
            content_type=XML_CONTENT_TYPE,
            accept=XML_CONTENT_TYPE,
            expected_statuses=frozenset({expected_status}),
            max_response_bytes=MAX_ERROR_BYTES,
            request_root=request_root,
        )

    def _request(
        self,
        method: Literal["GET", "POST", "PUT"],
        path: str,
        *,
        accept: str,
        expected_statuses: frozenset[int],
        max_response_bytes: int,
        body: bytes | None = None,
        content_type: str | None = None,
        request_root: str | None = None,
    ) -> _AdminResponse:
        target = _validate_internal_target(
            self._rest_root if request_root is None else request_root,
            path,
        )
        headers = {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Authorization": self._authorization,
            "Connection": "close",
            "User-Agent": f"AsistenteAyuntamientos/{settings.app_version}",
        }
        if body is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
        connection: http.client.HTTPConnection | None = None
        try:
            connection = self._connection_factory(
                "127.0.0.1",
                self._endpoint.port or 0,
                self._timeout_seconds,
            )
            connection.request(
                method,
                target,
                body=body,
                headers=headers,
            )
            raw_response = connection.getresponse()
            status = int(raw_response.status)
            content_encoding = raw_response.getheader("Content-Encoding") or "identity"
            if content_encoding.strip().casefold() != "identity":
                raise GeoServerAdminResponseError(
                    "unsupported local GeoServer content encoding"
                )
            body_limit = (
                max_response_bytes
                if status == 200
                else min(max_response_bytes, MAX_ERROR_BYTES)
            )
            content_length = _content_length(
                raw_response.getheader("Content-Length")
            )
            if content_length is not None and content_length > body_limit:
                raise GeoServerAdminResponseError(
                    "local GeoServer administrative response is too large"
                )
            response_body = raw_response.read(body_limit + 1)
            if len(response_body) > body_limit:
                raise GeoServerAdminResponseError(
                    "local GeoServer administrative response is too large"
                )
            response_content_type = _optional_content_type(
                raw_response.getheader("Content-Type")
            )
        except GeoServerAdminResponseError:
            raise
        except (
            OSError,
            TimeoutError,
            ValueError,
            http.client.HTTPException,
            socket.timeout,
        ) as error:
            raise GeoServerAdminUnavailableError(
                "local GeoServer administration is unavailable"
            ) from error
        finally:
            if connection is not None:
                connection.close()

        if status in {401, 403} and status not in expected_statuses:
            raise GeoServerAdminAuthenticationError(
                "local GeoServer rejected administrative credentials"
            )
        if status >= 500:
            raise GeoServerAdminUnavailableError(
                "local GeoServer administrative request failed"
            )
        if status not in expected_statuses:
            raise GeoServerAdminResponseError(
                f"unexpected local GeoServer administrative status {status}"
            )
        return _AdminResponse(status, response_body, response_content_type)


def style_smoke_zoom_candidates(
    *,
    bounds: Mapping[str, float],
    payload: bytes | None = None,
    asset_kind: Literal["style_sld", "style_package"] | None = None,
    expected_sld_sha256: str | None = None,
) -> tuple[int, ...]:
    """Choose deterministic Web Mercator zooms inside a style's scale rules."""

    normalized_bounds = _validate_smoke_bounds(bounds)
    if payload is None:
        if asset_kind is not None or expected_sld_sha256 is not None:
            raise InvalidGeoServerPublicationError(
                "local GeoServer smoke style evidence is incomplete"
            )
        ranges: tuple[tuple[float | None, float | None], ...] = (
            (None, None),
        )
    else:
        sld = _validated_smoke_sld(
            payload,
            asset_kind=asset_kind,
            expected_sld_sha256=expected_sld_sha256,
        )
        ranges = _sld_portrayal_scale_ranges(sld)

    allowed = [
        zoom
        for zoom in range(MAX_WEB_MERCATOR_ZOOM + 1)
        if any(
            _scale_range_contains(
                minimum,
                maximum,
                WEB_MERCATOR_SCALE_DENOMINATOR_Z0 / (2**zoom),
            )
            for minimum, maximum in ranges
        )
    ]
    if not allowed:
        raise InvalidGeoServerPublicationError(
            "local GeoServer style has no renderable Web Mercator scale"
        )
    target = _smoke_fit_zoom(normalized_bounds)
    ordered = sorted(allowed, key=lambda zoom: (abs(zoom - target), zoom))
    return tuple(ordered[:MAX_SMOKE_ZOOM_CANDIDATES])


def style_smoke_expected_colors(
    *,
    payload: bytes,
    asset_kind: Literal["style_sld", "style_package"],
    expected_sld_sha256: str | None = None,
) -> tuple[str, ...]:
    """Return one unambiguous literal portrayal color, when available."""

    sld = _validated_smoke_sld(
        payload,
        asset_kind=asset_kind,
        expected_sld_sha256=expected_sld_sha256,
    )
    colors: set[str] = set()
    for symbol_part in (
        element
        for element in ElementTree.fromstring(sld).iter()
        if isinstance(element.tag, str)
        and element.tag.rsplit("}", 1)[-1] in {"Fill", "Stroke"}
    ):
        parameters = {
            child.attrib.get("name", "").casefold(): (
                child.text or ""
            ).strip()
            for child in symbol_part
            if (
                isinstance(child.tag, str)
                and child.tag.rsplit("}", 1)[-1]
                in {"CssParameter", "SvgParameter"}
            )
        }
        local_name = symbol_part.tag.rsplit("}", 1)[-1].casefold()
        raw_color = parameters.get(local_name)
        raw_opacity = parameters.get(f"{local_name}-opacity", "1")
        try:
            opacity = float(raw_opacity)
        except ValueError:
            continue
        if (
            opacity <= 0
            or raw_color is None
            or re.fullmatch(r"#[0-9A-Fa-f]{6}", raw_color) is None
        ):
            continue
        colors.add(raw_color.casefold())
    return tuple(sorted(colors)) if len(colors) == 1 else ()


def _validated_smoke_sld(
    payload: bytes,
    *,
    asset_kind: Literal["style_sld", "style_package"] | None,
    expected_sld_sha256: str | None,
) -> bytes:
    if asset_kind == "style_sld" and expected_sld_sha256 is None:
        return _validate_sld(payload)
    if asset_kind == "style_package" and expected_sld_sha256 is not None:
        _package, sld = _validate_sld_package(
            payload,
            expected_sld_sha256=expected_sld_sha256,
        )
        return sld
    raise InvalidGeoServerPublicationError(
        "local GeoServer smoke style evidence is incomplete"
    )


def _sld_portrayal_scale_ranges(
    sld: bytes,
) -> tuple[tuple[float | None, float | None], ...]:
    try:
        root = ElementTree.fromstring(sld)
    except ElementTree.ParseError as error:  # pragma: no cover - prevalidated
        raise InvalidGeoServerPublicationError(
            "invalid immutable SLD"
        ) from error
    ranges: list[tuple[float | None, float | None]] = []
    for rule in (
        element
        for element in root.iter()
        if isinstance(element.tag, str)
        and element.tag.rsplit("}", 1)[-1] == "Rule"
    ):
        if not any(
            isinstance(element.tag, str)
            and element.tag.rsplit("}", 1)[-1].endswith("Symbolizer")
            for element in rule
        ):
            continue
        minimum = _single_scale_denominator(
            rule,
            "MinScaleDenominator",
        )
        maximum = _single_scale_denominator(
            rule,
            "MaxScaleDenominator",
        )
        if (
            minimum is not None
            and maximum is not None
            and minimum >= maximum
        ):
            raise InvalidGeoServerPublicationError(
                "local GeoServer style scale range is invalid"
            )
        ranges.append((minimum, maximum))
    # GeoServer may accept extension symbolizers that this closed parser does
    # not recognize.  Let the visible-pixel gate decide those styles.
    return tuple(ranges) or ((None, None),)


def _single_scale_denominator(
    rule: ElementTree.Element,
    local_name: str,
) -> float | None:
    values = [
        element
        for element in rule
        if (
            isinstance(element.tag, str)
            and element.tag.rsplit("}", 1)[-1] == local_name
        )
    ]
    if not values:
        return None
    if (
        len(values) != 1
        or values[0].attrib
        or list(values[0])
        or values[0].text is None
        or values[0].text != values[0].text.strip()
    ):
        raise InvalidGeoServerPublicationError(
            "local GeoServer style scale denominator is invalid"
        )
    try:
        value = float(values[0].text)
    except ValueError as error:
        raise InvalidGeoServerPublicationError(
            "local GeoServer style scale denominator is invalid"
        ) from error
    if not isfinite(value) or value < 0:
        raise InvalidGeoServerPublicationError(
            "local GeoServer style scale denominator is invalid"
        )
    return value


def _scale_range_contains(
    minimum: float | None,
    maximum: float | None,
    scale: float,
) -> bool:
    return (
        (minimum is None or scale >= minimum)
        and (maximum is None or scale < maximum)
    )


def _validate_smoke_bounds(
    bounds: Mapping[str, float],
) -> dict[str, float]:
    if (
        not isinstance(bounds, Mapping)
        or set(bounds) != {"west", "south", "east", "north"}
    ):
        raise InvalidGeoServerPublicationError(
            "local GeoServer smoke bounds are invalid"
        )
    normalized: dict[str, float] = {}
    for name in ("west", "south", "east", "north"):
        raw = bounds[name]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not isfinite(float(raw))
        ):
            raise InvalidGeoServerPublicationError(
                "local GeoServer smoke bounds are invalid"
            )
        normalized[name] = float(raw)
    if (
        not -180 <= normalized["west"] < normalized["east"] <= 180
        or not -90 <= normalized["south"] < normalized["north"] <= 90
    ):
        raise InvalidGeoServerPublicationError(
            "local GeoServer smoke bounds are outside WGS84"
        )
    return normalized


def _validate_smoke_zooms(value: tuple[int, ...]) -> tuple[int, ...]:
    if (
        not isinstance(value, tuple)
        or not 1 <= len(value) <= MAX_SMOKE_ZOOM_CANDIDATES
        or len(set(value)) != len(value)
        or any(
            isinstance(zoom, bool)
            or not isinstance(zoom, int)
            or not 0 <= zoom <= MAX_WEB_MERCATOR_ZOOM
            for zoom in value
        )
    ):
        raise InvalidGeoServerPublicationError(
            "local GeoServer smoke zooms are invalid"
        )
    return value


def _smoke_fit_zoom(bounds: Mapping[str, float]) -> int:
    result = 0
    for zoom in range(MAX_WEB_MERCATOR_ZOOM + 1):
        west, east, north, south = _smoke_tile_window(bounds, zoom)
        tile_count = (east - west + 1) * (south - north + 1)
        if tile_count > SMOKE_TARGET_TILE_COUNT:
            break
        result = zoom
    return result


def _representative_tile_coordinates(
    bounds: Mapping[str, float],
    zoom: int,
    *,
    limit: int = MAX_SMOKE_TILE_CANDIDATES,
) -> tuple[tuple[int, int, int], ...]:
    normalized = _validate_smoke_bounds(bounds)
    if (
        isinstance(zoom, bool)
        or not isinstance(zoom, int)
        or not 0 <= zoom <= MAX_WEB_MERCATOR_ZOOM
    ):
        raise InvalidGeoServerPublicationError(
            "local GeoServer smoke zoom is invalid"
        )
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_SMOKE_TILE_CANDIDATES
    ):
        raise InvalidGeoServerPublicationError(
            "local GeoServer smoke tile limit is invalid"
        )
    west, east, north, south = _smoke_tile_window(normalized, zoom)
    tile_count = (east - west + 1) * (south - north + 1)
    if tile_count <= limit:
        xs = tuple(range(west, east + 1))
        ys = tuple(range(north, south + 1))
    else:
        side = max(1, int(math.sqrt(limit)))
        xs = _spread_tile_indexes(west, east, side)
        ys = _spread_tile_indexes(north, south, side)
    center_x = (west + east) / 2
    center_y = (north + south) / 2
    coordinates = [
        (zoom, x, y)
        for y in ys
        for x in xs
    ]
    coordinates.sort(
        key=lambda item: (
            abs(item[1] - center_x) + abs(item[2] - center_y),
            item[2],
            item[1],
        )
    )
    return tuple(coordinates[:limit])


def _spread_tile_indexes(
    lower: int,
    upper: int,
    count: int,
) -> tuple[int, ...]:
    if lower == upper:
        return (lower,)
    values = {
        round(lower + (upper - lower) * index / (count - 1))
        for index in range(count)
    }
    return tuple(sorted(values))


def _smoke_tile_window(
    bounds: Mapping[str, float],
    zoom: int,
) -> tuple[int, int, int, int]:
    scale = 2**zoom

    def tile_x(longitude: float) -> float:
        return (longitude + 180.0) / 360.0 * scale

    def tile_y(latitude: float) -> float:
        clamped = max(
            -WEB_MERCATOR_MAX_LATITUDE,
            min(WEB_MERCATOR_MAX_LATITUDE, latitude),
        )
        radians = math.radians(clamped)
        return (
            1 - math.asinh(math.tan(radians)) / math.pi
        ) / 2 * scale

    west = math.floor(tile_x(float(bounds["west"])))
    east = math.floor(
        math.nextafter(tile_x(float(bounds["east"])), -math.inf)
    )
    north = math.floor(tile_y(float(bounds["north"])))
    south = math.floor(
        math.nextafter(tile_y(float(bounds["south"])), -math.inf)
    )
    maximum = scale - 1
    return (
        max(0, min(maximum, west)),
        max(0, min(maximum, east)),
        max(0, min(maximum, north)),
        max(0, min(maximum, south)),
    )


def _validate_smoke_colors(value: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or len(value) > 32
        or len(set(value)) != len(value)
        or any(
            not isinstance(color, str)
            or re.fullmatch(r"#[0-9a-f]{6}", color) is None
            for color in value
        )
    ):
        raise InvalidGeoServerPublicationError(
            "local GeoServer smoke style colors are invalid"
        )
    return value


def _analyze_smoke_png(
    body: bytes,
    *,
    expected_colors: tuple[str, ...],
) -> tuple[int, str | None]:
    """Fully decode a smoke PNG and inspect alpha and expected portrayal."""

    try:
        with Image.open(io.BytesIO(body)) as image:
            if image.format != "PNG" or image.size != (256, 256):
                raise GeoServerLayerSmokeError(
                    "local GeoServer smoke image is not a complete tile"
                )
            image.load()
            rgba = image.convert("RGBA")
            histogram = rgba.getchannel("A").histogram()
            pixels = rgba.tobytes() if expected_colors else b""
    except GeoServerLayerSmokeError:
        raise
    except (
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as error:
        raise GeoServerLayerSmokeError(
            "local GeoServer smoke image could not be decoded"
        ) from error
    if len(histogram) != 256:  # pragma: no cover - Pillow contract
        raise GeoServerLayerSmokeError(
            "local GeoServer smoke image alpha is invalid"
        )
    visible = sum(histogram[1:])
    matched = None
    for expected in expected_colors:
        target = tuple(
            int(expected[index : index + 2], 16)
            for index in (1, 3, 5)
        )
        if any(
            pixels[offset + 3] > 0
            and max(
                abs(pixels[offset] - target[0]),
                abs(pixels[offset + 1] - target[1]),
                abs(pixels[offset + 2] - target[2]),
            )
            <= SMOKE_COLOR_TOLERANCE
            for offset in range(0, len(pixels), 4)
        ):
            matched = expected
            break
    return visible, matched


def _basic_authorization(user: str | None, password: SecretStr | None) -> str:
    if (
        not isinstance(user, str)
        or re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", user) is None
        or not isinstance(password, SecretStr)
    ):
        raise UnsafeGeoServerAdminConfigurationError(
            "local GeoServer administrative credentials are not configured"
        )
    secret = _validate_secret(password, "administrative credentials")
    token = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _validate_secret(value: SecretStr, label: str) -> str:
    secret = value.get_secret_value()
    if not secret or len(secret) > 1024 or "\x00" in secret:
        raise UnsafeGeoServerAdminConfigurationError(
            f"invalid local GeoServer {label}"
        )
    return secret


def expected_geowebcache_tile_blob_store(
    *,
    file_system_block_size: int,
) -> GeoWebCacheFileBlobStore:
    """Build the sole supported tile store without accepting a path or id."""

    return GeoWebCacheFileBlobStore(
        id=GEOWEBCACHE_TILE_BLOB_STORE_ID,
        enabled=True,
        default=True,
        base_directory=GEOWEBCACHE_TILE_BLOB_STORE_DIRECTORY,
        file_system_block_size=_validate_geowebcache_block_size(
            file_system_block_size
        ),
        path_generator_type="DEFAULT",
    )


def _validate_geowebcache_block_size(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 512 <= value <= 1024 * 1024
        or value & (value - 1)
    ):
        raise InvalidGeoServerPublicationError(
            "GeoWebCache filesystem block size must be a power of two "
            "between 512 bytes and 1 MiB"
        )
    return value


def _validate_geowebcache_tile_blob_store_set(
    stores: tuple[GeoWebCacheFileBlobStore, ...],
    *,
    expected: GeoWebCacheFileBlobStore,
    require_present: bool,
    allow_file_system_block_size_migration: bool = False,
) -> GeoWebCacheFileBlobStore | None:
    defaults = tuple(store for store in stores if store.default)
    current = next((store for store in stores if store.id == expected.id), None)
    if (
        current is not None
        and current != expected
        and not (
            allow_file_system_block_size_migration
            and _blob_store_differs_only_in_file_system_block_size(
                current,
                expected,
            )
        )
    ):
        raise GeoServerAdminConflictError(
            "existing GeoWebCache tile blob store differs"
        )
    if current is None:
        if stores:
            raise GeoServerAdminConflictError(
                "another GeoWebCache blob store is configured"
            )
        if require_present:
            raise GeoServerAdminConflictError(
                "GeoWebCache tile blob store is missing after configuration"
            )
        return None
    if stores != (current,) or defaults != (current,):
        raise GeoServerAdminConflictError(
            "GeoWebCache has an unexpected additional blob store"
        )
    return current


def _blob_store_differs_only_in_file_system_block_size(
    current: GeoWebCacheFileBlobStore,
    expected: GeoWebCacheFileBlobStore,
) -> bool:
    return (
        current.file_system_block_size != expected.file_system_block_size
        and all(
            getattr(current, name) == getattr(expected, name)
            for name in (
                "id",
                "enabled",
                "default",
                "base_directory",
                "path_generator_type",
            )
        )
    )


def _serialize_geowebcache_file_blob_store(
    store: GeoWebCacheFileBlobStore,
) -> bytes:
    if store != expected_geowebcache_tile_blob_store(
        file_system_block_size=store.file_system_block_size,
    ):
        raise InvalidGeoServerPublicationError(
            "unsupported GeoWebCache tile blob store configuration"
        )
    root = ElementTree.Element("FileBlobStore", {"default": "true"})
    for name, value in (
        ("id", store.id),
        ("enabled", "true"),
        ("baseDirectory", store.base_directory),
        ("fileSystemBlockSize", str(store.file_system_block_size)),
    ):
        child = ElementTree.SubElement(root, name)
        child.text = value
    return ElementTree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=False,
        short_empty_elements=False,
    )


def _parse_geowebcache_blob_store_names(payload: bytes) -> tuple[str, ...]:
    root = _parse_geowebcache_xml(payload)
    if root.tag != "blobStores" or root.attrib or not _xml_whitespace(root.text):
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache blob store list response"
        )
    names: list[str] = []
    for item in root:
        if (
            item.tag != "blobStore"
            or item.attrib
            or not _xml_whitespace(item.text)
            or not _xml_whitespace(item.tail)
        ):
            raise GeoServerAdminResponseError(
                "invalid GeoWebCache blob store list response"
            )
        children = list(item)
        if len(children) != 2 or children[0].tag != "name":
            raise GeoServerAdminResponseError(
                "invalid GeoWebCache blob store list response"
            )
        name = _xml_scalar(
            children[0],
            error="invalid GeoWebCache blob store list response",
        )
        if RESOURCE_NAME.fullmatch(name) is None or name in names:
            raise GeoServerAdminResponseError(
                "invalid GeoWebCache blob store list response"
            )
        link = children[1]
        if (
            link.tag != "{http://www.w3.org/2005/Atom}link"
            or list(link)
            or not _xml_whitespace(link.text)
            or not _xml_whitespace(link.tail)
            or set(link.attrib) != {"rel", "href", "type"}
            or link.attrib.get("rel") != "alternate"
            or link.attrib.get("type") not in XML_CONTENT_TYPES
            or not _safe_xml_link(link.attrib.get("href"))
        ):
            raise GeoServerAdminResponseError(
                "invalid GeoWebCache blob store list response"
            )
        names.append(name)
    if not _xml_whitespace(root.tail):
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache blob store list response"
        )
    return tuple(names)


def _parse_geowebcache_file_blob_store(
    payload: bytes,
) -> GeoWebCacheFileBlobStore:
    root = _parse_geowebcache_xml(payload)
    default_attribute = root.attrib.get("default")
    if (
        root.tag != "FileBlobStore"
        or set(root.attrib) not in (set(), {"default"})
        or default_attribute not in (None, "true", "false")
        or not _xml_whitespace(root.text)
        or not _xml_whitespace(root.tail)
    ):
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache FileBlobStore response"
        )
    children = list(root)
    tags = [child.tag for child in children]
    if tags not in (
        ["id", "enabled", "baseDirectory", "fileSystemBlockSize"],
        [
            "id",
            "enabled",
            "baseDirectory",
            "fileSystemBlockSize",
            "pathGeneratorType",
        ],
    ):
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache FileBlobStore response"
        )
    values = {
        child.tag: _xml_scalar(
            child,
            error="invalid GeoWebCache FileBlobStore response",
        )
        for child in children
    }
    identifier = values["id"]
    enabled = values["enabled"]
    base_directory = values["baseDirectory"]
    raw_block_size = values["fileSystemBlockSize"]
    path_generator = values.get("pathGeneratorType", "DEFAULT")
    if (
        RESOURCE_NAME.fullmatch(identifier) is None
        or enabled not in {"true", "false"}
        or path_generator not in {"DEFAULT", "TMS", "SLIPPY"}
        or not _canonical_absolute_posix_directory(base_directory)
        or not raw_block_size.isascii()
        or not raw_block_size.isdecimal()
    ):
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache FileBlobStore response"
        )
    block_size = int(raw_block_size)
    try:
        block_size = _validate_geowebcache_block_size(block_size)
    except InvalidGeoServerPublicationError as error:
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache FileBlobStore response"
        ) from error
    if str(block_size) != raw_block_size:
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache FileBlobStore response"
        )
    return GeoWebCacheFileBlobStore(
        id=identifier,
        enabled=enabled == "true",
        default=default_attribute == "true",
        base_directory=base_directory,
        file_system_block_size=block_size,
        path_generator_type=path_generator,
    )


def _parse_geowebcache_xml(payload: bytes) -> ElementTree.Element:
    lowered = payload.lower()
    if (
        not payload
        or len(payload) > MAX_XML_BYTES
        or b"<!doctype" in lowered
        or b"<!entity" in lowered
    ):
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache XML response"
        )
    try:
        parser = ElementTree.XMLParser(
            target=ElementTree.TreeBuilder(
                insert_comments=True,
                insert_pis=True,
            )
        )
        root = ElementTree.fromstring(payload, parser=parser)
    except (ElementTree.ParseError, RecursionError, ValueError) as error:
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache XML response"
        ) from error
    if not isinstance(root.tag, str):
        raise GeoServerAdminResponseError(
            "invalid GeoWebCache XML response"
        )
    return root


def _xml_scalar(element: ElementTree.Element, *, error: str) -> str:
    value = element.text
    if (
        element.attrib
        or list(element)
        or value is None
        or value != value.strip()
        or not value
        or not _xml_whitespace(element.tail)
    ):
        raise GeoServerAdminResponseError(error)
    return value


def _xml_whitespace(value: str | None) -> bool:
    return value is None or not value.strip()


def _safe_xml_link(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_REQUEST_TARGET_BYTES
        and all(character >= " " and character != "\x7f" for character in value)
    )


def _canonical_absolute_posix_directory(value: str) -> bool:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or value == "/"
        or len(value) > 1024
        or any(character < " " or character == "\x7f" for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return (
        str(path) == value
        and "." not in path.parts
        and ".." not in path.parts
    )


def _parse_geowebcache_disk_quota(payload: bytes) -> GeoWebCacheDiskQuota:
    """Parse the real GeoServer 3.0.0/GWC 2.0.0 XML representation.

    That release does not serialize the documented alias and value/unit shape
    through REST. It emits the concrete class name and a byte-valued Quota.
    Accepting only the observed, version-pinned shape prevents the misleading
    ``diskquota.json`` content type from weakening the startup gate.
    """

    error = "invalid GeoWebCache disk quota response"
    root = _parse_geowebcache_xml(payload)
    if (
        root.tag != "org.geowebcache.diskquota.DiskQuotaConfig"
        or root.attrib
        or not _xml_whitespace(root.text)
        or not _xml_whitespace(root.tail)
    ):
        raise GeoServerAdminResponseError(error)
    children = list(root)
    if [child.tag for child in children] != [
        "enabled",
        "cacheCleanUpFrequency",
        "cacheCleanUpUnits",
        "maxConcurrentCleanUps",
        "globalExpirationPolicyName",
        "globalQuota",
    ]:
        raise GeoServerAdminResponseError(error)
    enabled_text = _xml_scalar(children[0], error=error)
    cleanup_text = _xml_scalar(children[1], error=error)
    cleanup_units = _xml_scalar(children[2], error=error)
    cleanups_text = _xml_scalar(children[3], error=error)
    expiration_policy = _xml_scalar(children[4], error=error)
    quota = children[5]
    if (
        quota.attrib
        or not _xml_whitespace(quota.text)
        or not _xml_whitespace(quota.tail)
        or [child.tag for child in quota] != ["id", "bytes"]
    ):
        raise GeoServerAdminResponseError(error)
    quota_id = _xml_scalar(quota[0], error=error)
    quota_bytes_text = _xml_scalar(quota[1], error=error)
    if (
        enabled_text not in {"true", "false"}
        or cleanup_units not in {"SECONDS", "MINUTES", "HOURS", "DAYS"}
        or expiration_policy not in {"LRU", "LFU"}
        or quota_id != "0"
    ):
        raise GeoServerAdminResponseError(error)
    cleanup_frequency = _strict_positive_decimal(
        cleanup_text,
        maximum=86_400,
        error=error,
    )
    max_concurrent_cleanups = _strict_positive_decimal(
        cleanups_text,
        maximum=64,
        error=error,
    )
    quota_bytes = _strict_positive_decimal(
        quota_bytes_text,
        maximum=1024**5,
        error=error,
    )
    for units, unit_bytes in (
        ("TiB", 1024**4),
        ("GiB", 1024**3),
        ("MiB", 1024**2),
    ):
        if quota_bytes % unit_bytes == 0:
            quota_value = quota_bytes // unit_bytes
            quota_units: Literal["MiB", "GiB", "TiB"] = units
            break
    else:
        raise GeoServerAdminResponseError(error)
    return GeoWebCacheDiskQuota(
        enabled=enabled_text == "true",
        quota_bytes=quota_bytes,
        quota_value=quota_value,
        quota_units=quota_units,
        cleanup_frequency=cleanup_frequency,
        cleanup_units=cleanup_units,
        max_concurrent_cleanups=max_concurrent_cleanups,
        expiration_policy=expiration_policy,
    )


def _strict_positive_decimal(
    value: str,
    *,
    maximum: int,
    error: str,
) -> int:
    if (
        not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        raise GeoServerAdminResponseError(error)
    parsed = int(value)
    if not 1 <= parsed <= maximum or str(parsed) != value:
        raise GeoServerAdminResponseError(error)
    return parsed


def _parse_geowebcache_quota_health(
    payload: Mapping[str, Any],
) -> GeoWebCacheQuotaHealth:
    error = "invalid GeoWebCache quota health response"
    expected_keys = {
        "schema_version",
        "healthy",
        "monitor_enabled",
        "monitor_running",
        "scheduled_cleanup_active",
        "provider_error",
        "store_class",
        "dialect_class",
        "global_used_bytes",
    }
    schema_version = payload.get("schema_version")
    if (
        set(payload) != expected_keys
        or type(schema_version) is not int
        or schema_version != 1
    ):
        raise GeoServerAdminResponseError(error)
    boolean_keys = (
        "healthy",
        "monitor_enabled",
        "monitor_running",
        "scheduled_cleanup_active",
        "provider_error",
    )
    if any(not isinstance(payload.get(key), bool) for key in boolean_keys):
        raise GeoServerAdminResponseError(error)
    store_class = payload.get("store_class")
    dialect_class = payload.get("dialect_class")
    raw_used = payload.get("global_used_bytes")
    if (
        store_class is not None
        and (
            not isinstance(store_class, str)
            or re.fullmatch(r"[A-Za-z0-9_.$]{1,256}", store_class) is None
        )
    ):
        raise GeoServerAdminResponseError(error)
    if (
        dialect_class is not None
        and (
            not isinstance(dialect_class, str)
            or re.fullmatch(r"[A-Za-z0-9_.$]{1,256}", dialect_class) is None
        )
    ):
        raise GeoServerAdminResponseError(error)
    if raw_used is None:
        used = None
    elif (
        not isinstance(raw_used, str)
        or not raw_used.isascii()
        or not raw_used.isdecimal()
        or (raw_used.startswith("0") and raw_used != "0")
        or len(raw_used) > 40
    ):
        raise GeoServerAdminResponseError(error)
    else:
        used = int(raw_used)
    return GeoWebCacheQuotaHealth(
        healthy=payload["healthy"],
        monitor_enabled=payload["monitor_enabled"],
        monitor_running=payload["monitor_running"],
        scheduled_cleanup_active=payload["scheduled_cleanup_active"],
        provider_error=payload["provider_error"],
        store_class=store_class,
        dialect_class=dialect_class,
        global_used_bytes=used,
    )


def _validate_resource_name(
    value: str,
    label: str,
    *,
    configuration: bool = False,
) -> str:
    error_type = (
        UnsafeGeoServerAdminConfigurationError
        if configuration
        else InvalidGeoServerPublicationError
    )
    if not isinstance(value, str):
        raise error_type(f"invalid local GeoServer {label}")
    normalized = value.strip()
    if RESOURCE_NAME.fullmatch(normalized) is None:
        raise error_type(f"invalid local GeoServer {label}")
    return normalized


def _validate_technical_name(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidGeoServerPublicationError(f"invalid local GeoServer {label}")
    normalized = value.strip()
    if TECHNICAL_NAME.fullmatch(normalized) is None:
        raise InvalidGeoServerPublicationError(f"invalid local GeoServer {label}")
    return normalized


def _validate_versioned_name(value: str, label: str) -> str:
    normalized = _validate_technical_name(value, label)
    if VERSIONED_NAME.fullmatch(normalized) is None:
        raise InvalidGeoServerPublicationError(
            f"local GeoServer {label} must have an immutable version suffix"
        )
    return normalized


def _validate_connection_name(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise UnsafeGeoServerAdminConfigurationError(
            f"invalid local GeoServer {label}"
        )
    normalized = value.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", normalized) is None:
        raise UnsafeGeoServerAdminConfigurationError(
            f"invalid local GeoServer {label}"
        )
    return normalized


def _validate_postgis_host(value: str) -> str:
    if not isinstance(value, str):
        raise UnsafeGeoServerAdminConfigurationError(
            "invalid local GeoServer PostGIS host"
        )
    normalized = value.strip().lower()
    if (
        len(normalized) > 253
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?",
            normalized,
        )
        is None
        or ".." in normalized
    ):
        raise UnsafeGeoServerAdminConfigurationError(
            "invalid local GeoServer PostGIS host"
        )
    return normalized


def _validate_srs(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidGeoServerPublicationError("invalid declared SRS")
    normalized = value.strip().upper()
    if SRS_NAME.fullmatch(normalized) is None:
        raise InvalidGeoServerPublicationError("invalid declared SRS")
    return normalized


def _validate_projection_policy(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidGeoServerPublicationError("invalid projection policy")
    normalized = value.strip().upper()
    if normalized not in PROJECTION_POLICIES:
        raise InvalidGeoServerPublicationError("invalid projection policy")
    return normalized


def _validate_title(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidGeoServerPublicationError("invalid publication title")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 for character in normalized)
    ):
        raise InvalidGeoServerPublicationError("invalid publication title")
    return normalized


def _validate_artifact_relative_path(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
    ):
        raise InvalidGeoServerPublicationError("invalid local GeoTIFF path")
    path = PurePosixPath(value)
    parts = path.parts
    if (
        str(path) != value
        or len(parts) != 4
        or parts[:2] != ("blobs", "sha256")
        or SHA256_HEX.fullmatch(parts[3]) is None
        or parts[2] != parts[3][:2]
    ):
        raise InvalidGeoServerPublicationError(
            "invalid content-addressed local GeoTIFF path"
        )
    return path


def _validate_sld_package(
    value: bytes,
    *,
    expected_sld_sha256: str,
) -> tuple[bytes, bytes]:
    if (
        not isinstance(value, bytes)
        or not value
        or len(value) > MAX_STYLE_PACKAGE_BYTES
        or not isinstance(expected_sld_sha256, str)
        or SHA256_HEX.fullmatch(expected_sld_sha256) is None
    ):
        raise InvalidGeoServerPublicationError(
            "invalid immutable style package"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(value), "r") as archive:
            entries = archive.infolist()
            if (
                not entries
                or len(entries) > MAX_STYLE_PACKAGE_RESOURCES + 1
                or len({item.filename for item in entries}) != len(entries)
            ):
                raise InvalidGeoServerPublicationError(
                    "invalid immutable style package entries"
                )
            resources: dict[str, bytes] = {}
            sld: bytes | None = None
            total_size = 0
            for item in entries:
                name = item.filename
                path = PurePosixPath(name)
                mode = item.external_attr >> 16
                if (
                    item.is_dir()
                    or item.flag_bits & 0x1
                    or not name
                    or len(name) > 255
                    or "\\" in name
                    or path.is_absolute()
                    or str(path) != name
                    or ".." in path.parts
                    or (mode & 0o170000) not in {0, 0o100000}
                    or item.file_size <= 0
                ):
                    raise InvalidGeoServerPublicationError(
                        "unsafe immutable style package entry"
                    )
                total_size += item.file_size
                if total_size > MAX_STYLE_PACKAGE_BYTES:
                    raise InvalidGeoServerPublicationError(
                        "immutable style package expands beyond its limit"
                    )
                payload = archive.read(item)
                if len(payload) != item.file_size:
                    raise InvalidGeoServerPublicationError(
                        "immutable style package entry is truncated"
                    )
                if name == "style.sld":
                    sld = payload
                    continue
                match = STYLE_PACKAGE_RESOURCE.fullmatch(name)
                if (
                    match is None
                    or hashlib.sha256(payload).hexdigest()
                    != match.group("sha")
                ):
                    raise InvalidGeoServerPublicationError(
                        "style package resource is not content-addressed"
                    )
                _validate_style_resource_payload(
                    payload,
                    match.group("extension"),
                )
                resources[name] = payload
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        KeyError,
        RuntimeError,
    ) as error:
        raise InvalidGeoServerPublicationError(
            "invalid immutable style package"
        ) from error
    if sld is None:
        raise InvalidGeoServerPublicationError(
            "style package is missing its SLD"
        )
    if hashlib.sha256(sld).hexdigest() != expected_sld_sha256:
        raise InvalidGeoServerPublicationError(
            "style package SLD does not match its approved digest"
        )
    normalized_sld, references = _validate_sld_document(
        sld,
        allowed_resource_paths=frozenset(resources),
    )
    if references != frozenset(resources):
        raise InvalidGeoServerPublicationError(
            "style package contains unreferenced resources"
        )
    return value, normalized_sld


def _validate_style_resource_payload(
    payload: bytes,
    extension: str,
) -> None:
    valid = {
        "png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": (
            payload.startswith(b"\xff\xd8\xff")
            and payload.endswith(b"\xff\xd9")
        ),
        "gif": payload.startswith((b"GIF87a", b"GIF89a")),
        "webp": (
            len(payload) >= 12
            and payload[:4] == b"RIFF"
            and payload[8:12] == b"WEBP"
        ),
    }
    if extension in valid:
        if not valid[extension]:
            raise InvalidGeoServerPublicationError(
                "style package resource type is invalid"
            )
        return
    if extension != "svg":
        raise InvalidGeoServerPublicationError(
            "style package resource type is invalid"
        )
    lowered = payload.lower()
    if (
        b"\x00" in payload
        or b"<!doctype" in lowered
        or b"<!entity" in lowered
    ):
        raise InvalidGeoServerPublicationError(
            "style package SVG is unsafe"
        )
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise InvalidGeoServerPublicationError(
            "style package SVG is invalid"
        ) from error
    elements = list(root.iter())
    if (
        root.tag.rsplit("}", 1)[-1].casefold() != "svg"
        or len(elements) > 20_000
    ):
        raise InvalidGeoServerPublicationError(
            "style package SVG is invalid"
        )
    for element in elements:
        if element.tag.rsplit("}", 1)[-1].casefold() in {
            "script",
            "foreignobject",
            "iframe",
        }:
            raise InvalidGeoServerPublicationError(
                "style package SVG contains active content"
            )
        if any(
            attribute.rsplit("}", 1)[-1].casefold()
            in {"href", "src", "url", "uri"}
            for attribute in element.attrib
        ):
            raise InvalidGeoServerPublicationError(
                "style package SVG has an external dependency"
            )


def _validate_sld(value: bytes | str) -> bytes:
    return _validate_sld_document(
        value,
        allowed_resource_paths=None,
    )[0]


def _validate_sld_document(
    value: bytes | str,
    *,
    allowed_resource_paths: frozenset[str] | None,
) -> tuple[bytes, frozenset[str]]:
    if isinstance(value, str):
        body = value.encode("utf-8")
    elif isinstance(value, bytes):
        body = value
    else:
        raise InvalidGeoServerPublicationError("invalid immutable SLD")
    if not body or len(body) > MAX_SLD_BYTES or b"\x00" in body:
        raise InvalidGeoServerPublicationError("invalid immutable SLD")
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise InvalidGeoServerPublicationError(
            "immutable SLD cannot declare external entities"
        )
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise InvalidGeoServerPublicationError("invalid immutable SLD") from error
    if root.tag != "{http://www.opengis.net/sld}StyledLayerDescriptor":
        raise InvalidGeoServerPublicationError("invalid immutable SLD root")
    references: set[str] = set()
    for element in root.iter():
        local_element = element.tag.rsplit("}", 1)[-1].casefold()
        for attribute, raw_value in element.attrib.items():
            local_attribute = attribute.rsplit("}", 1)[-1].casefold()
            if local_attribute in {"href", "src", "url"}:
                if (
                    allowed_resource_paths is None
                    or local_element != "onlineresource"
                    or local_attribute != "href"
                    or raw_value not in allowed_resource_paths
                ):
                    raise InvalidGeoServerPublicationError(
                        "immutable SLD cannot reference auxiliary resources"
                    )
                references.add(raw_value)
    return body, frozenset(references)


def _validate_workspace_payload(payload: Mapping[str, Any], name: str) -> None:
    workspace = _nested_mapping(payload, "workspace")
    _assert_fields(workspace, {"name": name}, "workspace")


def _validate_datastore_payload(
    payload: Mapping[str, Any],
    *,
    name: str,
    expected_parameters: Mapping[str, str],
) -> None:
    datastore = _nested_mapping(payload, "dataStore")
    _assert_fields(
        datastore,
        {"name": name, "enabled": True},
        "PostGIS datastore",
    )
    store_type = datastore.get("type")
    if not isinstance(store_type, str) or store_type.casefold() != "postgis":
        raise GeoServerAdminConflictError(
            "existing local GeoServer PostGIS datastore differs"
        )
    parameters = datastore.get("connectionParameters")
    if not isinstance(parameters, Mapping):
        raise GeoServerAdminResponseError(
            "invalid local GeoServer datastore response"
        )
    raw_entries = parameters.get("entry")
    if isinstance(raw_entries, Mapping):
        entries: list[Any] = [raw_entries]
    elif isinstance(raw_entries, list):
        entries = raw_entries
    else:
        raise GeoServerAdminResponseError(
            "invalid local GeoServer datastore response"
        )
    actual_parameters: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise GeoServerAdminResponseError(
                "invalid local GeoServer datastore response"
            )
        key = entry.get("@key")
        raw_value = entry.get("$")
        if isinstance(key, str) and isinstance(raw_value, (str, int)):
            actual_parameters[key] = str(raw_value)
    for key, expected in expected_parameters.items():
        actual = actual_parameters.get(key)
        if actual is None or (
            actual.casefold() != expected.casefold()
            if key == "dbtype"
            else actual != expected
        ):
            raise GeoServerAdminConflictError(
                "existing local GeoServer PostGIS datastore differs"
            )


def _validate_feature_type_payload(
    payload: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    feature_type = _nested_mapping(payload, "featureType")
    _assert_fields(feature_type, expected, "versioned table")


def _validate_coverage_store_payload(
    payload: Mapping[str, Any],
    *,
    name: str,
    artifact_uri: str,
) -> None:
    store = _nested_mapping(payload, "coverageStore")
    _assert_fields(store, {"name": name, "enabled": True}, "GeoTIFF store")
    store_type = store.get("type")
    if not isinstance(store_type, str) or store_type.casefold() != "geotiff":
        raise GeoServerAdminConflictError(
            "existing local GeoServer GeoTIFF store differs"
        )
    actual_uri = store.get("url")
    if not isinstance(actual_uri, str) or _file_uri_path(
        actual_uri
    ) != _file_uri_path(artifact_uri):
        raise GeoServerAdminConflictError(
            "existing local GeoServer GeoTIFF store differs"
        )


def _validate_coverage_payload(
    payload: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    coverage = _nested_mapping(payload, "coverage")
    _assert_fields(coverage, expected, "GeoTIFF coverage")


def _validate_layer_payload(payload: Mapping[str, Any], name: str) -> None:
    layer = _nested_mapping(payload, "layer")
    try:
        _validate_layer_identity(layer, name)
    except GeoServerAdminConflictError as error:
        raise GeoServerLayerSmokeError(
            "local GeoServer layer catalog smoke failed"
        ) from error


def _validate_publication_layer_payload(
    payload: Mapping[str, Any],
    name: str,
) -> None:
    layer = _nested_mapping(payload, "layer")
    _validate_layer_identity(layer, name)


def _validate_layer_identity(
    layer: Mapping[str, Any],
    name: str,
) -> None:
    # GeoServer's layer representation omits ``enabled`` in current releases;
    # that flag belongs to the underlying feature type or coverage, which the
    # publication method validates separately. Reject an explicit false value
    # while allowing the documented layer response shape. The subsequent WMS
    # smoke proves that the resource is actually renderable.
    _assert_fields(layer, {"name": name}, "layer")
    if "enabled" in layer and layer.get("enabled") is not True:
        raise GeoServerAdminConflictError(
            "existing local GeoServer layer differs"
        )


def _layer_styles_contain(
    payload: Mapping[str, Any],
    style_name: str,
    *,
    layer_name: str,
    workspace: str,
    rest_root: str,
) -> bool:
    styles = payload.get("styles")
    if not isinstance(styles, Mapping):
        raise GeoServerAdminResponseError(
            "invalid local GeoServer layer styles response"
        )
    raw_entries = styles.get("style")
    if raw_entries is None or raw_entries == "":
        return False
    if isinstance(raw_entries, Mapping):
        entries: list[Any] = [raw_entries]
    elif isinstance(raw_entries, list):
        entries = raw_entries
    else:
        raise GeoServerAdminResponseError(
            "invalid local GeoServer layer styles response"
        )
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise GeoServerAdminResponseError(
                "invalid local GeoServer layer styles response"
            )
        name = entry.get("name")
        if not isinstance(name, str):
            raise GeoServerAdminResponseError(
                "invalid local GeoServer layer styles response"
            )
        qualified_name = f"{workspace}:{style_name}"
        if name == qualified_name:
            return True
        if RESOURCE_NAME.fullmatch(name) is None:
            qualified_parts = name.split(":", maxsplit=1)
            if (
                len(qualified_parts) != 2
                or any(
                    RESOURCE_NAME.fullmatch(part) is None
                    for part in qualified_parts
                )
            ):
                raise GeoServerAdminResponseError(
                    "invalid local GeoServer layer styles response"
                )
        if name != style_name:
            continue

        entry_workspace = _layer_style_workspace(entry)
        if entry_workspace is not None:
            if entry_workspace == workspace:
                return True
            continue
        href = entry.get("href")
        if href is None:
            continue
        if not isinstance(href, str):
            raise GeoServerAdminResponseError(
                "invalid local GeoServer layer styles response"
            )
        try:
            parsed_href = urlsplit(href)
        except ValueError as error:
            raise GeoServerAdminResponseError(
                "invalid local GeoServer layer styles response"
            ) from error
        expected_path = (
            f"{rest_root}/workspaces/{_segment(workspace)}/styles/"
            f"{_segment(style_name)}.json"
        )
        layer_scoped_path = (
            f"{rest_root}/layers/{_segment(workspace)}:"
            f"{_segment(layer_name)}/styles/{_segment(style_name)}.json"
        )
        if (
            parsed_href.path in {expected_path, layer_scoped_path}
            and not parsed_href.query
            and not parsed_href.fragment
        ):
            return True
    return False


def _layer_style_workspace(entry: Mapping[str, Any]) -> str | None:
    raw_workspace = entry.get("workspace")
    if raw_workspace is None:
        return None
    if isinstance(raw_workspace, Mapping):
        raw_workspace = raw_workspace.get("name")
    if (
        not isinstance(raw_workspace, str)
        or RESOURCE_NAME.fullmatch(raw_workspace) is None
    ):
        raise GeoServerAdminResponseError(
            "invalid local GeoServer layer styles response"
        )
    return raw_workspace


def _nested_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise GeoServerAdminResponseError(
            "invalid local GeoServer administrative response"
        )
    return value


def _assert_fields(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    label: str,
) -> None:
    if any(actual.get(key) != value for key, value in expected.items()):
        raise GeoServerAdminConflictError(
            f"existing local GeoServer {label} differs"
        )


def _assert_same_bytes(actual: bytes, expected: bytes, label: str) -> None:
    if not hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest():
        raise GeoServerAdminConflictError(
            f"existing local GeoServer immutable {label} differs"
        )


def _file_uri_path(value: str) -> PurePosixPath:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() != "file"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise GeoServerAdminConflictError(
            "existing local GeoServer GeoTIFF store differs"
        )
    path = PurePosixPath(parsed.path)
    if GEOSERVER_ARTIFACT_ROOT not in path.parents:
        raise GeoServerAdminConflictError(
            "existing local GeoServer GeoTIFF store differs"
        )
    return path


def _segment(value: str) -> str:
    return quote(value, safe="")


def _validate_internal_target(root: str, path: str) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or "\\" in path
        or ".." in path.partition("?")[0].split("/")
    ):
        raise InvalidGeoServerPublicationError(
            "invalid local GeoServer administrative path"
        )
    target = f"{root}{path}"
    if len(target.encode("ascii", "strict")) > MAX_REQUEST_TARGET_BYTES:
        raise InvalidGeoServerPublicationError(
            "local GeoServer administrative path is too large"
        )
    return target


def _optional_content_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.partition(";")[0].strip().casefold()
    if not normalized or len(normalized) > 128:
        raise GeoServerAdminResponseError(
            "invalid local GeoServer content type"
        )
    return normalized


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GeoServerAdminResponseError(
                "duplicate key in local GeoServer JSON response"
            )
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise GeoServerAdminResponseError(
        "invalid constant in local GeoServer JSON response"
    )


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_CONTENT_LENGTH_DIGITS
        or not normalized.isascii()
        or not normalized.isdecimal()
    ):
        raise GeoServerAdminResponseError(
            "invalid local GeoServer content length"
        )
    return int(normalized)


def _http_connection(
    host: str,
    port: int,
    timeout: float,
) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port=port, timeout=timeout)
