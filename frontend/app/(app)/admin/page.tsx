"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useSession } from "../../lib/session";
import { getAdminNavItems } from "./nav";

// /admin redirige a la primera sección visible según los permisos del
// usuario, con la misma prioridad que las pestañas de la subnavegación.
export default function AdminIndexPage() {
  const router = useRouter();
  const { user } = useSession();
  const targetHref = user ? getAdminNavItems(user)[0]?.href : undefined;

  useEffect(() => {
    if (targetHref) {
      router.replace(targetHref);
    }
  }, [targetHref, router]);

  if (!user || !targetHref) {
    return (
      <section className="panel">
        <p className="eyebrow">Administración</p>
        <h2>Acceso restringido</h2>
        <p className="muted">No tienes permisos de administración.</p>
      </section>
    );
  }

  return null;
}
