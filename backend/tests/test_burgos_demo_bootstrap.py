from urllib import parse as urlparse

from app.ordinances import import_service
from app.ordinances.demo_bootstrap import (
    BOP_BURGOS_DOMAIN,
    DEMO_ORDINANCE_SOURCES,
    bootstrap_burgos_demo_ordinances,
)
from app.ordinances.models import Ordinance, OrdinanceLegalChunk


def test_burgos_demo_sources_are_official_bopbur_urls():
    assert len(DEMO_ORDINANCE_SOURCES) >= 3
    for source in DEMO_ORDINANCE_SOURCES:
        parsed = urlparse.urlparse(source.source_url)
        assert parsed.scheme == "http"
        assert parsed.hostname == BOP_BURGOS_DOMAIN
        assert source.bulletin_number.startswith("BOPBUR-")
        assert "ordenanza" in source.title.lower()


def test_bootstrap_burgos_demo_ordinances_imports_and_approves_real_source_metadata(
    db,
    monkeypatch,
):
    def fake_fetch(url, _sources):
        source = next(
            source for source in DEMO_ORDINANCE_SOURCES if source.source_url == url
        )
        text = (
            f"{source.title}. Ayuntamiento de {source.municipality_name}.\n\n"
            f"Artículo 1. Objeto. Esta ordenanza regula {source.subtopic}.\n\n"
            "Artículo 2. Obligaciones. Las personas obligadas deberán respetar "
            "las condiciones, horarios, tarifas y demás reglas establecidas por "
            "el Ayuntamiento.\n\n"
            "Publicado el 12/05/2026 en el Boletín Oficial de la Provincia de Burgos."
        )
        return import_service.FetchedSource(
            content=text.encode("utf-8"),
            content_type="text/plain",
        )

    def fake_extract_text(content, _content_type, _url):
        return content.decode("utf-8")

    monkeypatch.setattr(import_service, "_fetch_source", fake_fetch)
    monkeypatch.setattr(import_service, "_extract_text", fake_extract_text)

    summary = bootstrap_burgos_demo_ordinances(db)
    second_summary = bootstrap_burgos_demo_ordinances(db)

    assert summary["official_source_domain"] == BOP_BURGOS_DOMAIN
    assert summary["metrics"]["demo_ordinances"] == len(DEMO_ORDINANCE_SOURCES)
    assert summary["metrics"]["approved_ordinances"] == len(DEMO_ORDINANCE_SOURCES)
    assert summary["metrics"]["approved_ready_chunks"] >= len(DEMO_ORDINANCE_SOURCES)
    assert second_summary["metrics"] == summary["metrics"]

    ordinances = db.query(Ordinance).all()
    assert len(ordinances) == len(DEMO_ORDINANCE_SOURCES)
    assert {ordinance.curation_status for ordinance in ordinances} == {"approved"}
    assert {ordinance.status for ordinance in ordinances} == {"active"}
    assert all(ordinance.source_url for ordinance in ordinances)
    assert all("validación jurídica humana" in ordinance.legal_review_notes for ordinance in ordinances)

    chunks = db.query(OrdinanceLegalChunk).all()
    assert chunks
    assert {chunk.review_status for chunk in chunks} == {"approved"}
    assert {chunk.embedding_status for chunk in chunks} == {"ready"}
    assert summary["smoke_searches"][0]["results"] > 0
