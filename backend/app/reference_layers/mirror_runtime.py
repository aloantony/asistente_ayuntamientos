"""Signal-aware process entrypoints for the reference mirror services."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from typing import Sequence

from app.core.config import settings
from app.reference_layers.mirror_orchestrator import (
    MirrorRunProcessor,
    enqueue_reference_sources_once,
    reconcile_reference_sources_once,
)

logger = logging.getLogger(__name__)


def run_scheduler(
    stop_event: threading.Event,
    *,
    once: bool = False,
) -> None:
    first_poll = True
    while not stop_event.is_set():
        try:
            reconciled = reconcile_reference_sources_once()
            for item in reconciled:
                if first_poll or any(
                    (
                        item.created_count,
                        item.updated_count,
                        item.deactivated_count,
                    )
                ):
                    logger.info(
                        "Reference mirror sources reconciled",
                        extra={
                            "provider_key": item.provider_key,
                            "created_count": item.created_count,
                            "updated_count": item.updated_count,
                            "deactivated_count": item.deactivated_count,
                        },
                    )
            run_ids = enqueue_reference_sources_once(reconcile=False)
            if run_ids:
                logger.info(
                    "Reference mirror scheduler queued runs",
                    extra={"run_count": len(run_ids)},
                )
            first_poll = False
        except Exception as error:
            logger.error(
                "Reference mirror scheduler poll failed (%s)",
                type(error).__name__,
            )
        if once:
            return
        stop_event.wait(settings.reference_mirror_scheduler_poll_seconds)


def run_worker(
    stop_event: threading.Event,
    *,
    once: bool = False,
) -> None:
    processor = MirrorRunProcessor(stop_event=stop_event)
    try:
        while not stop_event.is_set():
            try:
                result = processor.process_next()
            except Exception as error:
                logger.error(
                    "Reference mirror worker poll failed (%s)",
                    type(error).__name__,
                )
                result = None
            if once:
                return
            if result is None or result.state == "idle":
                stop_event.wait(settings.reference_mirror_worker_poll_seconds)
    finally:
        processor.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local reference mirror scheduler or worker",
    )
    parser.add_argument("service", choices=("scheduler", "worker"))
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one scheduler poll or process at most one claimed run",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()

    def stop(signum, frame) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        if arguments.service == "scheduler":
            run_scheduler(stop_event, once=arguments.once)
        else:
            run_worker(stop_event, once=arguments.once)
    except Exception as error:
        logger.error(
            "Reference mirror service could not start (%s)",
            type(error).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by Compose
    raise SystemExit(main())
