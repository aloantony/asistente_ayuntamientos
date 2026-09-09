def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_worker_readiness_is_not_redis_ping(client, monkeypatch):
    from app.core import jobs
    monkeypatch.setattr(jobs, 'worker_is_available', lambda: False)
    response=client.get('/ready/worker')
    assert response.status_code==503
    monkeypatch.setattr(jobs, 'worker_is_available', lambda: True)
    assert client.get('/ready/worker').json()=={'status':'ready','worker':'ok'}


def test_worker_readiness_handles_naive_heartbeats_and_queues(monkeypatch):
    from app.core import jobs
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from rq import Worker
    worker=SimpleNamespace(last_heartbeat=datetime.utcnow(),worker_ttl=420,
                           queue_names=lambda:['default'],get_state=lambda:'idle')
    monkeypatch.setattr(jobs,'get_redis_connection',lambda: object())
    monkeypatch.setattr(Worker,'all',lambda **kw:[worker])
    assert jobs.worker_is_available()
    worker.last_heartbeat-=timedelta(hours=1)
    assert not jobs.worker_is_available()
    worker.last_heartbeat=datetime.utcnow()
    worker.queue_names=lambda:['other']
    assert not jobs.worker_is_available()
