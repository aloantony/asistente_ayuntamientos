"use client";

import { MapPanel } from "../../components/MapPanel";
import { useSession } from "../../lib/session";

export default function MapaPage() {
  const { user } = useSession();

  if (!user) {
    return null;
  }

  return (
    <div className="workspace map-workspace">
      <MapPanel user={user} />
    </div>
  );
}
