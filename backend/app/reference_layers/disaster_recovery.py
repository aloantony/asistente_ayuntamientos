"""Safe SIUR backup, verification and isolated restore tooling.

This module intentionally uses only the Python standard library so the exact
same file can run in a small PostgreSQL-client operations image.  Every
mutating command is dry-run by default.  Apply mode:

* requires explicit absolute paths and a recent quiescence evidence file;
* rejects symlinks, special files, path overlap and existing destinations;
* writes to a unique sibling partial directory and atomically renames it;
* never removes a file, drops a database, or overwrites a runtime directory;
* compensates only a just-restored exact drill target if publication fails;
* keeps database credentials out of argv, reports and exception messages.

Redis queues/cache and GeoWebCache tiles are reconstructible and are therefore
declared exclusions rather than backup inputs. GeoWebCache configuration
remains in the fully inventoried GeoServer data directory.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit
from uuid import uuid4

SCHEMA_VERSION = 5
FILESYSTEM_IDENTITY_SCHEMA_VERSION = 4
GWC_FILE_BLOB_STORE_SCHEMA_VERSION = 5
DATABASE_IDENTITY_SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {
        LEGACY_SCHEMA_VERSION,
        DATABASE_IDENTITY_SCHEMA_VERSION,
        FILESYSTEM_IDENTITY_SCHEMA_VERSION,
        GWC_FILE_BLOB_STORE_SCHEMA_VERSION,
        SCHEMA_VERSION,
    }
)
QUIESCENCE_EVIDENCE_SCHEMA_VERSION = 2
LEGACY_QUIESCENCE_EVIDENCE_SCHEMA_VERSION = 1
DRILL_IDENTITY_SCHEMA_VERSION = 2
MAX_JSON_BYTES = 256 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 8192
COPY_CHUNK_BYTES = 1024 * 1024
MAX_OWNERSHIP_ID = 2**31 - 1
BACKUP_PREFIX = "siur-backup-"
RESTORE_PREFIX = "siur-drill-restore-"
QUIESCENCE_EVIDENCE_NAME = "quiescence-evidence.json"
DRILL_MARKER_PREFIX = "siur-drill-v1:"
DRILL_BASELINE_ZERO_COUNT = 12
REQUIRED_EXTENSION_SCHEMAS = {
    "plpgsql": "pg_catalog",
    "postgis": "public",
    "vector": "public",
}
PG_DUMP_EXTENSION_EXCLUSIONS = tuple(sorted(REQUIRED_EXTENSION_SCHEMAS))
GWC_CONFIGURATION_DIRECTORY = "/opt/geoserver_data/gwc"
GWC_TILE_CACHE_DIRECTORY = "/var/lib/geowebcache"
GWC_TILE_CACHE_VOLUME = "geowebcache_tile_cache_v3"
GWC_TILE_BLOB_STORE = {
    "type": "FileBlobStore",
    "id": "siur-tile-cache-v3",
    "default": True,
    "enabled": True,
    "base_directory": GWC_TILE_CACHE_DIRECTORY,
    "path_generator_type": "DEFAULT",
    "file_system_block_size": "measured-from-target-filesystem",
}
GWC_TILE_EXCLUDED_SCOPE = (
    "external explicit FileBlobStore v3 tile volume only"
)
LEGACY_GWC_TILE_EXCLUDED_SCOPE = (
    "external GEOWEBCACHE_CACHE_DIR tile volume only"
)
LEGACY_GWC_TILE_CACHE_DIRECTORY = "/opt/geoserver_data/gwc-cache"
EXPECTED_STOPPED_SERVICES = frozenset(
    {
        "backend",
        "worker",
        "reference-worker",
        "reference-scheduler",
        "geoserver",
    }
)
PROTECTED_RUNTIME_PATHS = (
    Path("/var/lib/asistente_ayuntamientos/reference-artifacts"),
    Path("/var/lib/geowebcache"),
    Path("/mnt/reference_artifacts"),
    Path("/opt/geoserver_data"),
    Path("/sources/reference_artifacts"),
    Path("/sources/geoserver_data"),
)
DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,62}$", re.ASCII)
OPERATOR_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$", re.ASCII)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
SYSTEM_IDENTIFIER_RE = re.compile(r"^[1-9][0-9]{0,31}$", re.ASCII)
DRILL_TOKEN_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.ASCII,
)
EXTENSION_VERSION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$",
    re.ASCII,
)
EXTENSION_MEMBER_CANONICALIZATION = "pg-identify-object-json-v1"
CANONICAL_EXTENSION_MEMBER_INVENTORIES = {
    ("plpgsql", "1.0"): (
        4,
        "995d6dd1dcea93e19ecfd163283c4ee2150ee6e6b4fb07828e389bca30b9f3be",
    ),
    ("postgis", "3.6.4"): (
        909,
        "d5aafa675b5917f596cae61f26525333b173ce51854e3274915cff88830cc8dd",
    ),
    ("vector", "0.8.2"): (
        237,
        "f75948c2610ad89be5f64d5da86ffbad33ffca79ea4b4a21d808680e6a6761f6",
    ),
}
BACKUP_PAYLOAD_NAMES = (
    "geoserver_data.tar",
    "manifest.json",
    "manifest.sha256",
    "postgres.dump",
    QUIESCENCE_EVIDENCE_NAME,
    "reference_artifacts.tar",
)
PASSTHROUGH_FD_ENVIRONMENT_KEY = "SIUR_RECOVERY_PASSTHROUGH_FD"


class DisasterRecoveryError(RuntimeError):
    """Base error for a rejected or failed recovery operation."""


class DisasterRecoverySafetyError(DisasterRecoveryError):
    """An input or target cannot be proven safe."""


class DisasterRecoveryVerificationError(DisasterRecoveryError):
    """Backup bytes do not match their manifest."""


class DisasterRecoveryCommandError(DisasterRecoveryError):
    """A PostgreSQL client command failed without exposing its output."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
    ) -> CommandResult: ...


@dataclass(frozen=True)
class CreateBackupRequest:
    destination: Path
    reference_artifacts_source: Path
    geoserver_data_source: Path
    database_url_file: Path
    quiescence_evidence: Path | None = None
    quiescence_max_age_seconds: int = 3600


@dataclass(frozen=True)
class RestoreBackupRequest:
    backup: Path
    destination: Path
    target_database_url_file: Path
    target_database_identity_file: Path


@dataclass(frozen=True)
class _DatabaseLocation:
    database_name: str
    hostname: str
    port: int
    username: str
    password: str | None = field(repr=False)


@dataclass(frozen=True)
class _DatabaseExtension:
    name: str
    schema: str
    version: str
    owner: str

    def manifest_value(self, *, include_owner: bool) -> dict[str, object]:
        result = {
            "name": self.name,
            "schema": self.schema,
            "version": self.version,
        }
        if include_owner:
            result["owner"] = self.owner
        return result


@dataclass(frozen=True)
class _ExtensionMemberInventory:
    name: str
    version: str
    member_count: int
    inventory_sha256: str

    def manifest_value(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "member_count": self.member_count,
            "inventory_sha256": self.inventory_sha256,
        }


@dataclass(frozen=True)
class _DatabaseIdentity:
    database_name: str
    database_user: str
    system_identifier: str
    database_oid: int
    extensions: tuple[_DatabaseExtension, ...]
    drill_token: str | None = None


@dataclass
class _VerifiedBackup:
    backup_path: Path
    directory_metadata: os.stat_result
    payload_metadata: dict[str, os.stat_result]
    descriptors: dict[str, int]
    manifest: dict[str, Any]
    manifest_sha256: str
    result: dict[str, object]

    def close(self) -> None:
        for descriptor in self.descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.descriptors.clear()


@dataclass(frozen=True)
class _InventoryEntry:
    path: str
    kind: Literal["directory", "file"]
    size_bytes: int
    sha256: str | None
    mode: int
    mtime_ns: int
    ctime_ns: int
    links: int
    uid: int
    gid: int
    device: int
    inode: int

    def manifest_value(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "links": self.links,
            "uid": self.uid,
            "gid": self.gid,
        }
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        return result


@dataclass(frozen=True)
class _TreeInventory:
    source: Path
    entries: tuple[_InventoryEntry, ...]
    excluded_paths: tuple[str, ...]
    source_device: int
    source_inode: int
    source_mode: int
    source_mtime_ns: int
    source_ctime_ns: int
    source_links: int
    source_uid: int
    source_gid: int

    @property
    def file_count(self) -> int:
        return sum(entry.kind == "file" for entry in self.entries)

    @property
    def directory_count(self) -> int:
        return sum(entry.kind == "directory" for entry in self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(
            entry.size_bytes for entry in self.entries if entry.kind == "file"
        )


def create_backup(
    request: CreateBackupRequest,
    *,
    apply: bool = False,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Plan or create one consistent immutable SIUR backup directory."""

    timestamp = _utc_now(now)
    destination = _new_named_destination(
        request.destination,
        prefix=BACKUP_PREFIX,
    )
    reference_source = _existing_directory(
        request.reference_artifacts_source,
        label="reference artifacts source",
    )
    geoserver_source = _existing_directory(
        request.geoserver_data_source,
        label="GeoServer data source",
    )
    credential_path = _absolute_path(
        request.database_url_file,
        label="database credential file",
    )
    protected_inputs = {
        "destination": destination,
        "reference artifacts source": reference_source,
        "GeoServer data source": geoserver_source,
        "database credential file": credential_path,
    }
    if request.quiescence_evidence is not None:
        protected_inputs["quiescence evidence"] = _absolute_path(
            request.quiescence_evidence,
            label="quiescence evidence",
        )
    _reject_overlaps(protected_inputs)
    database = _read_database_location(
        credential_path,
        target=False,
    )
    evidence: dict[str, object] | None = None
    evidence_bytes: bytes | None = None
    evidence_sha256: str | None = None
    if request.quiescence_evidence is not None:
        evidence, evidence_bytes, evidence_sha256 = (
            _validate_quiescence_evidence(
                request.quiescence_evidence,
                reference_source=reference_source,
                geoserver_source=geoserver_source,
                database=database,
                now=timestamp,
                max_age_seconds=request.quiescence_max_age_seconds,
            )
        )
    if apply and evidence is None:
        raise DisasterRecoverySafetyError(
            "apply requires a recent quiescence evidence file"
        )
    command_runner = runner or subprocess_command_runner
    source_identity: _DatabaseIdentity | None = None
    source_extension_members: tuple[_ExtensionMemberInventory, ...] | None = (
        None
    )
    if apply:
        if evidence is None:
            raise DisasterRecoverySafetyError(
                "validated quiescence evidence is unavailable"
            )
        source_identity = _read_database_identity(
            command_runner,
            database,
            require_drill_marker=False,
        )
        _assert_evidence_database_identity(evidence, source_identity)
        source_extension_members = _read_extension_member_inventories(
            command_runner,
            database,
            extensions=source_identity.extensions,
            label="source",
        )

    reference_inventory = _inventory_tree(
        reference_source,
        excluded_roots=frozenset(
            {"staging", ".reference-blob-store.lock"}
        ),
    )
    # The actual tile volume is a distinct versioned Docker volume and is not
    # mounted in the recovery container.  The directory visible through the
    # parent GeoServer data volume must therefore be an empty mountpoint.  Any
    # content here could be configuration left by the former mixed gwc volume.
    _assert_empty_geowebcache_placeholder(
        geoserver_source / "gwc-cache",
    )
    geoserver_inventory = _inventory_tree(
        geoserver_source,
        excluded_roots=frozenset({"gwc-cache"}),
    )

    plan = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry-run",
        "destination": str(destination),
        "database_name": database.database_name,
        "reference_artifacts": _inventory_summary(reference_inventory),
        "geoserver_data": _inventory_summary(geoserver_inventory),
        "quiescence_evidence_sha256": evidence_sha256,
        "excluded_reconstructible_state": [
            {
                "component": "redis",
                "reason": (
                    "RQ coordination and cache state is reconstructed from "
                    "PostgreSQL mirror state after workers restart"
                ),
            },
            {
                "component": "geowebcache",
                "included": False,
                "reason": (
                    "only the external explicit FileBlobStore v3 tile volume "
                    "is derived; GEOWEBCACHE_CACHE_DIR is configuration "
                    "inside the included GeoServer data directory"
                ),
                "configuration_included": True,
                "configuration_directory": GWC_CONFIGURATION_DIRECTORY,
                "cache_directory": GWC_TILE_CACHE_DIRECTORY,
                "volume": GWC_TILE_CACHE_VOLUME,
                "blob_store": dict(GWC_TILE_BLOB_STORE),
            },
        ],
    }
    if not apply:
        return plan

    partial = destination.parent / (
        f".{destination.name}.partial-{uuid4().hex}"
    )
    _assert_new_path(partial, label="partial backup destination")
    try:
        _mkdir_new_directory(partial, 0o700)
        database_dump = partial / "postgres.dump"
        # Pre-create privately inside the 0700 staging directory so pg_dump
        # cannot inherit a permissive process umask or encounter a symlink.
        _write_exclusive(database_dump, b"")
        _run_pg_dump(command_runner, database, database_dump)
        _assert_regular_payload(database_dump, label="PostgreSQL dump")
        source_identity_after = _read_database_identity(
            command_runner,
            database,
            require_drill_marker=False,
        )
        if source_identity_after != source_identity:
            raise DisasterRecoverySafetyError(
                "source PostgreSQL identity changed while the dump was "
                "being created"
            )
        source_extension_members_after = _read_extension_member_inventories(
            command_runner,
            database,
            extensions=source_identity_after.extensions,
            label="source",
        )
        if source_extension_members_after != source_extension_members:
            raise DisasterRecoverySafetyError(
                "source PostgreSQL extension members changed while the dump "
                "was being created"
            )

        reference_archive = partial / "reference_artifacts.tar"
        geoserver_archive = partial / "geoserver_data.tar"
        _write_inventory_tar(reference_inventory, reference_archive)
        _write_inventory_tar(geoserver_inventory, geoserver_archive)
        evidence_payload = partial / QUIESCENCE_EVIDENCE_NAME
        if evidence_bytes is None:
            raise DisasterRecoverySafetyError(
                "validated quiescence evidence bytes are unavailable"
            )
        _write_exclusive(evidence_payload, evidence_bytes)

        reference_after = _inventory_tree(
            reference_source,
            excluded_roots=frozenset(
                {"staging", ".reference-blob-store.lock"}
            ),
        )
        _assert_empty_geowebcache_placeholder(
            geoserver_source / "gwc-cache",
        )
        geoserver_after = _inventory_tree(
            geoserver_source,
            excluded_roots=frozenset({"gwc-cache"}),
        )
        if (
            reference_inventory != reference_after
            or geoserver_inventory != geoserver_after
        ):
            raise DisasterRecoverySafetyError(
                "backup sources changed while the snapshot was being created"
            )
        source_identity_final = _read_database_identity(
            command_runner,
            database,
            require_drill_marker=False,
        )
        if source_identity_final != source_identity:
            raise DisasterRecoverySafetyError(
                "source PostgreSQL identity changed before backup "
                "publication"
            )
        source_extension_members_final = _read_extension_member_inventories(
            command_runner,
            database,
            extensions=source_identity_final.extensions,
            label="source",
        )
        if source_extension_members_final != source_extension_members:
            raise DisasterRecoverySafetyError(
                "source PostgreSQL extension members changed before backup "
                "publication"
            )
        if source_extension_members is None:
            raise DisasterRecoverySafetyError(
                "source PostgreSQL extension member inventory is unavailable"
            )

        manifest = _build_manifest(
            created_at=timestamp,
            database_name=database.database_name,
            database_dump=database_dump,
            evidence=evidence or {},
            evidence_payload=evidence_payload,
            reference_inventory=reference_inventory,
            reference_archive=reference_archive,
            geoserver_inventory=geoserver_inventory,
            geoserver_archive=geoserver_archive,
            extension_member_inventories=source_extension_members,
        )
        manifest_bytes = _canonical_json_bytes(manifest)
        if len(manifest_bytes) > MAX_JSON_BYTES:
            raise DisasterRecoverySafetyError(
                "backup manifest exceeds the supported verification limit"
            )
        _write_exclusive(partial / "manifest.json", manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        _write_exclusive(
            partial / "manifest.sha256",
            f"{manifest_sha}  manifest.json\n".encode("ascii"),
        )
        _fsync_directory(partial)
        durability_verified = _publish_directory_noreplace(
            partial,
            destination,
        )
    except Exception:
        # Deliberately leave an unmistakable partial directory for forensic
        # inspection.  This tool has no delete path.
        raise
    return {
        **plan,
        "backup_id": manifest["backup_id"],
        "manifest_sha256": manifest_sha,
        "completed": True,
        "durability_verified": durability_verified,
    }


def verify_backup(
    backup: Path,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    """Verify manifest, payload hashes, tar members and pg_dump readability."""

    verified = _open_verified_backup(
        backup,
        runner=runner or subprocess_command_runner,
    )
    try:
        return dict(verified.result)
    finally:
        verified.close()


def _open_verified_backup(
    backup: Path,
    *,
    runner: CommandRunner,
    expected_manifest_sha256: str | None = None,
) -> _VerifiedBackup:
    backup_path = _existing_named_directory(
        backup,
        prefix=BACKUP_PREFIX,
        label="backup",
    )
    directory_descriptor = _open_absolute_directory_descriptor(backup_path)
    descriptors: dict[str, int] = {}
    try:
        directory_metadata = os.fstat(directory_descriptor)
        names = os.listdir(directory_descriptor)
        if sorted(names) != sorted(BACKUP_PAYLOAD_NAMES):
            raise DisasterRecoveryVerificationError(
                "backup directory contains missing, unexpected or unsafe "
                "entries"
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        for name in BACKUP_PAYLOAD_NAMES:
            descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            descriptors[name] = descriptor
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise DisasterRecoveryVerificationError(
                    "backup directory contains missing, unexpected or unsafe "
                    "entries"
                )
    except OSError as error:
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise DisasterRecoveryVerificationError(
            "backup directory contains missing, unexpected or unsafe entries"
        ) from error
    except BaseException:
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise
    finally:
        os.close(directory_descriptor)

    try:
        manifest_bytes = _read_descriptor_bytes(
            descriptors["manifest.json"],
            limit=MAX_JSON_BYTES,
            label="backup manifest",
        )
        checksum_bytes = _read_descriptor_bytes(
            descriptors["manifest.sha256"],
            limit=1024,
            label="backup manifest checksum",
        )
        expected_hash = _parse_manifest_checksum(checksum_bytes)
        actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if (
            actual_manifest_hash != expected_hash
            or (
                expected_manifest_sha256 is not None
                and actual_manifest_hash != expected_manifest_sha256
            )
        ):
            raise DisasterRecoveryVerificationError(
                "backup manifest checksum does not match"
            )
        manifest = _decode_json_object(
            manifest_bytes,
            label="backup manifest",
        )
        _validate_manifest_header(manifest)
        manifest_schema_version = int(manifest["schema_version"])
        database = _manifest_mapping(manifest, "database")
        database_name = _database_name_from_manifest(database)
        trees = _manifest_mapping(manifest, "trees")
        reference = _mapping_member(trees, "reference_artifacts")
        geoserver = _mapping_member(trees, "geoserver_data")
        consistency = _manifest_mapping(manifest, "consistency")
        evidence_description = _mapping_member(
            consistency,
            "quiescence_evidence",
        )
        _assert_payload_description_name(
            evidence_description,
            expected_name=QUIESCENCE_EVIDENCE_NAME,
        )
        _verify_payload_descriptor(
            descriptors[QUIESCENCE_EVIDENCE_NAME],
            evidence_description,
        )
        _verify_stored_quiescence_evidence_bytes(
            _read_descriptor_bytes(
                descriptors[QUIESCENCE_EVIDENCE_NAME],
                limit=64 * 1024,
                label="stored quiescence evidence",
            ),
            consistency=consistency,
            manifest_schema_version=manifest_schema_version,
        )
        dump_description = _mapping_member(database, "dump")
        _assert_payload_description_name(
            dump_description,
            expected_name="postgres.dump",
        )
        _verify_payload_descriptor(
            descriptors["postgres.dump"],
            dump_description,
        )

        verified_trees: dict[str, dict[str, int]] = {}
        for name, description, expected_archive, expected_exclusions in (
            (
                "reference_artifacts",
                reference,
                "reference_artifacts.tar",
                [".reference-blob-store.lock", "staging"],
            ),
            (
                "geoserver_data",
                geoserver,
                "geoserver_data.tar",
                ["gwc-cache"],
            ),
        ):
            archive_description = _mapping_member(description, "archive")
            _assert_payload_description_name(
                archive_description,
                expected_name=expected_archive,
            )
            _verify_payload_descriptor(
                descriptors[expected_archive],
                archive_description,
            )
            entries = _parse_manifest_entries(
                description.get("entries"),
                manifest_schema_version=manifest_schema_version,
            )
            _validate_tree_statistics(
                description,
                entries,
                expected_exclusions=expected_exclusions,
                manifest_schema_version=manifest_schema_version,
            )
            _verify_tar_archive(descriptors[expected_archive], entries)
            verified_trees[name] = {
                "files": sum(item.kind == "file" for item in entries),
                "directories": sum(
                    item.kind == "directory" for item in entries
                ),
                "bytes": sum(
                    item.size_bytes
                    for item in entries
                    if item.kind == "file"
                ),
            }

        _run_pg_restore_list(runner, descriptors["postgres.dump"])
        backup_id = manifest.get("backup_id")
        if not isinstance(backup_id, str):
            raise DisasterRecoveryVerificationError("backup id is invalid")
        required_extensions = (
            _parse_required_extension_manifest(
                database.get("required_extensions")
            )
            if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION
            else None
        )
        extension_member_inventories = (
            _parse_manifest_extension_member_inventories(
                database.get("extension_members"),
                extensions=_parse_database_extensions(
                    consistency.get("postgres_extensions"),
                    label="backup PostgreSQL extensions",
                    error_type=DisasterRecoveryVerificationError,
                ),
            )
            if manifest_schema_version >= FILESYSTEM_IDENTITY_SCHEMA_VERSION
            else None
        )
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "manifest_schema_version": manifest_schema_version,
            "mode": "verify",
            "backup": str(backup_path),
            "backup_id": backup_id,
            "manifest_sha256": actual_manifest_hash,
            "database_name": database_name,
            "quiescence_evidence_sha256": evidence_description["sha256"],
            "required_extensions": required_extensions,
            "extension_member_inventories": extension_member_inventories,
            "trees": verified_trees,
            "verified": True,
        }
        return _VerifiedBackup(
            backup_path=backup_path,
            directory_metadata=directory_metadata,
            payload_metadata={
                name: os.fstat(descriptor)
                for name, descriptor in descriptors.items()
            },
            descriptors=descriptors,
            manifest=manifest,
            manifest_sha256=actual_manifest_hash,
            result=result,
        )
    except BaseException:
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise


def _assert_verified_backup_path_unchanged(
    verified: _VerifiedBackup,
) -> None:
    directory_descriptor = _open_absolute_directory_descriptor(
        verified.backup_path
    )
    opened: list[int] = []
    try:
        current_directory = os.fstat(directory_descriptor)
        _assert_same_verified_node(
            verified.directory_metadata,
            current_directory,
            kind="directory",
        )
        if sorted(os.listdir(directory_descriptor)) != sorted(
            BACKUP_PAYLOAD_NAMES
        ):
            raise DisasterRecoveryVerificationError(
                "verified backup path changed before restore consumption"
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        for name in BACKUP_PAYLOAD_NAMES:
            descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            opened.append(descriptor)
            _assert_same_verified_node(
                verified.payload_metadata[name],
                os.fstat(descriptor),
                kind="file",
            )
    except (OSError, DisasterRecoverySafetyError) as error:
        raise DisasterRecoveryVerificationError(
            "verified backup path changed before restore consumption"
        ) from error
    finally:
        for descriptor in opened:
            os.close(descriptor)
        os.close(directory_descriptor)


def _assert_same_verified_node(
    expected: os.stat_result,
    actual: os.stat_result,
    *,
    kind: Literal["directory", "file"],
) -> None:
    correct_kind = (
        stat.S_ISDIR(actual.st_mode)
        if kind == "directory"
        else stat.S_ISREG(actual.st_mode)
    )
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_uid",
        "st_gid",
        "st_nlink",
    )
    if not correct_kind or any(
        getattr(expected, field) != getattr(actual, field) for field in fields
    ):
        raise DisasterRecoveryVerificationError(
            "verified backup node identity changed"
        )


def _snapshot_verified_payloads(
    verified: _VerifiedBackup,
    *,
    directory: Path,
    runner: CommandRunner,
) -> dict[str, int]:
    if not hasattr(os, "O_TMPFILE"):
        raise DisasterRecoverySafetyError(
            "anonymous verified payload staging is unavailable"
        )
    descriptions = _backup_consumed_payload_descriptions(verified.manifest)
    directory_descriptor = _open_absolute_directory_descriptor(directory)
    snapshots: dict[str, int] = {}
    try:
        for name in (
            "reference_artifacts.tar",
            "geoserver_data.tar",
            "postgres.dump",
        ):
            snapshots[name] = _copy_descriptor_to_anonymous_file(
                verified.descriptors[name],
                directory_descriptor=directory_descriptor,
                description=descriptions[name],
            )
        for archive_name, tree_name in (
            ("reference_artifacts.tar", "reference_artifacts"),
            ("geoserver_data.tar", "geoserver_data"),
        ):
            tree = _mapping_member(
                _manifest_mapping(verified.manifest, "trees"),
                tree_name,
            )
            entries = _parse_manifest_entries(
                tree.get("entries"),
                manifest_schema_version=int(
                    verified.manifest["schema_version"]
                ),
            )
            _verify_tar_archive(snapshots[archive_name], entries)
        _run_pg_restore_list(runner, snapshots["postgres.dump"])
        return snapshots
    except BaseException:
        for descriptor in snapshots.values():
            os.close(descriptor)
        raise
    finally:
        os.close(directory_descriptor)


def _backup_consumed_payload_descriptions(
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    database = _manifest_mapping(manifest, "database")
    trees = _manifest_mapping(manifest, "trees")
    result = {
        "postgres.dump": _mapping_member(database, "dump"),
        "reference_artifacts.tar": _mapping_member(
            _mapping_member(trees, "reference_artifacts"),
            "archive",
        ),
        "geoserver_data.tar": _mapping_member(
            _mapping_member(trees, "geoserver_data"),
            "archive",
        ),
    }
    for name, description in result.items():
        _assert_payload_description_name(
            description,
            expected_name=name,
        )
    return result


def _copy_descriptor_to_anonymous_file(
    source_descriptor: int,
    *,
    directory_descriptor: int,
    description: Mapping[str, Any],
) -> int:
    expected_size, expected_sha256 = _payload_description_values(description)
    flags = os.O_RDWR | os.O_TMPFILE
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        writable = os.open(
            ".",
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "anonymous verified payload staging is unavailable"
        ) from error
    readonly: int | None = None
    try:
        source_before = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_before.st_mode)
            or source_before.st_nlink != 1
        ):
            raise DisasterRecoveryVerificationError(
                "verified backup payload identity is invalid"
            )
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(writable, view)
                view = view[written:]
        source_after = os.fstat(source_descriptor)
        _assert_same_verified_node(
            source_before,
            source_after,
            kind="file",
        )
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise DisasterRecoveryVerificationError(
                "backup payload changed before anonymous staging"
            )
        os.fsync(writable)
        os.fchmod(writable, 0o400)
        readonly_flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            readonly_flags |= os.O_CLOEXEC
        readonly = os.open(
            f"/proc/self/fd/{writable}",
            readonly_flags,
        )
        writable_metadata = os.fstat(writable)
        readonly_metadata = os.fstat(readonly)
        if (
            writable_metadata.st_dev != readonly_metadata.st_dev
            or writable_metadata.st_ino != readonly_metadata.st_ino
        ):
            raise DisasterRecoveryVerificationError(
                "anonymous verified payload could not be reopened safely"
            )
        os.close(writable)
        writable = -1
        _verify_payload_descriptor(readonly, description)
        return readonly
    except BaseException:
        if readonly is not None:
            os.close(readonly)
        raise
    finally:
        if writable >= 0:
            os.close(writable)


def restore_backup(
    request: RestoreBackupRequest,
    *,
    apply: bool = False,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Plan or restore into a new directory and an empty drill-only database."""

    command_runner = runner or subprocess_command_runner
    verified = _open_verified_backup(
        request.backup,
        runner=command_runner,
    )
    try:
        return _restore_verified_backup(
            request,
            verified=verified,
            apply=apply,
            command_runner=command_runner,
            now=now,
        )
    finally:
        verified.close()


def _restore_verified_backup(
    request: RestoreBackupRequest,
    *,
    verified: _VerifiedBackup,
    apply: bool,
    command_runner: CommandRunner,
    now: datetime | None,
) -> dict[str, object]:
    verification = verified.result
    backup_path = verified.backup_path
    destination = _new_named_destination(
        request.destination,
        prefix=RESTORE_PREFIX,
    )
    _reject_protected_restore_path(destination)
    target_credential_path = _absolute_path(
        request.target_database_url_file,
        label="target database credential file",
    )
    target_identity_path = _absolute_path(
        request.target_database_identity_file,
        label="target database identity file",
    )
    _reject_overlaps(
        {
            "backup": backup_path,
            "restore destination": destination,
            "target database credential file": target_credential_path,
            "target database identity file": target_identity_path,
        }
    )
    target = _read_database_location(
        target_credential_path,
        target=True,
    )
    expected_target_identity = _read_drill_identity(
        target_identity_path,
        database=target,
    )
    required_extensions = verification.get("required_extensions")
    if (
        not isinstance(
            verification.get("manifest_schema_version"),
            int,
        )
        or verification["manifest_schema_version"]
        < FILESYSTEM_IDENTITY_SCHEMA_VERSION
        or not isinstance(required_extensions, list)
    ):
        raise DisasterRecoverySafetyError(
            "legacy backup lacks a verified extension baseline and cannot "
            "be restored automatically"
        )
    if required_extensions != [
        item.manifest_value(include_owner=False)
        for item in expected_target_identity.extensions
    ]:
        raise DisasterRecoverySafetyError(
            "target database identity does not preinstall the exact source "
            "extension versions"
        )
    source_database_name = str(verification["database_name"])
    if target.database_name == source_database_name:
        raise DisasterRecoverySafetyError(
            "restore target database must differ from the source database"
        )
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry-run",
        "backup": str(backup_path),
        "backup_id": verification["backup_id"],
        "destination": str(destination),
        "source_database_name": source_database_name,
        "target_database_name": target.database_name,
        "target_system_identifier": (
            expected_target_identity.system_identifier
        ),
        "target_database_oid": expected_target_identity.database_oid,
        "target_drill_token": expected_target_identity.drill_token,
        "required_extensions": required_extensions,
        "database_contract": (
            "existing empty isolated drill-only database; no create or drop; "
            "a failed post-restore publication is compensated with DROP "
            "OWNED only in this exact target"
        ),
        "verified": verification["verified"],
    }
    if not apply:
        return report

    _assert_verified_backup_path_unchanged(verified)
    snapshot_descriptors = _snapshot_verified_payloads(
        verified,
        directory=destination.parent,
        runner=command_runner,
    )
    try:
        return _apply_verified_restore(
            verified=verified,
            snapshot_descriptors=snapshot_descriptors,
            destination=destination,
            target=target,
            expected_target_identity=expected_target_identity,
            report=report,
            command_runner=command_runner,
            now=now,
        )
    finally:
        for descriptor in snapshot_descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass


def _apply_verified_restore(
    *,
    verified: _VerifiedBackup,
    snapshot_descriptors: Mapping[str, int],
    destination: Path,
    target: _DatabaseLocation,
    expected_target_identity: _DatabaseIdentity,
    report: Mapping[str, object],
    command_runner: CommandRunner,
    now: datetime | None,
) -> dict[str, object]:
    manifest = verified.manifest
    manifest_schema_version = int(manifest["schema_version"])
    database_description = _manifest_mapping(manifest, "database")
    expected_member_inventories = (
        _parse_manifest_extension_member_inventories(
            database_description.get("extension_members"),
            extensions=expected_target_identity.extensions,
        )
    )
    target_member_inventories = _read_extension_member_inventories(
        command_runner,
        target,
        extensions=expected_target_identity.extensions,
        label="target",
    )
    if [
        item.manifest_value() for item in target_member_inventories
    ] != expected_member_inventories:
        raise DisasterRecoverySafetyError(
            "target extension member inventory does not match the verified "
            "backup baseline"
        )
    _assert_empty_drill_database(
        command_runner,
        target,
        expected_target_identity,
    )
    _assert_verified_backup_path_unchanged(verified)
    partial = destination.parent / (
        f".{destination.name}.partial-{uuid4().hex}"
    )
    _assert_new_path(partial, label="partial restore destination")
    _mkdir_new_directory(partial, 0o700)
    trees = _manifest_mapping(manifest, "trees")
    for tree_name, archive_name in (
        ("reference_artifacts", "reference_artifacts.tar"),
        ("geoserver_data", "geoserver_data.tar"),
    ):
        _assert_verified_backup_path_unchanged(verified)
        tree = _mapping_member(trees, tree_name)
        entries = _parse_manifest_entries(
            tree.get("entries"),
            manifest_schema_version=manifest_schema_version,
        )
        _safe_extract_tar(
            snapshot_descriptors[archive_name],
            partial / tree_name,
            entries,
            root_mode=_manifest_mode(tree, "root_mode"),
            root_mtime_ns=_manifest_nonnegative_integer(
                tree,
                "root_mtime_ns",
            ),
            root_uid=_manifest_owner_id(tree, "root_uid"),
            root_gid=_manifest_owner_id(tree, "root_gid"),
        )
    dump_description = _mapping_member(database_description, "dump")
    _assert_payload_description_name(
        dump_description,
        expected_name="postgres.dump",
    )
    restored_at = _utc_now(now).isoformat().replace("+00:00", "Z")
    restore_record = {
        **report,
        "mode": "applied",
        "restored_at": restored_at,
        "completion_contract": (
            "the restore is complete only when this report is contained by "
            "the exact final no-replace destination and the returned result "
            "confirms completed=true"
        ),
        "prepared": True,
        "reference_artifacts_path": str(
            destination / "reference_artifacts"
        ),
        "geoserver_data_path": str(destination / "geoserver_data"),
    }
    _write_exclusive(
        partial / "restore-report.json",
        _canonical_json_bytes(restore_record),
    )
    os.chmod(partial, 0o750)
    _fsync_directory(partial)
    _verify_payload_descriptor(
        snapshot_descriptors["postgres.dump"],
        dump_description,
    )
    _assert_verified_backup_path_unchanged(verified)
    target_member_inventories = _read_extension_member_inventories(
        command_runner,
        target,
        extensions=expected_target_identity.extensions,
        label="target",
    )
    if [
        item.manifest_value() for item in target_member_inventories
    ] != expected_member_inventories:
        raise DisasterRecoverySafetyError(
            "target extension member inventory changed before restore"
        )
    _assert_empty_drill_database(
        command_runner,
        target,
        expected_target_identity,
    )

    restore_attempted = False
    try:
        _assert_verified_backup_path_unchanged(verified)
        restore_attempted = True
        _run_pg_restore(
            command_runner,
            target,
            snapshot_descriptors["postgres.dump"],
        )
        _assert_verified_backup_path_unchanged(verified)
        _assert_database_identity(
            command_runner,
            target,
            expected_target_identity,
        )
        restored_member_inventories = _read_extension_member_inventories(
            command_runner,
            target,
            extensions=expected_target_identity.extensions,
            label="restored target",
        )
        if [
            item.manifest_value() for item in restored_member_inventories
        ] != expected_member_inventories:
            raise DisasterRecoverySafetyError(
                "restored extension member inventory changed unexpectedly"
            )
        _assert_verified_backup_path_unchanged(verified)
        durability_verified = _publish_directory_noreplace(
            partial,
            destination,
        )
    except BaseException:
        if restore_attempted:
            try:
                _compensate_restored_drill_database(
                    command_runner,
                    target,
                    expected_target_identity,
                )
            except DisasterRecoveryError as compensation_error:
                raise DisasterRecoveryCommandError(
                    "restore failed and exact-target compensation could not "
                    "be proven"
                ) from compensation_error
        raise

    # Once the atomic rename succeeds, both the populated drill database and
    # the self-describing final directory are present.  A parent-directory
    # fsync error must not turn that completed pair into an unrecoverable
    # "failed" run, so it is retried and reported explicitly.
    return {
        **restore_record,
        "completed": True,
        "durability_verified": durability_verified,
    }


def subprocess_command_runner(
    argv: Sequence[str],
    environment: Mapping[str, str],
) -> CommandResult:
    """Run a fixed argv without a shell and without echoing command output."""

    command_environment = dict(environment)
    raw_passthrough_descriptor = command_environment.pop(
        PASSTHROUGH_FD_ENVIRONMENT_KEY,
        None,
    )
    pass_fds: tuple[int, ...] = ()
    if raw_passthrough_descriptor is not None:
        try:
            passthrough_descriptor = int(raw_passthrough_descriptor)
        except ValueError as error:
            raise DisasterRecoveryCommandError(
                "verified payload descriptor handoff is invalid"
            ) from error
        if (
            passthrough_descriptor < 0
            or f"/proc/self/fd/{passthrough_descriptor}" not in argv
        ):
            raise DisasterRecoveryCommandError(
                "verified payload descriptor handoff is invalid"
            )
        pass_fds = (passthrough_descriptor,)
    try:
        completed = subprocess.run(
            list(argv),
            env=command_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            pass_fds=pass_fds,
        )
    except (OSError, ValueError) as error:
        raise DisasterRecoveryCommandError(
            "required PostgreSQL client command is unavailable"
        ) from error
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan, create, verify or restore a SIUR mirror backup. "
            "Create and restore are dry-run unless --apply is present."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument(
        "--reference-artifacts-source",
        type=Path,
        required=True,
    )
    create.add_argument(
        "--geoserver-data-source",
        type=Path,
        required=True,
    )
    create.add_argument("--database-url-file", type=Path, required=True)
    create.add_argument("--quiescence-evidence", type=Path)
    create.add_argument(
        "--quiescence-max-age-seconds",
        type=int,
        default=3600,
    )
    create.add_argument("--apply", action="store_true")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument(
        "--target-database-url-file",
        type=Path,
        required=True,
    )
    restore.add_argument(
        "--target-database-identity-file",
        type=Path,
        required=True,
    )
    restore.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        result = create_backup(
            CreateBackupRequest(
                destination=args.destination,
                reference_artifacts_source=args.reference_artifacts_source,
                geoserver_data_source=args.geoserver_data_source,
                database_url_file=args.database_url_file,
                quiescence_evidence=args.quiescence_evidence,
                quiescence_max_age_seconds=(
                    args.quiescence_max_age_seconds
                ),
            ),
            apply=args.apply,
        )
    elif args.command == "verify":
        result = verify_backup(args.backup)
    else:
        result = restore_backup(
            RestoreBackupRequest(
                backup=args.backup,
                destination=args.destination,
                target_database_url_file=args.target_database_url_file,
                target_database_identity_file=(
                    args.target_database_identity_file
                ),
            ),
            apply=args.apply,
        )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


def _utc_now(value: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise DisasterRecoverySafetyError("current time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _inventory_summary(inventory: _TreeInventory) -> dict[str, object]:
    return {
        "source": str(inventory.source),
        "files": inventory.file_count,
        "directories": inventory.directory_count,
        "bytes": inventory.total_bytes,
        "excluded_paths": list(inventory.excluded_paths),
        "inventory_sha256": hashlib.sha256(
            _canonical_json_bytes(
                [entry.manifest_value() for entry in inventory.entries]
            )
        ).hexdigest(),
    }


def _build_manifest(
    *,
    created_at: datetime,
    database_name: str,
    database_dump: Path,
    evidence: Mapping[str, object],
    evidence_payload: Path,
    reference_inventory: _TreeInventory,
    reference_archive: Path,
    geoserver_inventory: _TreeInventory,
    geoserver_archive: Path,
    extension_member_inventories: tuple[
        _ExtensionMemberInventory, ...
    ],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "backup_id": str(uuid4()),
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "consistency": {
            "method": "operator-quiescence-plus-before-after-inventory",
            "quiescence_evidence": _payload_description(
                evidence_payload,
            ),
            "operator": evidence.get("operator"),
            "captured_at": evidence.get("captured_at"),
            "reference_artifacts_source": evidence.get(
                "reference_artifacts_source"
            ),
            "geoserver_data_source": evidence.get(
                "geoserver_data_source"
            ),
            "postgres_running": evidence.get("postgres_running"),
            "postgres_database_name": evidence.get(
                "postgres_database_name"
            ),
            "postgres_database_user": evidence.get(
                "postgres_database_user"
            ),
            "postgres_hostname": evidence.get("postgres_hostname"),
            "postgres_port": evidence.get("postgres_port"),
            "postgres_system_identifier": evidence.get(
                "postgres_system_identifier"
            ),
            "postgres_database_oid": evidence.get(
                "postgres_database_oid"
            ),
            "postgres_extensions": evidence.get("postgres_extensions"),
            "stopped_services": sorted(EXPECTED_STOPPED_SERVICES),
        },
        "database": {
            "name": database_name,
            "format": "postgresql-custom",
            "dump": _payload_description(database_dump),
            "pg_dump_contract": [
                "--format=custom",
                "--serializable-deferrable",
                "--no-owner",
                "--no-privileges",
                *[
                    f"--exclude-extension={name}"
                    for name in PG_DUMP_EXTENSION_EXCLUSIONS
                ],
            ],
            "required_extensions": [
                {
                    "name": item["name"],
                    "schema": item["schema"],
                    "version": item["version"],
                }
                for item in evidence.get("postgres_extensions", [])
                if isinstance(item, Mapping)
            ],
            "extension_members": {
                "canonicalization": EXTENSION_MEMBER_CANONICALIZATION,
                "inventories": [
                    item.manifest_value()
                    for item in extension_member_inventories
                ],
            },
        },
        "trees": {
            "reference_artifacts": _tree_manifest(
                reference_inventory,
                reference_archive,
            ),
            "geoserver_data": _tree_manifest(
                geoserver_inventory,
                geoserver_archive,
            ),
        },
        "excluded_reconstructible_state": [
            {
                "component": "redis",
                "included": False,
                "reconstruction": (
                    "restart Redis, then restart scheduler/workers; durable "
                    "mirror and delivery state comes from PostgreSQL"
                ),
            },
            {
                "component": "geowebcache",
                "included": False,
                "excluded_scope": GWC_TILE_EXCLUDED_SCOPE,
                "configuration_included": True,
                "configuration_directory": GWC_CONFIGURATION_DIRECTORY,
                "cache_directory": GWC_TILE_CACHE_DIRECTORY,
                "volume": GWC_TILE_CACHE_VOLUME,
                "blob_store": dict(GWC_TILE_BLOB_STORE),
                "reconstruction": (
                    "restore the complete GeoServer data directory, reapply "
                    "and verify the exact FileBlobStore and declarative disk "
                    "quota, then regenerate requested tiles from restored "
                    "local delivery artifacts"
                ),
            },
        ],
        "restore_contract": {
            "directory_prefix": RESTORE_PREFIX,
            "database_prefix": "app_drill_",
            "overwrite_allowed": False,
            "database_clean_allowed": (
                "compensation-only-drop-owned-exact-drill-target"
            ),
            "database_drop_allowed": False,
            "stable_identity_required": [
                "system_identifier",
                "database_oid",
                "drill_token",
            ],
            "connection_limit": 1,
            "connection_exclusivity": {
                "database_connection_limit": 1,
                "role_connection_limit": 1,
                "role_must_be_non_superuser": True,
                "required_role_membership": "pg_monitor",
                "plpgsql_owner_must_differ": True,
            },
            "extensions": {
                "preinstalled_by_administrator": True,
                "exact_versions_required": True,
                "exact_canonical_members_required": True,
                "owners_must_differ_from_restore_role": True,
                "restore_comments": False,
            },
        },
    }


def _tree_manifest(
    inventory: _TreeInventory,
    archive: Path,
) -> dict[str, object]:
    return {
        "archive": _payload_description(archive),
        "entries": [
            entry.manifest_value() for entry in inventory.entries
        ],
        "excluded_paths": list(inventory.excluded_paths),
        "root_mode": inventory.source_mode,
        "root_mtime_ns": inventory.source_mtime_ns,
        "root_ctime_ns": inventory.source_ctime_ns,
        "root_links": inventory.source_links,
        "root_uid": inventory.source_uid,
        "root_gid": inventory.source_gid,
        "file_count": inventory.file_count,
        "directory_count": inventory.directory_count,
        "total_bytes": inventory.total_bytes,
    }


def _payload_description(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise DisasterRecoverySafetyError("backup payload is not regular")
    return {
        "path": path.name,
        "size_bytes": metadata.st_size,
        "sha256": _hash_file(path, expected=metadata),
    }


def _inventory_tree(
    source: Path,
    *,
    excluded_roots: frozenset[str],
) -> _TreeInventory:
    directory_flags = os.O_RDONLY
    file_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    try:
        source_path_metadata = source.lstat()
        root_descriptor = os.open(source, directory_flags)
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "backup source cannot be inspected"
        ) from error
    try:
        source_before = os.fstat(root_descriptor)
        _assert_same_node(
            source_path_metadata,
            source_before,
            kind="directory",
        )
        _validate_source_metadata(source_before, root=True)
        entries: list[_InventoryEntry] = []

        def visit(
            directory_descriptor: int,
            relative: PurePosixPath,
        ) -> None:
            directory_before = os.fstat(directory_descriptor)
            if not stat.S_ISDIR(directory_before.st_mode):
                raise DisasterRecoverySafetyError(
                    "backup source changed during inventory"
                )
            try:
                with os.scandir(directory_descriptor) as iterator:
                    children = sorted(iterator, key=lambda item: item.name)
            except OSError as error:
                raise DisasterRecoverySafetyError(
                    "backup source cannot be inventoried"
                ) from error
            for child in children:
                child_relative = (
                    PurePosixPath(child.name)
                    if not relative.parts
                    else relative / child.name
                )
                relative_text = child_relative.as_posix()
                _validate_relative_path(relative_text)
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as error:
                    raise DisasterRecoverySafetyError(
                        "backup source changed during inventory"
                    ) from error
                if not relative.parts and child.name in excluded_roots:
                    if stat.S_ISLNK(metadata.st_mode):
                        raise DisasterRecoverySafetyError(
                            "excluded backup roots cannot be symlinks"
                        )
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise DisasterRecoverySafetyError(
                        "backup sources cannot contain symlinks"
                    )
                permissions = stat.S_IMODE(metadata.st_mode)
                _validate_source_metadata(metadata, root=False)
                if stat.S_ISDIR(metadata.st_mode):
                    try:
                        child_descriptor = os.open(
                            child.name,
                            directory_flags,
                            dir_fd=directory_descriptor,
                        )
                    except OSError as error:
                        raise DisasterRecoverySafetyError(
                            "backup source changed during inventory"
                        ) from error
                    try:
                        opened = os.fstat(child_descriptor)
                        _assert_same_node(
                            metadata,
                            opened,
                            kind="directory",
                        )
                        entries.append(
                            _InventoryEntry(
                                path=relative_text,
                                kind="directory",
                                size_bytes=0,
                                sha256=None,
                                mode=permissions,
                                mtime_ns=opened.st_mtime_ns,
                                ctime_ns=opened.st_ctime_ns,
                                links=opened.st_nlink,
                                uid=opened.st_uid,
                                gid=opened.st_gid,
                                device=opened.st_dev,
                                inode=opened.st_ino,
                            )
                        )
                        visit(child_descriptor, child_relative)
                    finally:
                        os.close(child_descriptor)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise DisasterRecoverySafetyError(
                        "backup sources can contain only directories and "
                        "regular files"
                    )
                if metadata.st_nlink != 1:
                    raise DisasterRecoverySafetyError(
                        "backup sources cannot contain hard-linked files"
                    )
                try:
                    child_descriptor = os.open(
                        child.name,
                        file_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise DisasterRecoverySafetyError(
                        "backup source changed during inventory"
                    ) from error
                try:
                    opened = os.fstat(child_descriptor)
                    _assert_same_node(metadata, opened, kind="file")
                    digest = _hash_descriptor(
                        child_descriptor,
                        expected=opened,
                    )
                    entries.append(
                        _InventoryEntry(
                            path=relative_text,
                            kind="file",
                            size_bytes=opened.st_size,
                            sha256=digest,
                            mode=permissions,
                            mtime_ns=opened.st_mtime_ns,
                            ctime_ns=opened.st_ctime_ns,
                            links=opened.st_nlink,
                            uid=opened.st_uid,
                            gid=opened.st_gid,
                            device=opened.st_dev,
                            inode=opened.st_ino,
                        )
                    )
                finally:
                    os.close(child_descriptor)
            directory_after = os.fstat(directory_descriptor)
            _assert_same_node(
                directory_before,
                directory_after,
                kind="directory",
            )

        visit(root_descriptor, PurePosixPath())
        source_after = os.fstat(root_descriptor)
        _assert_same_node(source_before, source_after, kind="directory")
        return _TreeInventory(
            source=source,
            entries=tuple(entries),
            excluded_paths=tuple(sorted(excluded_roots)),
            source_device=source_before.st_dev,
            source_inode=source_before.st_ino,
            source_mode=stat.S_IMODE(source_before.st_mode),
            source_mtime_ns=source_before.st_mtime_ns,
            source_ctime_ns=source_before.st_ctime_ns,
            source_links=source_before.st_nlink,
            source_uid=source_before.st_uid,
            source_gid=source_before.st_gid,
        )
    finally:
        os.close(root_descriptor)


def _assert_empty_geowebcache_placeholder(path: Path) -> None:
    """Require the parent-volume view of the external tile mount to be empty."""

    parent = _existing_directory(
        path.parent,
        label="GeoServer data source",
    )
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        parent_descriptor = os.open(parent, directory_flags)
        try:
            descriptor = os.open(
                path.name,
                directory_flags,
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            raise DisasterRecoverySafetyError(
                "GeoWebCache tile mountpoint must exist in the GeoServer "
                "parent volume"
            ) from error
        try:
            parent_metadata = os.fstat(parent_descriptor)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(before.st_mode)
                or before.st_dev != parent_metadata.st_dev
            ):
                raise DisasterRecoverySafetyError(
                    "GeoWebCache tile data must not be mounted or copied into "
                    "the recovery source"
                )
            with os.scandir(descriptor) as iterator:
                if next(iterator, None) is not None:
                    raise DisasterRecoverySafetyError(
                        "GeoWebCache tile mountpoint in the parent volume is "
                        "not empty; legacy configuration or cache data may be "
                        "present"
                    )
            after = os.fstat(descriptor)
            _assert_same_node(before, after, kind="directory")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "GeoWebCache tile mountpoint cannot be inspected safely"
        ) from error
    finally:
        if "parent_descriptor" in locals():
            os.close(parent_descriptor)


def _validate_source_metadata(
    metadata: os.stat_result,
    *,
    root: bool,
) -> None:
    _validate_source_ownership(metadata)
    permissions = stat.S_IMODE(metadata.st_mode)
    if permissions & ~0o777:
        subject = "backup source root" if root else "backup source"
        raise DisasterRecoverySafetyError(
            f"{subject} cannot contain setuid, setgid or sticky bits"
        )


def _assert_same_node(
    expected: os.stat_result,
    actual: os.stat_result,
    *,
    kind: Literal["directory", "file"],
) -> None:
    expected_kind = (
        stat.S_ISDIR(expected.st_mode)
        if kind == "directory"
        else stat.S_ISREG(expected.st_mode)
    )
    actual_kind = (
        stat.S_ISDIR(actual.st_mode)
        if kind == "directory"
        else stat.S_ISREG(actual.st_mode)
    )
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_uid",
        "st_gid",
        "st_nlink",
    )
    if (
        not expected_kind
        or not actual_kind
        or any(
            getattr(expected, field) != getattr(actual, field)
            for field in fields
        )
    ):
        raise DisasterRecoverySafetyError(
            "backup source changed while it was being traversed"
        )


def _hash_descriptor(
    descriptor: int,
    *,
    expected: os.stat_result,
) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    before = os.fstat(descriptor)
    _assert_same_node(expected, before, kind="file")
    while True:
        chunk = os.read(descriptor, COPY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(descriptor)
    _assert_same_node(before, after, kind="file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _valid_ownership_id(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= MAX_OWNERSHIP_ID
    )


def _validate_source_ownership(metadata: os.stat_result) -> None:
    if (
        not _valid_ownership_id(metadata.st_uid)
        or not _valid_ownership_id(metadata.st_gid)
    ):
        raise DisasterRecoverySafetyError(
            "backup source ownership is outside the supported range"
        )


def _hash_file(path: Path, *, expected: os.stat_result) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "regular file cannot be opened safely"
        ) from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        _assert_same_file(expected, before)
        while True:
            chunk = os.read(descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        _assert_same_file(before, after)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _assert_same_file(
    expected: os.stat_result,
    actual: os.stat_result,
) -> None:
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_uid",
        "st_gid",
    )
    if (
        not stat.S_ISREG(actual.st_mode)
        or any(getattr(expected, field) != getattr(actual, field) for field in fields)
    ):
        raise DisasterRecoverySafetyError(
            "regular file changed while it was being read"
        )


def _write_inventory_tar(
    inventory: _TreeInventory,
    destination: Path,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    output = os.fdopen(descriptor, "wb", closefd=True)
    root_descriptor = _open_inventory_root(inventory)
    try:
        with tarfile.open(
            fileobj=output,
            mode="w",
            format=tarfile.PAX_FORMAT,
        ) as archive:
            for entry in inventory.entries:
                info = tarfile.TarInfo(entry.path)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = entry.mode & 0o777
                info.mtime = entry.mtime_ns // 1_000_000_000
                if entry.kind == "directory":
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                    entry_descriptor = _open_inventory_entry(
                        root_descriptor,
                        entry,
                    )
                    try:
                        _assert_entry_identity(
                            entry,
                            os.fstat(entry_descriptor),
                        )
                        archive.addfile(info)
                    finally:
                        os.close(entry_descriptor)
                    continue
                info.type = tarfile.REGTYPE
                info.size = entry.size_bytes
                file_descriptor = _open_inventory_entry(
                    root_descriptor,
                    entry,
                )
                with os.fdopen(file_descriptor, "rb", closefd=True) as source:
                    _assert_entry_identity(entry, os.fstat(source.fileno()))
                    archive.addfile(info, source)
                    _assert_entry_identity(entry, os.fstat(source.fileno()))
        output.flush()
        os.fsync(output.fileno())
    finally:
        os.close(root_descriptor)
        output.close()


def _open_inventory_root(inventory: _TreeInventory) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(inventory.source, flags)
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "backup source root cannot be reopened safely"
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != inventory.source_device
        or metadata.st_ino != inventory.source_inode
        or stat.S_IMODE(metadata.st_mode) != inventory.source_mode
        or metadata.st_mtime_ns != inventory.source_mtime_ns
        or metadata.st_ctime_ns != inventory.source_ctime_ns
        or metadata.st_nlink != inventory.source_links
        or metadata.st_uid != inventory.source_uid
        or metadata.st_gid != inventory.source_gid
    ):
        os.close(descriptor)
        raise DisasterRecoverySafetyError(
            "backup source root changed before archive creation"
        )
    return descriptor


def _open_inventory_entry(
    root_descriptor: int,
    entry: _InventoryEntry,
) -> int:
    directory_flags = os.O_RDONLY
    file_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    current = os.dup(root_descriptor)
    parts = PurePosixPath(entry.path).parts
    try:
        for part in parts[:-1]:
            next_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=current,
            )
            os.close(current)
            current = next_descriptor
        flags = directory_flags if entry.kind == "directory" else file_flags
        result = os.open(parts[-1], flags, dir_fd=current)
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "backup entry cannot be reopened beneath its source root"
        ) from error
    finally:
        os.close(current)
    return result


def _assert_entry_identity(
    entry: _InventoryEntry,
    metadata: os.stat_result,
) -> None:
    correct_kind = (
        stat.S_ISDIR(metadata.st_mode)
        if entry.kind == "directory"
        else stat.S_ISREG(metadata.st_mode)
    )
    if (
        not correct_kind
        or metadata.st_dev != entry.device
        or metadata.st_ino != entry.inode
        or (
            entry.kind == "file"
            and metadata.st_size != entry.size_bytes
        )
        or metadata.st_mtime_ns != entry.mtime_ns
        or metadata.st_ctime_ns != entry.ctime_ns
        or metadata.st_nlink != entry.links
        or stat.S_IMODE(metadata.st_mode) != entry.mode
        or metadata.st_uid != entry.uid
        or metadata.st_gid != entry.gid
    ):
        raise DisasterRecoverySafetyError(
            "backup source changed while archive was being written"
        )


def _run_pg_dump(
    runner: CommandRunner,
    database: _DatabaseLocation,
    output: Path,
) -> None:
    result = runner(
        (
            "pg_dump",
            "--format=custom",
            "--serializable-deferrable",
            "--no-owner",
            "--no-privileges",
            *(
                f"--exclude-extension={name}"
                for name in PG_DUMP_EXTENSION_EXCLUSIONS
            ),
            f"--file={output}",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError("PostgreSQL backup command failed")


def _run_pg_restore_list(
    runner: CommandRunner,
    dump: Path | int,
) -> None:
    dump_argument, environment = _payload_command_input(dump)
    result = runner(
        ("pg_restore", "--list", dump_argument),
        environment,
    )
    if result.returncode != 0:
        raise DisasterRecoveryVerificationError(
            "PostgreSQL dump cannot be listed by pg_restore"
        )


def _assert_empty_drill_database(
    runner: CommandRunner,
    database: _DatabaseLocation,
    expected_identity: _DatabaseIdentity,
) -> None:
    # This is intentionally broader than counting tables.  A drill target is
    # accepted only when it is owned by the connecting role, has no concurrent
    # sessions, contains the standard public schema contract, and has no
    # user schemas or objects outside the exact admin-owned extension baseline.
    _assert_database_identity(runner, database, expected_identity)
    marker = _drill_marker(expected_identity)
    query = (
        "WITH RECURSIVE extension_objects(classid, objid) AS ("
        "SELECT d.classid, d.objid FROM pg_depend d "
        "WHERE d.refclassid = 'pg_extension'::regclass "
        "AND d.deptype = 'e' "
        "UNION "
        "SELECT d.classid, d.objid FROM pg_depend d "
        "JOIN extension_objects parent "
        "ON d.refclassid = parent.classid "
        "AND d.refobjid = parent.objid "
        "WHERE d.deptype IN ('a', 'i')) "
        "SELECT current_database(), current_user, "
        "(SELECT system_identifier::text FROM pg_control_system()), "
        "(SELECT oid::text FROM pg_database "
        "WHERE datname = current_database()), "
        "COALESCE((SELECT shobj_description(oid, 'pg_database') "
        "FROM pg_database WHERE datname = current_database()), ''), "
        "(SELECT CASE WHEN pg_get_userbyid(datdba) = current_user "
        "THEN 1 ELSE 0 END FROM pg_database "
        "WHERE datname = current_database()), "
        "(SELECT CASE WHEN datconnlimit = 1 THEN 1 ELSE 0 END "
        "FROM pg_database WHERE datname = current_database()), "
        "(SELECT CASE WHEN datallowconn THEN 1 ELSE 0 END "
        "FROM pg_database WHERE datname = current_database()), "
        "(SELECT CASE WHEN has_database_privilege("
        "current_user, current_database(), 'CONNECT,CREATE,TEMPORARY') "
        "THEN 1 ELSE 0 END), "
        "(SELECT CASE WHEN rolcanlogin AND NOT rolsuper "
        "AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication "
        "AND NOT rolbypassrls AND rolconnlimit = 1 "
        "AND pg_has_role(current_user, 'pg_monitor', 'MEMBER') "
        "AND NOT EXISTS (SELECT 1 FROM pg_auth_members memberships "
        "JOIN pg_roles granted_role "
        "ON granted_role.oid = memberships.roleid "
        "WHERE memberships.member = pg_roles.oid "
        "AND granted_role.rolname <> 'pg_monitor') "
        "AND NOT EXISTS (SELECT 1 FROM pg_database other_database "
        "WHERE other_database.datdba = pg_roles.oid "
        "AND other_database.datname <> current_database()) "
        "AND NOT EXISTS (SELECT 1 FROM pg_tablespace tablespace "
        "WHERE tablespace.spcowner = pg_roles.oid) "
        "THEN 1 ELSE 0 END FROM pg_roles "
        "WHERE rolname = current_user), "
        "(SELECT CASE WHEN count(*) = 3 AND count(*) FILTER "
        "(WHERE pg_get_userbyid(extowner) <> current_user) = 3 "
        "THEN 1 ELSE 0 END FROM pg_extension), "
        "(SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()), "
        "(SELECT count(*) FROM pg_namespace WHERE nspname <> 'public' "
        "AND nspname NOT IN ('pg_catalog','information_schema') "
        "AND nspname !~ '^pg_(toast|temp|toast_temp)(_|$)'), "
        "(SELECT count(*) FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname NOT IN "
        "('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)' "
        "AND NOT EXISTS (SELECT 1 FROM extension_objects d "
        "WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid "
        ")), "
        "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname NOT IN "
        "('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)' "
        "AND NOT EXISTS (SELECT 1 FROM extension_objects d "
        "WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid "
        ")), "
        "(SELECT count(*) FROM pg_type t JOIN pg_namespace n "
        "ON n.oid = t.typnamespace WHERE n.nspname NOT IN "
        "('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)' "
        "AND NOT EXISTS (SELECT 1 FROM extension_objects d "
        "WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid "
        ")), "
        "(SELECT count(*) FROM pg_extension "
        "WHERE pg_get_userbyid(extowner) = current_user), "
        "("
        "(SELECT CASE WHEN count(*) = 1 AND count(*) FILTER "
        "(WHERE pg_get_userbyid(nspowner) = 'pg_database_owner') = 1 "
        "THEN 0 ELSE 1 END FROM pg_namespace WHERE nspname = 'public') + "
        "(SELECT count(*) FROM pg_language WHERE lanname NOT IN "
        "('internal','c','sql','plpgsql')) + "
        "(SELECT count(*) FROM pg_foreign_data_wrapper o WHERE NOT EXISTS "
        "(SELECT 1 FROM extension_objects d WHERE "
        "d.classid = 'pg_foreign_data_wrapper'::regclass "
        "AND d.objid = o.oid)) + "
        "(SELECT count(*) FROM pg_foreign_server o WHERE NOT EXISTS "
        "(SELECT 1 FROM extension_objects d WHERE "
        "d.classid = 'pg_foreign_server'::regclass "
        "AND d.objid = o.oid)) + "
        "(SELECT count(*) FROM pg_event_trigger o WHERE NOT EXISTS "
        "(SELECT 1 FROM extension_objects d WHERE "
        "d.classid = 'pg_event_trigger'::regclass "
        "AND d.objid = o.oid)) + "
        "(SELECT count(*) FROM pg_publication) + "
        "(SELECT count(*) FROM pg_subscription) + "
        "(SELECT count(*) FROM pg_largeobject_metadata) + "
        "(SELECT count(*) FROM pg_default_acl) + "
        "(SELECT count(*) FROM pg_cast o WHERE oid >= 16384 "
        "AND NOT EXISTS (SELECT 1 FROM extension_objects d WHERE "
        "d.classid = 'pg_cast'::regclass AND d.objid = o.oid "
        ")) + "
        "(SELECT count(*) FROM pg_transform o WHERE oid >= 16384 "
        "AND NOT EXISTS (SELECT 1 FROM extension_objects d WHERE "
        "d.classid = 'pg_transform'::regclass AND d.objid = o.oid "
        ")) + "
        "(SELECT count(*) FROM pg_am o WHERE oid >= 16384 "
        "AND NOT EXISTS (SELECT 1 FROM extension_objects d WHERE "
        "d.classid = 'pg_am'::regclass AND d.objid = o.oid "
        ")) + "
        "(SELECT count(*) FROM ("
        "SELECT 'pg_collation'::regclass AS classid, oid, "
        "collnamespace AS namespace FROM pg_collation "
        "UNION ALL SELECT 'pg_conversion'::regclass, oid, connamespace "
        "FROM pg_conversion "
        "UNION ALL SELECT 'pg_operator'::regclass, oid, oprnamespace "
        "FROM pg_operator "
        "UNION ALL SELECT 'pg_opclass'::regclass, oid, opcnamespace "
        "FROM pg_opclass "
        "UNION ALL SELECT 'pg_opfamily'::regclass, oid, opfnamespace "
        "FROM pg_opfamily "
        "UNION ALL SELECT 'pg_statistic_ext'::regclass, oid, stxnamespace "
        "FROM pg_statistic_ext "
        "UNION ALL SELECT 'pg_ts_config'::regclass, oid, cfgnamespace "
        "FROM pg_ts_config "
        "UNION ALL SELECT 'pg_ts_dict'::regclass, oid, dictnamespace "
        "FROM pg_ts_dict "
        "UNION ALL SELECT 'pg_ts_parser'::regclass, oid, prsnamespace "
        "FROM pg_ts_parser "
        "UNION ALL SELECT 'pg_ts_template'::regclass, oid, tmplnamespace "
        "FROM pg_ts_template"
        ") scoped JOIN pg_namespace n ON n.oid = scoped.namespace "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)' "
        "AND NOT EXISTS (SELECT 1 FROM extension_objects d "
        "WHERE d.classid = scoped.classid AND d.objid = scoped.oid "
        "))"
        "), "
        "(SELECT count(*) FROM pg_database d CROSS JOIN LATERAL "
        "aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) acl "
        "WHERE d.datname = current_database() "
        "AND acl.grantee <> d.datdba), "
        "(SELECT count(*) FROM pg_namespace n CROSS JOIN LATERAL "
        "aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) acl "
        "WHERE n.nspname = 'public' AND NOT (NOT acl.is_grantable AND "
        "((acl.grantee = 0 AND acl.privilege_type = 'USAGE') OR "
        "(acl.grantee = n.nspowner AND acl.privilege_type IN "
        "('USAGE','CREATE'))))), "
        "(SELECT count(*) FROM pg_db_role_setting s "
        "WHERE s.setdatabase IN (0, "
        "(SELECT oid FROM pg_database WHERE datname = current_database())) "
        "AND s.setrole IN (0, "
        "(SELECT oid FROM pg_roles WHERE rolname = current_user))), "
        "(SELECT count(*) FROM pg_shseclabel "
        "WHERE classoid = 'pg_database'::regclass "
        "AND objoid = (SELECT oid FROM pg_database "
        "WHERE datname = current_database())), "
        "(SELECT count(*) FROM pg_seclabel)"
    )
    result = runner(
        (
            "psql",
            "-X",
            "--no-align",
            "--tuples-only",
            "--field-separator=|",
            "--set=ON_ERROR_STOP=1",
            f"--command={query}",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            "drill database preflight command failed"
        )
    lines = _command_output_lines(result)
    expected = (
        f"{database.database_name}|{database.username}|"
        f"{expected_identity.system_identifier}|"
        f"{expected_identity.database_oid}|{marker}|1|1|1|1|"
        "1|1|"
        + "|".join("0" for _ in range(DRILL_BASELINE_ZERO_COUNT))
    )
    if lines != [expected]:
        raise DisasterRecoverySafetyError(
            "restore target is not the expected empty drill database"
        )


def _compensate_restored_drill_database(
    runner: CommandRunner,
    database: _DatabaseLocation,
    expected_identity: _DatabaseIdentity,
) -> None:
    """Return only the exact isolated drill target to its empty preflight state."""

    if (
        not database.database_name.startswith("app_drill_")
        or database.hostname != "127.0.0.1"
        or database.port == 5432
    ):
        raise DisasterRecoverySafetyError(
            "database compensation target is not an isolated drill database"
        )
    database_identifier = _quote_sql_identifier(database.database_name)
    user_identifier = _quote_sql_identifier(database.username)
    marker = _drill_marker(expected_identity)
    extension_guard = (
        f"OR (SELECT count(*) FROM pg_extension) <> "
        f"{len(expected_identity.extensions)} "
    )
    for extension in expected_identity.extensions:
        extension_guard += (
            "OR NOT EXISTS (SELECT 1 FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = "
            f"{_quote_sql_literal(extension.name)} "
            f"AND n.nspname = {_quote_sql_literal(extension.schema)} "
            f"AND e.extversion = {_quote_sql_literal(extension.version)} "
            "AND pg_get_userbyid(e.extowner) = "
            f"{_quote_sql_literal(extension.owner)} "
            "AND EXISTS (SELECT 1 FROM pg_available_extension_versions a "
            "WHERE a.name = e.extname AND a.version = e.extversion)) "
        )
    identity_guard = (
        "DO $siur$ DECLARE actual_system text; actual_oid oid; "
        "actual_marker text; BEGIN "
        "SELECT system_identifier::text INTO actual_system "
        "FROM pg_control_system(); "
        "SELECT oid, COALESCE(shobj_description(oid, 'pg_database'), '') "
        "INTO actual_oid, actual_marker FROM pg_database "
        "WHERE datname = current_database(); "
        f"IF current_database() <> {_quote_sql_literal(database.database_name)} "
        f"OR current_user <> {_quote_sql_literal(database.username)} "
        f"OR actual_system <> "
        f"{_quote_sql_literal(expected_identity.system_identifier)} "
        f"OR actual_oid <> {expected_identity.database_oid} "
        f"OR actual_marker <> {_quote_sql_literal(marker)} "
        f"{extension_guard}"
        "THEN RAISE EXCEPTION 'SIUR drill identity mismatch'; END IF; "
        "END $siur$"
    )
    result = runner(
        (
            "psql",
            "-X",
            "--set=ON_ERROR_STOP=1",
            "--single-transaction",
            "--command="
            f"{identity_guard}; "
            "DROP OWNED BY CURRENT_USER CASCADE; "
            "CREATE SCHEMA IF NOT EXISTS public "
            "AUTHORIZATION pg_database_owner; "
            "REVOKE ALL ON SCHEMA public FROM PUBLIC; "
            "GRANT USAGE ON SCHEMA public TO PUBLIC; "
            "GRANT CREATE, USAGE ON SCHEMA public TO pg_database_owner; "
            f"ALTER DATABASE {database_identifier} RESET ALL; "
            f"ALTER ROLE {user_identifier} IN DATABASE "
            f"{database_identifier} RESET ALL; "
            f"ALTER DATABASE {database_identifier} CONNECTION LIMIT 1; "
            f"REVOKE ALL ON DATABASE {database_identifier} FROM PUBLIC; "
            f"COMMENT ON DATABASE {database_identifier} IS "
            f"{_quote_sql_literal(marker)}",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            "post-restore database compensation failed"
        )
    try:
        _assert_empty_drill_database(
            runner,
            database,
            expected_identity,
        )
    except DisasterRecoveryError as error:
        raise DisasterRecoveryCommandError(
            "post-restore database compensation could not prove an empty target"
        ) from error


def _run_pg_restore(
    runner: CommandRunner,
    database: _DatabaseLocation,
    dump: Path | int,
) -> None:
    dump_argument, descriptor_environment = _payload_command_input(dump)
    environment = _postgres_environment(database)
    environment.update(descriptor_environment)
    result = runner(
        (
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            "--no-comments",
            f"--dbname={database.database_name}",
            dump_argument,
        ),
        environment,
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            "PostgreSQL drill restore command failed"
        )


def _payload_command_input(
    payload: Path | int,
) -> tuple[str, dict[str, str]]:
    environment = _client_environment()
    if isinstance(payload, int):
        metadata = os.fstat(payload)
        if not stat.S_ISREG(metadata.st_mode):
            raise DisasterRecoveryVerificationError(
                "verified PostgreSQL payload descriptor is invalid"
            )
        environment[PASSTHROUGH_FD_ENVIRONMENT_KEY] = str(payload)
        return f"/proc/self/fd/{payload}", environment
    return str(payload), environment


def _client_environment() -> dict[str, str]:
    return {
        "PATH": (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }


def _postgres_environment(
    database: _DatabaseLocation,
) -> dict[str, str]:
    result = _client_environment()
    result["PGDATABASE"] = database.database_name
    result["PGHOST"] = database.hostname
    result["PGPORT"] = str(database.port)
    result["PGUSER"] = database.username
    if database.password is not None:
        result["PGPASSWORD"] = database.password
    result["PGCONNECT_TIMEOUT"] = "10"
    return result


def _read_drill_identity(
    path: Path,
    *,
    database: _DatabaseLocation,
) -> _DatabaseIdentity:
    raw = _read_regular_file(
        path,
        limit=64 * 1024,
        label="target database identity file",
        private=True,
    )
    value = _decode_json_object(raw, label="target database identity file")
    expected_keys = {
        "schema_version",
        "database_name",
        "database_user",
        "system_identifier",
        "database_oid",
        "drill_token",
        "extensions",
    }
    database_name = value.get("database_name")
    database_user = value.get("database_user")
    system_identifier = value.get("system_identifier")
    database_oid = value.get("database_oid")
    drill_token = value.get("drill_token")
    extensions = _parse_database_extensions(
        value.get("extensions"),
        label="target database identity extensions",
        error_type=DisasterRecoverySafetyError,
    )
    if (
        set(value) != expected_keys
        or value.get("schema_version") != DRILL_IDENTITY_SCHEMA_VERSION
        or database_name != database.database_name
        or database_user != database.username
        or not isinstance(system_identifier, str)
        or SYSTEM_IDENTIFIER_RE.fullmatch(system_identifier) is None
        or isinstance(database_oid, bool)
        or not isinstance(database_oid, int)
        or not 1 <= database_oid <= 2**32 - 1
        or not isinstance(drill_token, str)
        or DRILL_TOKEN_RE.fullmatch(drill_token) is None
        or any(item.owner == database.username for item in extensions)
    ):
        raise DisasterRecoverySafetyError(
            "target database identity file is invalid or does not match "
            "the credential target"
        )
    return _DatabaseIdentity(
        database_name=database_name,
        database_user=database_user,
        system_identifier=system_identifier,
        database_oid=database_oid,
        extensions=extensions,
        drill_token=drill_token,
    )


def _read_database_identity(
    runner: CommandRunner,
    database: _DatabaseLocation,
    *,
    require_drill_marker: bool,
) -> _DatabaseIdentity:
    marker_expression = (
        "COALESCE((SELECT shobj_description(oid, 'pg_database') "
        "FROM pg_database WHERE datname = current_database()), '')"
        if require_drill_marker
        else "''"
    )
    query = (
        "SELECT current_database(), current_user, "
        "(SELECT system_identifier::text FROM pg_control_system()), "
        "(SELECT oid::text FROM pg_database "
        "WHERE datname = current_database()), "
        f"{marker_expression}"
    )
    result = runner(
        (
            "psql",
            "-X",
            "--no-align",
            "--tuples-only",
            "--field-separator=|",
            "--set=ON_ERROR_STOP=1",
            f"--command={query}",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            "database identity command failed"
        )
    lines = _command_output_lines(result)
    if len(lines) != 1:
        raise DisasterRecoverySafetyError(
            "database identity output is invalid"
        )
    parts = lines[0].split("|")
    if len(parts) != 5:
        raise DisasterRecoverySafetyError(
            "database identity output is invalid"
        )
    database_name, database_user, system_identifier, oid_raw, marker = parts
    try:
        database_oid = int(oid_raw)
    except ValueError as error:
        raise DisasterRecoverySafetyError(
            "database identity output is invalid"
        ) from error
    drill_token: str | None = None
    if require_drill_marker:
        if not marker.startswith(DRILL_MARKER_PREFIX):
            raise DisasterRecoverySafetyError(
                "drill database marker is missing"
            )
        drill_token = marker.removeprefix(DRILL_MARKER_PREFIX)
    if (
        database_name != database.database_name
        or database_user != database.username
        or SYSTEM_IDENTIFIER_RE.fullmatch(system_identifier) is None
        or not 1 <= database_oid <= 2**32 - 1
        or (
            require_drill_marker
            and (
                drill_token is None
                or DRILL_TOKEN_RE.fullmatch(drill_token) is None
            )
        )
    ):
        raise DisasterRecoverySafetyError(
            "database identity does not match the requested endpoint"
        )
    return _DatabaseIdentity(
        database_name=database_name,
        database_user=database_user,
        system_identifier=system_identifier,
        database_oid=database_oid,
        extensions=_read_database_extensions(runner, database),
        drill_token=drill_token,
    )


def _read_database_extensions(
    runner: CommandRunner,
    database: _DatabaseLocation,
) -> tuple[_DatabaseExtension, ...]:
    query = (
        "SELECT e.extname, n.nspname, e.extversion, "
        "pg_get_userbyid(e.extowner), "
        "CASE WHEN EXISTS (SELECT 1 FROM pg_available_extension_versions a "
        "WHERE a.name = e.extname AND a.version = e.extversion) "
        "THEN 1 ELSE 0 END "
        "FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
        "ORDER BY e.extname"
    )
    result = runner(
        (
            "psql",
            "-X",
            "--no-align",
            "--tuples-only",
            "--field-separator=|",
            "--set=ON_ERROR_STOP=1",
            f"--command={query}",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            "database extension identity command failed"
        )
    values: list[dict[str, str]] = []
    for line in _command_output_lines(result):
        parts = line.split("|")
        if len(parts) != 5 or parts[4] != "1":
            raise DisasterRecoverySafetyError(
                "an installed database extension version is unavailable"
            )
        values.append(
            {
                "name": parts[0],
                "schema": parts[1],
                "version": parts[2],
                "owner": parts[3],
            }
        )
    return _parse_database_extensions(
        values,
        label="live database extensions",
        error_type=DisasterRecoverySafetyError,
    )


def _read_extension_member_inventories(
    runner: CommandRunner,
    database: _DatabaseLocation,
    *,
    extensions: tuple[_DatabaseExtension, ...],
    label: str,
) -> tuple[_ExtensionMemberInventory, ...]:
    # The descriptor deliberately contains no OID. pg_identify_object yields
    # stable, schema-qualified identities, while the catalog and sub-object ID
    # keep otherwise similar object classes distinct. Any ALTER EXTENSION ADD
    # changes both the member count and this ordered SHA-256.
    query = (
        "/* siur-extension-members-v1 */ "
        "WITH members AS ("
        "SELECT e.extname, e.extversion, "
        "jsonb_build_object("
        "'catalog', d.classid::regclass::text, "
        "'object_subid', d.objsubid, "
        "'type', identified.type, "
        "'schema', COALESCE(identified.schema, ''), "
        "'name', COALESCE(identified.name, ''), "
        "'identity', identified.identity)::text AS descriptor "
        "FROM pg_extension e "
        "JOIN pg_depend d ON d.refclassid = 'pg_extension'::regclass "
        "AND d.refobjid = e.oid AND d.deptype = 'e' "
        "CROSS JOIN LATERAL pg_identify_object("
        "d.classid, d.objid, d.objsubid) AS identified "
        "WHERE e.extname IN ('plpgsql','postgis','vector')) "
        "SELECT extname, extversion, count(*)::text, "
        "encode(sha256(convert_to(string_agg("
        "descriptor, E'\\n' ORDER BY descriptor), 'UTF8')), 'hex') "
        "FROM members GROUP BY extname, extversion ORDER BY extname"
    )
    result = runner(
        (
            "psql",
            "-X",
            "--no-align",
            "--tuples-only",
            "--field-separator=|",
            "--set=ON_ERROR_STOP=1",
            f"--command={query}",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            f"{label} extension member inventory command failed"
        )
    inventories = _parse_extension_member_inventories(
        [
            {
                "name": parts[0],
                "version": parts[1],
                "member_count": parts[2],
                "inventory_sha256": parts[3],
            }
            for line in _command_output_lines(result)
            if len(parts := line.split("|")) == 4
        ],
        extensions=extensions,
        error_type=DisasterRecoverySafetyError,
        label=f"{label} extension member inventory",
    )
    if len(_command_output_lines(result)) != len(inventories):
        raise DisasterRecoverySafetyError(
            f"{label} extension member inventory output is invalid"
        )
    _assert_canonical_extension_member_inventories(
        inventories,
        error_type=DisasterRecoverySafetyError,
        label=label,
    )
    return inventories


def _parse_extension_member_inventories(
    value: object,
    *,
    extensions: Sequence[_DatabaseExtension] | None,
    error_type: type[DisasterRecoveryError],
    label: str,
) -> tuple[_ExtensionMemberInventory, ...]:
    if not isinstance(value, list):
        raise error_type(f"{label} is invalid")
    result: list[_ExtensionMemberInventory] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "version",
            "member_count",
            "inventory_sha256",
        }:
            raise error_type(f"{label} is invalid")
        name = item.get("name")
        version = item.get("version")
        raw_member_count = item.get("member_count")
        inventory_sha256 = item.get("inventory_sha256")
        if (
            isinstance(raw_member_count, int)
            and not isinstance(raw_member_count, bool)
        ):
            member_count = raw_member_count
        elif isinstance(raw_member_count, str) and re.fullmatch(
            r"[1-9][0-9]*",
            raw_member_count,
        ):
            member_count = int(raw_member_count)
        else:
            member_count = -1
        if (
            not isinstance(name, str)
            or name not in REQUIRED_EXTENSION_SCHEMAS
            or not isinstance(version, str)
            or EXTENSION_VERSION_RE.fullmatch(version) is None
            or member_count < 1
            or member_count > 1_000_000
            or not isinstance(inventory_sha256, str)
            or SHA256_RE.fullmatch(inventory_sha256) is None
        ):
            raise error_type(f"{label} is invalid")
        result.append(
            _ExtensionMemberInventory(
                name=name,
                version=version,
                member_count=member_count,
                inventory_sha256=inventory_sha256,
            )
        )
    expected_names = sorted(REQUIRED_EXTENSION_SCHEMAS)
    if [item.name for item in result] != expected_names:
        raise error_type(f"{label} must contain the exact reviewed baseline")
    if extensions is not None and [
        (item.name, item.version) for item in result
    ] != [(item.name, item.version) for item in extensions]:
        raise error_type(f"{label} does not match the extension identity")
    return tuple(result)


def _assert_canonical_extension_member_inventories(
    inventories: Sequence[_ExtensionMemberInventory],
    *,
    error_type: type[DisasterRecoveryError],
    label: str,
) -> None:
    for inventory in inventories:
        expected = CANONICAL_EXTENSION_MEMBER_INVENTORIES.get(
            (inventory.name, inventory.version)
        )
        if expected is None or expected != (
            inventory.member_count,
            inventory.inventory_sha256,
        ):
            raise error_type(
                f"{label} extension member inventory is not canonical; "
                "an unexpected or missing extension member was detected"
            )


def _parse_manifest_extension_member_inventories(
    value: object,
    *,
    extensions: Sequence[_DatabaseExtension],
) -> list[dict[str, object]]:
    if not isinstance(value, Mapping) or set(value) != {
        "canonicalization",
        "inventories",
    }:
        raise DisasterRecoveryVerificationError(
            "backup extension member inventory is invalid"
        )
    if value.get("canonicalization") != EXTENSION_MEMBER_CANONICALIZATION:
        raise DisasterRecoveryVerificationError(
            "backup extension member canonicalization is invalid"
        )
    inventories = _parse_extension_member_inventories(
        value.get("inventories"),
        extensions=extensions,
        error_type=DisasterRecoveryVerificationError,
        label="backup extension member inventory",
    )
    _assert_canonical_extension_member_inventories(
        inventories,
        error_type=DisasterRecoveryVerificationError,
        label="backup",
    )
    return [item.manifest_value() for item in inventories]


def _parse_database_extensions(
    value: object,
    *,
    label: str,
    error_type: type[DisasterRecoveryError],
) -> tuple[_DatabaseExtension, ...]:
    if not isinstance(value, list):
        raise error_type(f"{label} are invalid")
    result: list[_DatabaseExtension] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "schema",
            "version",
            "owner",
        }:
            raise error_type(f"{label} are invalid")
        name = item.get("name")
        schema = item.get("schema")
        version = item.get("version")
        owner = item.get("owner")
        if (
            not isinstance(name, str)
            or name not in REQUIRED_EXTENSION_SCHEMAS
            or schema != REQUIRED_EXTENSION_SCHEMAS.get(name)
            or not isinstance(version, str)
            or EXTENSION_VERSION_RE.fullmatch(version) is None
            or not isinstance(owner, str)
            or re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_.-]{0,127}",
                owner,
            )
            is None
        ):
            raise error_type(f"{label} are invalid")
        result.append(
            _DatabaseExtension(
                name=name,
                schema=schema,
                version=version,
                owner=owner,
            )
        )
    if (
        [item.name for item in result] != sorted(REQUIRED_EXTENSION_SCHEMAS)
        or len(result) != len(REQUIRED_EXTENSION_SCHEMAS)
    ):
        raise error_type(f"{label} must contain the exact reviewed baseline")
    return tuple(result)


def _parse_required_extension_manifest(
    value: object,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise DisasterRecoveryVerificationError(
            "backup required extension baseline is invalid"
        )
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "schema",
            "version",
        }:
            raise DisasterRecoveryVerificationError(
                "backup required extension baseline is invalid"
            )
        name = item.get("name")
        schema = item.get("schema")
        version = item.get("version")
        if (
            not isinstance(name, str)
            or name not in REQUIRED_EXTENSION_SCHEMAS
            or schema != REQUIRED_EXTENSION_SCHEMAS.get(name)
            or not isinstance(version, str)
            or EXTENSION_VERSION_RE.fullmatch(version) is None
        ):
            raise DisasterRecoveryVerificationError(
                "backup required extension baseline is invalid"
            )
        result.append(
            {"name": name, "schema": schema, "version": version}
        )
    if (
        [item["name"] for item in result]
        != sorted(REQUIRED_EXTENSION_SCHEMAS)
        or len(result) != len(REQUIRED_EXTENSION_SCHEMAS)
    ):
        raise DisasterRecoveryVerificationError(
            "backup required extension baseline is incomplete"
        )
    return result


def _assert_database_identity(
    runner: CommandRunner,
    database: _DatabaseLocation,
    expected: _DatabaseIdentity,
) -> None:
    actual = _read_database_identity(
        runner,
        database,
        require_drill_marker=True,
    )
    if actual != expected:
        raise DisasterRecoverySafetyError(
            "drill database identity changed during restore"
        )


def _assert_evidence_database_identity(
    evidence: Mapping[str, object],
    actual: _DatabaseIdentity,
) -> None:
    if (
        evidence.get("postgres_database_name") != actual.database_name
        or evidence.get("postgres_database_user") != actual.database_user
        or evidence.get("postgres_system_identifier")
        != actual.system_identifier
        or evidence.get("postgres_database_oid") != actual.database_oid
        or evidence.get("postgres_extensions")
        != [
            item.manifest_value(include_owner=True)
            for item in actual.extensions
        ]
    ):
        raise DisasterRecoverySafetyError(
            "quiescence evidence does not match the live PostgreSQL identity"
        )


def _drill_marker(identity: _DatabaseIdentity) -> str:
    token = identity.drill_token
    if token is None or DRILL_TOKEN_RE.fullmatch(token) is None:
        raise DisasterRecoverySafetyError(
            "drill database identity token is invalid"
        )
    return f"{DRILL_MARKER_PREFIX}{token}"


def _command_output_lines(result: CommandResult) -> list[str]:
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def _quote_sql_literal(value: str) -> str:
    if "\x00" in value:
        raise DisasterRecoverySafetyError("SQL identity value is invalid")
    return "'" + value.replace("'", "''") + "'"


def _quote_sql_identifier(value: str) -> str:
    if DATABASE_NAME_RE.fullmatch(value) is None and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.-]{0,127}",
        value,
    ) is None:
        raise DisasterRecoverySafetyError("SQL identity name is invalid")
    return '"' + value.replace('"', '""') + '"'


def _validate_quiescence_evidence(
    path: Path,
    *,
    reference_source: Path,
    geoserver_source: Path,
    database: _DatabaseLocation,
    now: datetime,
    max_age_seconds: int,
) -> tuple[dict[str, object], bytes, str]:
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or not 60 <= max_age_seconds <= 86_400
    ):
        raise DisasterRecoverySafetyError(
            "quiescence maximum age must be between 60 and 86400 seconds"
        )
    raw = _read_regular_file(
        path,
        limit=64 * 1024,
        label="quiescence evidence",
    )
    evidence = _decode_json_object(raw, label="quiescence evidence")
    if (
        evidence.get("schema_version")
        != QUIESCENCE_EVIDENCE_SCHEMA_VERSION
    ):
        raise DisasterRecoverySafetyError(
            "quiescence evidence schema is unsupported"
        )
    if evidence.get("reference_artifacts_source") != str(reference_source):
        raise DisasterRecoverySafetyError(
            "quiescence evidence does not bind the reference source"
        )
    if evidence.get("geoserver_data_source") != str(geoserver_source):
        raise DisasterRecoverySafetyError(
            "quiescence evidence does not bind the GeoServer source"
        )
    if evidence.get("postgres_running") is not True:
        raise DisasterRecoverySafetyError(
            "quiescence evidence must confirm PostgreSQL remained running"
        )
    system_identifier = evidence.get("postgres_system_identifier")
    database_oid = evidence.get("postgres_database_oid")
    _parse_database_extensions(
        evidence.get("postgres_extensions"),
        label="quiescence PostgreSQL extensions",
        error_type=DisasterRecoverySafetyError,
    )
    if (
        evidence.get("postgres_database_name") != database.database_name
        or evidence.get("postgres_database_user") != database.username
        or evidence.get("postgres_hostname") != database.hostname
        or evidence.get("postgres_port") != database.port
        or not isinstance(system_identifier, str)
        or SYSTEM_IDENTIFIER_RE.fullmatch(system_identifier) is None
        or isinstance(database_oid, bool)
        or not isinstance(database_oid, int)
        or not 1 <= database_oid <= 2**32 - 1
    ):
        raise DisasterRecoverySafetyError(
            "quiescence evidence does not bind the PostgreSQL identity"
        )
    stopped = evidence.get("stopped_services")
    if (
        not isinstance(stopped, list)
        or any(not isinstance(item, str) for item in stopped)
        or len(stopped) != len(set(stopped))
        or set(stopped) != EXPECTED_STOPPED_SERVICES
    ):
        raise DisasterRecoverySafetyError(
            "quiescence evidence is missing a stopped writer service"
        )
    operator = evidence.get("operator")
    if not isinstance(operator, str) or OPERATOR_RE.fullmatch(operator) is None:
        raise DisasterRecoverySafetyError(
            "quiescence evidence operator is invalid"
        )
    captured_raw = evidence.get("captured_at")
    if not isinstance(captured_raw, str):
        raise DisasterRecoverySafetyError(
            "quiescence evidence timestamp is invalid"
        )
    try:
        captured = datetime.fromisoformat(captured_raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise DisasterRecoverySafetyError(
            "quiescence evidence timestamp is invalid"
        ) from error
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise DisasterRecoverySafetyError(
            "quiescence evidence timestamp must include a timezone"
        )
    captured = captured.astimezone(timezone.utc)
    if captured > now + timedelta(seconds=60) or (
        now - captured > timedelta(seconds=max_age_seconds)
    ):
        raise DisasterRecoverySafetyError(
            "quiescence evidence is stale or from the future"
        )
    return evidence, raw, hashlib.sha256(raw).hexdigest()


def _verify_stored_quiescence_evidence(
    path: Path,
    *,
    consistency: Mapping[str, Any],
    manifest_schema_version: int,
) -> None:
    """Verify the preserved exact evidence bytes against manifest semantics."""

    raw = _read_regular_file(
        path,
        limit=64 * 1024,
        label="stored quiescence evidence",
    )
    _verify_stored_quiescence_evidence_bytes(
        raw,
        consistency=consistency,
        manifest_schema_version=manifest_schema_version,
    )


def _verify_stored_quiescence_evidence_bytes(
    raw: bytes,
    *,
    consistency: Mapping[str, Any],
    manifest_schema_version: int,
) -> None:
    evidence = _decode_json_object(
        raw,
        label="stored quiescence evidence",
    )
    stopped = evidence.get("stopped_services")
    expected_evidence_schema = (
        QUIESCENCE_EVIDENCE_SCHEMA_VERSION
        if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION
        else LEGACY_QUIESCENCE_EVIDENCE_SCHEMA_VERSION
    )
    if (
        evidence.get("schema_version") != expected_evidence_schema
        or evidence.get("postgres_running") is not True
        or not isinstance(stopped, list)
        or any(not isinstance(item, str) for item in stopped)
        or len(stopped) != len(set(stopped))
        or set(stopped) != EXPECTED_STOPPED_SERVICES
        or evidence.get("operator") != consistency.get("operator")
        or evidence.get("captured_at") != consistency.get("captured_at")
        or evidence.get("reference_artifacts_source")
        != consistency.get("reference_artifacts_source")
        or evidence.get("geoserver_data_source")
        != consistency.get("geoserver_data_source")
        or consistency.get("postgres_running") is not True
        or consistency.get("stopped_services")
        != sorted(EXPECTED_STOPPED_SERVICES)
    ):
        raise DisasterRecoveryVerificationError(
            "stored quiescence evidence does not match its manifest"
        )
    if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION and any(
        evidence.get(key) != consistency.get(key)
        for key in (
            "postgres_database_name",
            "postgres_database_user",
            "postgres_hostname",
            "postgres_port",
            "postgres_system_identifier",
            "postgres_database_oid",
            "postgres_extensions",
        )
    ):
        raise DisasterRecoveryVerificationError(
            "stored PostgreSQL quiescence identity does not match its manifest"
        )
    if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION:
        _parse_database_extensions(
            evidence.get("postgres_extensions"),
            label="stored PostgreSQL extensions",
            error_type=DisasterRecoveryVerificationError,
        )
    operator = evidence.get("operator")
    captured_raw = evidence.get("captured_at")
    if (
        not isinstance(operator, str)
        or OPERATOR_RE.fullmatch(operator) is None
        or not isinstance(captured_raw, str)
    ):
        raise DisasterRecoveryVerificationError(
            "stored quiescence evidence identity is invalid"
        )
    try:
        captured = datetime.fromisoformat(
            captured_raw.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise DisasterRecoveryVerificationError(
            "stored quiescence evidence timestamp is invalid"
        ) from error
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise DisasterRecoveryVerificationError(
            "stored quiescence evidence timestamp is invalid"
        )


def _read_database_location(path: Path, *, target: bool) -> _DatabaseLocation:
    raw = _read_regular_file(
        path,
        limit=MAX_CREDENTIAL_BYTES,
        label="database credential file",
        private=True,
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise DisasterRecoverySafetyError(
            "database credential file is invalid"
        ) from error
    if (
        not value
        or "\x00" in value
        or any(character.isspace() for character in value)
    ):
        raise DisasterRecoverySafetyError(
            "database credential file is invalid"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
        raw_username = parsed.username
        raw_password = parsed.password
    except ValueError as error:
        raise DisasterRecoverySafetyError(
            "database credential URL is invalid"
        ) from error
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or raw_username is None
        or hostname is None
        or port is None
        or not 1 <= port <= 65535
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "/" in parsed.path[1:]
    ):
        raise DisasterRecoverySafetyError(
            "database credential URL is invalid"
        )
    database_name = unquote(parsed.path[1:])
    username = unquote(raw_username)
    password = None if raw_password is None else unquote(raw_password)
    if DATABASE_NAME_RE.fullmatch(database_name) is None:
        raise DisasterRecoverySafetyError(
            "database name in credential URL is invalid"
        )
    if (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", username) is None
        or re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
            hostname,
        )
        is None
        or ".." in hostname
        or (
            password is not None
            and (
                len(password) > 1024
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in password
                )
            )
        )
    ):
        raise DisasterRecoverySafetyError(
            "database credential URL is invalid"
        )
    if target and not database_name.startswith("app_drill_"):
        raise DisasterRecoverySafetyError(
            "restore target database must use the app_drill_ prefix"
        )
    if target and (hostname != "127.0.0.1" or port == 5432):
        raise DisasterRecoverySafetyError(
            "restore target must be an isolated loopback PostgreSQL port "
            "different from the development runtime port 5432"
        )
    return _DatabaseLocation(
        database_name=database_name,
        hostname=hostname,
        port=port,
        username=username,
        password=password,
    )


def _read_regular_file(
    path: Path,
    *,
    limit: int,
    label: str,
    private: bool = False,
) -> bytes:
    absolute = _absolute_path(path, label=label)
    _reject_symlink_components(absolute)
    try:
        metadata = absolute.lstat()
    except OSError as error:
        raise DisasterRecoverySafetyError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise DisasterRecoverySafetyError(
            f"{label} must be a regular non-symlink file"
        )
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DisasterRecoverySafetyError(
            f"{label} permissions must not grant group or other access"
        )
    if metadata.st_size > limit:
        raise DisasterRecoverySafetyError(f"{label} is too large")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(absolute, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != metadata.st_dev
            or before.st_ino != metadata.st_ino
            or before.st_size != metadata.st_size
        ):
            raise DisasterRecoverySafetyError(f"{label} changed while opening")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(COPY_CHUNK_BYTES, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise DisasterRecoverySafetyError(f"{label} is too large")
        after = os.fstat(descriptor)
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise DisasterRecoverySafetyError(f"{label} changed while reading")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _read_descriptor_bytes(
    descriptor: int,
    *,
    limit: int,
    label: str,
) -> bytes:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise DisasterRecoveryVerificationError(f"{label} is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(COPY_CHUNK_BYTES, limit + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise DisasterRecoveryVerificationError(
                    f"{label} is too large"
                )
        after = os.fstat(descriptor)
        _assert_same_verified_node(before, after, kind="file")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return b"".join(chunks)
    except OSError as error:
        raise DisasterRecoveryVerificationError(
            f"{label} cannot be read safely"
        ) from error


def _decode_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except DisasterRecoveryVerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise DisasterRecoveryVerificationError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise DisasterRecoveryVerificationError(f"{label} is invalid")
    return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DisasterRecoveryVerificationError(
                "JSON evidence contains a duplicate key"
            )
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise DisasterRecoveryVerificationError(
        "JSON evidence contains a non-finite constant"
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_payload_hash(
    path: Path,
    description: Mapping[str, Any],
) -> None:
    expected_size = description.get("size_bytes")
    expected_hash = description.get("sha256")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or not isinstance(expected_hash, str)
        or SHA256_RE.fullmatch(expected_hash) is None
    ):
        raise DisasterRecoveryVerificationError(
            "backup payload description is invalid"
        )
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DisasterRecoveryVerificationError(
            "backup payload is unavailable"
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != expected_size
        or _hash_file(path, expected=metadata) != expected_hash
    ):
        raise DisasterRecoveryVerificationError(
            "backup payload checksum does not match"
        )


def _payload_description_values(
    description: Mapping[str, Any],
) -> tuple[int, str]:
    expected_size = description.get("size_bytes")
    expected_hash = description.get("sha256")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or not isinstance(expected_hash, str)
        or SHA256_RE.fullmatch(expected_hash) is None
    ):
        raise DisasterRecoveryVerificationError(
            "backup payload description is invalid"
        )
    return expected_size, expected_hash


def _verify_payload_descriptor(
    descriptor: int,
    description: Mapping[str, Any],
) -> None:
    expected_size, expected_hash = _payload_description_values(description)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected_size
            or _hash_descriptor(descriptor, expected=metadata)
            != expected_hash
        ):
            raise DisasterRecoveryVerificationError(
                "backup payload checksum does not match"
            )
    except OSError as error:
        raise DisasterRecoveryVerificationError(
            "backup payload cannot be verified safely"
        ) from error


def _open_tar_from_source(
    source: Path | int,
) -> tarfile.TarFile:
    if isinstance(source, int):
        os.lseek(source, 0, os.SEEK_SET)
        stream = os.fdopen(os.dup(source), "rb", closefd=True)
        try:
            return tarfile.open(fileobj=stream, mode="r:")
        except BaseException:
            stream.close()
            raise
    return tarfile.open(source, mode="r:")


def _verify_tar_archive(
    archive_path: Path | int,
    entries: tuple[_InventoryEntry, ...],
) -> None:
    expected = {entry.path: entry for entry in entries}
    seen: set[str] = set()
    try:
        with _open_tar_from_source(archive_path) as archive:
            for member in archive:
                name = _validate_relative_path(member.name)
                if name in seen or name not in expected:
                    raise DisasterRecoveryVerificationError(
                        "tar archive contains an unexpected or duplicate member"
                    )
                seen.add(name)
                entry = expected[name]
                if member.issym() or member.islnk() or member.isdev():
                    raise DisasterRecoveryVerificationError(
                        "tar archive contains an unsafe member type"
                    )
                if (
                    member.uid != 0
                    or member.gid != 0
                    or member.uname not in {"", None}
                    or member.gname not in {"", None}
                ):
                    raise DisasterRecoveryVerificationError(
                        "tar ownership fields are not canonical"
                    )
                if entry.kind == "directory":
                    if not member.isdir() or member.size != 0:
                        raise DisasterRecoveryVerificationError(
                            "tar directory does not match manifest"
                        )
                    continue
                if not member.isfile() or member.size != entry.size_bytes:
                    raise DisasterRecoveryVerificationError(
                        "tar file does not match manifest"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise DisasterRecoveryVerificationError(
                        "tar file cannot be read"
                    )
                digest = hashlib.sha256()
                while True:
                    chunk = source.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                if digest.hexdigest() != entry.sha256:
                    raise DisasterRecoveryVerificationError(
                        "tar member checksum does not match manifest"
                    )
    except (tarfile.TarError, OSError) as error:
        raise DisasterRecoveryVerificationError(
            "tar archive cannot be verified"
        ) from error
    if seen != set(expected):
        raise DisasterRecoveryVerificationError(
            "tar archive is missing a manifest member"
        )


def _safe_extract_tar(
    archive_path: Path | int,
    destination: Path,
    entries: tuple[_InventoryEntry, ...],
    *,
    root_mode: int,
    root_mtime_ns: int,
    root_uid: int,
    root_gid: int,
) -> None:
    _assert_new_path(destination, label="tree restore destination")
    _mkdir_new_directory(destination, 0o700)
    root_descriptor = _open_absolute_directory_descriptor(destination)
    expected = {entry.path: entry for entry in entries}
    seen: set[str] = set()
    directories: list[_InventoryEntry] = []
    try:
        with _open_tar_from_source(archive_path) as archive:
            for member in archive:
                name = _validate_relative_path(member.name)
                if name in seen or name not in expected:
                    raise DisasterRecoveryVerificationError(
                        "tar archive contains an unexpected or duplicate member"
                    )
                seen.add(name)
                entry = expected[name]
                parts = PurePosixPath(name).parts
                parent_descriptor = _open_relative_parent_descriptor(
                    root_descriptor,
                    parts[:-1],
                )
                if entry.kind == "directory":
                    try:
                        if not member.isdir():
                            raise DisasterRecoveryVerificationError(
                                "tar directory does not match manifest"
                            )
                        os.mkdir(
                            parts[-1],
                            entry.mode & 0o777,
                            dir_fd=parent_descriptor,
                        )
                        directories.append(entry)
                    finally:
                        os.close(parent_descriptor)
                    continue
                try:
                    if not member.isfile() or member.size != entry.size_bytes:
                        raise DisasterRecoveryVerificationError(
                            "tar file does not match manifest"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise DisasterRecoveryVerificationError(
                            "tar file cannot be read"
                        )
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(
                        parts[-1],
                        flags,
                        entry.mode & 0o777,
                        dir_fd=parent_descriptor,
                    )
                    digest = hashlib.sha256()
                    size = 0
                    try:
                        while True:
                            chunk = source.read(COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            digest.update(chunk)
                            size += len(chunk)
                            view = memoryview(chunk)
                            while view:
                                written = os.write(descriptor, view)
                                view = view[written:]
                        if (
                            size != entry.size_bytes
                            or digest.hexdigest() != entry.sha256
                        ):
                            raise DisasterRecoveryVerificationError(
                                "extracted file does not match manifest"
                            )
                        os.fchown(descriptor, entry.uid, entry.gid)
                        os.fchmod(descriptor, entry.mode & 0o777)
                        os.utime(
                            descriptor,
                            ns=(entry.mtime_ns, entry.mtime_ns),
                        )
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                finally:
                    os.close(parent_descriptor)
    except DisasterRecoveryVerificationError:
        os.close(root_descriptor)
        raise
    except (tarfile.TarError, OSError) as error:
        os.close(root_descriptor)
        raise DisasterRecoveryVerificationError(
            "tar archive cannot be extracted safely"
        ) from error
    try:
        if seen != set(expected):
            raise DisasterRecoveryVerificationError(
                "tar archive is missing a manifest member"
            )
        for entry in reversed(directories):
            descriptor = _open_relative_directory_descriptor(
                root_descriptor,
                PurePosixPath(entry.path).parts,
            )
            try:
                os.fchown(descriptor, entry.uid, entry.gid)
                os.fchmod(descriptor, entry.mode & 0o777)
                os.utime(
                    descriptor,
                    ns=(entry.mtime_ns, entry.mtime_ns),
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fchown(root_descriptor, root_uid, root_gid)
        os.fchmod(root_descriptor, root_mode)
        os.utime(
            root_descriptor,
            ns=(root_mtime_ns, root_mtime_ns),
        )
        os.fsync(root_descriptor)
    except OSError as error:
        raise DisasterRecoveryVerificationError(
            "restored ownership or metadata could not be applied safely"
        ) from error
    finally:
        os.close(root_descriptor)


def _open_relative_parent_descriptor(
    root_descriptor: int,
    parts: Sequence[str],
) -> int:
    return _open_relative_directory_descriptor(
        root_descriptor,
        tuple(parts),
    )


def _open_relative_directory_descriptor(
    root_descriptor: int,
    parts: Sequence[str],
) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            next_descriptor = os.open(
                part,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as error:
        os.close(descriptor)
        raise DisasterRecoveryVerificationError(
            "tar member parent cannot be opened beneath restore root"
        ) from error
    return descriptor


def _parse_manifest_entries(
    value: object,
    *,
    manifest_schema_version: int,
) -> tuple[_InventoryEntry, ...]:
    if not isinstance(value, list):
        raise DisasterRecoveryVerificationError(
            "backup tree entries are invalid"
        )
    result: list[_InventoryEntry] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise DisasterRecoveryVerificationError(
                "backup tree entry is invalid"
            )
        path = item.get("path")
        kind = item.get("kind")
        size = item.get("size_bytes")
        mode = item.get("mode")
        mtime_ns = item.get("mtime_ns")
        ctime_ns = item.get("ctime_ns", mtime_ns)
        links = item.get("links", 1)
        uid = item.get("uid")
        gid = item.get("gid")
        sha256 = item.get("sha256")
        if (
            (
                manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION
                and ("ctime_ns" not in item or "links" not in item)
            )
            or not isinstance(path, str)
            or _validate_relative_path(path) in seen
            or kind not in {"directory", "file"}
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(mode, bool)
            or not isinstance(mode, int)
            or not 0 <= mode <= 0o777
            or isinstance(mtime_ns, bool)
            or not isinstance(mtime_ns, int)
            or mtime_ns < 0
            or isinstance(ctime_ns, bool)
            or not isinstance(ctime_ns, int)
            or ctime_ns < 0
            or isinstance(links, bool)
            or not isinstance(links, int)
            or links < 1
            or not _valid_ownership_id(uid)
            or not _valid_ownership_id(gid)
        ):
            raise DisasterRecoveryVerificationError(
                "backup tree entry is invalid"
            )
        seen.add(path)
        if kind == "directory":
            if size != 0 or sha256 is not None:
                raise DisasterRecoveryVerificationError(
                    "backup directory entry is invalid"
                )
        elif not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise DisasterRecoveryVerificationError(
                "backup file entry is invalid"
            )
        assert isinstance(uid, int)
        assert isinstance(gid, int)
        result.append(
            _InventoryEntry(
                path=path,
                kind=kind,
                size_bytes=size,
                sha256=sha256,
                mode=mode,
                mtime_ns=mtime_ns,
                ctime_ns=ctime_ns,
                links=links,
                uid=uid,
                gid=gid,
                device=0,
                inode=0,
            )
        )
    return tuple(result)


def _validate_tree_statistics(
    description: Mapping[str, Any],
    entries: tuple[_InventoryEntry, ...],
    *,
    expected_exclusions: list[str],
    manifest_schema_version: int,
) -> None:
    _manifest_mode(description, "root_mode")
    _manifest_nonnegative_integer(description, "root_mtime_ns")
    if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION and (
        "root_ctime_ns" not in description
        or "root_links" not in description
    ):
        raise DisasterRecoveryVerificationError(
            "backup manifest root identity is incomplete"
        )
    if "root_ctime_ns" in description:
        _manifest_nonnegative_integer(description, "root_ctime_ns")
    if "root_links" in description:
        root_links = _manifest_nonnegative_integer(
            description,
            "root_links",
        )
        if root_links < 1:
            raise DisasterRecoveryVerificationError(
                "backup manifest root link count is invalid"
            )
    _manifest_owner_id(description, "root_uid")
    _manifest_owner_id(description, "root_gid")
    expected_files = sum(item.kind == "file" for item in entries)
    expected_directories = sum(
        item.kind == "directory" for item in entries
    )
    expected_bytes = sum(
        item.size_bytes for item in entries if item.kind == "file"
    )
    if (
        description.get("file_count") != expected_files
        or description.get("directory_count") != expected_directories
        or description.get("total_bytes") != expected_bytes
        or description.get("excluded_paths") != expected_exclusions
    ):
        raise DisasterRecoveryVerificationError(
            "backup tree statistics or exclusions do not match its entries"
        )


def _validate_manifest_header(manifest: Mapping[str, Any]) -> None:
    manifest_schema_version = manifest.get("schema_version")
    if manifest_schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DisasterRecoveryVerificationError(
            "backup manifest schema is unsupported"
        )
    backup_id = manifest.get("backup_id")
    if (
        not isinstance(backup_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            backup_id,
        )
        is None
    ):
        raise DisasterRecoveryVerificationError("backup id is invalid")
    exclusions = manifest.get("excluded_reconstructible_state")
    if (
        not isinstance(exclusions, list)
        or len(exclusions) != 2
        or {
            item.get("component")
            for item in exclusions
            if isinstance(item, Mapping)
        }
        != {"redis", "geowebcache"}
        or any(
            not isinstance(item, Mapping)
            or item.get("included") is not False
            for item in exclusions
        )
    ):
        raise DisasterRecoveryVerificationError(
            "backup reconstructible-state declaration is invalid"
        )
    exclusion_map = {
        str(item["component"]): item
        for item in exclusions
        if isinstance(item, Mapping)
    }
    geowebcache_exclusion = exclusion_map["geowebcache"]
    if geowebcache_exclusion.get("configuration_included") is not True:
        raise DisasterRecoveryVerificationError(
            "GeoWebCache backup scope declaration is invalid"
        )
    if manifest_schema_version >= GWC_FILE_BLOB_STORE_SCHEMA_VERSION:
        if (
            geowebcache_exclusion.get("excluded_scope")
            != GWC_TILE_EXCLUDED_SCOPE
            or geowebcache_exclusion.get("configuration_directory")
            != GWC_CONFIGURATION_DIRECTORY
            or geowebcache_exclusion.get("cache_directory")
            != GWC_TILE_CACHE_DIRECTORY
            or geowebcache_exclusion.get("volume")
            != GWC_TILE_CACHE_VOLUME
            or geowebcache_exclusion.get("blob_store")
            != GWC_TILE_BLOB_STORE
        ):
            raise DisasterRecoveryVerificationError(
                "GeoWebCache FileBlobStore backup scope declaration is invalid"
            )
    elif (
        geowebcache_exclusion.get("excluded_scope")
        != LEGACY_GWC_TILE_EXCLUDED_SCOPE
        or geowebcache_exclusion.get("cache_directory")
        != LEGACY_GWC_TILE_CACHE_DIRECTORY
    ):
        raise DisasterRecoveryVerificationError(
            "legacy GeoWebCache backup scope declaration is invalid"
        )
    consistency = _manifest_mapping(manifest, "consistency")
    evidence_description = _mapping_member(
        consistency,
        "quiescence_evidence",
    )
    stopped_services = consistency.get("stopped_services")
    if (
        consistency.get("method")
        != "operator-quiescence-plus-before-after-inventory"
        or evidence_description.get("path") != QUIESCENCE_EVIDENCE_NAME
        or not isinstance(consistency.get("operator"), str)
        or not isinstance(consistency.get("captured_at"), str)
        or not isinstance(
            consistency.get("reference_artifacts_source"),
            str,
        )
        or not isinstance(consistency.get("geoserver_data_source"), str)
        or consistency.get("postgres_running") is not True
        or stopped_services != sorted(EXPECTED_STOPPED_SERVICES)
    ):
        raise DisasterRecoveryVerificationError(
            "backup consistency evidence declaration is invalid"
        )
    if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION:
        system_identifier = consistency.get("postgres_system_identifier")
        database_oid = consistency.get("postgres_database_oid")
        if (
            not isinstance(
                consistency.get("postgres_database_name"),
                str,
            )
            or not isinstance(
                consistency.get("postgres_database_user"),
                str,
            )
            or not isinstance(consistency.get("postgres_hostname"), str)
            or isinstance(consistency.get("postgres_port"), bool)
            or not isinstance(consistency.get("postgres_port"), int)
            or not isinstance(system_identifier, str)
            or SYSTEM_IDENTIFIER_RE.fullmatch(system_identifier) is None
            or isinstance(database_oid, bool)
            or not isinstance(database_oid, int)
            or not 1 <= database_oid <= 2**32 - 1
        ):
            raise DisasterRecoveryVerificationError(
                "backup PostgreSQL consistency identity is invalid"
            )
        source_extensions = _parse_database_extensions(
            consistency.get("postgres_extensions"),
            label="backup PostgreSQL extensions",
            error_type=DisasterRecoveryVerificationError,
        )
    else:
        source_extensions = ()
    database = _manifest_mapping(manifest, "database")
    database_name = _database_name_from_manifest(database)
    if (
        manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION
        and consistency.get("postgres_database_name") != database_name
    ):
        raise DisasterRecoveryVerificationError(
            "backup PostgreSQL identity does not match its dump declaration"
        )
    if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION:
        required_extensions = _parse_required_extension_manifest(
            database.get("required_extensions")
        )
        if required_extensions != [
            item.manifest_value(include_owner=False)
            for item in source_extensions
        ]:
            raise DisasterRecoveryVerificationError(
                "backup extension baseline does not match its source identity"
            )
        if manifest_schema_version >= FILESYSTEM_IDENTITY_SCHEMA_VERSION:
            _parse_manifest_extension_member_inventories(
                database.get("extension_members"),
                extensions=source_extensions,
            )
    legacy_pg_dump_contract = [
        "--format=custom",
        "--serializable-deferrable",
        "--no-owner",
        "--no-privileges",
    ]
    current_pg_dump_contract = [
        *legacy_pg_dump_contract,
        *[
            f"--exclude-extension={name}"
            for name in PG_DUMP_EXTENSION_EXCLUSIONS
        ],
    ]
    expected_pg_dump_contract = (
        current_pg_dump_contract
        if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION
        else legacy_pg_dump_contract
    )
    if (
        database.get("format") != "postgresql-custom"
        or database.get("pg_dump_contract") != expected_pg_dump_contract
    ):
        raise DisasterRecoveryVerificationError(
            "backup PostgreSQL contract is invalid"
        )
    restore_contract = _manifest_mapping(manifest, "restore_contract")
    legacy_restore_contract = {
        "directory_prefix": RESTORE_PREFIX,
        "database_prefix": "app_drill_",
        "overwrite_allowed": False,
        "database_clean_allowed": (
            "compensation-only-drop-owned-exact-drill-target"
        ),
        "database_drop_allowed": False,
    }
    current_restore_contract = {
        **legacy_restore_contract,
        "stable_identity_required": [
            "system_identifier",
            "database_oid",
            "drill_token",
        ],
        "connection_limit": 1,
        "connection_exclusivity": {
            "database_connection_limit": 1,
            "role_connection_limit": 1,
            "role_must_be_non_superuser": True,
            "required_role_membership": "pg_monitor",
            "plpgsql_owner_must_differ": True,
        },
        "extensions": {
            "preinstalled_by_administrator": True,
            "exact_versions_required": True,
            **(
                {"exact_canonical_members_required": True}
                if manifest_schema_version >= FILESYSTEM_IDENTITY_SCHEMA_VERSION
                else {}
            ),
            "owners_must_differ_from_restore_role": True,
            "restore_comments": False,
        },
    }
    expected_restore_contract = (
        current_restore_contract
        if manifest_schema_version >= DATABASE_IDENTITY_SCHEMA_VERSION
        else legacy_restore_contract
    )
    if restore_contract != expected_restore_contract:
        raise DisasterRecoveryVerificationError(
            "backup restore contract is invalid"
        )


def _manifest_mapping(
    parent: Mapping[str, Any],
    key: str,
) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise DisasterRecoveryVerificationError(
            "backup manifest structure is invalid"
        )
    return value


def _mapping_member(
    parent: Mapping[str, Any],
    key: str,
) -> Mapping[str, Any]:
    return _manifest_mapping(parent, key)


def _manifest_nonnegative_integer(
    parent: Mapping[str, Any],
    key: str,
) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DisasterRecoveryVerificationError(
            "backup manifest integer is invalid"
        )
    return value


def _manifest_mode(parent: Mapping[str, Any], key: str) -> int:
    value = _manifest_nonnegative_integer(parent, key)
    if value > 0o777:
        raise DisasterRecoveryVerificationError(
            "backup manifest mode is invalid"
        )
    return value


def _manifest_owner_id(parent: Mapping[str, Any], key: str) -> int:
    value = parent.get(key)
    if not _valid_ownership_id(value):
        raise DisasterRecoveryVerificationError(
            "backup manifest ownership id is invalid"
        )
    assert isinstance(value, int)
    return value


def _database_name_from_manifest(database: Mapping[str, Any]) -> str:
    value = database.get("name")
    if not isinstance(value, str) or DATABASE_NAME_RE.fullmatch(value) is None:
        raise DisasterRecoveryVerificationError(
            "backup database identity is invalid"
        )
    return value


def _fixed_payload_path(
    backup: Path,
    description: Mapping[str, Any],
    *,
    expected_name: str,
) -> Path:
    _assert_payload_description_name(
        description,
        expected_name=expected_name,
    )
    return backup / expected_name


def _assert_payload_description_name(
    description: Mapping[str, Any],
    *,
    expected_name: str,
) -> None:
    if description.get("path") != expected_name:
        raise DisasterRecoveryVerificationError(
            "backup payload path is invalid"
        )


def _parse_manifest_checksum(raw: bytes) -> str:
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise DisasterRecoveryVerificationError(
            "manifest checksum file is invalid"
        ) from error
    match = re.fullmatch(r"([0-9a-f]{64})  manifest\.json\n", value)
    if match is None:
        raise DisasterRecoveryVerificationError(
            "manifest checksum file is invalid"
        )
    return match.group(1)


def _read_checked_manifest(
    backup: Path,
) -> tuple[dict[str, Any], str]:
    manifest_bytes = _read_regular_file(
        backup / "manifest.json",
        limit=MAX_JSON_BYTES,
        label="backup manifest",
    )
    checksum_bytes = _read_regular_file(
        backup / "manifest.sha256",
        limit=1024,
        label="backup manifest checksum",
    )
    expected = _parse_manifest_checksum(checksum_bytes)
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if actual != expected:
        raise DisasterRecoveryVerificationError(
            "backup manifest checksum does not match"
        )
    manifest = _decode_json_object(
        manifest_bytes,
        label="backup manifest",
    )
    _validate_manifest_header(manifest)
    return manifest, actual


def _assert_regular_payload(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DisasterRecoveryCommandError(f"{label} was not created") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise DisasterRecoverySafetyError(
            f"{label} must be a private non-empty regular file"
        )


def _assert_backup_directory_contents(path: Path) -> None:
    expected = {
        "postgres.dump",
        "reference_artifacts.tar",
        "geoserver_data.tar",
        QUIESCENCE_EVIDENCE_NAME,
        "manifest.json",
        "manifest.sha256",
    }
    try:
        with os.scandir(path) as iterator:
            entries = list(iterator)
    except OSError as error:
        raise DisasterRecoveryVerificationError(
            "backup directory cannot be inspected"
        ) from error
    if {entry.name for entry in entries} != expected or any(
        not entry.is_file(follow_symlinks=False) for entry in entries
    ):
        raise DisasterRecoveryVerificationError(
            "backup directory contains missing, unexpected or unsafe entries"
        )


def _publish_directory_noreplace(source: Path, destination: Path) -> bool:
    """Atomically publish a directory without POSIX rename replacement."""

    if source.parent != destination.parent:
        raise DisasterRecoverySafetyError(
            "atomic publication requires one exact destination parent"
        )
    parent_descriptor = _open_absolute_directory_descriptor(source.parent)
    published = False
    durability_verified = False
    try:
        try:
            source_metadata = os.stat(
                source.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise DisasterRecoverySafetyError(
                "publication source cannot be inspected safely"
            ) from error
        try:
            os.stat(
                destination.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as error:
            raise DisasterRecoverySafetyError(
                "publication destination cannot be inspected safely"
            ) from error
        else:
            raise DisasterRecoverySafetyError(
                "destination appeared before atomic publication"
            )
        if not stat.S_ISDIR(source_metadata.st_mode):
            raise DisasterRecoverySafetyError(
                "publication source must remain a directory"
            )
        _rename_directory_noreplace(
            parent_descriptor,
            source.name,
            destination.name,
        )
        published = True
        try:
            durability_verified = _fsync_descriptor_with_retries(
                parent_descriptor
            )
        except BaseException:
            # Atomic publication is already committed. Treat even an
            # interruption during the durability probe as an unverified but
            # completed publication.
            durability_verified = False
    finally:
        try:
            os.close(parent_descriptor)
        except OSError:
            # Once renameat2 succeeded, a close error cannot turn the
            # published directory back into a failure: restore would otherwise
            # compensate its database while leaving a visible final tree.
            if published:
                durability_verified = False
    return durability_verified


def _rename_directory_noreplace(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as error:
        raise DisasterRecoverySafetyError(
            "atomic no-replace directory publication is unavailable"
        ) from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    rename_noreplace = 1
    result = renameat2(
        parent_descriptor,
        os.fsencode(source_name),
        parent_descriptor,
        os.fsencode(destination_name),
        rename_noreplace,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise DisasterRecoverySafetyError(
            "destination appeared before atomic publication"
        )
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise DisasterRecoverySafetyError(
            "filesystem does not support atomic no-replace publication"
        )
    raise DisasterRecoverySafetyError(
        "backup or restore directory could not be published atomically"
    ) from OSError(error_number, os.strerror(error_number))


def _open_absolute_directory_descriptor(path: Path) -> int:
    absolute = _absolute_path(path, label="directory")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(Path(absolute.anchor), flags)
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise DisasterRecoverySafetyError(
            "directory path cannot be opened without following symlinks"
        ) from error
    return descriptor


def _new_named_destination(path: Path, *, prefix: str) -> Path:
    absolute = _absolute_path(path, label="destination")
    if (
        not absolute.name.startswith(prefix)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", absolute.name)
        is None
    ):
        raise DisasterRecoverySafetyError(
            f"destination basename must start with {prefix}"
        )
    _assert_new_path(absolute, label="destination")
    parent = _existing_directory(absolute.parent, label="destination parent")
    if parent != absolute.parent:
        raise DisasterRecoverySafetyError(
            "destination parent path is not canonical"
        )
    return absolute


def _existing_named_directory(
    path: Path,
    *,
    prefix: str,
    label: str,
) -> Path:
    absolute = _existing_directory(path, label=label)
    if not absolute.name.startswith(prefix):
        raise DisasterRecoverySafetyError(
            f"{label} basename must start with {prefix}"
        )
    return absolute


def _existing_directory(path: Path, *, label: str) -> Path:
    absolute = _absolute_path(path, label=label)
    _reject_symlink_components(absolute)
    try:
        metadata = absolute.lstat()
    except OSError as error:
        raise DisasterRecoverySafetyError(f"{label} is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise DisasterRecoverySafetyError(
            f"{label} must be a non-symlink directory"
        )
    return absolute


def _absolute_path(path: Path, *, label: str) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute() or path == Path("/") or "\x00" in str(path):
        raise DisasterRecoverySafetyError(
            f"{label} must be an explicit absolute path other than /"
        )
    normalized = Path(os.path.normpath(path))
    if normalized != path:
        raise DisasterRecoverySafetyError(f"{label} must be normalized")
    return normalized


def _assert_new_path(path: Path, *, label: str) -> None:
    _reject_symlink_components(path)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise DisasterRecoverySafetyError(
            f"{label} cannot be inspected safely"
        ) from error
    raise DisasterRecoverySafetyError(f"{label} must not already exist")


def _mkdir_new_directory(path: Path, mode: int) -> None:
    parent_descriptor = _open_absolute_directory_descriptor(path.parent)
    try:
        os.mkdir(path.name, mode, dir_fd=parent_descriptor)
        metadata = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(metadata.st_mode):
            raise DisasterRecoverySafetyError(
                "new staging path is not a directory"
            )
        os.fsync(parent_descriptor)
    except FileExistsError as error:
        raise DisasterRecoverySafetyError(
            "new staging path appeared before creation"
        ) from error
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "new staging directory cannot be created safely"
        ) from error
    finally:
        os.close(parent_descriptor)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as error:
            raise DisasterRecoverySafetyError(
                "path cannot be inspected safely"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise DisasterRecoverySafetyError(
                "paths cannot contain symlink components"
            )


def _reject_overlaps(paths: Mapping[str, Path]) -> None:
    items = list(paths.items())
    for index, (left_label, left) in enumerate(items):
        for right_label, right in items[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise DisasterRecoverySafetyError(
                    f"{left_label} and {right_label} must not overlap"
                )


def _reject_protected_restore_path(destination: Path) -> None:
    for protected in PROTECTED_RUNTIME_PATHS:
        if (
            destination == protected
            or destination in protected.parents
            or protected in destination.parents
        ):
            raise DisasterRecoverySafetyError(
                "restore destination overlaps a protected runtime path"
            )


def _validate_relative_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        raise DisasterRecoveryVerificationError(
            "archive-relative path is invalid"
        )
    path = PurePosixPath(value)
    if path.as_posix() != value or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise DisasterRecoveryVerificationError(
            "archive-relative path is invalid"
        )
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = _open_absolute_directory_descriptor(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_descriptor_with_retries(
    descriptor: int,
    *,
    attempts: int = 3,
) -> bool:
    for _attempt in range(attempts):
        try:
            os.fsync(descriptor)
        except OSError:
            continue
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
