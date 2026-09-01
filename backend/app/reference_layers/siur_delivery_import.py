"""Local-only, reviewed import of immutable SIUR WMS delivery evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat

from sqlalchemy.exc import SQLAlchemyError

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.delivery_evidence import (
    MAX_LICENSE_REVIEW_BYTES,
    DeliveryEvidenceError,
    LicenseReviewError,
    apply_delivery_evidence,
    build_delivery_evidence_plan,
    parse_license_review,
)
from app.reference_layers.wms_capabilities import (
    MAX_WMS_CAPABILITIES_BYTES,
    WMSCapabilitiesError,
    parse_wms_capabilities,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SiurDeliveryImportError(ValueError):
    """A local evidence file or explicit approval is invalid."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Valida e importa evidencia local para la entrega WMS de SIUR"
        ),
    )
    parser.add_argument(
        "--capabilities",
        type=Path,
        required=True,
        help="GetCapabilities XML revisado y descargado previamente",
    )
    parser.add_argument(
        "--license-review",
        type=Path,
        required=True,
        help="Documento JSON de revisión humana de licencia",
    )
    parser.add_argument("--approved-capabilities-sha256")
    parser.add_argument("--approved-normalized-capabilities-sha256")
    parser.add_argument("--approved-license-evidence-sha256")
    parser.add_argument("--approved-license-review-sha256")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Importa el plan aprobado; por defecto solo hace dry-run",
    )
    return parser


def _read_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SiurDeliveryImportError(
                    f"{label} debe ser un archivo local regular"
                )
            if not 0 < before.st_size <= max_bytes:
                raise SiurDeliveryImportError(
                    f"El tamaño de {label} no está permitido"
                )
            document = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise SiurDeliveryImportError(f"No se puede leer {label}") from exc
    if (
        len(document) > max_bytes
        or len(document) != before.st_size
        or after.st_size != before.st_size
    ):
        raise SiurDeliveryImportError(f"{label} cambió durante la lectura")
    return document


def _approval_issue(
    label: str,
    observed_sha256: str,
    approved_sha256: str | None,
) -> str | None:
    if approved_sha256 is None:
        return f"{label} approved SHA-256 is required"
    if not SHA256_RE.fullmatch(approved_sha256):
        return f"{label} approved SHA-256 is invalid"
    if observed_sha256 != approved_sha256:
        return f"{label} approved SHA-256 does not match"
    return None


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        register_all_models()
        capabilities = parse_wms_capabilities(
            _read_bounded(
                args.capabilities,
                MAX_WMS_CAPABILITIES_BYTES,
                "GetCapabilities",
            )
        )
        license_review = parse_license_review(
            _read_bounded(
                args.license_review,
                MAX_LICENSE_REVIEW_BYTES,
                "license review",
            )
        )
        with SessionLocal() as db:
            plan = build_delivery_evidence_plan(
                db,
                capabilities,
                license_review,
            )
            approvals = [
                _approval_issue(
                    "Capabilities",
                    capabilities.raw_sha256,
                    args.approved_capabilities_sha256,
                ),
                _approval_issue(
                    "Normalized capabilities",
                    capabilities.normalized_sha256,
                    args.approved_normalized_capabilities_sha256,
                ),
                _approval_issue(
                    "License evidence",
                    license_review.evidence_sha256,
                    args.approved_license_evidence_sha256,
                ),
                _approval_issue(
                    "License review",
                    license_review.review_sha256,
                    args.approved_license_review_sha256,
                ),
                _approval_issue(
                    "Plan",
                    plan.plan_sha256,
                    args.approved_plan_sha256,
                ),
            ]
            approval_issues = [issue for issue in approvals if issue]
            applied = None
            if args.apply and not approval_issues and not plan.fatal_issues:
                applied, plan = apply_delivery_evidence(
                    db,
                    capabilities,
                    license_review,
                    expected_plan_sha256=plan.plan_sha256,
                )

        summary = {
            "ok": not approval_issues and not plan.fatal_issues,
            "mode": "apply" if args.apply else "dry-run",
            "applied": applied is not None,
            "attestable": plan.attestable,
            "delivery_ready": plan.delivery_ready,
            "capabilities": {
                "raw_sha256": capabilities.raw_sha256,
                "normalized_sha256": capabilities.normalized_sha256,
                "size_bytes": len(capabilities.raw_xml),
                "version": capabilities.version,
                "get_map_formats": list(capabilities.get_map_formats),
                "get_feature_info_formats": list(
                    capabilities.get_feature_info_formats
                ),
                "layers": len(capabilities.layers),
            },
            "license_review": {
                "evidence_sha256": license_review.evidence_sha256,
                "review_sha256": license_review.review_sha256,
                "decision": license_review.decision,
                "supersedes_review_sha256": (
                    license_review.supersedes_review_sha256
                ),
                "allow_proxy": license_review.allow_proxy,
                "allow_cache": license_review.allow_cache,
            },
            "catalog_snapshot_id": plan.catalog_snapshot_id,
            "catalog_definition_sha256": plan.catalog_definition_sha256,
            "attestation_sha256": plan.attestation_sha256,
            "attestation_kind": plan.attestation_kind,
            "attestation_sequence_number": plan.sequence_number,
            "plan_sha256": plan.plan_sha256,
            "approval_issues": approval_issues,
            "fatal_issues": list(plan.fatal_issues),
            "attestation_issues": list(plan.attestation_issues),
            "delivery_issues": list(plan.delivery_issues),
            "actions": {
                "capabilities": (
                    "reuse"
                    if plan.existing_capabilities_snapshot_id is not None
                    else "insert"
                ),
                "license_review": (
                    "reuse"
                    if plan.existing_license_review_id is not None
                    else "insert"
                ),
                "attestation": (
                    "none"
                    if not plan.attestable
                    else (
                        "reuse"
                        if plan.existing_attestation_id is not None
                        else "insert"
                    )
                ),
            },
        }
        if applied is not None:
            summary["stored"] = {
                "capabilities_snapshot_id": (
                    applied.capabilities_snapshot_id
                ),
                "license_review_id": applied.license_review_id,
                "attestation_id": applied.attestation_id,
            }
        print(
            json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return (
            0
            if not approval_issues
            and not plan.fatal_issues
            and plan.delivery_ready
            else 3
        )
    except (
        DeliveryEvidenceError,
        LicenseReviewError,
        SiurDeliveryImportError,
        WMSCapabilitiesError,
    ) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except SQLAlchemyError:
        print(
            json.dumps(
                {"ok": False, "error": "No se pudo completar la transacción"},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
