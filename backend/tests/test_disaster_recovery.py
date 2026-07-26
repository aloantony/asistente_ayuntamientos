from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tarfile
from typing import Mapping, Sequence

import pytest

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


class FakePostgresRunner:
    def __init__(
        self,
        *,
        fail_command: str | None = None,
        mutate_during_dump: Path | None = None,
        target_relation_count: int = 0,
        target_preflight_counts: tuple[int, ...] | None = None,
        race_destination: Path | None = None,
    ) -> None:
        self.fail_command = fail_command
        self.mutate_during_dump = mutate_during_dump
        self.target_preflight_counts = list(
            target_preflight_counts or (0, 0, 0, 0, 0, 0, 0)
        )
        if len(self.target_preflight_counts) != 7:
            raise ValueError("target_preflight_counts must have seven values")
        if target_relation_count:
            self.target_preflight_counts[2] = target_relation_count
        self.race_destination = race_destination
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
    ) -> CommandResult:
        command = tuple(argv)
        env = dict(environment)
        self.calls.append((command, env))
        if command[0] == self.fail_command:
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
        elif command[0] == "psql":
            if any(
                value.startswith(
                    "--command=DROP OWNED BY CURRENT_USER CASCADE"
                )
                for value in command
            ):
                self.target_preflight_counts = [0] * 7
                return CommandResult(0)
            database_name = env["PGDATABASE"]
            return CommandResult(
                0,
                stdout=(
                    f"{database_name}|{env['PGUSER']}|1|"
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
    (geoserver / "gwc-cache" / "derived-tile.png").write_bytes(b"tile")
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
                "schema_version": 1,
                "captured_at": captured_at.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "operator": "codex-test",
                "reference_artifacts_source": str(reference),
                "geoserver_data_source": str(geoserver),
                "stopped_services": sorted(EXPECTED_STOPPED_SERVICES),
                "postgres_running": True,
            }
        ),
        encoding="utf-8",
    )
    return path


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
    assert manifest["schema_version"] == 2
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
            ),
            runner=FakePostgresRunner(),
        )


@pytest.mark.parametrize(
    "index",
    range(7),
    ids=[
        "concurrent-session",
        "schema",
        "relation",
        "function",
        "type",
        "extension",
        "other-object",
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
    counts = [0] * 7
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
            argument.startswith(
                "--command=DROP OWNED BY CURRENT_USER CASCADE"
            )
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
    assert "CREATE EXTENSION IF NOT EXISTS plpgsql" in compensation_sql
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
        "_fsync_directory_with_retries",
        lambda _path: False,
    )

    report = restore_backup(
        RestoreBackupRequest(
            backup=backup,
            destination=destination,
            target_database_url_file=target,
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
    assert stored["completed"] is True


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
