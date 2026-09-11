import pytest
from types import SimpleNamespace

from app.ordinances import import_service as service


def test_burgos_does_not_hide_other_sources(monkeypatch):
    burgos = SimpleNamespace(id=1, domain=service.BOP_BURGOS_DOMAIN)
    soria = SimpleNamespace(id=2, domain='bop.dipsoria.es')
    job = SimpleNamespace(search_query='ordenanza', topic='agua', subtopic=None, error_message=None)
    municipality = SimpleNamespace(id=1, name='Pueblo ficticio')
    first = service.SourceCandidate('https://'+burgos.domain+'/1.pdf', 1, 1)
    monkeypatch.setattr(service, '_discover_bop_burgos_candidates', lambda *a: [first])
    queries = []
    def search(**kwargs):
        queries.append(kwargs['query'])
        return [{'url': 'https://bop.dipsoria.es/2.pdf'}, {'url': 'https://'+burgos.domain+'/wrong-source.pdf'}]
    monkeypatch.setattr(service, 'web_search_client', SimpleNamespace(enabled=True, search=search))
    candidates = service._discover_candidates(job, [burgos, soria], [municipality])
    assert len(candidates) == 2
    assert candidates[0] == first
    assert candidates[1].official_source_id == 2
    assert len(queries) == 1 and 'site:bop.dipsoria.es' in queries[0]


def test_partial_discovery_is_explicit(monkeypatch):
    burgos = SimpleNamespace(id=1, domain=service.BOP_BURGOS_DOMAIN)
    soria = SimpleNamespace(id=2, domain='bop.dipsoria.es')
    job = SimpleNamespace(search_query='ordenanza', topic=None, subtopic=None, error_message=None)
    monkeypatch.setattr(service, '_discover_bop_burgos_candidates', lambda *a: [service.SourceCandidate('https://'+burgos.domain+'/1.pdf')])
    monkeypatch.setattr(service, 'web_search_client', SimpleNamespace(enabled=False))
    assert len(service._discover_candidates(job, [burgos, soria], [SimpleNamespace(id=1, name='Demo')])) == 1
    assert 'parcial' in job.error_message


@pytest.mark.parametrize("error_type", [service.WebSearchUnavailableError, ValueError])
def test_search_failure_does_not_silently_accept_partial_discovery(monkeypatch, error_type):
    burgos = SimpleNamespace(id=1, domain=service.BOP_BURGOS_DOMAIN)
    soria = SimpleNamespace(id=2, domain="bop.dipsoria.es")
    job = SimpleNamespace(search_query="ordenanza", topic=None, subtopic=None, error_message=None)
    candidate = service.SourceCandidate("https://" + burgos.domain + "/1.pdf")
    monkeypatch.setattr(service, "_discover_bop_burgos_candidates", lambda *args: [candidate])
    def fail(**kwargs):
        raise error_type("fallo simulado")
    monkeypatch.setattr(service, "web_search_client", SimpleNamespace(enabled=True, search=fail))
    assert service._discover_candidates(job, [burgos, soria], [SimpleNamespace(id=1, name="Demo")]) == [candidate]
    assert "Descubrimiento parcial" in job.error_message
