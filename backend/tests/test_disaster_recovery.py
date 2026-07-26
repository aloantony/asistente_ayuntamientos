from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tarfile
from typing import Mapping, Sequence

import pytest

from app.reference_layers import disaster_recovery as recovery_module
from app.reference_layers.disaster_recovery import (
    BACKUP_PREFIX,
    EXPECTED_STOPPED_SERVICES,
    MAX_OWNERSHIP_ID,
    QUIESCENCE_EVIDENCE_NAME,
    RESTORE_PREFIX,
    CommandResult,
    CreateBackupRequest,
    DisasterRecoveryCommandError,
    DisasterRecoveryError,
    DisasterRecoverySafetyError,
    DisasterRecoveryVerificationError,
    RestoreBackupRequest,
    build_parser,
    create_backup,
    restore_backup,
    verify_backup,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
SOURCE_SECRET = "source-password-that-must-not-leak"
TARGET_SECRET = "target-password-that-must-not-leak"
SOURCE_SYSTEM_IDENTIFIER = "7412345678901234567"
TARGET_SYSTEM_IDENTIFIER = "7523456789012345678"
SOURCE_DATABASE_OID = 16384
TARGET_DATABASE_OID = 24576
DRILL_TOKEN = "123e4567-e89b-42d3-a456-426614174000"
DRILL_MARKER = f"siur-drill-v1:{DRILL_TOKEN}"
SOURCE_EXTENSIONS = [
    {
        "name": "plpgsql",
        "schema": "pg_catalog",
        "version": "1.0",
        "owner": "source_admin",
    },
    {
        "name": "postgis",
        "schema": "public",
        "version": "3.6.4",
        "owner": "source_admin",
    },
    {
        "name": "vector",
        "schema": "public",
        "version": "0.8.2",
        "owner": "source_admin",
    },
]
TARGET_EXTENSIONS = [
    {**item, "owner": "drill_admin"} for item in SOURCE_EXTENSIONS
]


class FakePostgresRunner:
    def __init__(
        self,
        *,
        fail_command: str | None = None,
        mutate_during_dump: Path | None = None,
        target_relation_count: int = 0,
        target_preflight_counts: tuple[int, ...] | None = None,
        race_destination: Path | None = None,
        target_system_identifier: str = TARGET_SYSTEM_IDENTIFIER,
        target_database_oid: int = TARGET_DATABASE_OID,
        target_drill_token: str = DRILL_TOKEN,
        compensation_identity_mismatch: bool = False,
        source_identity_changes_after_dump: bool = False,
        target_contract_flags: tuple[int, ...] | None = None,
        target_extension_available: bool = True,
    ) -> None:
        self.fail_command = fail_command
        self.mutate_during_dump = mutate_during_dump
        self.target_preflight_counts = list(
            target_preflight_counts or (0,) * 12
        )
        if len(self.target_preflight_counts) != 12:
            raise ValueError("target_preflight_counts must have twelve values")
        if target_relation_count:
            self.target_preflight_counts[2] = target_relation_count
        self.race_destination = race_destination
        self.target_system_identifier = target_system_identifier
        self.target_database_oid = target_database_oid
        self.target_drill_token = target_drill_token
        self.compensation_identity_mismatch = (
            compensation_identity_mismatch
        )
        self.source_identity_changes_after_dump = (
            source_identity_changes_after_dump
        )
        self.target_contract_flags = tuple(
            target_contract_flags or (1,) * 6
        )
        if len(self.target_contract_flags) != 6:
            raise ValueError("target_contract_flags must have six values")
        self.dump_created = False
        self.target_extension_available = target_extension_available
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
    ) -> CommandResult:
        command = tuple(argv)
        env = dict(environment)
        self.calls.append((command, env))
        if command[0] == self.fail_command and not (
            command[0] == "pg_restore" and "--list" in command
        ):
            if command[0] == "pg_restore":
                self.target_preflight_counts[2] = 1
            return CommandResult(1, stderr="sensitive upstream diagnostic")
        if command[0] == "pg_dump":
            if self.mutate_during_dump is not None:
                self.mutate_during_dump.write_bytes(b"changed")
            if self.race_destination is not None:
                self.race_destination.mkdir()
                (self.race_destination / "owner-marker").write_text(
                    "must survive",
                    encoding="utf-8",
                )
            output_argument = next(
                value for value in command if value.startswith("--file=")
            )
            output = Path(output_argument.removeprefix("--file="))
            output.write_bytes(b"fake-postgresql-custom-dump")
            output.chmod(0o600)
            self.dump_created = True
        elif command[0] == "psql":
            sql = next(
                (
                    value.removeprefix("--command=")
                    for value in command
                    if value.startswith("--command=")
                ),
                "",
            )
            if "DROP OWNED BY CURRENT_USER CASCADE" in sql:
                if self.compensation_identity_mismatch:
                    return CommandResult(1)
                self.target_preflight_counts = [0] * 12
                return CommandResult(0)
            database_name = env["PGDATABASE"]
            if database_name.startswith("app_drill_"):
                system_identifier = self.target_system_identifier
                database_oid = self.target_database_oid
                marker = f"siur-drill-v1:{self.target_drill_token}"
            else:
                system_identifier = (
                    str(int(SOURCE_SYSTEM_IDENTIFIER) + 1)
                    if (
                        self.source_identity_changes_after_dump
                        and self.dump_created
                    )
                    else SOURCE_SYSTEM_IDENTIFIER
                )
                database_oid = SOURCE_DATABASE_OID
                marker = ""
            if (
                "pg_available_extension_versions" in sql
                and "ORDER BY e.extname" in sql
            ):
                extensions = (
                    TARGET_EXTENSIONS
                    if database_name.startswith("app_drill_")
                    else SOURCE_EXTENSIONS
                )
                available = (
                    self.target_extension_available
                    if database_name.startswith("app_drill_")
                    else True
                )
                return CommandResult(
                    0,
                    stdout="".join(
                        f"{item['name']}|{item['schema']}|"
                        f"{item['version']}|{item['owner']}|"
                        f"{int(available)}\n"
                        for item in extensions
                    ),
                )
            if "pg_stat_activity" not in sql:
                return CommandResult(
                    0,
                    stdout=(
                        f"{database_name}|{env['PGUSER']}|"
                        f"{system_identifier}|{database_oid}|{marker}\n"
                    ),
                )
            return CommandResult(
                0,
                stdout=(
                    f"{database_name}|{env['PGUSER']}|"
                    f"{system_identifier}|{database_oid}|{marker}|"
                    + "|".join(
                        str(value) for value in self.target_contract_flags
                    )
                    + "|"
                    + "|".join(
                        str(value)
                        for value in self.target_preflight_counts
                    )
                    + "\n"
                ),
            )
        return CommandResult(0, stdout="verified\n")


def make_sources(tmp_path: Path) -> tuple[Path, Path]:
    reference = tmp_path / "reference"
    geoserver = tmp_path / "geoserver"
    (reference / "blobs" / "sha256" / "ab").mkdir(parents=True)
    (reference / "blobs" / "sha256" / "ab" / ("ab" * 32)).write_bytes(
        b"immutable-reference-artifact"
    )
    (reference / "metadata").mkdir()
    (reference / "metadata" / "catalog.json").write_text(
        '{"catalog":"local"}',
        encoding="utf-8",
    )
    (reference / "staging").mkdir()
    (reference / "staging" / "partial.bin").write_bytes(b"partial")
    (reference / ".reference-blob-store.lock").write_bytes(b"lock")

    (geoserver / "workspaces" / "siur").mkdir(parents=True)
    (geoserver / "workspaces" / "siur" / "workspace.xml").write_text(
        "<workspace/>",
        encoding="utf-8",
    )
    (geoserver / "gwc").mkdir()
    (geoserver / "gwc" / "geowebcache.xml").write_text(
        "<gwc-configuration/>",
        encoding="utf-8",
    )
    (geoserver / "gwc-layers").mkdir()
    (geoserver / "gwc-layers" / "siur.xml").write_text(
        "<layer-configuration/>",
        encoding="utf-8",
    )
    (geoserver / "gwc-gs.xml").write_text(
        "<global-configuration/>",
        encoding="utf-8",
    )
    (geoserver / "gwc-cache").mkdir()
    return reference, geoserver


def private_database_file(
    tmp_path: Path,
    *,
    name: str,
    password: str,
) -> Path:
    path = tmp_path / f"{name}.url"
    path.write_text(
        f"postgresql://app:{password}@127.0.0.1:55432/{name}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def evidence_file(
    tmp_path: Path,
    *,
    reference: Path,
    geoserver: Path,
    captured_at: datetime = NOW,
) -> Path:
    path = tmp_path / "quiescence.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "captured_at": captured_at.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "operator": "codex-test",
                "reference_artifacts_source": str(reference),
                "geoserver_data_source": str(geoserver),
                "postgres_database_name": "app",
                "postgres_database_user": "app",
                "postgres_hostname": "127.0.0.1",
                "postgres_port": 55432,
                "postgres_system_identifier": SOURCE_SYSTEM_IDENTIFIER,
                "postgres_database_oid": SOURCE_DATABASE_OID,
                "postgres_extensions": SOURCE_EXTENSIONS,
                "stopped_services": sorted(EXPECTED_STOPPED_SERVICES),
                "postgres_running": True,
            }
        ),
        encoding="utf-8",
    )
    return path


def drill_identity_file(
    tmp_path: Path,
    *,
    database_name: str,
    system_identifier: str = TARGET_SYSTEM_IDENTIFIER,
    database_oid: int = TARGET_DATABASE_OID,
    drill_token: str = DRILL_TOKEN,
    extensions: list[dict[str, str]] | None = None,
) -> Path:
    path = tmp_path / f"{database_name}.identity.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "database_name": database_name,
                "database_user": "app",
                "system_identifier": system_identifier,
                "database_oid": database_oid,
                "drill_token": drill_token,
                "extensions": extensions or TARGET_EXTENSIONS,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def restore_request(
    tmp_path: Path,
    *,
    backup: Path,
    database_name: str,
    destination_name: str,
) -> RestoreBackupRequest:
    return RestoreBackupRequest(
        backup=backup,
        destination=tmp_path / destination_name,
        target_database_url_file=private_database_file(
            tmp_path,
            name=database_name,
            password=TARGET_SECRET,
        ),
        target_database_identity_file=drill_identity_file(
            tmp_path,
            database_name=database_name,
        ),
    )


def rewrite_manifest(
    backup: Path,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    manifest = json.loads(
        (backup / "manifest.json").read_text(encoding="ascii")
    )
    mutate(manifest)
    payload = (
        json.dumps(
            manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    (backup / "manifest.json").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (backup / "manifest.sha256").write_text(
        f"{digest}  manifest.json\n",
        encoding="ascii",
    )


def downgrade_backup_to_schema_v2(backup: Path) -> None:
    evidence_path = backup / QUIESCENCE_EVIDENCE_NAME
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["schema_version"] = 1
    for key in (
        "postgres_database_name",
        "postgres_database_user",
        "postgres_hostname",
        "postgres_port",
        "postgres_system_identifier",
        "postgres_database_oid",
        "postgres_extensions",
    ):
        evidence.pop(key)
    evidence_payload = json.dumps(evidence).encode("utf-8")
    evidence_path.write_bytes(evidence_payload)

    def mutate(manifest: dict[str, object]) -> None:
        manifest["schema_version"] = 2
        consistency = manifest["consistency"]  # type: ignore[assignment]
        for key in (
            "postgres_database_name",
            "postgres_database_user",
            "postgres_hostname",
            "postgres_port",
            "postgres_system_identifier",
            "postgres_database_oid",
            "postgres_extensions",
        ):
            consistency.pop(key)  # type: ignore[union-attr]
        consistency["quiescence_evidence"] = {  # type: ignore[index]
            "path": QUIESCENCE_EVIDENCE_NAME,
            "size_bytes": len(evidence_payload),
            "sha256": hashlib.sha256(evidence_payload).hexdigest(),
        }
        trees = manifest["trees"]  # type: ignore[assignment]
        for tree in trees.values():  # type: ignore[union-attr]
            tree.pop("root_ctime_ns")
            tree.pop("root_links")
            for entry in tree["entries"]:
                entry.pop("ctime_ns")
                entry.pop("links")
        restore_contract = manifest["restore_contract"]  # type: ignore[assignment]
        restore_contract.pop("stable_identity_required")  # type: ignore[union-attr]
        restore_contract.pop("connection_limit")  # type: ignore[union-attr]
        restore_contract.pop("connection_exclusivity")  # type: ignore[union-attr]
        restore_contract.pop("extensions")  # type: ignore[union-attr]
        database = manifest["database"]  # type: ignore[assignment]
        database.pop("required_extensions")  # type: ignore[union-attr]
        database["pg_dump_contract"] = [  # type: ignore[index]
            "--format=custom",
            "--serializable-deferrable",
            "--no-owner",
            "--no-privileges",
        ]

    rewrite_manifest(backup, mutate)


def create_request(
    tmp_path: Path,
    *,
    reference: Path,
    geoserver: Path,
    with_evidence: bool,
) -> CreateBackupRequest:
    return CreateBackupRequest(
        destination=tmp_path / f"{BACKUP_PREFIX}20260726T120000Z",
        reference_artifacts_source=reference,
        geoserver_data_source=geoserver,
        database_url_file=private_database_file(
            tmp_path,
            name="app",
            password=SOURCE_SECRET,
        ),
        quiescence_evidence=(
            evidence_file(
                tmp_path,
                reference=reference,
                geoserver=geoserver,
            )
            if with_evidence
            else None
        ),
    )


def completed_backup(
    tmp_path: Path,
) -> tuple[Path, FakePostgresRunner, Path, Path]:
    reference, geoserver = make_sources(tmp_path)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )
    runner = FakePostgresRunner()
    create_backup(request, apply=True, runner=runner, now=NOW)
    return request.destination, runner, reference, geoserver


def test_create_defaults_to_read_only_dry_run_and_inventories_exclusions(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=False,
    )
    runner = FakePostgresRunner()

    result = create_backup(request, runner=runner, now=NOW)

    assert result["mode"] == "dry-run"
    assert result["reference_artifacts"]["files"] == 2  # type: ignore[index]
    assert result["reference_artifacts"]["excluded_paths"] == [  # type: ignore[index]
        ".reference-blob-store.lock",
        "staging",
    ]
    assert result["geoserver_data"]["excluded_paths"] == [  # type: ignore[index]
        "gwc-cache"
    ]
    assert {  # type: ignore[index]
        item["component"]
        for item in result["excluded_reconstructible_state"]
    } == {"redis", "geowebcache"}
    assert not request.destination.exists()
    assert runner.calls == []
    parsed = build_parser().parse_args(
        [
            "create",
            "--destination",
            str(request.destination),
            "--reference-artifacts-source",
            str(reference),
            "--geoserver-data-source",
            str(geoserver),
            "--database-url-file",
            str(request.database_url_file),
        ]
    )
    assert parsed.apply is False


def test_create_apply_writes_hashed_manifest_and_verifiable_payloads(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)

    assert backup.is_dir()
    assert sorted(path.name for path in backup.iterdir()) == [
        "geoserver_data.tar",
        "manifest.json",
        "manifest.sha256",
        "postgres.dump",
        QUIESCENCE_EVIDENCE_NAME,
        "reference_artifacts.tar",
    ]
    manifest_text = (backup / "manifest.json").read_text(encoding="ascii")
    assert SOURCE_SECRET not in manifest_text
    manifest = json.loads(manifest_text)
    assert manifest["database"]["name"] == "app"
    assert {
        item["component"]
        for item in manifest["excluded_reconstructible_state"]
    } == {"redis", "geowebcache"}
    assert manifest["trees"]["reference_artifacts"]["excluded_paths"] == [
        ".reference-blob-store.lock",
        "staging",
    ]
    assert manifest["schema_version"] == 3
    assert manifest["consistency"]["postgres_system_identifier"] == (
        SOURCE_SYSTEM_IDENTIFIER
    )
    assert manifest["consistency"]["postgres_database_oid"] == (
        SOURCE_DATABASE_OID
    )
    assert manifest["consistency"]["postgres_extensions"] == (
        SOURCE_EXTENSIONS
    )
    assert manifest["database"]["required_extensions"] == [
        {key: item[key] for key in ("name", "schema", "version")}
        for item in SOURCE_EXTENSIONS
    ]
    assert manifest["database"]["pg_dump_contract"] == [
        "--format=custom",
        "--serializable-deferrable",
        "--no-owner",
        "--no-privileges",
        "--exclude-extension=plpgsql",
        "--exclude-extension=postgis",
        "--exclude-extension=vector",
    ]
    assert manifest["restore_contract"]["extensions"] == {
        "preinstalled_by_administrator": True,
        "exact_versions_required": True,
        "owners_must_differ_from_restore_role": True,
        "restore_comments": False,
    }
    assert manifest["trees"]["geoserver_data"]["excluded_paths"] == [
        "gwc-cache"
    ]
    evidence = (backup / QUIESCENCE_EVIDENCE_NAME).read_bytes()
    assert evidence == (
        tmp_path / "quiescence.json"
    ).read_bytes()
    assert (
        manifest["consistency"]["quiescence_evidence"]["sha256"]
        == hashlib.sha256(evidence).hexdigest()
    )
    assert (
        manifest["excluded_reconstructible_state"][1][
            "configuration_included"
        ]
        is True
    )
    assert (
        sum(
            command[0] == "psql"
            and environment.get("PGDATABASE") == "app"
            for command, environment in runner.calls
        )
        == 6
    )
    dump_command = next(
        command for command, _environment in runner.calls
        if command[0] == "pg_dump"
    )
    assert {
        "--exclude-extension=plpgsql",
        "--exclude-extension=postgis",
        "--exclude-extension=vector",
    }.issubset(dump_command)

    report = verify_backup(backup, runner=runner)

    assert report["verified"] is True
    assert report["database_name"] == "app"
    assert any(call[0][:2] == ("pg_restore", "--list") for call in runner.calls)
    assert all(
        SOURCE_SECRET not in " ".join(command)
        for command, _environment in runner.calls
    )
    assert any(
        environment.get("PGPASSWORD") == SOURCE_SECRET
        for _command, environment in runner.calls
    )


def test_declared_exclusions_verify_even_when_runtime_paths_are_absent(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    (reference / "staging" / "partial.bin").unlink()
    (reference / "staging").rmdir()
    (reference / ".reference-blob-store.lock").unlink()
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )
    runner = FakePostgresRunner()

    create_backup(request, apply=True, runner=runner, now=NOW)
    report = verify_backup(request.destination, runner=runner)

    assert report["verified"] is True
    manifest = json.loads(
        (request.destination / "manifest.json").read_text(encoding="ascii")
    )
    assert manifest["trees"]["reference_artifacts"]["excluded_paths"] == [
        ".reference-blob-store.lock",
        "staging",
    ]


def test_verify_detects_payload_tampering(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)
    with (backup / "reference_artifacts.tar").open("ab") as archive:
        archive.write(b"tamper")

    with pytest.raises(DisasterRecoveryError):
        verify_backup(backup, runner=runner)


def test_verify_detects_exact_quiescence_evidence_tampering(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)
    with (backup / QUIESCENCE_EVIDENCE_NAME).open("ab") as evidence:
        evidence.write(b"\n")

    with pytest.raises(DisasterRecoveryVerificationError):
        verify_backup(backup, runner=runner)


def test_verify_remains_compatible_with_schema_v2_backup(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)
    downgrade_backup_to_schema_v2(backup)

    report = verify_backup(backup, runner=runner)

    assert report["verified"] is True
    assert report["database_name"] == "app"
    assert report["required_extensions"] is None


def test_restore_rejects_legacy_backup_without_extension_baseline(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)
    downgrade_backup_to_schema_v2(backup)
    request = restore_request(
        tmp_path,
        backup=backup,
        database_name="app_drill_legacy",
        destination_name=f"{RESTORE_PREFIX}legacy",
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="lacks a verified extension baseline",
    ):
        restore_backup(request, runner=runner)

    assert not request.destination.exists()


@pytest.mark.parametrize(
    "field",
    ["entry_ctime", "entry_links", "root_ctime", "root_links"],
)
def test_schema_v3_requires_complete_filesystem_identity(
    tmp_path: Path,
    field: str,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)

    def mutate(manifest: dict[str, object]) -> None:
        tree = manifest["trees"]["reference_artifacts"]  # type: ignore[index]
        if field == "entry_ctime":
            tree["entries"][0].pop("ctime_ns")
        elif field == "entry_links":
            tree["entries"][0].pop("links")
        elif field == "root_ctime":
            tree.pop("root_ctime_ns")
        else:
            tree.pop("root_links")

    rewrite_manifest(backup, mutate)

    with pytest.raises(DisasterRecoveryVerificationError):
        verify_backup(backup, runner=runner)


def test_schema_v3_database_identity_must_match_dump_declaration(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)

    def mutate(manifest: dict[str, object]) -> None:
        manifest["consistency"]["postgres_database_name"] = (  # type: ignore[index]
            "other_database"
        )

    rewrite_manifest(backup, mutate)

    with pytest.raises(DisasterRecoveryVerificationError):
        verify_backup(backup, runner=runner)


def test_schema_v3_extension_baseline_must_match_source_identity(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)

    def mutate(manifest: dict[str, object]) -> None:
        required = manifest["database"]["required_extensions"]  # type: ignore[index]
        required[-1]["version"] = "0.8.0"

    rewrite_manifest(backup, mutate)

    with pytest.raises(
        DisasterRecoveryVerificationError,
        match="extension baseline does not match",
    ):
        verify_backup(backup, runner=runner)


def test_schema_v3_requires_extension_exclusions_in_dump_contract(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)

    def mutate(manifest: dict[str, object]) -> None:
        contract = manifest["database"]["pg_dump_contract"]  # type: ignore[index]
        contract.remove("--exclude-extension=postgis")

    rewrite_manifest(backup, mutate)

    with pytest.raises(
        DisasterRecoveryVerificationError,
        match="PostgreSQL contract is invalid",
    ):
        verify_backup(backup, runner=runner)


@pytest.mark.parametrize("invalid_owner", [-1, MAX_OWNERSHIP_ID + 1, True])
def test_verify_rejects_out_of_range_manifest_ownership(
    tmp_path: Path,
    invalid_owner: object,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)

    def mutate(manifest: dict[str, object]) -> None:
        trees = manifest["trees"]
        trees["reference_artifacts"]["root_uid"] = invalid_owner  # type: ignore[index]

    rewrite_manifest(backup, mutate)

    with pytest.raises(DisasterRecoveryVerificationError):
        verify_backup(backup, runner=runner)


def test_verify_rejects_unmanifested_backup_entries(
    tmp_path: Path,
) -> None:
    backup, runner, _reference, _geoserver = completed_backup(tmp_path)
    (backup / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(DisasterRecoveryVerificationError):
        verify_backup(backup, runner=runner)


def test_create_apply_requires_fresh_bound_quiescence_evidence(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    missing = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=False,
    )
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(
            missing,
            apply=True,
            runner=FakePostgresRunner(),
            now=NOW,
        )

    stale_path = evidence_file(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        captured_at=NOW - timedelta(hours=2),
    )
    stale = CreateBackupRequest(
        **{
            **missing.__dict__,
            "quiescence_evidence": stale_path,
        }
    )
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(
            stale,
            apply=True,
            runner=FakePostgresRunner(),
            now=NOW,
        )


def test_create_apply_rejects_quiescence_database_identity_mismatch(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )
    evidence_path = request.quiescence_evidence
    assert evidence_path is not None
    evidence = json.loads(
        evidence_path.read_text(encoding="utf-8")
    )
    evidence["postgres_database_oid"] = SOURCE_DATABASE_OID + 1
    evidence_path.write_text(
        json.dumps(evidence),
        encoding="utf-8",
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="live PostgreSQL identity",
    ):
        create_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(),
            now=NOW,
        )


def test_create_rejects_symlinks_existing_destinations_and_overlap(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    (reference / "unsafe").symlink_to(
        reference / "metadata" / "catalog.json"
    )
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=False,
    )
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(request, now=NOW)

    (reference / "unsafe").unlink()
    request.destination.mkdir()
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(request, now=NOW)

    overlapping = CreateBackupRequest(
        **{
            **request.__dict__,
            "destination": reference / f"{BACKUP_PREFIX}inside-source",
        }
    )
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(overlapping, now=NOW)


def test_create_rejects_nonempty_legacy_geowebcache_mountpoint(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    (geoserver / "gwc-cache" / "geowebcache.xml").write_text(
        "<legacy-mixed-volume/>",
        encoding="utf-8",
    )
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=False,
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="legacy configuration or cache data",
    ):
        create_backup(request, now=NOW)


def test_create_rejects_special_permission_bits_on_tree_root(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    reference.chmod(0o2770)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=False,
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="source root",
    ):
        create_backup(request, now=NOW)


def test_archive_reopen_rejects_swapped_source_ancestor(
    tmp_path: Path,
) -> None:
    reference, _geoserver = make_sources(tmp_path)
    inventory = recovery_module._inventory_tree(
        reference,
        excluded_roots=frozenset(
            {"staging", ".reference-blob-store.lock"}
        ),
    )
    moved = tmp_path / "moved-metadata"
    (reference / "metadata").rename(moved)
    (reference / "metadata").symlink_to(moved, target_is_directory=True)

    with pytest.raises(DisasterRecoverySafetyError):
        recovery_module._write_inventory_tar(
            inventory,
            tmp_path / "unsafe.tar",
        )


def test_failed_apply_never_publishes_destination_or_deletes_partial(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )

    with pytest.raises(DisasterRecoveryCommandError) as captured:
        create_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(fail_command="pg_dump"),
            now=NOW,
        )

    assert SOURCE_SECRET not in str(captured.value)
    assert not request.destination.exists()
    partials = list(
        tmp_path.glob(f".{request.destination.name}.partial-*")
    )
    assert len(partials) == 1
    assert partials[0].is_dir()


def test_atomic_publication_never_replaces_a_destination_created_by_race(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )

    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(
                race_destination=request.destination,
            ),
            now=NOW,
        )

    assert (request.destination / "owner-marker").read_text(
        encoding="utf-8"
    ) == "must survive"
    assert len(
        list(tmp_path.glob(f".{request.destination.name}.partial-*"))
    ) == 1


def test_atomic_publication_uses_one_open_parent_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".partial-source"
    destination = tmp_path / "published"
    source.mkdir()
    captured: dict[str, object] = {}

    def capture_rename(
        parent_descriptor: int,
        source_name: str,
        destination_name: str,
    ) -> None:
        captured["parent_is_directory"] = os.path.isdir(
            f"/proc/self/fd/{parent_descriptor}"
        )
        captured["source_name"] = source_name
        captured["destination_name"] = destination_name

    monkeypatch.setattr(
        recovery_module,
        "_rename_directory_noreplace",
        capture_rename,
    )

    durable = recovery_module._publish_directory_noreplace(
        source,
        destination,
    )

    assert durable is True
    assert captured == {
        "parent_is_directory": True,
        "source_name": ".partial-source",
        "destination_name": "published",
    }


def test_post_rename_close_error_reports_completed_but_not_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".partial-close"
    destination = tmp_path / "published-close"
    source.mkdir()
    real_close = recovery_module.os.close
    published_parent: list[int] = []

    def capture_rename(
        parent_descriptor: int,
        _source_name: str,
        _destination_name: str,
    ) -> None:
        published_parent.append(parent_descriptor)

    def fail_final_close(descriptor: int) -> None:
        if published_parent and descriptor == published_parent[0]:
            raise OSError("injected close failure")
        real_close(descriptor)

    monkeypatch.setattr(
        recovery_module,
        "_rename_directory_noreplace",
        capture_rename,
    )
    monkeypatch.setattr(recovery_module.os, "close", fail_final_close)
    try:
        durable = recovery_module._publish_directory_noreplace(
            source,
            destination,
        )
    finally:
        if published_parent:
            real_close(published_parent[0])

    assert durable is False


def test_post_rename_interruption_reports_completed_but_not_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".partial-interrupted"
    destination = tmp_path / "published-interrupted"
    source.mkdir()
    monkeypatch.setattr(
        recovery_module,
        "_rename_directory_noreplace",
        lambda _descriptor, _source, _destination: None,
    )

    def interrupt_fsync(_descriptor: int) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        recovery_module,
        "_fsync_descriptor_with_retries",
        interrupt_fsync,
    )

    assert (
        recovery_module._publish_directory_noreplace(source, destination)
        is False
    )


def test_source_mutation_aborts_consistency_snapshot(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )
    mutable = reference / "metadata" / "catalog.json"

    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(mutate_during_dump=mutable),
            now=NOW,
        )

    assert not request.destination.exists()


def test_source_database_identity_change_aborts_snapshot(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="PostgreSQL identity changed",
    ):
        create_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(
                source_identity_changes_after_dump=True,
            ),
            now=NOW,
        )

    assert not request.destination.exists()


def test_restore_dry_run_verifies_but_does_not_connect_or_write(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    target_file = private_database_file(
        tmp_path,
        name="app_drill_preview",
        password=TARGET_SECRET,
    )
    destination = tmp_path / f"{RESTORE_PREFIX}preview"
    runner = FakePostgresRunner()

    report = restore_backup(
        RestoreBackupRequest(
            backup=backup,
            destination=destination,
            target_database_url_file=target_file,
            target_database_identity_file=drill_identity_file(
                tmp_path,
                database_name="app_drill_preview",
            ),
        ),
        runner=runner,
        now=NOW,
    )

    assert report["mode"] == "dry-run"
    assert report["target_database_name"] == "app_drill_preview"
    assert not destination.exists()
    assert [call[0][0] for call in runner.calls] == ["pg_restore"]
    assert all(
        TARGET_SECRET not in " ".join(command)
        for command, _environment in runner.calls
    )
    parsed = build_parser().parse_args(
        [
            "restore",
            "--backup",
            str(backup),
            "--destination",
            str(destination),
            "--target-database-url-file",
            str(target_file),
            "--target-database-identity-file",
            str(
                drill_identity_file(
                    tmp_path,
                    database_name="app_drill_preview",
                )
            ),
        ]
    )
    assert parsed.apply is False


def test_restore_rejects_target_identity_with_wrong_extension_version(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    database_name = "app_drill_wrong_vector"
    target_extensions = [
        {**item, "version": "0.8.0"}
        if item["name"] == "vector"
        else item
        for item in TARGET_EXTENSIONS
    ]
    runner = FakePostgresRunner()
    request = RestoreBackupRequest(
        backup=backup,
        destination=tmp_path / f"{RESTORE_PREFIX}wrong-vector",
        target_database_url_file=private_database_file(
            tmp_path,
            name=database_name,
            password=TARGET_SECRET,
        ),
        target_database_identity_file=drill_identity_file(
            tmp_path,
            database_name=database_name,
            extensions=target_extensions,
        ),
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="exact source extension versions",
    ):
        restore_backup(request, runner=runner)

    assert [call[0][0] for call in runner.calls] == ["pg_restore"]
    assert not request.destination.exists()


def test_restore_rejects_extensions_owned_by_restore_role(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    database_name = "app_drill_role_owned_extensions"
    target_extensions = [
        {**item, "owner": "app"} for item in TARGET_EXTENSIONS
    ]
    request = RestoreBackupRequest(
        backup=backup,
        destination=tmp_path / f"{RESTORE_PREFIX}role-owned-extensions",
        target_database_url_file=private_database_file(
            tmp_path,
            name=database_name,
            password=TARGET_SECRET,
        ),
        target_database_identity_file=drill_identity_file(
            tmp_path,
            database_name=database_name,
            extensions=target_extensions,
        ),
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="identity file is invalid",
    ):
        restore_backup(request, runner=FakePostgresRunner())

    assert not request.destination.exists()


def test_restore_apply_rejects_unavailable_exact_extension_version(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    request = restore_request(
        tmp_path,
        backup=backup,
        database_name="app_drill_unavailable_extension",
        destination_name=f"{RESTORE_PREFIX}unavailable-extension",
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="extension version is unavailable",
    ):
        restore_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(target_extension_available=False),
        )

    assert not request.destination.exists()


def test_restore_apply_extracts_only_to_new_drill_path_and_empty_database(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    target_file = private_database_file(
        tmp_path,
        name="app_drill_20260726",
        password=TARGET_SECRET,
    )
    destination = tmp_path / f"{RESTORE_PREFIX}20260726"
    runner = FakePostgresRunner()

    report = restore_backup(
        RestoreBackupRequest(
            backup=backup,
            destination=destination,
            target_database_url_file=target_file,
            target_database_identity_file=drill_identity_file(
                tmp_path,
                database_name="app_drill_20260726",
            ),
        ),
        apply=True,
        runner=runner,
        now=NOW,
    )

    assert report["completed"] is True
    assert (
        destination
        / "reference_artifacts"
        / "metadata"
        / "catalog.json"
    ).read_text(encoding="utf-8") == '{"catalog":"local"}'
    assert not (destination / "reference_artifacts" / "staging").exists()
    assert (
        destination
        / "geoserver_data"
        / "workspaces"
        / "siur"
        / "workspace.xml"
    ).is_file()
    assert (
        destination
        / "geoserver_data"
        / "gwc"
        / "geowebcache.xml"
    ).is_file()
    assert (
        destination
        / "geoserver_data"
        / "gwc-layers"
        / "siur.xml"
    ).is_file()
    assert not (destination / "geoserver_data" / "gwc-cache").exists()
    assert (destination / "restore-report.json").is_file()
    assert report["durability_verified"] is True
    commands = [command for command, _environment in runner.calls]
    assert any(command[0] == "psql" for command in commands)
    restore_commands = [
        command
        for command in commands
        if command[0] == "pg_restore" and "--list" not in command
    ]
    assert len(restore_commands) == 1
    assert "--single-transaction" in restore_commands[0]
    assert "--no-comments" in restore_commands[0]
    assert "--dbname=app_drill_20260726" in restore_commands[0]
    assert not any(
        option in restore_commands[0]
        for option in ("--clean", "--create", "--if-exists")
    )
    assert all(
        TARGET_SECRET not in " ".join(command) for command in commands
    )
    assert any(
        environment.get("PGPASSWORD") == TARGET_SECRET
        for _command, environment in runner.calls
    )
    preflight_sql = next(
        argument
        for command in commands
        if command[0] == "psql"
        for argument in command
        if (
            argument.startswith("--command=")
            and "pg_stat_activity" in argument
        )
    )
    assert "WITH RECURSIVE extension_objects" in preflight_sql
    assert "d.deptype IN ('a', 'i')" in preflight_sql


def test_restore_uses_manifest_ownership_not_canonical_tar_fields(
    tmp_path: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("arbitrary ownership restoration requires root")
    reference, geoserver = make_sources(tmp_path)
    owned_file = reference / "metadata" / "catalog.json"
    os.chown(owned_file, 12345, 23456)
    request = create_request(
        tmp_path,
        reference=reference,
        geoserver=geoserver,
        with_evidence=True,
    )
    runner = FakePostgresRunner()
    create_backup(request, apply=True, runner=runner, now=NOW)
    with tarfile.open(
        request.destination / "reference_artifacts.tar",
        mode="r:",
    ) as archive:
        member = archive.getmember("metadata/catalog.json")
        assert (member.uid, member.gid, member.uname, member.gname) == (
            0,
            0,
            "",
            "",
        )
    destination = tmp_path / f"{RESTORE_PREFIX}ownership"
    target = private_database_file(
        tmp_path,
        name="app_drill_ownership",
        password=TARGET_SECRET,
    )

    restore_backup(
        RestoreBackupRequest(
            backup=request.destination,
            destination=destination,
            target_database_url_file=target,
            target_database_identity_file=drill_identity_file(
                tmp_path,
                database_name="app_drill_ownership",
            ),
        ),
        apply=True,
        runner=runner,
        now=NOW,
    )

    restored = (
        destination
        / "reference_artifacts"
        / "metadata"
        / "catalog.json"
    ).stat()
    assert (restored.st_uid, restored.st_gid) == (12345, 23456)


def test_restore_refuses_non_drill_or_nonempty_database_and_existing_path(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    unsafe_database = private_database_file(
        tmp_path,
        name="production",
        password=TARGET_SECRET,
    )
    request = RestoreBackupRequest(
        backup=backup,
        destination=tmp_path / f"{RESTORE_PREFIX}unsafe",
        target_database_url_file=unsafe_database,
        target_database_identity_file=drill_identity_file(
            tmp_path,
            database_name="production",
        ),
    )
    with pytest.raises(DisasterRecoverySafetyError):
        restore_backup(request, runner=FakePostgresRunner())

    target = private_database_file(
        tmp_path,
        name="app_drill_nonempty",
        password=TARGET_SECRET,
    )
    nonempty = RestoreBackupRequest(
        backup=backup,
        destination=tmp_path / f"{RESTORE_PREFIX}nonempty",
        target_database_url_file=target,
        target_database_identity_file=drill_identity_file(
            tmp_path,
            database_name="app_drill_nonempty",
        ),
    )
    with pytest.raises(DisasterRecoverySafetyError):
        restore_backup(
            nonempty,
            apply=True,
            runner=FakePostgresRunner(target_relation_count=1),
        )
    assert not nonempty.destination.exists()

    existing = tmp_path / f"{RESTORE_PREFIX}existing"
    existing.mkdir()
    with pytest.raises(DisasterRecoverySafetyError):
        restore_backup(
            RestoreBackupRequest(
                backup=backup,
                destination=existing,
                target_database_url_file=target,
                target_database_identity_file=drill_identity_file(
                    tmp_path,
                    database_name="app_drill_nonempty",
                ),
            ),
            runner=FakePostgresRunner(),
        )


def test_restore_apply_requires_exact_persisted_database_identity(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    database_name = "app_drill_identity_mismatch"
    request = RestoreBackupRequest(
        backup=backup,
        destination=tmp_path / f"{RESTORE_PREFIX}identity-mismatch",
        target_database_url_file=private_database_file(
            tmp_path,
            name=database_name,
            password=TARGET_SECRET,
        ),
        target_database_identity_file=drill_identity_file(
            tmp_path,
            database_name=database_name,
            system_identifier=str(int(TARGET_SYSTEM_IDENTIFIER) + 1),
        ),
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="identity changed",
    ):
        restore_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(),
        )

    assert not request.destination.exists()


@pytest.mark.parametrize(
    "index",
    range(6),
    ids=[
        "database-owner",
        "database-connection-limit",
        "database-accepts-connections",
        "owner-database-privileges",
        "dedicated-constrained-role",
        "independent-plpgsql-owner",
    ],
)
def test_restore_preflight_rejects_every_fixed_target_contract_flag(
    tmp_path: Path,
    index: int,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    request = restore_request(
        tmp_path,
        backup=backup,
        database_name=f"app_drill_contract_{index}",
        destination_name=f"{RESTORE_PREFIX}contract-{index}",
    )
    flags = [1] * 6
    flags[index] = 0

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="expected empty drill",
    ):
        restore_backup(
            request,
            apply=True,
            runner=FakePostgresRunner(
                target_contract_flags=tuple(flags),
            ),
        )

    assert not request.destination.exists()


@pytest.mark.parametrize(
    "index",
    range(12),
    ids=[
        "concurrent-session",
        "schema",
        "relation",
        "function",
        "type",
        "extension-owned-by-drill-role",
        "other-object",
        "database-acl",
        "public-schema-acl",
        "database-role-setting",
        "database-security-label",
        "local-security-label",
    ],
)
def test_restore_preflight_rejects_every_nonempty_database_category(
    tmp_path: Path,
    index: int,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    target = private_database_file(
        tmp_path,
        name=f"app_drill_{index}",
        password=TARGET_SECRET,
    )
    counts = [0] * 12
    counts[index] = 1

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="expected empty drill",
    ):
        restore_backup(
            RestoreBackupRequest(
                backup=backup,
                destination=tmp_path / f"{RESTORE_PREFIX}{index}",
                target_database_url_file=target,
                target_database_identity_file=drill_identity_file(
                    tmp_path,
                    database_name=f"app_drill_{index}",
                ),
            ),
            apply=True,
            runner=FakePostgresRunner(
                target_preflight_counts=tuple(counts)
            ),
        )

def test_post_restore_publication_failure_compensates_only_exact_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    target = private_database_file(
        tmp_path,
        name="app_drill_compensated",
        password=TARGET_SECRET,
    )
    destination = tmp_path / f"{RESTORE_PREFIX}compensated"
    runner = FakePostgresRunner()

    def reject_publication(_source: Path, _destination: Path) -> None:
        raise DisasterRecoverySafetyError("injected publication failure")

    monkeypatch.setattr(
        "app.reference_layers.disaster_recovery."
        "_publish_directory_noreplace",
        reject_publication,
    )

    with pytest.raises(
        DisasterRecoverySafetyError,
        match="injected publication failure",
    ):
        restore_backup(
            RestoreBackupRequest(
                backup=backup,
                destination=destination,
                target_database_url_file=target,
                target_database_identity_file=drill_identity_file(
                    tmp_path,
                    database_name="app_drill_compensated",
                ),
            ),
            apply=True,
            runner=runner,
            now=NOW,
        )

    commands = [command for command, _environment in runner.calls]
    restore_index = next(
        index
        for index, command in enumerate(commands)
        if command[0] == "pg_restore" and "--list" not in command
    )
    compensation_index = next(
        index
        for index, command in enumerate(commands)
        if any(
            "DROP OWNED BY CURRENT_USER CASCADE" in argument
            for argument in command
        )
    )
    assert compensation_index > restore_index
    compensation_sql = next(
        argument
        for argument in commands[compensation_index]
        if argument.startswith("--command=")
    )
    assert "CREATE SCHEMA IF NOT EXISTS public" in compensation_sql
    assert "pg_available_extension_versions" in compensation_sql
    assert commands[-1][0] == "psql"
    assert not any(
        "DROP DATABASE" in argument or "DROP SCHEMA" in argument
        for command in commands
        for argument in command
    )
    assert all(
        environment.get("PGDATABASE") == "app_drill_compensated"
        for command, environment in runner.calls
        if command[0] == "psql"
    )
    assert not destination.exists()
    assert len(list(tmp_path.glob(f".{destination.name}.partial-*"))) == 1


def test_failed_pg_restore_is_treated_as_mutating_and_compensated(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    request = restore_request(
        tmp_path,
        backup=backup,
        database_name="app_drill_failed_restore",
        destination_name=f"{RESTORE_PREFIX}failed-restore",
    )
    runner = FakePostgresRunner(fail_command="pg_restore")

    with pytest.raises(
        DisasterRecoveryCommandError,
        match="drill restore command failed",
    ):
        restore_backup(
            request,
            apply=True,
            runner=runner,
            now=NOW,
        )

    commands = [command for command, _environment in runner.calls]
    restore_index = next(
        index
        for index, command in enumerate(commands)
        if command[0] == "pg_restore" and "--list" not in command
    )
    compensation_index = next(
        index
        for index, command in enumerate(commands)
        if any(
            "DROP OWNED BY CURRENT_USER CASCADE" in argument
            for argument in command
        )
    )
    assert compensation_index > restore_index
    assert runner.target_preflight_counts == [0] * 12
    assert not request.destination.exists()


def test_compensation_fails_closed_before_drop_on_identity_mismatch(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    request = restore_request(
        tmp_path,
        backup=backup,
        database_name="app_drill_guarded_compensation",
        destination_name=f"{RESTORE_PREFIX}guarded-compensation",
    )
    runner = FakePostgresRunner(
        fail_command="pg_restore",
        compensation_identity_mismatch=True,
    )

    with pytest.raises(
        DisasterRecoveryCommandError,
        match="compensation could not be proven",
    ):
        restore_backup(
            request,
            apply=True,
            runner=runner,
            now=NOW,
        )

    compensation_sql = next(
        argument.removeprefix("--command=")
        for command, _environment in runner.calls
        for argument in command
        if "DROP OWNED BY CURRENT_USER CASCADE" in argument
    )
    assert compensation_sql.index("SIUR drill identity mismatch") < (
        compensation_sql.index("DROP OWNED BY CURRENT_USER CASCADE")
    )
    assert runner.target_preflight_counts[2] == 1
    assert not request.destination.exists()


def test_parent_fsync_failure_after_atomic_publication_reports_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    target = private_database_file(
        tmp_path,
        name="app_drill_durability",
        password=TARGET_SECRET,
    )
    destination = tmp_path / f"{RESTORE_PREFIX}durability"
    monkeypatch.setattr(
        "app.reference_layers.disaster_recovery."
        "_fsync_descriptor_with_retries",
        lambda _descriptor: False,
    )

    report = restore_backup(
        RestoreBackupRequest(
            backup=backup,
            destination=destination,
            target_database_url_file=target,
            target_database_identity_file=drill_identity_file(
                tmp_path,
                database_name="app_drill_durability",
            ),
        ),
        apply=True,
        runner=FakePostgresRunner(),
        now=NOW,
    )

    assert report["completed"] is True
    assert report["durability_verified"] is False
    assert destination.is_dir()
    stored = json.loads(
        (destination / "restore-report.json").read_text(encoding="ascii")
    )
    assert stored["prepared"] is True
    assert "completed" not in stored


def test_restore_refuses_development_postgres_port_even_for_drill_name(
    tmp_path: Path,
) -> None:
    backup, _create_runner, _reference, _geoserver = completed_backup(tmp_path)
    target = tmp_path / "dev-runtime.url"
    target.write_text(
        "postgresql://app:secret@127.0.0.1:5432/app_drill_unsafe\n",
        encoding="utf-8",
    )
    target.chmod(0o600)

    with pytest.raises(DisasterRecoverySafetyError):
        restore_backup(
            RestoreBackupRequest(
                backup=backup,
                destination=tmp_path / f"{RESTORE_PREFIX}dev-runtime",
                target_database_url_file=target,
                target_database_identity_file=drill_identity_file(
                    tmp_path,
                    database_name="app_drill_unsafe",
                ),
            ),
            runner=FakePostgresRunner(),
        )


def test_private_database_file_and_absolute_path_are_mandatory(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    credential = private_database_file(
        tmp_path,
        name="app",
        password=SOURCE_SECRET,
    )
    credential.chmod(0o644)
    request = CreateBackupRequest(
        destination=tmp_path / f"{BACKUP_PREFIX}permissions",
        reference_artifacts_source=reference,
        geoserver_data_source=geoserver,
        database_url_file=credential,
    )
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(request)

    relative = CreateBackupRequest(
        **{
            **request.__dict__,
            "destination": Path(f"{BACKUP_PREFIX}relative"),
        }
    )
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(relative)

    query_override = tmp_path / "query-override.url"
    query_override.write_text(
        "postgresql://app:secret@127.0.0.1:55432/app?dbname=other\n",
        encoding="utf-8",
    )
    query_override.chmod(0o600)
    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(
            CreateBackupRequest(
                destination=tmp_path / f"{BACKUP_PREFIX}query-override",
                reference_artifacts_source=reference,
                geoserver_data_source=geoserver,
                database_url_file=query_override,
            )
        )


def test_database_credentials_cannot_live_inside_a_backed_up_tree(
    tmp_path: Path,
) -> None:
    reference, geoserver = make_sources(tmp_path)
    credential = reference / "source-db.url"
    credential.write_text(
        f"postgresql://app:{SOURCE_SECRET}@127.0.0.1:55432/app\n",
        encoding="utf-8",
    )
    credential.chmod(0o600)

    with pytest.raises(DisasterRecoverySafetyError):
        create_backup(
            CreateBackupRequest(
                destination=tmp_path / f"{BACKUP_PREFIX}secret-overlap",
                reference_artifacts_source=reference,
                geoserver_data_source=geoserver,
                database_url_file=credential,
            )
        )
