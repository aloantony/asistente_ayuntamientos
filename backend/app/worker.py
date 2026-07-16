"""RQ entrypoints for long-running backend jobs."""

from app.ordinances.import_service import (
    embed_ordinance_chunk,
    embed_ordinance_chunks,
    run_import_job,
)

__all__ = ["embed_ordinance_chunk", "embed_ordinance_chunks", "run_import_job"]
