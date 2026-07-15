import type {
  MaintenanceOrder,
  MaintenanceOrderCreate,
  MaintenanceOrderDetail,
  MaintenanceOrderTransition,
  MaintenanceOrderUpdate,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

type MaintenanceOrderListFilters = {
  organizationId: number;
  assetId: number;
  includeClosed?: boolean;
  limit?: number;
};

export function fetchMaintenanceOrders(
  filters: MaintenanceOrderListFilters,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(filters.organizationId),
    asset_id: String(filters.assetId),
    include_closed: String(filters.includeClosed ?? true),
    limit: String(filters.limit ?? 100),
  });

  return adminRequestWithTotal<MaintenanceOrder[]>(
    `/maintenance/orders?${params.toString()}`,
    "",
    "No se pudieron cargar las órdenes de mantenimiento.",
    { signal },
  );
}

export function fetchMaintenanceOrder(orderId: number, signal?: AbortSignal) {
  return adminRequest<MaintenanceOrderDetail>(
    `/maintenance/orders/${orderId}`,
    "",
    "No se pudo cargar el historial de mantenimiento.",
    { signal },
  );
}

export function createMaintenanceOrder(
  accessToken: string,
  payload: MaintenanceOrderCreate,
  signal?: AbortSignal,
) {
  return adminRequest<MaintenanceOrderDetail>(
    "/maintenance/orders",
    accessToken,
    "No se pudo programar el mantenimiento.",
    {
      method: "POST",
      body: JSON.stringify(payload),
      signal,
    },
  );
}

export function updateMaintenanceOrder(
  accessToken: string,
  orderId: number,
  payload: MaintenanceOrderUpdate,
  signal?: AbortSignal,
) {
  return adminRequest<MaintenanceOrderDetail>(
    `/maintenance/orders/${orderId}`,
    accessToken,
    "No se pudo actualizar la orden de mantenimiento.",
    {
      method: "PATCH",
      body: JSON.stringify(payload),
      signal,
    },
  );
}

export function transitionMaintenanceOrder(
  accessToken: string,
  orderId: number,
  payload: MaintenanceOrderTransition,
  signal?: AbortSignal,
) {
  return adminRequest<MaintenanceOrderDetail>(
    `/maintenance/orders/${orderId}/transition`,
    accessToken,
    "No se pudo cambiar el estado de la orden de mantenimiento.",
    {
      method: "POST",
      body: JSON.stringify({
        status: payload.status,
        note: payload.note?.trim() || undefined,
        scheduled_for: payload.scheduled_for || undefined,
      }),
      signal,
    },
  );
}
