from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
MAIN_COMPOSE = REPOSITORY / "docker-compose.yml"
OFFLINE_COMPOSE = REPOSITORY / "docker-compose.siur-offline.yml"


def _resolved_compose(
    *files: Path,
    environment_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update(
        {
            "GEOSERVER_ADMIN_USER": "compose-contract-user",
            "GEOSERVER_ADMIN_PASSWORD": "compose-contract-password",
            "GEOWEBCACHE_TILE_CACHE_HOST_PATH": (
                "/tmp/siur-gwc-compose-contract"
            ),
            "REDIS_URL": "redis://127.0.0.1:6379/0",
        }
    )
    if environment_overrides is not None:
        environment.update(environment_overrides)
    command = ["docker", "compose"]
    for compose_file in files:
        command.extend(("-f", str(compose_file)))
    command.extend(
        ("config", "--no-env-resolution", "--format", "json")
    )
    completed = subprocess.run(
        command,
        cwd=REPOSITORY,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Could not resolve Compose contract.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


class SiurOfflineComposeContractTests(unittest.TestCase):
    def test_main_gateway_starts_only_after_successful_bootstrap(self) -> None:
        compose = _resolved_compose(MAIN_COMPOSE)
        services = compose["services"]

        gateway_dependencies = services["gwc-gateway"]["depends_on"]
        self.assertEqual(
            gateway_dependencies["gwc-bootstrap"]["condition"],
            "service_completed_successfully",
        )
        self.assertTrue(
            gateway_dependencies["gwc-bootstrap"]["restart"]
        )
        for service_name in ("reference-worker", "reference-scheduler"):
            self.assertEqual(
                services[service_name]["restart"],
                "unless-stopped",
            )
        for service_name in (
            "backend",
            "worker",
            "reference-worker",
            "reference-scheduler",
        ):
            self.assertEqual(
                services[service_name]["environment"][
                    "LOCAL_GEOSERVER_BASE_URL"
                ],
                "http://127.0.0.1:8081/geoserver",
                service_name,
            )

    def test_offline_gwc_path_uses_one_internal_network_namespace(
        self,
    ) -> None:
        compose = _resolved_compose(MAIN_COMPOSE, OFFLINE_COMPOSE)
        services = compose["services"]
        shared_namespace = "service:siur-offline-netns"

        for service_name in (
            "backend",
            "geoserver",
            "gwc-bootstrap",
            "gwc-gateway",
            "reference-worker",
            "reference-scheduler",
        ):
            self.assertEqual(
                services[service_name]["network_mode"],
                shared_namespace,
                service_name,
            )

        self.assertEqual(set(compose["networks"]), {"siur-offline"})
        self.assertTrue(compose["networks"]["siur-offline"]["internal"])
        self.assertEqual(
            set(services["siur-offline-netns"]["networks"]),
            {"siur-offline"},
        )
        for service_name, service in services.items():
            if service.get("profiles"):
                continue
            if service_name == "siur-offline-netns":
                continue
            if service_name == "gwc-config-init":
                self.assertEqual(service["network_mode"], "none")
                continue
            self.assertEqual(
                service["network_mode"],
                shared_namespace,
                f"{service_name} has an offline egress path",
            )

    def test_offline_consumers_cannot_bypass_gateway_configuration(
        self,
    ) -> None:
        compose = _resolved_compose(MAIN_COMPOSE, OFFLINE_COMPOSE)
        services = compose["services"]

        for service_name in (
            "backend",
            "worker",
            "reference-worker",
            "reference-scheduler",
        ):
            self.assertEqual(
                services[service_name]["environment"][
                    "LOCAL_GEOSERVER_BASE_URL"
                ],
                "http://127.0.0.1:8081/geoserver",
                service_name,
            )
        for service_name in ("gwc-bootstrap", "gwc-gateway"):
            self.assertEqual(
                services[service_name]["environment"][
                    "LOCAL_GEOSERVER_BASE_URL"
                ],
                "http://127.0.0.1:8080/geoserver",
                service_name,
            )

        public_geoserver_ports = [
            port
            for port in services["siur-offline-netns"]["ports"]
            if port["published"] == "8081"
        ]
        self.assertEqual(len(public_geoserver_ports), 1)
        self.assertEqual(public_geoserver_ports[0]["target"], 8081)
        self.assertEqual(
            public_geoserver_ports[0]["host_ip"],
            "127.0.0.1",
        )

    def test_offline_contract_overrides_hostile_remote_configuration(
        self,
    ) -> None:
        compose = _resolved_compose(
            MAIN_COMPOSE,
            OFFLINE_COMPOSE,
            environment_overrides={
                "API_PROXY_TARGET": "https://hostile.example.invalid",
                "NEXT_PUBLIC_API_BASE_URL": (
                    "https://hostile.example.invalid/api"
                ),
                "REFERENCE_REMOTE_PROXY_ENABLED": "true",
            },
        )
        services = compose["services"]
        frontend = services["frontend"]

        self.assertEqual(
            frontend["build"]["args"]["NEXT_PUBLIC_API_BASE_URL"],
            "/api",
        )
        self.assertEqual(
            frontend["build"]["args"]["API_PROXY_TARGET"],
            "http://127.0.0.1:8000",
        )
        self.assertEqual(
            frontend["environment"]["NEXT_PUBLIC_API_BASE_URL"],
            "/api",
        )
        self.assertEqual(
            frontend["environment"]["API_PROXY_TARGET"],
            "http://127.0.0.1:8000",
        )
        for service_name in (
            "backend",
            "worker",
            "reference-worker",
            "reference-scheduler",
        ):
            self.assertEqual(
                services[service_name]["environment"][
                    "REFERENCE_REMOTE_PROXY_ENABLED"
                ],
                "false",
                service_name,
            )

    def test_offline_startup_chain_replaces_online_namespace_dependencies(
        self,
    ) -> None:
        compose = _resolved_compose(MAIN_COMPOSE, OFFLINE_COMPOSE)
        services = compose["services"]

        geoserver_dependencies = services["geoserver"]["depends_on"]
        self.assertNotIn("geoserver-network", geoserver_dependencies)
        self.assertEqual(
            geoserver_dependencies["gwc-config-init"]["condition"],
            "service_completed_successfully",
        )
        self.assertEqual(
            geoserver_dependencies["postgres"]["condition"],
            "service_healthy",
        )

        bootstrap_dependencies = services["gwc-bootstrap"]["depends_on"]
        self.assertNotIn("geoserver-network", bootstrap_dependencies)
        self.assertEqual(
            bootstrap_dependencies["geoserver"]["condition"],
            "service_started",
        )

        gateway_dependencies = services["gwc-gateway"]["depends_on"]
        self.assertNotIn("geoserver-network", gateway_dependencies)
        self.assertEqual(
            gateway_dependencies["gwc-bootstrap"]["condition"],
            "service_completed_successfully",
        )

        for service_name in (
            "backend",
            "reference-worker",
            "reference-scheduler",
        ):
            self.assertEqual(
                services[service_name]["depends_on"]["gwc-gateway"][
                    "condition"
                ],
                "service_healthy",
                service_name,
            )


if __name__ == "__main__":
    unittest.main()
