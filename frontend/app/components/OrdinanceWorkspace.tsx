"use client";

import { ArrowLeft } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import { OrdinanceCatalog } from "./OrdinanceCatalog";
import { OrdinanceLibrary } from "./OrdinanceLibrary";
import styles from "./OrdinanceCatalog.module.css";

export function OrdinanceWorkspace() {
  const searchParams = useSearchParams();
  const [advancedSearchOpen, setAdvancedSearchOpen] = useState(
    () => searchParams.has("q") || searchParams.has("compare"),
  );

  if (advancedSearchOpen) {
    return (
      <div className={styles.advancedWorkspace}>
        <button
          className={styles.backToCatalog}
          onClick={() => setAdvancedSearchOpen(false)}
          type="button"
        >
          <ArrowLeft aria-hidden="true" />
          Volver al catálogo
        </button>
        <OrdinanceLibrary />
      </div>
    );
  }

  return (
    <OrdinanceCatalog
      onOpenAdvancedSearch={() => setAdvancedSearchOpen(true)}
    />
  );
}
