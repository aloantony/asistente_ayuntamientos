"""Local operator entrypoint for review-gated official style updates."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat as stat_module
from typing import Sequence

from app.core.config import Settings, settings
from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.style_update_watcher import (
    MAX_STYLE_REVIEW_DOCUMENT_BYTES,
    StyleUpdateReviewDocumentError,
    apply_style_update_review,
    pending_official_style_review_status,
    plan_style_update_review,
)


def build_style_review_store(config: Settings = settings) -> ReferenceBlobStore:
    return ReferenceBlobStore(
        config.reference_storage_root,
        max_blob_bytes=config.reference_blob_max_bytes,
        quota_bytes=config.reference_storage_quota_bytes,
        min_free_bytes=config.reference_storage_min_free_bytes,
    )


def _read_local_review_document(path_value: str) -> bytes:
    if "://" in path_value:
        raise StyleUpdateReviewDocumentError(
            "style review input must be a local file"
        )
    path = Path(path_value)
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise StyleUpdateReviewDocumentError(
            "style review file is unavailable"
        ) from error
    if (
        not stat_module.S_ISREG(path_stat.st_mode)
        or not 1 <= path_stat.st_size <= MAX_STYLE_REVIEW_DOCUMENT_BYTES
    ):
        raise StyleUpdateReviewDocumentError(
            "style review file size is invalid"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StyleUpdateReviewDocumentError(
            "style review file is unavailable"
        ) from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            opened_stat = os.fstat(stream.fileno())
            if (
                not stat_module.S_ISREG(opened_stat.st_mode)
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
                or not (
                    1
                    <= opened_stat.st_size
                    <= MAX_STYLE_REVIEW_DOCUMENT_BYTES
                )
            ):
                raise StyleUpdateReviewDocumentError(
                    "style review file is unavailable"
                )
            document = stream.read(MAX_STYLE_REVIEW_DOCUMENT_BYTES + 1)
            final_stat = os.fstat(stream.fileno())
    except StyleUpdateReviewDocumentError:
        raise
    except OSError as error:
        raise StyleUpdateReviewDocumentError(
            "style review file is unavailable"
        ) from error
    if (
        len(document) != opened_stat.st_size
        or len(document) > MAX_STYLE_REVIEW_DOCUMENT_BYTES
        or (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
            opened_stat.st_ctime_ns,
        )
        != (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        )
    ):
        raise StyleUpdateReviewDocumentError(
            "style review file changed while it was read"
        )
    return document


def _positive_source_id(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "source id must be a positive integer"
        ) from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            "source id must be a positive integer"
        )
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or review pending official style changes"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status",
        help="list promotion blockers and JSON review templates",
    )
    status.add_argument("--source-id", type=_positive_source_id)
    review = commands.add_parser(
        "review",
        help="dry-run or append one exact local review document",
    )
    review.add_argument("--file", required=True)
    review.add_argument(
        "--apply",
        action="store_true",
        help="append after exact hash confirmation; default is dry-run",
    )
    review.add_argument("--expected-review-sha256")
    review.add_argument("--expected-document-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    store: ReferenceBlobStore | None = None
    try:
        if arguments.command == "review":
            if arguments.apply and (
                arguments.expected_review_sha256 is None
                or arguments.expected_document_sha256 is None
            ):
                raise StyleUpdateReviewDocumentError(
                    "--apply requires --expected-review-sha256 and "
                    "--expected-document-sha256"
                )
            if not arguments.apply and (
                arguments.expected_review_sha256 is not None
                or arguments.expected_document_sha256 is not None
            ):
                raise StyleUpdateReviewDocumentError(
                    "expected hashes are only valid with --apply"
                )
            document = _read_local_review_document(arguments.file)
        else:
            document = None

        register_all_models()
        store = build_style_review_store()
        with SessionLocal() as db:
            if arguments.command == "status":
                result = pending_official_style_review_status(
                    db,
                    store=store,
                    source_id=arguments.source_id,
                )
            elif arguments.apply:
                assert document is not None
                review = apply_style_update_review(
                    db,
                    document,
                    store=store,
                    expected_review_sha256=(
                        arguments.expected_review_sha256
                    ),
                    expected_document_sha256=(
                        arguments.expected_document_sha256
                    ),
                )
                plan = plan_style_update_review(
                    db,
                    bytes(review.reviewed_document),
                    store=store,
                )
                result = plan.public_summary(applied=True)
                result["review_id"] = review.id
            else:
                assert document is not None
                plan = plan_style_update_review(
                    db,
                    document,
                    store=store,
                )
                result = plan.public_summary(applied=False)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except StyleUpdateReviewDocumentError as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "style_update_review_document_rejected",
                    "error_summary": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except Exception as error:  # pragma: no cover - operator boundary
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "style_update_review_service_unavailable",
                    "error_summary": type(error).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":  # pragma: no cover - operator entrypoint
    raise SystemExit(main())
