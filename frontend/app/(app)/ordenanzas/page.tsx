import { Suspense } from "react";
import { OrdinanceLibrary } from "../../components/OrdinanceLibrary";

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
      <OrdinanceLibrary />
    </Suspense>
  );
}
