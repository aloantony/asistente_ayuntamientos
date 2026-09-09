"use client";

import { useEffect, useState } from "react";
import { adminRequest } from "../lib/api";

type Coverage = {
  checked_at: string;
  notice: string;
  provinces: Array<{ code: string; name: string; ordinances: number; municipalities: number; with_reviewed_text: number }>;
};

export function OrdinanceCoverage() {
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void adminRequest<Coverage>("/ordinances/coverage/cyl", "", "No se pudo consultar la cobertura.", { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setCoverage(result); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, []);
  return <details>
    <summary>Cobertura de la biblioteca en Castilla y León</summary>
    {error ? <p role="alert">No se pudo consultar la cobertura actual.</p> : !coverage ? <p role="status">Consultando biblioteca…</p> : <>
      <p>{coverage.notice}</p>
      <p>Comprobación de la biblioteca: {new Date(coverage.checked_at).toLocaleString("es-ES")}. La cobertura de los boletines oficiales está pendiente de verificación.</p>
      <div style={{ overflowX: "auto" }}><table>
        <caption>Textos revisados disponibles por provincia</caption>
        <thead><tr><th scope="col">Provincia</th><th scope="col">Ordenanzas</th><th scope="col">Municipios</th><th scope="col">Con fragmentos revisados</th></tr></thead>
        <tbody>{coverage.provinces.map((province) => <tr key={province.code}>
          <th scope="row">{province.name}</th><td>{province.ordinances}</td><td>{province.municipalities}</td><td>{province.with_reviewed_text}</td>
        </tr>)}</tbody>
      </table></div>
    </>}
  </details>;
}
