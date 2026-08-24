import unittest
from pathlib import Path
from xml.etree import ElementTree


REPOSITORY = Path(__file__).resolve().parents[2]


class GeoWebCacheDeploymentContractTests(unittest.TestCase):
    @staticmethod
    def _service_block(compose: str, name: str, next_name: str) -> str:
        return compose.split(f"  {name}:\n", 1)[1].split(
            f"  {next_name}:\n",
            1,
        )[0]

    def test_tomcat_exposes_only_one_loopback_http_connector(self) -> None:
        root = ElementTree.parse(
            REPOSITORY / "ops" / "geoserver" / "server.xml"
        ).getroot()
        self.assertEqual(root.tag, "Server")
        self.assertEqual(root.attrib.get("address"), "127.0.0.1")
        connectors = root.findall("./Service/Connector")
        self.assertEqual(len(connectors), 1)
        self.assertEqual(connectors[0].attrib.get("address"), "127.0.0.1")
        self.assertEqual(connectors[0].attrib.get("port"), "8080")
        self.assertEqual(connectors[0].attrib.get("protocol"), "HTTP/1.1")

    def test_geoserver_image_is_version_pinned_and_installs_both_guards(
        self,
    ) -> None:
        dockerfile = (
            REPOSITORY / "ops" / "geoserver" / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "FROM docker.osgeo.org/geoserver:3.0.0",
            dockerfile,
        )
        self.assertIn(
            "/opt/additional_libs/siur-gwc-quota-health-3.0.0.jar",
            dockerfile,
        )
        self.assertIn(
            "ops/geoserver/server.xml /opt/config_overrides/server.xml",
            dockerfile,
        )

    def test_compose_never_publishes_tomcat_and_requires_bounded_v4(
        self,
    ) -> None:
        compose = (REPOSITORY / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(":8080:8080", compose)
        self.assertNotIn('"8080:8080"', compose)
        self.assertIn(
            '"127.0.0.1:${GEOSERVER_PORT:-8081}:8081"',
            compose,
        )
        self.assertIn(
            "source: geowebcache_tile_cache_v4",
            compose,
        )
        self.assertIn(
            "GEOWEBCACHE_TILE_CACHE_HOST_PATH must reference a dedicated "
            "hard-limited filesystem mount",
            compose,
        )
        self.assertIn(
            "gwc-config-init:\n        condition: "
            "service_completed_successfully",
            compose,
        )
        self.assertNotIn(
            'CHANGE_OWNERSHIP_ON_FOLDERS: '
            '"/opt /opt/geoserver_data /var/lib/geowebcache"',
            compose,
        )

    def test_generic_processes_do_not_receive_publication_credentials(
        self,
    ) -> None:
        compose = (REPOSITORY / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        for service, next_service in (
            ("backend", "worker"),
            ("worker", "reference-worker"),
            ("reference-scheduler", "frontend"),
        ):
            block = self._service_block(
                compose,
                service,
                next_service,
            )
            self.assertIn('GEOSERVER_ADMIN_USER: ""', block)
            self.assertIn('GEOSERVER_ADMIN_PASSWORD: ""', block)
            self.assertIn('LOCAL_GEOSERVER_POSTGIS_PASSWORD: ""', block)


if __name__ == "__main__":
    unittest.main()
