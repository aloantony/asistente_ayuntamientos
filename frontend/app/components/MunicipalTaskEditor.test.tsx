// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const request = vi.fn();
const optionsRequest = vi.fn();
vi.mock("../lib/api", () => ({ adminRequest: (...args: unknown[]) => request(...args), adminRequestWithTotal: (...args: unknown[]) => optionsRequest(...args) }));
const { MunicipalTaskEditor } = await import("./MunicipalTaskEditor");
afterEach(() => { cleanup(); request.mockReset(); optionsRequest.mockReset(); });
beforeEach(() => optionsRequest.mockResolvedValue({ items: [], total: 0 }));
const task = { id: 1, organization_id: 2, title: "Revisar parque", description: null, status: "pending", priority: "normal", due_date: null, events: [] };
const props = { organizationId: 2, canEdit: true, canManage: false, onClose: vi.fn(), onSaved: vi.fn() };

describe("Gestión de tareas humanas", () => {
  it("crea la tarea en el mismo servicio que usa el asistente", async () => {
    request.mockResolvedValue(task);
    render(<MunicipalTaskEditor {...props} taskId="new" />);
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Revisar parque" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    await waitFor(() => expect(request).toHaveBeenCalledOnce());
    expect(request.mock.calls[0][0]).toBe("/tasks");
    expect(JSON.parse(request.mock.calls[0][3].body)).toMatchObject({ title: "Revisar parque", organization_id: 2 });
  });
  it("exige un motivo al bloquear y usa la transición auditada", async () => {
    request.mockResolvedValueOnce(task).mockResolvedValueOnce({ ...task, status: "blocked" });
    render(<MunicipalTaskEditor {...props} taskId={1} />);
    await screen.findByDisplayValue(task.title);
    fireEvent.click(screen.getByRole("button", { name: "Bloqueada" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(request).toHaveBeenCalledOnce();
    fireEvent.change(screen.getByLabelText("Motivo del cambio"), { target: { value: "Falta suministro" } });
    fireEvent.click(screen.getByRole("button", { name: "Bloqueada" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(request.mock.calls[1][0]).toBe("/tasks/1/transition");
    expect(JSON.parse(request.mock.calls[1][3].body)).toEqual({ status: "blocked", reason: "Falta suministro" });
  });
  it("solo ofrece lectura cuando faltan permisos de edición", async () => {
    request.mockResolvedValue(task);
    render(<MunicipalTaskEditor {...props} taskId={1} canEdit={false} />);
    await screen.findByDisplayValue(task.title);
    expect(screen.queryByRole("button", { name: "Guardar tarea" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Bloqueada" })).toBeNull();
  });
});


it("carga opciones paginadas y guarda responsable y proyecto", async () => {
  optionsRequest.mockImplementation(async (path: string) => path.includes("kind=worker")
    ? path.includes("offset=0") ? { items: [{ id: 7, name: "Ana" }], total: 2 } : { items: [{ id: 8, name: "Luis" }], total: 2 }
    : { items: [{ id: 9, name: "Parque" }], total: 1 });
  request.mockResolvedValue(task);
  render(<MunicipalTaskEditor {...props} taskId="new" />);
  await screen.findByRole("option", { name: "Luis" });
  fireEvent.change(screen.getByLabelText("Responsable"), { target: { value: "8" } });
  fireEvent.change(screen.getByLabelText("Proyecto"), { target: { value: "9" } });
  fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Revisión" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
  await waitFor(() => expect(request).toHaveBeenCalledOnce());
  expect(JSON.parse(request.mock.calls[0][3].body)).toMatchObject({ assignee_worker_id: 8, project_id: 9 });
});

it("conserva relaciones existentes si la carga de opciones falla", async () => {
  optionsRequest.mockRejectedValue(new Error("sin acceso"));
  request.mockResolvedValue({ ...task, assignee_worker_id: 7, project_id: 9, assignee: { id: 7, full_name: "Ana" }, project: { id: 9, name: "Parque" } });
  render(<MunicipalTaskEditor {...props} taskId={1} />);
  await screen.findByDisplayValue(task.title);
  await screen.findByText(/Las relaciones actuales se conservan/);
  fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Revisión actualizada" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  const patch = JSON.parse(request.mock.calls[1][3].body);
  expect(patch).not.toHaveProperty("assignee_worker_id");
  expect(patch).not.toHaveProperty("project_id");
});
