import base64
import hashlib
import io
import json
import struct
import zipfile
import zlib
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.reference_layers.geoserver_admin import (
    MAX_JSON_BYTES,
    GEOWEBCACHE_TILE_BLOB_STORE_DIRECTORY,
    GEOWEBCACHE_TILE_BLOB_STORE_ID,
    GeoServerAdminAuthenticationError,
    GeoServerAdminClient,
    GeoServerAdminConflictError,
    GeoWebCacheFileBlobStore,
    GeoServerAdminResponseError,
    GeoServerAdminUnavailableError,
    GeoServerLayerSmokeError,
    InvalidGeoServerPublicationError,
    UnsafeGeoServerAdminConfigurationError,
)

ADMIN_PASSWORD = "admin-password-that-must-not-leak"
POSTGIS_PASSWORD = "postgis-password-that-must-not-leak"
RASTER_DIGEST = "ab" + "0" * 62
RASTER_STORAGE_KEY = (
    f"blobs/sha256/{RASTER_DIGEST[:2]}/{RASTER_DIGEST}"
)


def make_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", checksum)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


VALID_SLD = b"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
  xmlns="http://www.opengis.net/sld"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/sld StyledLayerDescriptor.xsd">
  <NamedLayer><Name>planning</Name><UserStyle><FeatureTypeStyle>
    <Rule><PolygonSymbolizer>
      <Fill><CssParameter name="fill">#336699</CssParameter></Fill>
      <Stroke><CssParameter name="stroke">#112233</CssParameter></Stroke>
    </PolygonSymbolizer></Rule>
  </FeatureTypeStyle></UserStyle></NamedLayer>
</StyledLayerDescriptor>"""

SLD_WITH_RELATIVE_RESOURCE = b"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
  xmlns="http://www.opengis.net/sld"
  xmlns:xlink="http://www.w3.org/1999/xlink">
  <NamedLayer><Name>planning</Name><UserStyle><FeatureTypeStyle>
    <Rule><PointSymbolizer><Graphic><ExternalGraphic>
      <OnlineResource xlink:href="symbols/planning.png" />
      <Format>image/png</Format>
    </ExternalGraphic></Graphic></PointSymbolizer></Rule>
  </FeatureTypeStyle></UserStyle></NamedLayer>
</StyledLayerDescriptor>"""


def make_sld_package(
    *,
    resource_name: str | None = None,
    include_unreferenced: bool = False,
) -> tuple[bytes, bytes]:
    resource = make_png(1, 1)
    digest = hashlib.sha256(resource).hexdigest()
    local_name = resource_name or f"resources/{digest}.png"
    sld = SLD_WITH_RELATIVE_RESOURCE.replace(
        b"symbols/planning.png",
        f"resources/{digest}.png".encode(),
    )
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("style.sld", sld)
        archive.writestr(local_name, resource)
        if include_unreferenced:
            extra = make_png(2, 1)
            extra_digest = hashlib.sha256(extra).hexdigest()
            archive.writestr(f"resources/{extra_digest}.png", extra)
    return package.getvalue(), sld


def make_sld_only_package() -> bytes:
    package = io.BytesIO()
    with zipfile.ZipFile(
        package,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("style.sld", VALID_SLD)
    return package.getvalue()


def packaged_sld_sha256(package: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        return hashlib.sha256(archive.read("style.sld")).hexdigest()


class FakeResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        content_type: str | None = "application/json",
        content_encoding: str | None = None,
        content_length: str | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers: dict[str, str] = {
            "Content-Length": str(len(body))
            if content_length is None
            else content_length,
        }
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding
        self.read_sizes: list[int] = []

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body[:size]


class FakeConnection:
    def __init__(
        self,
        response: FakeResponse,
        *,
        request_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.request_error = request_error
        self.requests: list[
            tuple[str, str, bytes | None, dict[str, str]]
        ] = []
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        body: bytes | None = None,
        *,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, target, body, headers))
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class QueueFactory:
    def __init__(
        self,
        responses: list[FakeResponse],
        *,
        request_error: Exception | None = None,
    ) -> None:
        self.responses = list(responses)
        self.request_error = request_error
        self.connections: list[FakeConnection] = []
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, host: str, port: int, timeout: float) -> FakeConnection:
        self.calls.append((host, port, timeout))
        if not self.responses:
            raise AssertionError("unexpected GeoServer request")
        connection = FakeConnection(
            self.responses.pop(0),
            request_error=self.request_error,
        )
        self.connections.append(connection)
        return connection


def json_response(payload: dict, *, status: int = 200) -> FakeResponse:
    return FakeResponse(
        json.dumps(payload).encode(),
        status=status,
        content_type="application/json; charset=UTF-8",
    )


def status_response(status: int) -> FakeResponse:
    return FakeResponse(b"", status=status, content_type=None)


def xml_response(payload: bytes, *, status: int = 200) -> FakeResponse:
    return FakeResponse(
        payload,
        status=status,
        content_type="application/xml; charset=UTF-8",
    )


def blob_store_list_xml(*names: str) -> bytes:
    stores = "".join(
        (
            "<blobStore>"
            f"<name>{name}</name>"
            '<atom:link xmlns:atom="http://www.w3.org/2005/Atom" '
            'rel="alternate" '
            f'href="http://127.0.0.1:8081/geoserver/gwc/rest/blobstores/{name}.xml" '
            'type="text/xml"/>'
            "</blobStore>"
        )
        for name in names
    )
    return f"<blobStores>{stores}</blobStores>".encode()


def file_blob_store_xml(
    *,
    identifier: str = GEOWEBCACHE_TILE_BLOB_STORE_ID,
    enabled: bool = True,
    default: bool = True,
    base_directory: str = GEOWEBCACHE_TILE_BLOB_STORE_DIRECTORY,
    block_size: int = 4096,
    path_generator: str | None = None,
) -> bytes:
    generator = (
        ""
        if path_generator is None
        else f"<pathGeneratorType>{path_generator}</pathGeneratorType>"
    )
    return (
        f'<FileBlobStore default="{str(default).lower()}">'
        f"<id>{identifier}</id>"
        f"<enabled>{str(enabled).lower()}</enabled>"
        f"<baseDirectory>{base_directory}</baseDirectory>"
        f"<fileSystemBlockSize>{block_size}</fileSystemBlockSize>"
        f"{generator}"
        "</FileBlobStore>"
    ).encode()


def workspace_payload(name: str = "siur") -> dict:
    return {"workspace": {"name": name}}


def datastore_payload(
    *,
    host: str = "postgres",
    schema: str = "siur_data",
) -> dict:
    values = {
        "dbtype": "postgis",
        "host": host,
        "port": "5432",
        "database": "app",
        "schema": schema,
        "user": "app",
        "passwd": "crypt1:masked-by-geoserver",
    }
    return {
        "dataStore": {
            "name": "siur_postgis",
            "type": "PostGIS",
            "enabled": True,
            "connectionParameters": {
                "entry": [
                    {"@key": key, "$": value}
                    for key, value in values.items()
                ]
            },
        }
    }


def feature_type_payload(
    *,
    native_name: str = "planning_v_012345",
) -> dict:
    return {
        "featureType": {
            "name": "planning_v_012345",
            "nativeName": native_name,
            "title": "Planeamiento",
            "enabled": True,
            "srs": "EPSG:25830",
            "projectionPolicy": "REPROJECT_TO_DECLARED",
        }
    }


def coverage_store_payload(
    uri: str = f"file:/mnt/reference_artifacts/{RASTER_STORAGE_KEY}",
) -> dict:
    return {
        "coverageStore": {
            "name": "planning_raster_v_012345",
            "type": "GeoTIFF",
            "enabled": True,
            "url": uri,
        }
    }


def coverage_payload() -> dict:
    return {
        "coverage": {
            "name": "planning_raster_v_012345",
            "nativeName": "planning",
            "title": "Planeamiento raster",
            "enabled": True,
        }
    }


def layer_payload(name: str = "planning_v_012345") -> dict:
    return {"layer": {"name": name, "enabled": True}}


def layer_styles_payload(*names: str) -> dict:
    return {
        "styles": {
            "style": [
                {
                    "name": name,
                    "href": (
                        "http://127.0.0.1:8081/geoserver/rest/workspaces/"
                        f"siur/styles/{name}.json"
                    ),
                }
                for name in names
            ],
        }
    }


def disk_quota_payload(
    *,
    enabled: bool = True,
    value: str | int = "20",
    units: str = "GiB",
    cleanup_frequency: int = 60,
    cleanup_units: str = "SECONDS",
    policy: str = "LRU",
) -> dict:
    return {
        "gwcQuotaConfiguration": {
            "enabled": enabled,
            "cacheCleanUpFrequency": cleanup_frequency,
            "cacheCleanUpUnits": cleanup_units,
            "globalExpirationPolicyName": policy,
            "globalQuota": {"value": value, "units": units},
        }
    }


def make_client(
    responses: list[FakeResponse],
    *,
    with_postgis_password: bool = True,
    request_error: Exception | None = None,
) -> tuple[GeoServerAdminClient, QueueFactory]:
    factory = QueueFactory(responses, request_error=request_error)
    client = GeoServerAdminClient(
        base_url="http://127.0.0.1:8081/geoserver",
        workspace="siur",
        timeout_seconds=3.5,
        admin_user="admin",
        admin_password=SecretStr(ADMIN_PASSWORD),
        postgis_host="postgres",
        postgis_port=5432,
        postgis_database="app",
        postgis_user="app",
        postgis_password=(
            SecretStr(POSTGIS_PASSWORD) if with_postgis_password else None
        ),
        connection_factory=factory,
    )
    return client, factory


def all_requests(
    factory: QueueFactory,
) -> list[tuple[str, str, bytes | None, dict]]:
    return [
        request
        for connection in factory.connections
        for request in connection.requests
    ]


def test_settings_keep_geoserver_passwords_masked() -> None:
    configured = Settings(
        _env_file=None,
        geoserver_admin_user=" admin ",
        geoserver_admin_password=ADMIN_PASSWORD,
        local_geoserver_postgis_host="POSTGRES",
        local_geoserver_postgis_password=POSTGIS_PASSWORD,
    )

    rendered = repr(configured)
    dumped = configured.model_dump()
    assert configured.geoserver_admin_user == "admin"
    assert configured.local_geoserver_postgis_host == "postgres"
    assert isinstance(dumped["geoserver_admin_password"], SecretStr)
    assert isinstance(dumped["local_geoserver_postgis_password"], SecretStr)
    assert ADMIN_PASSWORD not in rendered
    assert POSTGIS_PASSWORD not in rendered


def test_settings_allow_compose_to_remove_publisher_credentials() -> None:
    configured = Settings(
        _env_file=None,
        geoserver_admin_user="  ",
        geoserver_admin_password="",
        local_geoserver_postgis_password="",
    )

    assert configured.geoserver_admin_user is None
    assert configured.geoserver_admin_password is None
    assert configured.local_geoserver_postgis_password is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("geoserver_admin_user", "admin:other"),
        ("local_geoserver_postgis_password", "has\x00nul"),
        ("local_geoserver_postgis_host", "postgres/path"),
        ("local_geoserver_postgis_host", "127.0.0.1:5432"),
        ("local_geoserver_postgis_port", 0),
        ("local_geoserver_postgis_database", "app/name"),
    ],
)
def test_settings_reject_unsafe_admin_and_postgis_configuration(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("geowebcache_disk_quota_gib", 0),
        ("geowebcache_disk_quota_gib", True),
        ("geowebcache_disk_quota_min_free_gib", -1),
        ("geowebcache_disk_quota_cleanup_seconds", 0),
        ("geowebcache_disk_quota_policy", "fifo"),
    ],
)
def test_settings_reject_unsafe_geowebcache_quota_configuration(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_settings_parse_geowebcache_numeric_environment_values() -> None:
    configured = Settings(
        _env_file=None,
        geowebcache_disk_quota_gib="21",  # type: ignore[arg-type]
        geowebcache_disk_quota_min_free_gib="6",  # type: ignore[arg-type]
        geowebcache_disk_quota_cleanup_seconds="90",  # type: ignore[arg-type]
    )

    assert configured.geowebcache_disk_quota_gib == 21
    assert configured.geowebcache_disk_quota_min_free_gib == 6
    assert configured.geowebcache_disk_quota_cleanup_seconds == 90


def test_client_repr_and_errors_never_expose_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.reference_layers.geoserver_admin.settings."
        "local_geoserver_postgis_password",
        None,
    )
    client, _ = make_client([], with_postgis_password=False)
    assert ADMIN_PASSWORD not in repr(client)
    assert POSTGIS_PASSWORD not in repr(client)

    with pytest.raises(UnsafeGeoServerAdminConfigurationError) as captured:
        client.ensure_postgis_datastore(
            datastore_name="siur_postgis",
            schema_name="siur_data",
        )
    assert ADMIN_PASSWORD not in str(captured.value)
    assert POSTGIS_PASSWORD not in str(captured.value)


def test_client_requires_secretstr_and_fixed_numeric_loopback() -> None:
    with pytest.raises(UnsafeGeoServerAdminConfigurationError):
        GeoServerAdminClient(
            base_url="http://localhost:8081/geoserver",
            admin_user="admin",
            admin_password=SecretStr(ADMIN_PASSWORD),
        )
    with pytest.raises(UnsafeGeoServerAdminConfigurationError):
        GeoServerAdminClient(
            admin_user="admin",
            admin_password=ADMIN_PASSWORD,  # type: ignore[arg-type]
        )


def test_health_uses_only_loopback_basic_auth_and_returns_version() -> None:
    client, factory = make_client(
        [
            json_response(
                {
                    "about": {
                        "resource": [
                            {"@name": "GeoTools", "Version": "33.0"},
                            {"@name": "GeoServer", "Version": "3.0.0"},
                        ]
                    }
                }
            )
        ]
    )

    health = client.health()

    assert health.version == "3.0.0"
    assert factory.calls == [("127.0.0.1", 8081, 3.5)]
    method, target, body, headers = all_requests(factory)[0]
    assert (method, target, body) == (
        "GET",
        "/geoserver/rest/about/version.json",
        None,
    )
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Connection"] == "close"
    decoded = base64.b64decode(headers["Authorization"].removeprefix("Basic "))
    assert decoded == f"admin:{ADMIN_PASSWORD}".encode()
    assert factory.connections[0].closed


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (status_response(401), GeoServerAdminAuthenticationError),
        (status_response(302), GeoServerAdminResponseError),
        (status_response(503), GeoServerAdminUnavailableError),
        (
            FakeResponse(
                b"{}",
                content_encoding="gzip",
            ),
            GeoServerAdminResponseError,
        ),
        (
            FakeResponse(
                b"{}",
                content_length=str(MAX_JSON_BYTES + 1),
            ),
            GeoServerAdminResponseError,
        ),
    ],
)
def test_health_rejects_auth_redirect_server_and_bounded_response_failures(
    response: FakeResponse,
    error: type[Exception],
) -> None:
    client, _ = make_client([response])
    with pytest.raises(error):
        client.health()


def test_health_rejects_malformed_or_unexpected_json() -> None:
    for response in [
        FakeResponse(b"not-json"),
        FakeResponse(b'{"about": {}, "about": {}}'),
        FakeResponse(b'{"about": NaN}'),
        FakeResponse(b"{}", content_type="text/html"),
        json_response({"about": {"resource": []}}),
    ]:
        client, _ = make_client([response])
        with pytest.raises(GeoServerAdminResponseError):
            client.health()


def test_geowebcache_file_blob_store_reads_fixed_xml_resources() -> None:
    client, factory = make_client(
        [
            xml_response(blob_store_list_xml(GEOWEBCACHE_TILE_BLOB_STORE_ID)),
            xml_response(
                file_blob_store_xml(path_generator="DEFAULT")
            ),
        ]
    )

    stores = client.read_geowebcache_file_blob_stores()

    assert stores == (
        GeoWebCacheFileBlobStore(
            id=GEOWEBCACHE_TILE_BLOB_STORE_ID,
            enabled=True,
            default=True,
            base_directory=GEOWEBCACHE_TILE_BLOB_STORE_DIRECTORY,
            file_system_block_size=4096,
            path_generator_type="DEFAULT",
        ),
    )
    assert [request[:2] for request in all_requests(factory)] == [
        ("GET", "/geoserver/gwc/rest/blobstores.xml"),
        (
            "GET",
            "/geoserver/gwc/rest/blobstores/"
            f"{GEOWEBCACHE_TILE_BLOB_STORE_ID}.xml",
        ),
    ]
    assert all(
        request[3]["Accept"] == "application/xml"
        for request in all_requests(factory)
    )


def test_geowebcache_tile_blob_store_creation_is_xml_and_revalidated() -> None:
    client, factory = make_client(
        [
            xml_response(blob_store_list_xml()),
            status_response(200),
            xml_response(blob_store_list_xml(GEOWEBCACHE_TILE_BLOB_STORE_ID)),
            xml_response(file_blob_store_xml()),
        ]
    )

    store = client.ensure_geowebcache_tile_blob_store(
        file_system_block_size=4096,
    )

    assert store.id == GEOWEBCACHE_TILE_BLOB_STORE_ID
    requests = all_requests(factory)
    assert [request[:2] for request in requests] == [
        ("GET", "/geoserver/gwc/rest/blobstores.xml"),
        (
            "PUT",
            "/geoserver/gwc/rest/blobstores/"
            f"{GEOWEBCACHE_TILE_BLOB_STORE_ID}.xml",
        ),
        ("GET", "/geoserver/gwc/rest/blobstores.xml"),
        (
            "GET",
            "/geoserver/gwc/rest/blobstores/"
            f"{GEOWEBCACHE_TILE_BLOB_STORE_ID}.xml",
        ),
    ]
    put = requests[1]
    assert put[3]["Content-Type"] == "application/xml"
    root = ElementTree.fromstring(put[2] or b"")
    assert root.tag == "FileBlobStore"
    assert root.attrib == {"default": "true"}
    assert [(child.tag, child.text) for child in root] == [
        ("id", GEOWEBCACHE_TILE_BLOB_STORE_ID),
        ("enabled", "true"),
        ("baseDirectory", GEOWEBCACHE_TILE_BLOB_STORE_DIRECTORY),
        ("fileSystemBlockSize", "4096"),
    ]
    assert ADMIN_PASSWORD not in (put[2] or b"").decode()


def test_geowebcache_tile_blob_store_creation_is_idempotent() -> None:
    client, factory = make_client(
        [
            xml_response(blob_store_list_xml(GEOWEBCACHE_TILE_BLOB_STORE_ID)),
            xml_response(file_blob_store_xml()),
        ]
    )

    store = client.ensure_geowebcache_tile_blob_store(
        file_system_block_size=4096,
    )

    assert store.default is True
    assert [request[0] for request in all_requests(factory)] == ["GET", "GET"]


def test_geowebcache_tile_blob_store_fails_before_put_on_mismatch() -> None:
    client, factory = make_client(
        [
            xml_response(blob_store_list_xml(GEOWEBCACHE_TILE_BLOB_STORE_ID)),
            xml_response(file_blob_store_xml(base_directory="/wrong/cache")),
        ]
    )

    with pytest.raises(
        GeoServerAdminConflictError,
        match="blob store differs",
    ):
        client.ensure_geowebcache_tile_blob_store(
            file_system_block_size=4096,
        )

    assert [request[0] for request in all_requests(factory)] == ["GET", "GET"]


def test_geowebcache_tile_blob_store_fails_on_configured_default() -> None:
    legacy_id = "defaultCache"
    client, factory = make_client(
        [
            xml_response(blob_store_list_xml(legacy_id)),
            xml_response(
                file_blob_store_xml(
                    identifier=legacy_id,
                    base_directory="/opt/geoserver_data/gwc",
                )
            ),
        ]
    )

    with pytest.raises(
        GeoServerAdminConflictError,
        match="another GeoWebCache default",
    ):
        client.ensure_geowebcache_tile_blob_store(
            file_system_block_size=4096,
        )

    assert [request[0] for request in all_requests(factory)] == ["GET", "GET"]


def test_geowebcache_tile_blob_store_fails_on_extra_default() -> None:
    other_id = "other-default"
    client, factory = make_client(
        [
            xml_response(
                blob_store_list_xml(
                    GEOWEBCACHE_TILE_BLOB_STORE_ID,
                    other_id,
                )
            ),
            xml_response(file_blob_store_xml()),
            xml_response(
                file_blob_store_xml(
                    identifier=other_id,
                    base_directory="/other/cache",
                )
            ),
        ]
    )

    with pytest.raises(
        GeoServerAdminConflictError,
        match="additional default",
    ):
        client.ensure_geowebcache_tile_blob_store(
            file_system_block_size=4096,
        )

    assert [request[0] for request in all_requests(factory)] == [
        "GET",
        "GET",
        "GET",
    ]


def test_geowebcache_tile_blob_store_fails_if_reread_is_not_exact() -> None:
    client, factory = make_client(
        [
            xml_response(blob_store_list_xml()),
            status_response(200),
            xml_response(blob_store_list_xml(GEOWEBCACHE_TILE_BLOB_STORE_ID)),
            xml_response(file_blob_store_xml(block_size=8192)),
        ]
    )

    with pytest.raises(
        GeoServerAdminConflictError,
        match="blob store differs",
    ):
        client.ensure_geowebcache_tile_blob_store(
            file_system_block_size=4096,
        )

    assert [request[0] for request in all_requests(factory)] == [
        "GET",
        "PUT",
        "GET",
        "GET",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"<!DOCTYPE blobStores [<!ENTITY x 'unsafe'>]>"
        b"<blobStores>&x;</blobStores>",
        b"<blobStores><blobStore><name>duplicate</name>"
        b"<name>duplicate</name></blobStore></blobStores>",
        b"<S3BlobStore default=\"true\"><id>remote</id>"
        b"<enabled>true</enabled></S3BlobStore>",
        file_blob_store_xml(path_generator="UNKNOWN"),
        file_blob_store_xml(block_size=4097),
    ],
)
def test_geowebcache_blob_store_xml_is_strict(payload: bytes) -> None:
    responses = (
        [xml_response(payload)]
        if payload.startswith(b"<blobStores") or payload.startswith(b"<!")
        else [
            xml_response(blob_store_list_xml("remote")),
            xml_response(payload),
        ]
    )
    client, _ = make_client(responses)

    with pytest.raises(GeoServerAdminResponseError):
        client.read_geowebcache_file_blob_stores()


def test_geowebcache_disk_quota_reads_fixed_gwc_rest_resource() -> None:
    client, factory = make_client([json_response(disk_quota_payload())])

    quota = client.read_geowebcache_disk_quota()

    assert quota.enabled is True
    assert quota.quota_bytes == 20 * 1024**3
    assert quota.cleanup_frequency == 60
    assert quota.expiration_policy == "LRU"
    method, target, body, _headers = all_requests(factory)[0]
    assert (method, target, body) == (
        "GET",
        "/geoserver/gwc/rest/diskquota.json",
        None,
    )


def test_geowebcache_disk_quota_puts_then_rereads_exact_state() -> None:
    client, factory = make_client(
        [
            status_response(200),
            json_response(disk_quota_payload()),
        ]
    )

    quota = client.configure_geowebcache_disk_quota(
        quota_gib=20,
        cleanup_seconds=60,
        expiration_policy="LRU",
    )

    assert quota.quota_value == 20
    requests = all_requests(factory)
    assert [request[:2] for request in requests] == [
        ("PUT", "/geoserver/gwc/rest/diskquota.json"),
        ("GET", "/geoserver/gwc/rest/diskquota.json"),
    ]
    assert json.loads(requests[0][2] or b"") == {
        "gwcQuotaConfiguration": {
            "enabled": True,
            "cacheCleanUpFrequency": 60,
            "cacheCleanUpUnits": "SECONDS",
            "globalExpirationPolicyName": "LRU",
            "globalQuota": {"value": "20", "units": "GiB"},
        }
    }
    assert requests[0][3]["Authorization"].startswith("Basic ")
    assert ADMIN_PASSWORD not in (requests[0][2] or b"").decode()


def test_geowebcache_disk_quota_fails_closed_on_mismatch_or_bad_shape() -> None:
    client, _ = make_client(
        [
            status_response(200),
            json_response(disk_quota_payload(enabled=False)),
        ]
    )
    with pytest.raises(GeoServerAdminConflictError):
        client.configure_geowebcache_disk_quota(
            quota_gib=20,
            cleanup_seconds=60,
            expiration_policy="LRU",
        )

    for payload in [
        {},
        disk_quota_payload(value="20.0"),
        disk_quota_payload(units="GB"),
        disk_quota_payload(cleanup_frequency=0),
        disk_quota_payload(policy="FIFO"),
    ]:
        client, _ = make_client([json_response(payload)])
        with pytest.raises(GeoServerAdminResponseError):
            client.read_geowebcache_disk_quota()


def test_geowebcache_disk_quota_rejects_unsafe_requested_values() -> None:
    client, factory = make_client([])
    with pytest.raises(InvalidGeoServerPublicationError):
        client.configure_geowebcache_disk_quota(
            quota_gib=0,
            cleanup_seconds=60,
            expiration_policy="LRU",
        )
    with pytest.raises(InvalidGeoServerPublicationError):
        client.configure_geowebcache_disk_quota(
            quota_gib=20,
            cleanup_seconds=0,
            expiration_policy="LRU",
        )
    assert all_requests(factory) == []


def test_transport_failure_is_generic_and_closes_connection() -> None:
    client, factory = make_client(
        [json_response({})],
        request_error=OSError("connection refused"),
    )
    with pytest.raises(GeoServerAdminUnavailableError) as captured:
        client.health()
    assert "connection refused" not in str(captured.value)
    assert factory.connections[0].closed


def test_workspace_creation_is_closed_and_idempotent() -> None:
    client, factory = make_client([status_response(404), status_response(201)])

    result = client.ensure_workspace()

    assert result.created is True
    requests = all_requests(factory)
    assert [request[:2] for request in requests] == [
        (
            "GET",
            "/geoserver/rest/workspaces/siur.json?quietOnNotFound=true",
        ),
        ("POST", "/geoserver/rest/workspaces"),
    ]
    assert json.loads(requests[1][2] or b"") == {
        "workspace": {"name": "siur"}
    }

    client, factory = make_client([json_response(workspace_payload())])
    result = client.ensure_workspace()
    assert result.created is False
    assert len(all_requests(factory)) == 1


def test_workspace_creation_handles_a_concurrent_create_without_overwrite() -> None:
    client, factory = make_client(
        [
            status_response(404),
            status_response(409),
            json_response(workspace_payload()),
        ]
    )
    result = client.ensure_workspace()
    assert result.created is False
    assert [request[0] for request in all_requests(factory)] == [
        "GET",
        "POST",
        "GET",
    ]


def test_workspace_existing_under_wrong_identity_is_a_conflict() -> None:
    client, _ = make_client([json_response(workspace_payload("other"))])
    with pytest.raises(GeoServerAdminConflictError):
        client.ensure_workspace()


def test_postgis_datastore_creation_uses_secret_only_in_request_body() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            status_response(404),
            status_response(201),
        ]
    )

    result = client.ensure_postgis_datastore(
        datastore_name="siur_postgis",
        schema_name="siur_data",
    )

    assert result.created is True
    method, target, body, headers = all_requests(factory)[-1]
    assert (method, target) == (
        "POST",
        "/geoserver/rest/workspaces/siur/datastores",
    )
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body or b"")
    entries = payload["dataStore"]["connectionParameters"]["entry"]
    parameters = {entry["@key"]: entry["$"] for entry in entries}
    assert parameters == {
        "dbtype": "postgis",
        "host": "postgres",
        "port": "5432",
        "database": "app",
        "schema": "siur_data",
        "user": "app",
        "passwd": POSTGIS_PASSWORD,
    }


def test_existing_postgis_datastore_is_checked_without_comparing_masked_password() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            json_response(datastore_payload()),
        ]
    )
    result = client.ensure_postgis_datastore(
        datastore_name="siur_postgis",
        schema_name="siur_data",
    )
    assert result.created is False
    assert all(request[0] == "GET" for request in all_requests(factory))


def test_existing_postgis_datastore_mismatch_is_never_overwritten() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            json_response(datastore_payload(host="remote.example")),
        ]
    )
    with pytest.raises(GeoServerAdminConflictError):
        client.ensure_postgis_datastore(
            datastore_name="siur_postgis",
            schema_name="siur_data",
        )
    assert all(request[0] == "GET" for request in all_requests(factory))


def test_versioned_table_is_published_after_checking_store() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            json_response(datastore_payload()),
            status_response(404),
            status_response(201),
        ]
    )

    result = client.publish_versioned_table(
        datastore_name="siur_postgis",
        schema_name="siur_data",
        table_name="planning_v_012345",
        layer_name="planning_v_012345",
        title="Planeamiento",
        declared_srs="epsg:25830",
    )

    assert result.created is True
    method, target, body, _ = all_requests(factory)[-1]
    assert (method, target) == (
        "POST",
        "/geoserver/rest/workspaces/siur/datastores/siur_postgis/featuretypes",
    )
    assert json.loads(body or b"") == {
        "featureType": feature_type_payload()["featureType"]
    }


def test_existing_versioned_table_is_idempotent_but_mismatch_conflicts() -> None:
    common = [
        json_response(workspace_payload()),
        json_response(datastore_payload()),
    ]
    client, factory = make_client(common + [json_response(feature_type_payload())])
    result = client.publish_versioned_table(
        datastore_name="siur_postgis",
        schema_name="siur_data",
        table_name="planning_v_012345",
        layer_name="planning_v_012345",
        title="Planeamiento",
        declared_srs="EPSG:25830",
    )
    assert result.created is False
    assert all(request[0] == "GET" for request in all_requests(factory))

    client, factory = make_client(
        [
            json_response(workspace_payload()),
            json_response(datastore_payload()),
            json_response(feature_type_payload(native_name="other_v_012345")),
        ]
    )
    with pytest.raises(GeoServerAdminConflictError):
        client.publish_versioned_table(
            datastore_name="siur_postgis",
            schema_name="siur_data",
            table_name="planning_v_012345",
            layer_name="planning_v_012345",
            title="Planeamiento",
            declared_srs="EPSG:25830",
        )
    assert all(request[0] == "GET" for request in all_requests(factory))


@pytest.mark.parametrize(
    ("table", "layer"),
    [
        ("planning", "planning_v_012345"),
        ("planning_v_012345", "../planning_v_012345"),
        ("planning_v_short", "planning_v_012345"),
        ("planning-v-012345", "planning_v_012345"),
    ],
)
def test_versioned_table_rejects_mutable_or_unsafe_identifiers(
    table: str,
    layer: str,
) -> None:
    client, factory = make_client([])
    with pytest.raises(InvalidGeoServerPublicationError):
        client.publish_versioned_table(
            datastore_name="siur_postgis",
            schema_name="siur_data",
            table_name=table,
            layer_name=layer,
            title="Planeamiento",
            declared_srs="EPSG:25830",
        )
    assert factory.connections == []


def test_local_geotiff_store_and_coverage_are_created_from_fixed_mount() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            status_response(404),
            status_response(201),
            status_response(404),
            status_response(201),
        ]
    )

    store, coverage = client.publish_local_geotiff(
        store_name="planning_raster_v_012345",
        layer_name="planning_raster_v_012345",
        artifact_relative_path=RASTER_STORAGE_KEY,
        native_name="planning",
        title="Planeamiento raster",
    )

    assert store.created is True
    assert coverage.created is True
    requests = all_requests(factory)
    store_payload = json.loads(requests[2][2] or b"")
    assert store_payload["coverageStore"]["url"] == (
        f"file:/mnt/reference_artifacts/{RASTER_STORAGE_KEY}"
    )
    assert requests[2][1] == (
        "/geoserver/rest/workspaces/siur/coveragestores"
    )
    coverage_payload_sent = json.loads(requests[4][2] or b"")
    assert coverage_payload_sent == {"coverage": coverage_payload()["coverage"]}


def test_existing_local_geotiff_is_idempotent_and_accepts_canonical_file_uri() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            json_response(
                coverage_store_payload(
                    f"file:///mnt/reference_artifacts/{RASTER_STORAGE_KEY}"
                )
            ),
            json_response(coverage_payload()),
        ]
    )
    store, coverage = client.publish_local_geotiff(
        store_name="planning_raster_v_012345",
        layer_name="planning_raster_v_012345",
        artifact_relative_path=RASTER_STORAGE_KEY,
        native_name="planning",
        title="Planeamiento raster",
    )
    assert store.created is False
    assert coverage.created is False
    assert all(request[0] == "GET" for request in all_requests(factory))


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/planning.tif",
        "../planning.tif",
        "sha256/../planning.tif",
        "sha256//planning.tif",
        "sha256/planning.tif?url=http://example.test",
        "sha256/planning.png",
        "sha256\\planning.tif",
        "blobs/sha256/ab/short",
        f"blobs/sha256/cd/{RASTER_DIGEST}",
        f"blobs/sha256/AB/{RASTER_DIGEST.upper()}",
        f"{RASTER_STORAGE_KEY}.tif",
    ],
)
def test_local_geotiff_rejects_non_cas_and_unsafe_paths(path: str) -> None:
    client, factory = make_client([])
    with pytest.raises(InvalidGeoServerPublicationError):
        client.publish_local_geotiff(
            store_name="planning_raster_v_012345",
            layer_name="planning_raster_v_012345",
            artifact_relative_path=path,
            title="Planeamiento raster",
        )
    assert factory.connections == []


def test_immutable_sld_is_created_raw_and_is_idempotent() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            status_response(404),
            status_response(201),
        ]
    )
    result = client.publish_immutable_sld(
        style_name="planning_style_v_012345",
        sld=VALID_SLD,
    )
    assert result.created is True
    method, target, body, headers = all_requests(factory)[-1]
    parsed = urlsplit(target)
    assert method == "POST"
    assert parsed.path == "/geoserver/rest/workspaces/siur/styles"
    assert parse_qs(parsed.query) == {
        "name": ["planning_style_v_012345"],
        "raw": ["true"],
    }
    assert body == VALID_SLD
    assert headers["Content-Type"] == "application/vnd.ogc.sld+xml"

    client, factory = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
        ]
    )
    result = client.publish_immutable_sld(
        style_name="planning_style_v_012345",
        sld=VALID_SLD,
    )
    assert result.created is False
    assert all(request[0] == "GET" for request in all_requests(factory))


def test_immutable_sld_package_is_created_raw_and_is_idempotent() -> None:
    package, sld = make_sld_package()
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            status_response(404),
            status_response(201),
        ]
    )
    result = client.publish_immutable_sld_package(
        style_name="planning_style_v_012345",
        package=package,
        expected_sld_sha256=hashlib.sha256(sld).hexdigest(),
    )
    assert result.created is True
    method, target, body, headers = all_requests(factory)[-1]
    parsed = urlsplit(target)
    assert method == "POST"
    assert parsed.path == "/geoserver/rest/workspaces/siur/styles"
    assert parse_qs(parsed.query) == {
        "name": ["planning_style_v_012345"],
        "raw": ["true"],
    }
    assert body == package
    assert headers["Content-Type"] == "application/zip"

    client, factory = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(sld, content_type="application/vnd.ogc.sld+xml"),
        ]
    )
    result = client.publish_immutable_sld_package(
        style_name="planning_style_v_012345",
        package=package,
        expected_sld_sha256=hashlib.sha256(sld).hexdigest(),
    )
    assert result.created is False
    assert all(request[0] == "GET" for request in all_requests(factory))


def test_immutable_sld_package_accepts_closed_sld_without_resources() -> None:
    package = make_sld_only_package()
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            status_response(404),
            status_response(201),
        ]
    )

    result = client.publish_immutable_sld_package(
        style_name="planning_style_v_012345",
        package=package,
        expected_sld_sha256=hashlib.sha256(VALID_SLD).hexdigest(),
    )

    assert result.created is True
    method, _target, body, headers = all_requests(factory)[-1]
    assert method == "POST"
    assert body == package
    assert headers["Content-Type"] == "application/zip"


@pytest.mark.parametrize(
    "package",
    [
        make_sld_package(resource_name="../unsafe.png")[0],
        make_sld_package(resource_name="resources/" + "0" * 64 + ".png")[0],
        make_sld_package(include_unreferenced=True)[0],
    ],
)
def test_immutable_sld_package_rejects_unsafe_or_unbound_resources(
    package: bytes,
) -> None:
    client, factory = make_client([])
    with pytest.raises(InvalidGeoServerPublicationError):
        client.publish_immutable_sld_package(
            style_name="planning_style_v_012345",
            package=package,
            expected_sld_sha256=packaged_sld_sha256(package),
        )
    assert factory.connections == []


def test_immutable_sld_package_rejects_unapproved_internal_sld() -> None:
    package, sld = make_sld_package()
    client, factory = make_client([])

    with pytest.raises(InvalidGeoServerPublicationError):
        client.publish_immutable_sld_package(
            style_name="planning_style_v_012345",
            package=package,
            expected_sld_sha256=hashlib.sha256(
                sld.replace(b"planning", b"different", 1)
            ).hexdigest(),
        )

    assert factory.connections == []


def test_existing_immutable_sld_mismatch_is_never_overwritten() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD.replace(b"planning", b"different"),
                content_type="application/vnd.ogc.sld+xml",
            ),
        ]
    )
    with pytest.raises(GeoServerAdminConflictError):
        client.publish_immutable_sld(
            style_name="planning_style_v_012345",
            sld=VALID_SLD,
        )
    assert all(request[0] == "GET" for request in all_requests(factory))


def test_immutable_sld_accepts_forbidden_duplicate_only_after_exact_reread(
) -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            status_response(404),
            status_response(403),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
        ]
    )

    result = client.publish_immutable_sld(
        style_name="planning_style_v_012345",
        sld=VALID_SLD,
    )

    assert result.created is False
    assert [request[0] for request in all_requests(factory)] == [
        "GET",
        "GET",
        "POST",
        "GET",
    ]


def test_layer_style_is_associated_by_qualified_name_and_is_idempotent() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
            json_response(layer_payload()),
            json_response(layer_styles_payload()),
            status_response(201),
        ]
    )

    result = client.ensure_layer_style(
        layer_name="planning_v_012345",
        style_name="planning_style_v_012345",
    )

    assert result.created is True
    method, target, body, headers = all_requests(factory)[-1]
    assert (method, target) == (
        "POST",
        "/geoserver/rest/layers/siur:planning_v_012345/styles",
    )
    assert json.loads(body or b"") == {
        "style": {"name": "siur:planning_style_v_012345"}
    }
    assert headers["Content-Type"] == "application/json"

    client, factory = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
            json_response(layer_payload()),
            json_response(
                layer_styles_payload("planning_style_v_012345")
            ),
        ]
    )
    result = client.ensure_layer_style(
        layer_name="planning_v_012345",
        style_name="planning_style_v_012345",
    )
    assert result.created is False
    assert all(request[0] == "GET" for request in all_requests(factory))


def test_layer_style_concurrent_association_is_verified_after_forbidden() -> None:
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
            json_response(layer_payload()),
            json_response(layer_styles_payload()),
            status_response(403),
            json_response(
                layer_styles_payload("planning_style_v_012345")
            ),
        ]
    )

    result = client.ensure_layer_style(
        layer_name="planning_v_012345",
        style_name="planning_style_v_012345",
    )

    assert result.created is False
    assert [request[0] for request in all_requests(factory)] == [
        "GET",
        "GET",
        "GET",
        "GET",
        "POST",
        "GET",
    ]


def test_global_style_homonym_does_not_satisfy_workspace_association() -> None:
    style_name = "planning_style_v_012345"
    client, factory = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
            json_response(layer_payload()),
            json_response(
                {
                    "styles": {
                        "style": [
                            {
                                "name": style_name,
                                "href": (
                                    "http://127.0.0.1:8081/geoserver/rest/"
                                    f"styles/{style_name}.json"
                                ),
                            }
                        ]
                    }
                }
            ),
            status_response(201),
        ]
    )

    result = client.ensure_layer_style(
        layer_name="planning_v_012345",
        style_name=style_name,
    )

    assert result.created is True
    assert all_requests(factory)[-1][0] == "POST"


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (
            [json_response(workspace_payload()), status_response(404)],
            "style is not published",
        ),
        (
            [
                json_response(workspace_payload()),
                FakeResponse(
                    VALID_SLD,
                    content_type="application/vnd.ogc.sld+xml",
                ),
                status_response(404),
            ],
            "layer is not published",
        ),
    ],
)
def test_layer_style_requires_published_style_and_layer(
    responses: list[FakeResponse],
    message: str,
) -> None:
    client, factory = make_client(responses)
    with pytest.raises(GeoServerAdminConflictError, match=message):
        client.ensure_layer_style(
            layer_name="planning_v_012345",
            style_name="planning_style_v_012345",
        )
    assert all(request[0] == "GET" for request in all_requests(factory))


def test_layer_style_rejects_malformed_catalog_and_unversioned_names() -> None:
    client, _ = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
            json_response(layer_payload()),
            json_response({"styles": {"style": "invalid"}}),
        ]
    )
    with pytest.raises(GeoServerAdminResponseError):
        client.ensure_layer_style(
            layer_name="planning_v_012345",
            style_name="planning_style_v_012345",
        )

    client, _ = make_client(
        [
            json_response(workspace_payload()),
            FakeResponse(
                VALID_SLD,
                content_type="application/vnd.ogc.sld+xml",
            ),
            json_response(layer_payload()),
            json_response(
                {
                    "styles": {
                        "style": [
                            {
                                "name": "planning_style_v_012345",
                                "href": "http://[::1",
                            }
                        ]
                    }
                }
            ),
        ]
    )
    with pytest.raises(GeoServerAdminResponseError):
        client.ensure_layer_style(
            layer_name="planning_v_012345",
            style_name="planning_style_v_012345",
        )

    client, factory = make_client([])
    with pytest.raises(InvalidGeoServerPublicationError):
        client.ensure_layer_style(
            layer_name="planning",
            style_name="planning_style_v_012345",
        )
    assert factory.connections == []


@pytest.mark.parametrize(
    "sld",
    [
        b"<!DOCTYPE foo><StyledLayerDescriptor />",
        SLD_WITH_RELATIVE_RESOURCE,
        SLD_WITH_RELATIVE_RESOURCE.replace(
            b"symbols/planning.png",
            b"https://remote.example/planning.png",
        ),
        SLD_WITH_RELATIVE_RESOURCE.replace(
            b"symbols/planning.png", b"../planning.png"
        ),
        VALID_SLD.replace(b"<StyledLayerDescriptor", b"<OtherDescriptor"),
        b"not xml",
    ],
)
def test_immutable_sld_rejects_entities_external_resources_and_invalid_xml(
    sld: bytes,
) -> None:
    client, factory = make_client([])
    with pytest.raises(InvalidGeoServerPublicationError):
        client.publish_immutable_sld(
            style_name="planning_style_v_012345",
            sld=sld,
        )
    assert factory.connections == []


def test_layer_smoke_checks_catalog_then_renders_local_png() -> None:
    png = make_png(256, 256)
    client, factory = make_client(
        [
            json_response(
                {"layer": {"name": "planning_v_012345", "enabled": True}}
            ),
            FakeResponse(png, content_type="image/png"),
        ]
    )

    result = client.smoke_layer(
        layer_name="planning_v_012345",
        style_name="planning_style_v_012345",
    )

    assert result.layer_name == "planning_v_012345"
    assert result.style_name == "planning_style_v_012345"
    assert result.image_bytes == len(png)
    requests = all_requests(factory)
    assert requests[0][1] == (
        "/geoserver/rest/layers/siur:planning_v_012345.json"
        "?quietOnNotFound=true"
    )
    wms_target = urlsplit(requests[1][1])
    assert wms_target.path == "/geoserver/siur/wms"
    parameters = parse_qs(wms_target.query)
    assert parameters["LAYERS"] == ["siur:planning_v_012345"]
    assert parameters["STYLES"] == ["siur:planning_style_v_012345"]
    assert "Authorization" not in requests[1][3]


def test_layer_smoke_validates_map_legend_and_empty_identify_on_loopback() -> None:
    map_png = make_png(256, 256)
    legend_png = make_png(20, 40)
    empty_features = b'{"type":"FeatureCollection","features":[]}'
    client, factory = make_client(
        [
            json_response(
                {"layer": {"name": "planning_v_012345", "enabled": True}}
            ),
            FakeResponse(map_png, content_type="image/png"),
            FakeResponse(legend_png, content_type="image/png"),
            FakeResponse(
                empty_features,
                content_type="application/geo+json",
            ),
        ]
    )

    result = client.smoke_layer(
        layer_name="planning_v_012345",
        style_name="planning_style_v_012345",
        legend_available=True,
        identify_available=True,
        z=0,
        x=0,
        y=0,
        pixel_x=128,
        pixel_y=128,
    )

    assert result.legend_sha256 == hashlib.sha256(legend_png).hexdigest()
    assert result.identify_sha256 == hashlib.sha256(empty_features).hexdigest()
    assert result.identify_feature_count == 0
    assert result.pixel_x == result.pixel_y == 128
    assert all(call[0] == "127.0.0.1" for call in factory.calls)
    requests = all_requests(factory)
    operations = [
        parse_qs(urlsplit(request[1]).query)["REQUEST"]
        for request in requests[1:]
    ]
    assert operations == [["GetMap"], ["GetLegendGraphic"], ["GetFeatureInfo"]]


def test_layer_smoke_reports_missing_catalog_and_render_failures_safely() -> None:
    client, _ = make_client([status_response(404)])
    with pytest.raises(GeoServerLayerSmokeError):
        client.smoke_layer(
            layer_name="planning_v_012345",
            style_name=None,
        )

    client, _ = make_client(
        [
            json_response(
                {"layer": {"name": "planning_v_012345", "enabled": True}}
            ),
            FakeResponse(make_png(256, 256), content_type="image/png"),
            FakeResponse(make_png(20, 40), content_type="image/png"),
            FakeResponse(b"{}", content_type="application/geo+json"),
        ]
    )
    with pytest.raises(GeoServerLayerSmokeError):
        client.smoke_layer(
            layer_name="planning_v_012345",
            style_name=None,
            legend_available=True,
            identify_available=True,
        )

    client, _ = make_client(
        [
            json_response(
                {"layer": {"name": "planning_v_012345", "enabled": True}}
            ),
            FakeResponse(b"not a png", content_type="image/png"),
        ]
    )
    with pytest.raises(GeoServerLayerSmokeError):
        client.smoke_layer(
            layer_name="planning_v_012345",
            style_name=None,
        )
