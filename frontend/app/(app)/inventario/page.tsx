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

  const requestedAssetId = Number(searchParams.get("asset_id"));

  return (
    <AssetInventory
      key={`${requestedOrganizationId}:${user.id}`}
      initialAssetId={Number.isInteger(requestedAssetId) && requestedAssetId > 0 ? requestedAssetId : null}
      initialOrganizationId={
        Number.isInteger(requestedOrganizationId) && requestedOrganizationId > 0
          ? requestedOrganizationId
          : null
      }
      user={user}
    />
  );
}
