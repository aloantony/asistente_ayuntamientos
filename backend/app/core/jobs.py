from redis import Redis
from rq import Queue

from app.core.config import settings

DEFAULT_QUEUE_NAME = "default"


def get_redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def get_default_queue() -> Queue:
    return Queue(DEFAULT_QUEUE_NAME, connection=get_redis_connection())


def worker_is_available() -> bool:
    """At least one recent RQ worker serving the default queue can do work."""
    from datetime import datetime, timezone
    from rq import Worker

    now = datetime.now(timezone.utc)
    for worker in Worker.all(connection=get_redis_connection()):
        heartbeat = worker.last_heartbeat
        if heartbeat is None or DEFAULT_QUEUE_NAME not in worker.queue_names():
            continue
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        age = (now - heartbeat).total_seconds()
        if -60 <= age <= max(60, worker.worker_ttl) and worker.get_state() in {'idle', 'busy', 'started'}:
            return True
    return False
