import { Suspense } from "react";
import { OrdinanceWorkspace } from "../../components/OrdinanceWorkspace";

export default function OrdenanzasPage() {
  return (
    <Suspense
      fallback={
        <section className="panel">
          <p className="eyebrow">Repositorio normativo municipal</p>
          <h1>Cargando ordenanzas</h1>
          <p className="muted">Preparando la consulta jurídica.</p>
        </section>
      }
    >
      <OrdinanceWorkspace />
    </Suspense>
  );
}
