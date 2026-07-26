// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildReferenceTileUrl,
  type SiurMapLayer,
} from "../lib/referenceLayers";
import { LocalBaseMapSelect } from "./LocalBaseMapSelect";

afterEach(cleanup);

function makeLayer(
  layerId: number,
  title: string,
  overrides: Partial<SiurMapLayer> = {},
): SiurMapLayer {
  const layer: SiurMapLayer = {
    organizationId: 7,
    layerId,
    role: "base",
    title,
    tileUrl: buildReferenceTileUrl(7, layerId),
    styleId: null,
    attribution: null,
    bounds: null,
    minZoom: null,
    maxZoom: null,
    opacity: 1,
    visible: layerId === 10,
    identifyAvailable: false,
    zIndex: layerId - 9,
    ...overrides,
  };
  return layer;
}

describe("LocalBaseMapSelect", () => {
  it("renders all three local bases by canonical order from shuffled input", () => {
    const image = makeLayer(10, "IMAGEN", { zIndex: 1 });
    const map = makeLayer(11, "MAPA", { zIndex: 2 });
    const relief = makeLayer(12, "RELIEVE", { zIndex: 3 });
    const hidden = makeLayer(13, "OCULTO", { opacity: 0, zIndex: 4 });
    const unavailable = makeLayer(14, "REMOTO", {
      tileUrl: "https://tiles.example.test/{z}/{x}/{y}.png",
      zIndex: 5,
    });
    const overlay = makeLayer(15, "Superposición", {
      role: "overlay",
      zIndex: 6,
    });

    render(
      <LocalBaseMapSelect
        layers={[relief, unavailable, overlay, map, hidden, image]}
        onSelect={() => undefined}
        selectedLayerId={image.layerId}
      />,
    );

    const select = screen.getByRole("combobox", { name: "Mapa base" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Sin fondo", "IMAGEN", "MAPA", "RELIEVE"]);
    expect((select as HTMLSelectElement).value).toBe(String(image.layerId));
  });

  it("emits stable ids and an explicit no-background selection", () => {
    const onSelect = vi.fn();
    render(
      <LocalBaseMapSelect
        layers={[
          makeLayer(10, "IMAGEN"),
          makeLayer(11, "MAPA", { visible: false }),
          makeLayer(12, "RELIEVE", { visible: false }),
        ]}
        onSelect={onSelect}
        selectedLayerId={10}
      />,
    );

    const select = screen.getByRole("combobox", { name: "Mapa base" });
    fireEvent.change(select, { target: { value: "12" } });
    fireEvent.change(select, { target: { value: "" } });

    expect(onSelect).toHaveBeenNthCalledWith(1, 12);
    expect(onSelect).toHaveBeenNthCalledWith(2, null);
  });
});
