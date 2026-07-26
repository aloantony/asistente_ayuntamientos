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

SCHEMA_VERSION = 2
QUIESCENCE_EVIDENCE_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 256 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 8192
COPY_CHUNK_BYTES = 1024 * 1024
MAX_OWNERSHIP_ID = 2**31 - 1
BACKUP_PREFIX = "siur-backup-"
RESTORE_PREFIX = "siur-drill-restore-"
QUIESCENCE_EVIDENCE_NAME = "quiescence-evidence.json"
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
    Path("/mnt/reference_artifacts"),
    Path("/opt/geoserver_data"),
    Path("/sources/reference_artifacts"),
    Path("/sources/geoserver_data"),
)
DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,62}$", re.ASCII)
OPERATOR_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$", re.ASCII)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


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


@dataclass(frozen=True)
class _DatabaseLocation:
    database_name: str
    hostname: str
    port: int
    username: str
    password: str | None = field(repr=False)


@dataclass(frozen=True)
class _InventoryEntry:
    path: str
    kind: Literal["directory", "file"]
    size_bytes: int
    sha256: str | None
    mode: int
    mtime_ns: int
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
    reference_inventory = _inventory_tree(
        reference_source,
        excluded_roots=frozenset(
            {"staging", ".reference-blob-store.lock"}
        ),
    )
    # GeoWebCache's generated tile bytes live on their own volume through
    # GEOWEBCACHE_CACHE_DIR at the dedicated gwc-cache mount.  All persistent
    # configuration, including gwc-gs.xml, gwc-layers and
    # gwc/geowebcache.xml, remains outside that one mount and is backed up.
    geoserver_inventory = _inventory_tree(
        geoserver_source,
        excluded_roots=frozenset({"gwc-cache"}),
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
                now=timestamp,
                max_age_seconds=request.quiescence_max_age_seconds,
            )
        )
    if apply and evidence is None:
        raise DisasterRecoverySafetyError(
            "apply requires a recent quiescence evidence file"
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
                "reason": (
                    "only the external GEOWEBCACHE_CACHE_DIR tile volume is "
                    "derived; all GeoWebCache configuration inside the "
                    "GeoServer data directory is included"
                ),
            },
        ],
    }
    if not apply:
        return plan

    command_runner = runner or subprocess_command_runner
    partial = destination.parent / (
        f".{destination.name}.partial-{uuid4().hex}"
    )
    _assert_new_path(partial, label="partial backup destination")
    try:
        os.mkdir(partial, 0o700)
        database_dump = partial / "postgres.dump"
        # Pre-create privately inside the 0700 staging directory so pg_dump
        # cannot inherit a permissive process umask or encounter a symlink.
        _write_exclusive(database_dump, b"")
        _run_pg_dump(command_runner, database, database_dump)
        _assert_regular_payload(database_dump, label="PostgreSQL dump")

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
        _publish_directory_noreplace(partial, destination)
        _fsync_directory(destination.parent)
    except Exception:
        # Deliberately leave an unmistakable partial directory for forensic
        # inspection.  This tool has no delete path.
        raise
    return {
        **plan,
        "backup_id": manifest["backup_id"],
        "manifest_sha256": manifest_sha,
        "completed": True,
    }


def verify_backup(
    backup: Path,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    """Verify manifest, payload hashes, tar members and pg_dump readability."""

    backup_path = _existing_named_directory(
        backup,
        prefix=BACKUP_PREFIX,
        label="backup",
    )
    _assert_backup_directory_contents(backup_path)
    manifest, actual_manifest_hash = _read_checked_manifest(backup_path)
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
    evidence_path = _fixed_payload_path(
        backup_path,
        evidence_description,
        expected_name=QUIESCENCE_EVIDENCE_NAME,
    )
    _verify_payload_hash(evidence_path, evidence_description)
    _verify_stored_quiescence_evidence(
        evidence_path,
        consistency=consistency,
    )
    dump_description = _mapping_member(database, "dump")
    dump_path = _fixed_payload_path(
        backup_path,
        dump_description,
        expected_name="postgres.dump",
    )
    _verify_payload_hash(dump_path, dump_description)

    verified_trees: dict[str, dict[str, int]] = {}
    for name, description, expected_archive, expected_exclusions in (
        (
            "reference_artifacts",
            reference,
            "reference_artifacts.tar",
            [".reference-blob-store.lock", "staging"],
        ),
        ("geoserver_data", geoserver, "geoserver_data.tar", ["gwc-cache"]),
    ):
        archive_description = _mapping_member(description, "archive")
        archive_path = _fixed_payload_path(
            backup_path,
            archive_description,
            expected_name=expected_archive,
        )
        _verify_payload_hash(archive_path, archive_description)
        entries = _parse_manifest_entries(description.get("entries"))
        _validate_tree_statistics(
            description,
            entries,
            expected_exclusions=expected_exclusions,
        )
        _verify_tar_archive(archive_path, entries)
        verified_trees[name] = {
            "files": sum(item.kind == "file" for item in entries),
            "directories": sum(
                item.kind == "directory" for item in entries
            ),
            "bytes": sum(
                item.size_bytes for item in entries if item.kind == "file"
            ),
        }

    _run_pg_restore_list(runner or subprocess_command_runner, dump_path)
    backup_id = manifest.get("backup_id")
    if not isinstance(backup_id, str):
        raise DisasterRecoveryVerificationError("backup id is invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "verify",
        "backup": str(backup_path),
        "backup_id": backup_id,
        "manifest_sha256": actual_manifest_hash,
        "database_name": database_name,
        "quiescence_evidence_sha256": evidence_description["sha256"],
        "trees": verified_trees,
        "verified": True,
    }


def restore_backup(
    request: RestoreBackupRequest,
    *,
    apply: bool = False,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Plan or restore into a new directory and an empty drill-only database."""

    command_runner = runner or subprocess_command_runner
    verification = verify_backup(request.backup, runner=command_runner)
    backup_path = Path(str(verification["backup"]))
    destination = _new_named_destination(
        request.destination,
        prefix=RESTORE_PREFIX,
    )
    _reject_protected_restore_path(destination)
    target_credential_path = _absolute_path(
        request.target_database_url_file,
        label="target database credential file",
    )
    _reject_overlaps(
        {
            "backup": backup_path,
            "restore destination": destination,
            "target database credential file": target_credential_path,
        }
    )
    target = _read_database_location(
        target_credential_path,
        target=True,
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
        "database_contract": (
            "existing empty isolated drill-only database; no create or drop; "
            "a failed post-restore publication is compensated with DROP "
            "OWNED only in this exact target"
        ),
        "verified": verification["verified"],
    }
    if not apply:
        return report

    _assert_empty_drill_database(command_runner, target)
    partial = destination.parent / (
        f".{destination.name}.partial-{uuid4().hex}"
    )
    _assert_new_path(partial, label="partial restore destination")
    os.mkdir(partial, 0o700)
    manifest, _manifest_hash = _read_checked_manifest(backup_path)
    trees = _manifest_mapping(manifest, "trees")
    for tree_name, archive_name in (
        ("reference_artifacts", "reference_artifacts.tar"),
        ("geoserver_data", "geoserver_data.tar"),
    ):
        tree = _mapping_member(trees, tree_name)
        entries = _parse_manifest_entries(tree.get("entries"))
        _safe_extract_tar(
            backup_path / archive_name,
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
    database_description = _manifest_mapping(manifest, "database")
    dump_description = _mapping_member(database_description, "dump")
    dump_path = _fixed_payload_path(
        backup_path,
        dump_description,
        expected_name="postgres.dump",
    )
    restored_at = _utc_now(now).isoformat().replace("+00:00", "Z")
    restore_record = {
        **report,
        "mode": "applied",
        "restored_at": restored_at,
        "completed": True,
        "completion_contract": (
            "this record is visible at the final no-replace destination only "
            "after pg_restore committed successfully"
        ),
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
    _verify_payload_hash(dump_path, dump_description)
    _assert_empty_drill_database(command_runner, target)

    restored = False
    try:
        _run_pg_restore(command_runner, target, dump_path)
        restored = True
        _publish_directory_noreplace(partial, destination)
    except BaseException:
        if restored:
            _compensate_restored_drill_database(command_runner, target)
        raise

    # Once the atomic rename succeeds, both the populated drill database and
    # the self-describing final directory are present.  A parent-directory
    # fsync error must not turn that completed pair into an unrecoverable
    # "failed" run, so it is retried and reported explicitly.
    durability_verified = _fsync_directory_with_retries(destination.parent)
    return {
        **restore_record,
        "durability_verified": durability_verified,
    }


def subprocess_command_runner(
    argv: Sequence[str],
    environment: Mapping[str, str],
) -> CommandResult:
    """Run a fixed argv without a shell and without echoing command output."""

    try:
        completed = subprocess.run(
            list(argv),
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
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
            ],
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
                "excluded_scope": (
                    "external GEOWEBCACHE_CACHE_DIR tile volume only"
                ),
                "configuration_included": True,
                "cache_directory": "/opt/geoserver_data/gwc-cache",
                "reconstruction": (
                    "restore the complete GeoServer data directory, reapply "
                    "and verify the declarative disk quota, then regenerate "
                    "requested tiles from restored local delivery artifacts"
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
    try:
        source_before = source.lstat()
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "backup source cannot be inspected"
        ) from error
    if not stat.S_ISDIR(source_before.st_mode):
        raise DisasterRecoverySafetyError(
            "backup source must remain a directory"
        )
    _validate_source_ownership(source_before)
    entries: list[_InventoryEntry] = []
    excluded: list[str] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            with os.scandir(directory) as iterator:
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
            if not relative.parts and child.name in excluded_roots:
                excluded.append(relative_text)
                continue
            _validate_relative_path(relative_text)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise DisasterRecoverySafetyError(
                    "backup source changed during inventory"
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise DisasterRecoverySafetyError(
                    "backup sources cannot contain symlinks"
                )
            permissions = stat.S_IMODE(metadata.st_mode)
            _validate_source_ownership(metadata)
            if permissions & ~0o777:
                raise DisasterRecoverySafetyError(
                    "backup sources cannot contain setuid, setgid or sticky "
                    "entries"
                )
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    _InventoryEntry(
                        path=relative_text,
                        kind="directory",
                        size_bytes=0,
                        sha256=None,
                        mode=permissions,
                        mtime_ns=metadata.st_mtime_ns,
                        uid=metadata.st_uid,
                        gid=metadata.st_gid,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                    )
                )
                visit(Path(child.path), child_relative)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise DisasterRecoverySafetyError(
                    "backup sources can contain only directories and "
                    "regular files"
                )
            digest = _hash_file(Path(child.path), expected=metadata)
            entries.append(
                _InventoryEntry(
                    path=relative_text,
                    kind="file",
                    size_bytes=metadata.st_size,
                    sha256=digest,
                    mode=permissions,
                    mtime_ns=metadata.st_mtime_ns,
                    uid=metadata.st_uid,
                    gid=metadata.st_gid,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                )
            )

    visit(source, PurePosixPath())
    try:
        source_after = source.lstat()
    except OSError as error:
        raise DisasterRecoverySafetyError(
            "backup source changed during inventory"
        ) from error
    root_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_mtime_ns",
        "st_uid",
        "st_gid",
    )
    if any(
        getattr(source_before, name) != getattr(source_after, name)
        for name in root_fields
    ):
        raise DisasterRecoverySafetyError(
            "backup source changed during inventory"
        )
    return _TreeInventory(
        source=source,
        entries=tuple(entries),
        excluded_paths=tuple(sorted(excluded)),
        source_device=source_before.st_dev,
        source_inode=source_before.st_ino,
        source_mode=stat.S_IMODE(source_before.st_mode) & 0o777,
        source_mtime_ns=source_before.st_mtime_ns,
        source_uid=source_before.st_uid,
        source_gid=source_before.st_gid,
    )


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
                    archive.addfile(info)
                    continue
                info.type = tarfile.REGTYPE
                info.size = entry.size_bytes
                source_path = inventory.source / Path(entry.path)
                metadata = source_path.lstat()
                _assert_entry_identity(entry, metadata)
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                file_descriptor = os.open(source_path, flags)
                with os.fdopen(file_descriptor, "rb", closefd=True) as source:
                    _assert_entry_identity(entry, os.fstat(source.fileno()))
                    archive.addfile(info, source)
                    _assert_entry_identity(entry, os.fstat(source.fileno()))
        output.flush()
        os.fsync(output.fileno())
    finally:
        output.close()


def _assert_entry_identity(
    entry: _InventoryEntry,
    metadata: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != entry.device
        or metadata.st_ino != entry.inode
        or metadata.st_size != entry.size_bytes
        or metadata.st_mtime_ns != entry.mtime_ns
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
            f"--file={output}",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError("PostgreSQL backup command failed")


def _run_pg_restore_list(runner: CommandRunner, dump: Path) -> None:
    result = runner(
        ("pg_restore", "--list", str(dump)),
        _client_environment(),
    )
    if result.returncode != 0:
        raise DisasterRecoveryVerificationError(
            "PostgreSQL dump cannot be listed by pg_restore"
        )


def _assert_empty_drill_database(
    runner: CommandRunner,
    database: _DatabaseLocation,
) -> None:
    # This is intentionally broader than counting tables.  A drill target is
    # accepted only when it is owned by the connecting role, has no concurrent
    # sessions, contains the standard public schema contract, and has no
    # user schemas, relations, routines, types, non-built-in extensions or
    # other database-local objects.
    query = (
        "SELECT current_database(), current_user, "
        "(SELECT CASE WHEN pg_get_userbyid(datdba) = current_user "
        "THEN 1 ELSE 0 END FROM pg_database "
        "WHERE datname = current_database()), "
        "(SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()), "
        "(SELECT count(*) FROM pg_namespace WHERE nspname <> 'public' "
        "AND nspname NOT IN ('pg_catalog','information_schema') "
        "AND nspname !~ '^pg_(toast|temp|toast_temp)(_|$)'), "
        "(SELECT count(*) FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname NOT IN "
        "('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)'), "
        "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname NOT IN "
        "('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)'), "
        "(SELECT count(*) FROM pg_type t JOIN pg_namespace n "
        "ON n.oid = t.typnamespace WHERE n.nspname NOT IN "
        "('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)'), "
        "(SELECT count(*) FROM pg_extension e JOIN pg_namespace n "
        "ON n.oid = e.extnamespace WHERE NOT "
        "(e.extname = 'plpgsql' AND n.nspname = 'pg_catalog')), "
        "("
        "(SELECT CASE WHEN count(*) = 1 AND count(*) FILTER "
        "(WHERE pg_get_userbyid(nspowner) = 'pg_database_owner') = 1 "
        "THEN 0 ELSE 1 END FROM pg_namespace WHERE nspname = 'public') + "
        "(SELECT count(*) FROM pg_language WHERE lanname NOT IN "
        "('internal','c','sql','plpgsql')) + "
        "(SELECT count(*) FROM pg_foreign_data_wrapper) + "
        "(SELECT count(*) FROM pg_foreign_server) + "
        "(SELECT count(*) FROM pg_user_mapping) + "
        "(SELECT count(*) FROM pg_event_trigger) + "
        "(SELECT count(*) FROM pg_publication) + "
        "(SELECT count(*) FROM pg_subscription) + "
        "(SELECT count(*) FROM pg_largeobject_metadata) + "
        "(SELECT count(*) FROM pg_default_acl) + "
        "(SELECT count(*) FROM pg_cast WHERE oid >= 16384) + "
        "(SELECT count(*) FROM pg_transform WHERE oid >= 16384) + "
        "(SELECT count(*) FROM pg_am WHERE oid >= 16384) + "
        "(SELECT count(*) FROM ("
        "SELECT oid, collnamespace AS namespace FROM pg_collation "
        "UNION ALL SELECT oid, connamespace FROM pg_conversion "
        "UNION ALL SELECT oid, oprnamespace FROM pg_operator "
        "UNION ALL SELECT oid, opcnamespace FROM pg_opclass "
        "UNION ALL SELECT oid, opfnamespace FROM pg_opfamily "
        "UNION ALL SELECT oid, stxnamespace FROM pg_statistic_ext "
        "UNION ALL SELECT oid, cfgnamespace FROM pg_ts_config "
        "UNION ALL SELECT oid, dictnamespace FROM pg_ts_dict "
        "UNION ALL SELECT oid, prsnamespace FROM pg_ts_parser "
        "UNION ALL SELECT oid, tmplnamespace FROM pg_ts_template"
        ") scoped JOIN pg_namespace n ON n.oid = scoped.namespace "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp|toast_temp)(_|$)')"
        ")"
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
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    expected = (
        f"{database.database_name}|{database.username}|1|"
        + "|".join("0" for _ in range(7))
    )
    if lines != [expected]:
        raise DisasterRecoverySafetyError(
            "restore target is not the expected empty drill database"
        )


def _compensate_restored_drill_database(
    runner: CommandRunner,
    database: _DatabaseLocation,
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
    result = runner(
        (
            "psql",
            "-X",
            "--set=ON_ERROR_STOP=1",
            "--single-transaction",
            "--command="
            "DROP OWNED BY CURRENT_USER CASCADE; "
            "CREATE SCHEMA IF NOT EXISTS public "
            "AUTHORIZATION pg_database_owner; "
            "GRANT USAGE ON SCHEMA public TO PUBLIC; "
            "GRANT CREATE ON SCHEMA public TO pg_database_owner; "
            "CREATE EXTENSION IF NOT EXISTS plpgsql WITH SCHEMA pg_catalog",
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            "post-restore database compensation failed"
        )
    try:
        _assert_empty_drill_database(runner, database)
    except DisasterRecoveryError as error:
        raise DisasterRecoveryCommandError(
            "post-restore database compensation could not prove an empty target"
        ) from error


def _run_pg_restore(
    runner: CommandRunner,
    database: _DatabaseLocation,
    dump: Path,
) -> None:
    result = runner(
        (
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            f"--dbname={database.database_name}",
            str(dump),
        ),
        _postgres_environment(database),
    )
    if result.returncode != 0:
        raise DisasterRecoveryCommandError(
            "PostgreSQL drill restore command failed"
        )


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


def _validate_quiescence_evidence(
    path: Path,
    *,
    reference_source: Path,
    geoserver_source: Path,
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
) -> None:
    """Verify the preserved exact evidence bytes against manifest semantics."""

    raw = _read_regular_file(
        path,
        limit=64 * 1024,
        label="stored quiescence evidence",
    )
    evidence = _decode_json_object(
        raw,
        label="stored quiescence evidence",
    )
    stopped = evidence.get("stopped_services")
    if (
        evidence.get("schema_version")
        != QUIESCENCE_EVIDENCE_SCHEMA_VERSION
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


def _verify_tar_archive(
    archive_path: Path,
    entries: tuple[_InventoryEntry, ...],
) -> None:
    expected = {entry.path: entry for entry in entries}
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
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
    archive_path: Path,
    destination: Path,
    entries: tuple[_InventoryEntry, ...],
    *,
    root_mode: int,
    root_mtime_ns: int,
    root_uid: int,
    root_gid: int,
) -> None:
    _assert_new_path(destination, label="tree restore destination")
    os.mkdir(destination, 0o700)
    expected = {entry.path: entry for entry in entries}
    seen: set[str] = set()
    directories: list[tuple[Path, _InventoryEntry]] = []
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                name = _validate_relative_path(member.name)
                if name in seen or name not in expected:
                    raise DisasterRecoveryVerificationError(
                        "tar archive contains an unexpected or duplicate member"
                    )
                seen.add(name)
                entry = expected[name]
                target = destination.joinpath(*PurePosixPath(name).parts)
                if destination not in target.parents:
                    raise DisasterRecoveryVerificationError(
                        "tar member escapes restore destination"
                    )
                if entry.kind == "directory":
                    if not member.isdir():
                        raise DisasterRecoveryVerificationError(
                            "tar directory does not match manifest"
                        )
                    os.mkdir(target, entry.mode & 0o777)
                    directories.append((target, entry))
                    continue
                if not member.isfile() or member.size != entry.size_bytes:
                    raise DisasterRecoveryVerificationError(
                        "tar file does not match manifest"
                    )
                parent = target.parent
                if parent != destination and not parent.is_dir():
                    raise DisasterRecoveryVerificationError(
                        "tar member parent is missing"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise DisasterRecoveryVerificationError(
                        "tar file cannot be read"
                    )
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags, entry.mode & 0o777)
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
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if size != entry.size_bytes or digest.hexdigest() != entry.sha256:
                    raise DisasterRecoveryVerificationError(
                        "extracted file does not match manifest"
                    )
                os.chown(
                    target,
                    entry.uid,
                    entry.gid,
                    follow_symlinks=False,
                )
                os.chmod(target, entry.mode & 0o777, follow_symlinks=False)
                os.utime(
                    target,
                    ns=(entry.mtime_ns, entry.mtime_ns),
                    follow_symlinks=False,
                )
    except (tarfile.TarError, OSError) as error:
        raise DisasterRecoveryVerificationError(
            "tar archive cannot be extracted safely"
        ) from error
    if seen != set(expected):
        raise DisasterRecoveryVerificationError(
            "tar archive is missing a manifest member"
        )
    for target, entry in reversed(directories):
        os.chown(
            target,
            entry.uid,
            entry.gid,
            follow_symlinks=False,
        )
        os.chmod(target, entry.mode & 0o777, follow_symlinks=False)
        os.utime(
            target,
            ns=(entry.mtime_ns, entry.mtime_ns),
            follow_symlinks=False,
        )
    os.chown(
        destination,
        root_uid,
        root_gid,
        follow_symlinks=False,
    )
    os.chmod(destination, root_mode, follow_symlinks=False)
    os.utime(
        destination,
        ns=(root_mtime_ns, root_mtime_ns),
        follow_symlinks=False,
    )
    _fsync_directory(destination)


def _parse_manifest_entries(value: object) -> tuple[_InventoryEntry, ...]:
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
        uid = item.get("uid")
        gid = item.get("gid")
        sha256 = item.get("sha256")
        if (
            not isinstance(path, str)
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
) -> None:
    _manifest_mode(description, "root_mode")
    _manifest_nonnegative_integer(description, "root_mtime_ns")
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
    if manifest.get("schema_version") != SCHEMA_VERSION:
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
    if (
        geowebcache_exclusion.get("configuration_included") is not True
        or geowebcache_exclusion.get("excluded_scope")
        != "external GEOWEBCACHE_CACHE_DIR tile volume only"
        or geowebcache_exclusion.get("cache_directory")
        != "/opt/geoserver_data/gwc-cache"
    ):
        raise DisasterRecoveryVerificationError(
            "GeoWebCache backup scope declaration is invalid"
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
    database = _manifest_mapping(manifest, "database")
    if (
        database.get("format") != "postgresql-custom"
        or database.get("pg_dump_contract")
        != [
            "--format=custom",
            "--serializable-deferrable",
            "--no-owner",
            "--no-privileges",
        ]
    ):
        raise DisasterRecoveryVerificationError(
            "backup PostgreSQL contract is invalid"
        )
    restore_contract = _manifest_mapping(manifest, "restore_contract")
    if restore_contract != {
        "directory_prefix": RESTORE_PREFIX,
        "database_prefix": "app_drill_",
        "overwrite_allowed": False,
        "database_clean_allowed": (
            "compensation-only-drop-owned-exact-drill-target"
        ),
        "database_drop_allowed": False,
    }:
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
    if description.get("path") != expected_name:
        raise DisasterRecoveryVerificationError(
            "backup payload path is invalid"
        )
    return backup / expected_name


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


def _publish_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a directory without POSIX rename replacement."""

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
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
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
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_with_retries(path: Path, *, attempts: int = 3) -> bool:
    for _attempt in range(attempts):
        try:
            _fsync_directory(path)
        except OSError:
            continue
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
