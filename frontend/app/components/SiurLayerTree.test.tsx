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
});
