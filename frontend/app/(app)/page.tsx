"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { getDefaultRouteForUser, useSession } from "../lib/session";

export default function HomePage() {
  const router = useRouter();
  const { user } = useSession();

  useEffect(() => {
    if (!user) {
      return;
    }

    router.replace(getDefaultRouteForUser(user));
  }, [user, router]);

  // Mientras el efecto redirige a la sección por defecto, se muestra el
  // mismo panel de carga que el layout para no dejar el contenido en blanco.
  return (
    <section className="panel">
      <p className="eyebrow">Plataforma privada municipal</p>
      <h1>Comprobando sesión</h1>
      <p className="muted">Validando tus credenciales guardadas.</p>
    </section>
  );
}
