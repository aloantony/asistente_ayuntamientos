// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildReferenceLayerTree,
  type ReferenceCatalog,
  type ReferenceLayer,
} from "../lib/referenceLayers";
import { SiurLayerTree } from "./SiurLayerTree";

afterEach(cleanup);

const LAYER_BASE: ReferenceLayer = {
  id: 1,
  service_id: null,
  parent_id: null,
  source_key: "group:root",
  node_type: "group",
  title: "Urbanismo",
  description: null,
  role: null,
  renderer: null,
  delivery_mode: null,
  bounds_json: null,
  sort_order: 0,
  default_visible: false,
  default_opacity: 1,
  effective_visible: false,
  effective_opacity: 1,
  min_zoom: null,
  max_zoom: null,
  downloadable: false,
  delivery_available: false,
  identify_available: false,
  delivery_blocker: null,
  available_style_ids: [],
  legend_available: false,
  metadata_available: false,
  source_substitution_status: null,
  source_substitution_notice: null,
  source_substitution_selected_layer: null,
  source_substitution_profile: null,
  source_substitution_scope: null,
  source_substitution_attribution: null,
  source_substitution_content_sha256: null,
  mirror_status: "not_applicable",
  active_version_id: null,
  active_generation: null,
  active_source_version: null,
  active_reference_at: null,
  active_created_at: null,
  last_run_status: null,
  last_checked_at: null,
  last_sync_error_code: null,
  last_sync_error_summary: null,
  next_check_at: null,
  status: "active",
  updated_at: "2026-07-17T10:00:00Z",
};

function catalogFixture(): ReferenceCatalog {
  const visibleLayer: ReferenceLayer = {
    ...LAYER_BASE,
    id: 2,
    service_id: 8,
    parent_id: 1,
    source_key: "layer:zoning",
    node_type: "layer",
    title: "Clasificación del suelo",
    role: "overlay",
    renderer: "raster_tile",
    delivery_mode: "proxy",
    delivery_available: true,
    identify_available: true,
    available_style_ids: [12],
    legend_available: true,
    metadata_available: true,
    mirror_status: "active",
    active_version_id: 17,
    active_generation: 2,
    active_source_version: "2026-07-23",
    active_reference_at: LAYER_BASE.updated_at,
    active_created_at: LAYER_BASE.updated_at,
    last_run_status: "succeeded",
    last_checked_at: LAYER_BASE.updated_at,
  };
  const blockedLayer: ReferenceLayer = {
    ...visibleLayer,
    id: 3,
    source_key: "layer:blocked",
    title: "Inventario pendiente",
    delivery_available: false,
    delivery_blocker: "license_not_approved",
    available_style_ids: [],
    metadata_available: false,
  };
  return {
    snapshot: {
      id: 9,
      provider_key: "siur",
      content_sha256: "a".repeat(64),
      definition_sha256: "b".repeat(64),
      retrieved_at: LAYER_BASE.updated_at,
      service_count: 1,
      group_count: 1,
      layer_count: 2,
      unresolved_count: 0,
      status: "applied",
      is_current: true,
    },
    organization_id: 7,
    services: [
      {
        id: 8,
        title: "Servicio",
        upstream_protocol: "wms",
        attribution: null,
        status: "active",
        updated_at: LAYER_BASE.updated_at,
      },
    ],
    layers: [LAYER_BASE, visibleLayer, blockedLayer],
    styles: [
      {
        id: 12,
        layer_id: 2,
        title: "Color aprobado",
        description: null,
        sort_order: 0,
        is_default: true,
        legend_available: true,
        status: "active",
        updated_at: LAYER_BASE.updated_at,
      },
    ],
  };
}

describe("SiurLayerTree", () => {
  it("renders a separate recursive tree including blocked nodes and approved controls", () => {
    const catalog = catalogFixture();
    const tree = buildReferenceLayerTree(catalog.layers);
    const onControlChange = vi.fn();
    const onMove = vi.fn();
    const { container } = render(
      <SiurLayerTree
        catalog={catalog}
        error=""
        isLoading={false}
        onControlChange={onControlChange}
        onMove={onMove}
        preferences={{
          layers: {
            "2": { visible: true, opacity: 0.7, styleId: 12 },
            "3": { visible: false, opacity: 1, styleId: null },
          },
          stackOrder: [2, 3],
        }}
        structuralWarnings={[]}
        tree={tree.roots}
      />,
    );

    expect(screen.getByRole("heading", { name: "Cartografía SIUR" })).toBeTruthy();
    expect(screen.getByText("Urbanismo")).toBeTruthy();
    expect(screen.getByText("Clasificación del suelo")).toBeTruthy();
    expect(screen.getByText("Inventario pendiente")).toBeTruthy();
    expect(screen.getByText(/licencia todavía no tiene aprobación humana/i)).toBeTruthy();
    expect(container.querySelector(".map-layer-chips")).toBeNull();
    expect(
      screen.getByRole("link", { name: "Metadatos" }).getAttribute("href"),
    ).toBe(
      "/api/organizations/7/reference-layers/2/metadata.json",
    );

    fireEvent.change(
      screen.getByRole("slider", {
        name: "Opacidad de Clasificación del suelo",
      }),
      { target: { value: "0.45" } },
    );
    expect(onControlChange).toHaveBeenCalledWith(2, {
      visible: true,
      opacity: 0.45,
      styleId: 12,
    });
    expect(
      screen.getByRole("option", { name: "Color aprobado" }).getAttribute("value"),
    ).toBe("12");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Traer Clasificación del suelo delante",
      }),
    );
    expect(onMove).toHaveBeenCalledWith(2, "forward");
  });

  it("explains a failed refresh while keeping the previous local version", () => {
    const catalog = catalogFixture();
    catalog.layers[2] = {
      ...catalog.layers[2],
      delivery_blocker: "local_not_ready",
      mirror_status: "serving_previous",
      active_version_id: 19,
      active_generation: 3,
      last_run_status: "failed",
    };
    const tree = buildReferenceLayerTree(catalog.layers);

    render(
      <SiurLayerTree
        catalog={catalog}
        error=""
        isLoading={false}
        onControlChange={vi.fn()}
        onMove={vi.fn()}
        preferences={{
          layers: {
            "2": { visible: true, opacity: 1, styleId: 12 },
            "3": { visible: false, opacity: 1, styleId: null },
          },
          stackOrder: [2, 3],
        }}
        structuralWarnings={[]}
        tree={tree.roots}
      />,
    );

    expect(screen.getByText(/se mantiene la versión local anterior/i)).toBeTruthy();
    expect(screen.getByText(/copia local todavía no está lista/i)).toBeTruthy();
  });

  it("shows the explicit coverage degradation for a reviewed ortho substitute", () => {
    const catalog = catalogFixture();
    catalog.layers[1] = {
      ...catalog.layers[1],
      source_substitution_status: "substitute_degraded",
      source_substitution_notice:
        "SIGPAC agrupa vuelos 1997-2003 y no equivale a un mosaico anual completo.",
      source_substitution_selected_layer: "SIGPAC",
      source_substitution_profile: "ign-pnoa-historico-ortofoto-2002-v1",
      source_substitution_scope: "active_delivery",
      source_substitution_attribution:
        "Obra derivada de Orto-SIGPAC 1997-2003 CC-BY 4.0 scne.es",
      source_substitution_content_sha256: "a".repeat(64),
    };
    catalog.layers[2] = {
      ...catalog.layers[2],
      title: "Ortofoto 2021",
      delivery_blocker: "reviewed_ortho_substitution_blocked",
      source_substitution_status: "blocked",
      source_substitution_notice:
        "PNOA2021 no declara cobertura en Castilla y León y no se configura.",
      source_substitution_selected_layer: "PNOA2021",
      source_substitution_profile: "ign-pnoa-historico-ortofoto-2021-v1",
    };
    const tree = buildReferenceLayerTree(catalog.layers);

    render(
      <SiurLayerTree
        catalog={catalog}
        error=""
        isLoading={false}
        onControlChange={vi.fn()}
        onMove={vi.fn()}
        preferences={{
          layers: {
            "2": { visible: true, opacity: 1, styleId: 12 },
            "3": { visible: false, opacity: 1, styleId: null },
          },
          stackOrder: [2, 3],
        }}
        structuralWarnings={[]}
        tree={tree.roots}
      />,
    );

    expect(
      screen.getByText("Entrega local sustitutiva degradada"),
    ).toBeTruthy();
    expect(screen.getByText(/no equivale a un mosaico anual completo/i)).toBeTruthy();
    expect(screen.getByText("SIGPAC", { selector: "code" })).toBeTruthy();
    expect(
      screen.getByText(/clasificación de los bytes locales activos/i),
    ).toBeTruthy();
    expect(
      screen.getByText(/obra derivada de Orto-SIGPAC 1997-2003/i),
    ).toBeTruthy();
    expect(screen.getByText("Sustitución IGN bloqueada")).toBeTruthy();
    expect(screen.getByText(/no declara cobertura en Castilla y León/i)).toBeTruthy();
    expect(screen.getByText("PNOA2021", { selector: "code" })).toBeTruthy();
  });

  it("keeps incomplete style delivery disabled and exposes its technical reason", () => {
    const catalog = catalogFixture();
    catalog.layers[1] = {
      ...catalog.layers[1],
      delivery_available: false,
      delivery_blocker: "style_unsupported",
    };
    const tree = buildReferenceLayerTree(catalog.layers);

    render(
      <SiurLayerTree
        catalog={catalog}
        error=""
        isLoading={false}
        onControlChange={vi.fn()}
        onMove={vi.fn()}
        preferences={{
          layers: {
            "2": { visible: true, opacity: 1, styleId: 12 },
            "3": { visible: false, opacity: 1, styleId: null },
          },
          stackOrder: [2, 3],
        }}
        structuralWarnings={[]}
        tree={tree.roots}
      />,
    );

    const checkbox = screen.getByRole("checkbox", {
      name: "Clasificación del suelo",
    });
    expect(checkbox.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("style_unsupported")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Ver leyenda" }),
    ).toBeNull();
  });

  it("blocks a nominally available layer when one current style is missing", () => {
    const catalog = catalogFixture();
    catalog.styles.push({
      ...catalog.styles[0],
      id: 13,
      is_default: false,
      sort_order: 1,
      title: "Trama alternativa",
    });
    const tree = buildReferenceLayerTree(catalog.layers);

    render(
      <SiurLayerTree
        catalog={catalog}
        error=""
        isLoading={false}
        onControlChange={vi.fn()}
        onMove={vi.fn()}
        preferences={{
          layers: {
            "2": { visible: true, opacity: 1, styleId: 12 },
            "3": { visible: false, opacity: 1, styleId: null },
          },
          stackOrder: [2, 3],
        }}
        structuralWarnings={[]}
        tree={tree.roots}
      />,
    );

    expect(
      screen.getByRole("checkbox", {
        name: "Clasificación del suelo",
      }).hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.getByText("style_coverage_incomplete")).toBeTruthy();
    expect(screen.queryByRole("slider")).toBeNull();
  });
});
