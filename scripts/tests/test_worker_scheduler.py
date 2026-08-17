import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class WorkerSchedulerConfigurationTests(unittest.TestCase):
    def test_worker_runs_scheduler_for_delayed_rq_retries(self) -> None:
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("rq worker --with-scheduler", compose)


if __name__ == "__main__":
    unittest.main()
