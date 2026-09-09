// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
const request = vi.fn();
vi.mock("../lib/api", () => ({ adminRequest: (...args: unknown[]) => request(...args) }));
const { AssetDetailPanel } = await import("./AssetDetailPanel");
afterEach(() => { cleanup(); request.mockReset(); });
const asset = { id: 7, organization_id: 2, name: "Fuente de la plaza", asset_type: { name: "Fuente", category: { name: "Equipamiento" } } };

describe("Ficha del inventario", () => {
  it("abre un activo fuera de la página del listado y conserva el enlace al mapa", async () => {
    request.mockResolvedValue(asset);
    render(<AssetDetailPanel assetId={7} organizationId={2} canViewMap />);
    await screen.findByRole("heading", { name: "Fuente de la plaza" });
    expect(request.mock.calls[0][0]).toBe("/assets/7");
    expect(screen.getByRole("link", { name: "Abrir en el mapa" }).getAttribute("href")).toBe("/mapa?organization_id=2&entity_type=asset&entity_id=7");
    expect(screen.queryByRole("button", { name: "Editar elemento" })).toBeNull();
  });
  it("no muestra una ficha de otra organización", async () => {
    request.mockResolvedValue(asset);
    render(<AssetDetailPanel assetId={7} organizationId={3} canViewMap={false} />);
    await screen.findByRole("alert");
    expect(screen.queryByText(asset.name)).toBeNull();
  });
  it("oculta inmediatamente la ficha anterior al cambiar el enlace", async () => {
    request.mockResolvedValueOnce(asset).mockImplementationOnce(() => new Promise(() => {}));
    const view = render(<AssetDetailPanel assetId={7} organizationId={2} canViewMap />);
    await screen.findByText(asset.name);
    view.rerender(<AssetDetailPanel assetId={8} organizationId={2} canViewMap />);
    expect(screen.queryByText(asset.name)).toBeNull();
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect((request.mock.calls[0][3] as { signal: AbortSignal }).signal.aborted).toBe(true);
  });
});
