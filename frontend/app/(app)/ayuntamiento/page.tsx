"use client";

import { shouldShowTownHallPanel, useSession } from "../../lib/session";

export default function AyuntamientoPage() {
  const { user } = useSession();

  if (!user) {
    return null;
  }

  if (!shouldShowTownHallPanel(user)) {
    return (
      <div className="workspace">
        <section className="panel">
          <p className="eyebrow">Ayuntamiento</p>
          <h2>Acceso restringido</h2>
          <p className="muted">
            No tienes permisos para ver la información del Ayuntamiento.
          </p>
        </section>
      </div>
    );
  }

  return (
    <div className="townhall">
      <section className="panel">
        <p className="eyebrow">Ayuntamiento</p>
        <h2>Sin apartados todavía</h2>
        <p className="muted">
          Aquí vivirá la información del municipio. El menú de navegación se
          configura desde esta misma pantalla.
        </p>
      </section>
    </div>
  );
}
