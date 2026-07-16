import type {
  MaintenanceOrder,
  MaintenanceOrderCreate,
  MaintenanceOrderDetail,
  MaintenanceOrderPriority,
  MaintenanceOrderStatus,
  MaintenanceOrderTransition,
  MaintenanceOrderUpdate,
  MaintenanceType,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

export type MaintenanceOrderListFilters = {
  organizationId: number;
  assetId?: number;
  status?: MaintenanceOrderStatus;
  priority?: MaintenanceOrderPriority;
  maintenanceType?: MaintenanceType;
  assignedToId?: number;
  scheduledFrom?: string;
  scheduledTo?: string;
  query?: string;
  includeClosed?: boolean;
  limit?: number;
  offset?: number;
};

export function fetchMaintenanceOrders(
  filters: MaintenanceOrderListFilters,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(filters.organizationId),
    include_closed: String(filters.includeClosed ?? true),
    limit: String(filters.limit ?? 100),
    offset: String(filters.offset ?? 0),
  });

  if (filters.assetId !== undefined) {
    params.set("asset_id", String(filters.assetId));
  }
  if (filters.status) {
    params.set("status", filters.status);
  }
  if (filters.priority) {
    params.set("priority", filters.priority);
  }
  if (filters.maintenanceType) {
    params.set("maintenance_type", filters.maintenanceType);
  }
  if (filters.assignedToId !== undefined) {
    params.set("assigned_to_id", String(filters.assignedToId));
  }
  if (filters.scheduledFrom) {
    params.set("scheduled_from", filters.scheduledFrom);
  }
  if (filters.scheduledTo) {
    params.set("scheduled_to", filters.scheduledTo);
  }
  if (filters.query?.trim()) {
    params.set("q", filters.query.trim());
  }

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
