import { describe, expect, it } from "vitest";
import {
  buildMapViewHref,
  resolveMapView,
} from "./MapPanel";
import {
  buildWorkspaceTabHref,
  resolveWorkspaceTab,
} from "./MunicipalWorkspace";

describe("municipal workspace URL tabs", () => {
  it("derives the active tab from each query snapshot and rejects unknown tabs", () => {
    expect(resolveWorkspaceTab(new URLSearchParams("tab=facilities"))).toBe(
      "facilities",
    );
    expect(resolveWorkspaceTab(new URLSearchParams("tab=unknown"))).toBe(
      "summary",
    );
    expect(resolveWorkspaceTab(new URLSearchParams())).toBe("summary");
  });

  it("changes only the tab parameter", () => {
    const params = new URLSearchParams("organization_id=8&tab=people&source=home");

    expect(buildWorkspaceTabHref("/ayuntamiento", params, "roadmap")).toBe(
      "/ayuntamiento?organization_id=8&tab=roadmap&source=home",
    );
    expect(buildWorkspaceTabHref("/ayuntamiento", params, "summary")).toBe(
      "/ayuntamiento?organization_id=8&source=home",
    );
  });

  // Los apartados que crea el ayuntamiento viajan en la misma URL que las
  // áreas fijas y se aceptan por su forma, porque el árbol del menú todavía
  // no ha llegado cuando se lee la dirección (ADR-052).
  it("accepts the organization's own sections and rejects malformed ones", () => {
    expect(resolveWorkspaceTab(new URLSearchParams("tab=block-12"))).toBe(
      "block-12",
    );
    expect(resolveWorkspaceTab(new URLSearchParams("tab=block-"))).toBe(
      "summary",
    );
    expect(resolveWorkspaceTab(new URLSearchParams("tab=block-abc"))).toBe(
      "summary",
    );
  });

  it("puts a custom section in the tab parameter like any other tab", () => {
    const params = new URLSearchParams("organization_id=8&tab=people");

    expect(buildWorkspaceTabHref("/ayuntamiento", params, "block-12")).toBe(
      "/ayuntamiento?organization_id=8&tab=block-12",
    );
  });
});

describe("map URL views", () => {
  it("derives the authorized view from each query snapshot", () => {
    expect(
      resolveMapView(
        new URLSearchParams("view=municipalities"),
        true,
        true,
      ),
    ).toBe("municipalities");
    expect(resolveMapView(new URLSearchParams(), true, true)).toBe("territory");
    expect(
      resolveMapView(
        new URLSearchParams("view=municipalities"),
        true,
        false,
      ),
    ).toBe("territory");
    expect(resolveMapView(new URLSearchParams(), false, true)).toBe(
      "municipalities",
    );
  });

  it("changes only the view parameter and preserves map focus", () => {
    const params = new URLSearchParams(
      "view=municipalities&lat=40.4&lng=-3.7&zoom=14&organization_id=5",
    );

    expect(buildMapViewHref("/mapa", params, "territory")).toBe(
      "/mapa?lat=40.4&lng=-3.7&zoom=14&organization_id=5",
    );
    expect(buildMapViewHref("/mapa", params, "municipalities")).toBe(
      "/mapa?view=municipalities&lat=40.4&lng=-3.7&zoom=14&organization_id=5",
    );
  });
});
