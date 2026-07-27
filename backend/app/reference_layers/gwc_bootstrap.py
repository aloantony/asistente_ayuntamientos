"""Mandatory one-shot gate for the local GeoWebCache tile store.

Compose runs this service after GeoServer starts and requires a successful
exit before any web, queue, scheduler, or reference worker service may start.
Only transient connection failures are retried. Authentication, configuration,
capacity, XML, and revalidation failures remain immediately fail-closed.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Callable, Sequence

from app.core.config import Settings, settings
from app.reference_layers.geoserver_admin import (
    GeoServerAdminClient,
    GeoServerAdminUnavailableError,
)
from app.reference_layers.gwc_quota import (
    GeoWebCacheQuotaSafetyError,
    quota_status,
)

DEFAULT_WAIT_SECONDS = 180.0
DEFAULT_POLL_SECONDS = 1.0
MAX_WAIT_SECONDS = 600.0


class GeoWebCacheBootstrapTimeoutError(TimeoutError):
    """GeoServer did not become reachable within the bounded startup gate."""


def bootstrap_status(
    *,
    cache_path: Path,
    configured: Settings = settings,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    client: GeoServerAdminClient | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Wait for transport readiness, then apply and verify the exact contract."""

    wait = _bounded_seconds(
        wait_seconds,
        minimum=0.1,
        maximum=MAX_WAIT_SECONDS,
        label="wait",
    )
    poll = _bounded_seconds(
        poll_seconds,
        minimum=0.1,
        maximum=10.0,
        label="poll",
    )
    admin = client or GeoServerAdminClient()
    deadline = monotonic() + wait
    attempts = 0
    while True:
        attempts += 1
        try:
            health = admin.health()
            quota = quota_status(
                cache_path=cache_path,
                configured=configured,
                apply=True,
                client=admin,
            )
            if quota.get("verified") is not True:
                raise GeoWebCacheQuotaSafetyError(
                    "GeoWebCache bootstrap verification is incomplete"
                )
            return {
                "schema_version": 1,
                "mode": "bootstrap",
                "attempts": attempts,
                "geoserver": asdict(health),
                "quota": quota,
                "verified": True,
            }
        except GeoServerAdminUnavailableError as error:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise GeoWebCacheBootstrapTimeoutError(
                    "local GeoServer did not become ready before the "
                    "GeoWebCache bootstrap deadline"
                ) from error
            sleep(min(poll, remaining))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply and verify the mandatory local GeoWebCache FileBlobStore "
            "and disk quota before dependent services start."
        )
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        required=True,
        help="absolute read-only mount of the tile-only GeoWebCache volume",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help="bounded time to retry transient GeoServer connection failures",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="delay between transient connection retries",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = bootstrap_status(
        cache_path=args.cache_path,
        wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds,
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


def _bounded_seconds(
    value: float,
    *,
    minimum: float,
    maximum: float,
    label: str,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(
            f"GeoWebCache bootstrap {label} seconds must be between "
            f"{minimum} and {maximum}"
        )
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main())
