from pathlib import Path

import pytest

from app.core.config import Settings
from app.reference_layers import gwc_bootstrap as bootstrap_module
from app.reference_layers.geoserver_admin import (
    GeoServerAdminAuthenticationError,
    GeoServerAdminUnavailableError,
    GeoServerHealth,
)
from app.reference_layers.gwc_bootstrap import (
    GeoWebCacheBootstrapTimeoutError,
    bootstrap_status,
    build_parser,
)
from app.reference_layers.gwc_quota import GeoWebCacheQuotaSafetyError


class FakeAdmin:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.health_calls = 0

    def health(self) -> GeoServerHealth:
        self.health_calls += 1
        if not self.outcomes:
            raise AssertionError("unexpected health request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, GeoServerHealth)
        return outcome


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def configured() -> Settings:
    return Settings(_env_file=None)


def test_bootstrap_applies_verified_quota_after_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "tiles"
    cache.mkdir()
    admin = FakeAdmin([GeoServerHealth(version="3.0.0")])
    calls: list[dict[str, object]] = []

    def apply_quota(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"schema_version": 2, "mode": "apply", "verified": True}

    monkeypatch.setattr(bootstrap_module, "quota_status", apply_quota)

    report = bootstrap_status(
        cache_path=cache,
        configured=configured(),
        client=admin,  # type: ignore[arg-type]
    )

    assert report["verified"] is True
    assert report["attempts"] == 1
    assert report["geoserver"] == {"version": "3.0.0"}
    assert calls == [
        {
            "cache_path": cache,
            "configured": configured(),
            "apply": True,
            "client": admin,
        }
    ]


def test_bootstrap_retries_only_transient_unavailability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "tiles"
    cache.mkdir()
    admin = FakeAdmin(
        [
            GeoServerAdminUnavailableError("starting"),
            GeoServerAdminUnavailableError("starting"),
            GeoServerHealth(version="3.0.0"),
        ]
    )
    clock = FakeClock()
    monkeypatch.setattr(
        bootstrap_module,
        "quota_status",
        lambda **_kwargs: {"verified": True},
    )

    report = bootstrap_status(
        cache_path=cache,
        configured=configured(),
        wait_seconds=5,
        poll_seconds=1,
        client=admin,  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert report["attempts"] == 3
    assert clock.sleeps == [1.0, 1.0]


def test_bootstrap_fails_immediately_on_auth_or_contract_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "tiles"
    cache.mkdir()
    clock = FakeClock()
    auth_admin = FakeAdmin(
        [GeoServerAdminAuthenticationError("invalid credentials")]
    )

    with pytest.raises(GeoServerAdminAuthenticationError):
        bootstrap_status(
            cache_path=cache,
            configured=configured(),
            client=auth_admin,  # type: ignore[arg-type]
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.sleeps == []

    contract_admin = FakeAdmin([GeoServerHealth(version="3.0.0")])

    def reject_quota(**_kwargs: object) -> dict[str, object]:
        raise GeoWebCacheQuotaSafetyError("unsafe contract")

    monkeypatch.setattr(bootstrap_module, "quota_status", reject_quota)
    with pytest.raises(GeoWebCacheQuotaSafetyError):
        bootstrap_status(
            cache_path=cache,
            configured=configured(),
            client=contract_admin,  # type: ignore[arg-type]
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.sleeps == []


def test_bootstrap_times_out_without_weakening_failure(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "tiles"
    cache.mkdir()
    admin = FakeAdmin(
        [
            GeoServerAdminUnavailableError("starting"),
            GeoServerAdminUnavailableError("still starting"),
            GeoServerAdminUnavailableError("not ready"),
        ]
    )
    clock = FakeClock()

    with pytest.raises(
        GeoWebCacheBootstrapTimeoutError,
        match="bootstrap deadline",
    ):
        bootstrap_status(
            cache_path=cache,
            configured=configured(),
            wait_seconds=2,
            poll_seconds=1,
            client=admin,  # type: ignore[arg-type]
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert clock.sleeps == [1.0, 1.0]
    assert admin.health_calls == 3


def test_bootstrap_parser_is_bounded_and_not_dry_run() -> None:
    args = build_parser().parse_args(
        [
            "--cache-path",
            "/var/lib/geowebcache",
            "--wait-seconds",
            "90",
        ]
    )

    assert args.cache_path == Path("/var/lib/geowebcache")
    assert args.wait_seconds == 90
    assert not hasattr(args, "apply")

    with pytest.raises(ValueError):
        bootstrap_status(
            cache_path=Path("/var/lib/geowebcache"),
            configured=configured(),
            wait_seconds=601,
            client=FakeAdmin([]),  # type: ignore[arg-type]
        )
