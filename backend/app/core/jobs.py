from redis import Redis
from rq import Queue

from app.core.config import settings

DEFAULT_QUEUE_NAME = "default"


def get_redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def get_default_queue() -> Queue:
    return Queue(DEFAULT_QUEUE_NAME, connection=get_redis_connection())
