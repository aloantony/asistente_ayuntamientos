"use client";

import { useSearchParams } from "next/navigation";
import { MaintenanceDashboard } from "../../components/MaintenanceDashboard";

export default function MantenimientoPage() {
  const searchParams = useSearchParams();
  const requestedOrganizationId = Number(
    searchParams.get("organization_id"),
  );

  return (
    <MaintenanceDashboard
      initialOrganizationId={
        Number.isInteger(requestedOrganizationId) && requestedOrganizationId > 0
          ? requestedOrganizationId
          : null
      }
    />
  );
}
