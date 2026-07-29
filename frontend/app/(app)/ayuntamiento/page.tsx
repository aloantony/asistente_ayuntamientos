"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { townHallShieldUrl } from "../../lib/api";
import { TownHallBar } from "../../components/TownHallBar";
import { TownHallNavEditor } from "../../components/TownHallNavEditor";
import type { TownHallNavSection, User } from "../../components/types";
import {
  canEditTownHall,
  shouldShowTownHallPanel,
  useSession,
} from "../../lib/session";
import { useTownHallController } from "../../lib/useTownHallController";

// El municipio que rotula la barra cuando todavía no se ha fijado un nombre
// propio: el del municipio de la organización, o su nombre sin el prefijo.
function getFallbackMunicipalityName(user: User, organizationName: string) {
  const municipalityName = user.organizations?.[0]?.municipality?.name?.trim();

  if (municipalityName) {
    return municipalityName;
  }

  return organizationName.replace(/^Ayuntamiento\s+de\s+/i, "");
}

function findActiveTitle(nav: TownHallNavSection[], activeId: number | null) {
  if (activeId === null) {
    return null;
  }

  for (const section of nav) {
    if (section.id === activeId) {
      return section.title;
    }
    const item = section.items.find((candidate) => candidate.id === activeId);
    if (item) {
      return item.title;
    }
  }

  return null;
}

function AyuntamientoContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, handleRequestError } = useSession();
  const townHallController = useTownHallController({ handleRequestError });

  const canView = user !== null && shouldShowTownHallPanel(user);

  useEffect(() => {
    if (!canView) {
      return;
    }

    void townHallController.loadTownHall();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canView, user?.id]);

  if (!user) {
    return null;
  }

  if (!canView) {
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

  const { townHall, isLoadingTownHall, townHallError } = townHallController;

  // La selección viaja en la URL para que los enlaces profundos y el botón
  // atrás sigan funcionando.
  const rawSelection = searchParams.get("s");
  const parsedSelection = rawSelection === null ? Number.NaN : Number(rawSelection);
  const activeId = Number.isInteger(parsedSelection) ? parsedSelection : null;
  const isEditorOpen = searchParams.get("editor") === "1";

  function buildHref(selectedId: number | null, editorOpen: boolean) {
    const params = new URLSearchParams();

    if (selectedId !== null) {
      params.set("s", String(selectedId));
    }
    if (editorOpen) {
      params.set("editor", "1");
    }

    const query = params.toString();
    return query ? `/ayuntamiento?${query}` : "/ayuntamiento";
  }

  function handleSelect(blockId: number) {
    router.push(buildHref(blockId, isEditorOpen), { scroll: false });
  }

  function setEditorOpen(editorOpen: boolean) {
    router.push(buildHref(activeId, editorOpen), { scroll: false });
  }

  if (townHall === null) {
    return (
      <div className="townhall">
        <section className="panel">
          <p className="eyebrow">Ayuntamiento</p>
          <h2>{isLoadingTownHall ? "Cargando" : "Ayuntamiento"}</h2>
          <p className={townHallError ? "form-error" : "muted"}>
            {townHallError || "Recuperando la información del municipio."}
          </p>
        </section>
      </div>
    );
  }

  const activeTitle = findActiveTitle(townHall.nav, activeId);
  const canEdit = canEditTownHall(user);
  const fallbackName = getFallbackMunicipalityName(
    user,
    townHall.organization_name,
  );

  return (
    <div className="townhall">
      <TownHallBar
        activeId={activeId}
        canEdit={canEdit}
        fallbackName={fallbackName}
        onOpenEditor={() => setEditorOpen(true)}
        onSelect={handleSelect}
        onShieldDrop={(file) => void townHallController.uploadShield(file)}
        shieldUrl={
          townHall.profile.has_shield
            ? townHallShieldUrl(townHallController.shieldVersion)
            : null
        }
        townHall={townHall}
      />

      {canEdit && isEditorOpen ? (
        <TownHallNavEditor
          fallbackName={fallbackName}
          isSaving={townHallController.isSavingTownHall}
          onAddItem={(sectionId) =>
            void townHallController.addItem(sectionId, "Nuevo elemento")
          }
          onAddSection={() =>
            void townHallController.addSection("Nuevo apartado")
          }
          onArchiveBlock={(blockId) =>
            void townHallController.archiveBlock(blockId)
          }
          onClose={() => setEditorOpen(false)}
          onRenameBlock={(blockId, title) =>
            void townHallController.renameBlock(blockId, title)
          }
          onRenameMunicipality={(name) =>
            void townHallController.updateProfile({
              display_name: name === "" ? null : name,
            })
          }
          onReorder={(nav) => void townHallController.reorderNav(nav)}
          onToggleWeather={(enabled) =>
            void townHallController.updateProfile({ weather_enabled: enabled })
          }
          townHall={townHall}
        />
      ) : null}

      {townHallError ? <p className="form-error">{townHallError}</p> : null}

      <section className="panel">
        <p className="eyebrow">Ayuntamiento</p>
        <h2>{activeTitle ?? "Sin apartados todavía"}</h2>
        <p className="muted">
          {activeTitle
            ? "Este apartado aún no tiene contenido."
            : "Aquí vivirá la información del municipio. El menú de navegación se configura desde esta misma pantalla."}
        </p>
      </section>
    </div>
  );
}

export default function AyuntamientoPage() {
  return (
    <Suspense fallback={null}>
      <AyuntamientoContent />
    </Suspense>
  );
}
