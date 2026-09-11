import { describe, expect, it } from "vitest";
import type { Ordinance } from "./types";
import {
  classifyOrdinance,
  compactOrdinanceTitle,
} from "./OrdinanceCatalog";

function ordinance(overrides: Partial<Ordinance> = {}): Ordinance {
  return {
    id: 1,
    municipality_id: 1,
    document_id: null,
    title: "Ordenanza municipal",
    topic: "ordenanzas municipales",
    subtopic: null,
    ordinance_type: "ordinance",
    summary: null,
    source_url: null,
    official_bulletin: null,
    bulletin_number: null,
    approval_date: null,
    publication_date: null,
    effective_date: null,
    status: "active",
    curation_status: "approved",
    import_job_id: null,
    source_hash: null,
    extraction_status: "extracted",
    confidence_score: null,
    notes: null,
    legal_review_notes: null,
    created_by_id: null,
    updated_by_id: null,
    municipality: {
      id: 1,
      name: "Fuentelcésped",
      province: "Burgos",
      autonomous_community: "Castilla y León",
    },
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("OrdinanceCatalog", () => {
  it("prioritizes specific subject matter over a generic topic", () => {
    expect(
      classifyOrdinance(
        ordinance({ title: "Ordenanza fiscal IBI", topic: "Ordenanzas municipales" }),
      ),
    ).toBe("tax");
    expect(
      classifyOrdinance(
        ordinance({ title: "Ordenanza fiscal Agua", topic: "Ordenanzas municipales" }),
      ),
    ).toBe("environment");
    expect(
      classifyOrdinance(
        ordinance({
          title: "Ordenanza fiscal IBI",
          summary: "Documento compartido sobre agua, basuras e impuestos.",
        }),
      ),
    ).toBe("tax");
  });

  it("classifies the remaining municipal regulations in their general group", () => {
    expect(classifyOrdinance(ordinance())).toBe("municipal");
  });

  it("shortens municipal suffixes for the compact list", () => {
    expect(
      compactOrdinanceTitle(
        "Ordenanza fiscal IBI - Fuentelcésped (BOP 30/12/2009)",
      ),
    ).toBe("Ordenanza fiscal IBI");
  });
});
