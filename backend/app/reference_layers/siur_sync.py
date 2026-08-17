"""Operator CLI for reviewed SIUR catalog dry-runs and promotion.

No URL is accepted and no network request is made here. The operator supplies
the exact downloaded files. The command is read-only unless ``--apply`` is
present, and even then both payload hashes, the inventory baseline, the WMC
probe and the database sync plan must be free of blocking issues.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy.exc import SQLAlchemyError

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.catalog import (
    ReferenceCatalogSyncPlan,
    ReferenceCatalogValidationError,
    apply_catalog_definition,
    build_catalog_sync_plan,
)
from app.reference_layers.siur_settings import (
    SiurSettingsAnalysis,
    SiurSettingsBaseline,
    SiurSettingsError,
    SiurSettingsLimits,
    analyze_siur_settings,
)
from app.reference_layers.siur_wmc import (
    MAX_WMC_BYTES,
    SiurWmcError,
    WmcEvidence,
    WmcParityReport,
    augment_catalog_with_wmc_evidence,
    compare_wmc_to_catalog,
    parse_wmc_evidence,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LAYER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,254}$")


class SiurSyncInputError(ValueError):
    """The local files or review arguments are incomplete."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Valida y, opcionalmente, promociona el catálogo SIUR",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        required=True,
        help="settings.json descargado de SIUR",
    )
    parser.add_argument(
        "--wmc",
        type=Path,
        required=True,
        help="WMC revisado que se usará como sonda parcial",
    )
    parser.add_argument("--top-level-groups", type=int)
    parser.add_argument("--groups", type=int)
    parser.add_argument("--layers", type=int)
    parser.add_argument("--services", type=int)
    parser.add_argument(
        "--approved-sha256",
        help="SHA-256 aprobado de settings.json",
    )
    parser.add_argument(
        "--approved-wmc-sha256",
        help="SHA-256 aprobado del WMC",
    )
    parser.add_argument(
        "--layer-manifest",
        type=Path,
        help="JSON revisado con todas las identidades de capa",
    )
    parser.add_argument(
        "--approved-definition-sha256",
        help="SHA-256 aprobado de la definición normalizada",
    )
    parser.add_argument(
        "--approved-plan-sha256",
        help="SHA-256 aprobado del plan contra la base de datos",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica el plan; sin esta opción siempre se ejecuta en dry-run",
    )
    return parser


def _read_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SiurSyncInputError(f"No se puede leer {label}") from exc
    if not 0 < size <= max_bytes:
        raise SiurSyncInputError(f"El tamaño de {label} no está permitido")
    try:
        document = path.read_bytes()
    except OSError as exc:
        raise SiurSyncInputError(f"No se puede leer {label}") from exc
    if len(document) != size:
        raise SiurSyncInputError(f"{label} cambió durante la lectura")
    return document


def _baseline_from_args(
    args: argparse.Namespace,
) -> SiurSettingsBaseline | None:
    counts = (
        args.top_level_groups,
        args.groups,
        args.layers,
        args.services,
    )
    if all(value is None for value in counts):
        if args.layer_manifest is not None or args.approved_sha256 is not None:
            raise SiurSyncInputError(
                "Los conteos son obligatorios al aportar una baseline"
            )
        return None
    if any(value is None for value in counts):
        raise SiurSyncInputError(
            "Los cuatro conteos de baseline deben indicarse juntos"
        )
    if any(value < 0 for value in counts):
        raise SiurSyncInputError("Los conteos de baseline no pueden ser negativos")
    if (
        args.approved_sha256 is not None
        and not SHA256_RE.fullmatch(args.approved_sha256)
    ):
        raise SiurSyncInputError("El SHA-256 aprobado de settings no es válido")
    return SiurSettingsBaseline(
        top_level_groups=args.top_level_groups,
        groups=args.groups,
        layers=args.layers,
        services=args.services,
        raw_sha256=args.approved_sha256,
        layer_keys=(
            _load_layer_manifest(args.layer_manifest)
            if args.layer_manifest is not None
            else None
        ),
    )


def _load_layer_manifest(path: Path) -> frozenset[str]:
    document = _read_bounded(path, 512 * 1024, "layer manifest")
    try:
        value = json.loads(
            document.decode("utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(item)
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SiurSyncInputError(
            "El layer manifest no es JSON UTF-8 válido"
        ) from exc
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str) or not LAYER_KEY_RE.fullmatch(item)
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise SiurSyncInputError(
            "El layer manifest debe ser una lista única de identidades válidas"
        )
    return frozenset(value)


def _hash_issue(
    evidence: WmcEvidence,
    approved_sha256: str | None,
) -> str | None:
    if approved_sha256 is None:
        return "WMC approved SHA-256 is required"
    if not SHA256_RE.fullmatch(approved_sha256):
        return "WMC approved SHA-256 is invalid"
    if evidence.content_sha256 != approved_sha256:
        return "WMC approved SHA-256 does not match"
    return None


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


def _plan_sha256(plan: ReferenceCatalogSyncPlan) -> str:
    payload = {
        "definition_sha256": plan.definition_sha256,
        "base_state_sha256": plan.base_state_sha256,
        "new_services": plan.new_services,
        "updated_services": plan.updated_services,
        "missing_services": plan.missing_services,
        "new_layers": plan.new_layers,
        "updated_layers": plan.updated_layers,
        "missing_layers": plan.missing_layers,
        "new_styles": plan.new_styles,
        "updated_styles": plan.updated_styles,
        "missing_styles": plan.missing_styles,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _summary(
    analysis: SiurSettingsAnalysis,
    evidence: WmcEvidence,
    parity: WmcParityReport | None,
    plan: ReferenceCatalogSyncPlan | None,
    blockers: list[str],
    *,
    apply_requested: bool,
    applied: bool,
) -> dict:
    result = {
        "ok": not blockers,
        "mode": "apply" if apply_requested else "dry-run",
        "applied": applied,
        "settings": {
            "sha256": analysis.raw_sha256,
            "size_bytes": analysis.raw_size_bytes,
            "top_level_groups": analysis.top_level_group_count,
            "groups": analysis.group_count,
            "layers": analysis.layer_count,
            "services": len(analysis.services),
            "unresolved": len(analysis.unresolved),
            "layer_keys": sorted(
                node.source_key
                for node in analysis.nodes
                if node.kind == "layer" and node.source_key is not None
            ),
        },
        "wmc": {
            "sha256": evidence.content_sha256,
            "layers": len(evidence.layers),
            "services": evidence.service_count,
            "styles": evidence.style_count,
            "selected_styles": evidence.selected_style_count,
            "metadata": evidence.metadata_count,
        },
        "blocking_issues": blockers,
    }
    if parity is not None:
        result["wmc"]["matched_layers"] = len(parity.matched_layers)
        result["wmc"]["matched_styles"] = len(parity.matched_styles)
    if plan is not None:
        plan_sha256 = _plan_sha256(plan)
        result["plan"] = {
            "plan_sha256": plan_sha256,
            "content_sha256": plan.content_sha256,
            "definition_sha256": plan.definition_sha256,
            "base_state_sha256": plan.base_state_sha256,
            "new_services": list(plan.new_services),
            "updated_services": list(plan.updated_services),
            "missing_services": list(plan.missing_services),
            "new_layers": list(plan.new_layers),
            "updated_layers": list(plan.updated_layers),
            "missing_layers": list(plan.missing_layers),
            "new_styles": list(plan.new_styles),
            "updated_styles": list(plan.updated_styles),
            "missing_styles": list(plan.missing_styles),
            "unchanged": plan.unchanged_count,
        }
    return result


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    register_all_models()
    limits = SiurSettingsLimits()
    try:
        baseline = _baseline_from_args(args)
        settings_document = _read_bounded(
            args.settings,
            limits.max_bytes,
            "settings.json",
        )
        wmc_document = _read_bounded(args.wmc, MAX_WMC_BYTES, "WMC")
        analysis = analyze_siur_settings(
            settings_document,
            baseline=baseline,
            limits=limits,
        )
        evidence = parse_wmc_evidence(wmc_document)
        blockers = list(analysis.blocking_issues)
        wmc_hash_issue = _hash_issue(evidence, args.approved_wmc_sha256)
        if wmc_hash_issue:
            blockers.append(wmc_hash_issue)

        parity = None
        plan = None
        applied = False
        if analysis.definition is not None:
            definition = augment_catalog_with_wmc_evidence(
                analysis.definition,
                evidence,
            )
            parity = compare_wmc_to_catalog(evidence, definition)
            blockers.extend(parity.blocking_issues)
            with SessionLocal() as db:
                plan = build_catalog_sync_plan(db, definition)
                blockers.extend(plan.blocking_issues)
                definition_issue = _approval_issue(
                    "Definition",
                    plan.definition_sha256,
                    args.approved_definition_sha256,
                )
                if definition_issue:
                    blockers.append(definition_issue)
                plan_issue = _approval_issue(
                    "Plan",
                    _plan_sha256(plan),
                    args.approved_plan_sha256,
                )
                if plan_issue:
                    blockers.append(plan_issue)
                blockers = list(dict.fromkeys(blockers))
                if args.apply and not blockers:
                    apply_catalog_definition(
                        db,
                        definition,
                        expected_plan=plan,
                    )
                    applied = True

        blockers = list(dict.fromkeys(blockers))
        print(
            json.dumps(
                _summary(
                    analysis,
                    evidence,
                    parity,
                    plan,
                    blockers,
                    apply_requested=args.apply,
                    applied=applied,
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 3 if blockers else 0
    except (
        ReferenceCatalogValidationError,
        SiurSettingsError,
        SiurWmcError,
        SiurSyncInputError,
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
