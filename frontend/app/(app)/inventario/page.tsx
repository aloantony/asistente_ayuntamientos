"use client";

import { AssetInventory } from "../../components/AssetInventory";
import { useSession } from "../../lib/session";
import { useSearchParams } from "next/navigation";

export default function InventarioPage() {
  const { user } = useSession();
  const searchParams = useSearchParams();

  if (!user) {
    return null;
  }

  const requestedOrganizationId = Number(
    searchParams.get("organization_id"),
  );

  return (
    <AssetInventory
      initialOrganizationId={
        Number.isInteger(requestedOrganizationId) && requestedOrganizationId > 0
          ? requestedOrganizationId
          : null
      }
      user={user}
    />
  );
}
