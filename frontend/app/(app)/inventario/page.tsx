"use client";

import { AssetInventory } from "../../components/AssetInventory";
import { useSession } from "../../lib/session";

export default function InventarioPage() {
  const { user } = useSession();

  if (!user) {
    return null;
  }

  return <AssetInventory user={user} />;
}
