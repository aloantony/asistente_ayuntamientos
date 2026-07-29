"use client";

import { useCallback, useState } from "react";
import type {
  TownHall,
  TownHallBlockPlacement,
  TownHallNavSection,
  TownHallProfileUpdate,
} from "../components/types";
import {
  createTownHallBlock,
  fetchTownHall,
  reorderTownHallBlocks,
  updateTownHallBlock,
  updateTownHallProfile,
  uploadTownHallShield,
} from "./api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseTownHallControllerArgs = {
  handleRequestError: RequestErrorHandler;
};

// Renumera un árbol recién manipulado para que las posiciones vuelvan a ser
// 0..n en cada nivel: el backend guarda lo que reciba, así que la posición
// canónica la fija quien reordena.
export function toPlacements(nav: TownHallNavSection[]) {
  const placements: TownHallBlockPlacement[] = [];

  nav.forEach((section, sectionIndex) => {
    placements.push({ id: section.id, parent_id: null, position: sectionIndex });
    section.items.forEach((item, itemIndex) => {
      placements.push({
        id: item.id,
        parent_id: section.id,
        position: itemIndex,
      });
    });
  });

  return placements;
}

export function useTownHallController({
  handleRequestError,
}: UseTownHallControllerArgs) {
  const [townHall, setTownHall] = useState<TownHall | null>(null);
  const [isLoadingTownHall, setIsLoadingTownHall] = useState(false);
  const [townHallError, setTownHallError] = useState("");
  const [isSavingTownHall, setIsSavingTownHall] = useState(false);
  // Se incrementa al sustituir el escudo para invalidar la caché del <img>.
  const [shieldVersion, setShieldVersion] = useState(0);

  const loadTownHall = useCallback(async () => {
    setIsLoadingTownHall(true);
    setTownHallError("");

    try {
      setTownHall(await fetchTownHall());
    } catch (requestError) {
      handleRequestError(
        requestError,
        setTownHallError,
        "No se pudo cargar el Ayuntamiento.",
      );
    } finally {
      setIsLoadingTownHall(false);
    }
  }, [handleRequestError]);

  // Toda mutación recarga el árbol completo: son operaciones puntuales del
  // editor, y releer evita divergencias entre cliente y servidor.
  async function runMutation(mutation: () => Promise<unknown>, fallback: string) {
    setIsSavingTownHall(true);
    setTownHallError("");

    try {
      await mutation();
      setTownHall(await fetchTownHall());
      return true;
    } catch (requestError) {
      handleRequestError(requestError, setTownHallError, fallback);
      return false;
    } finally {
      setIsSavingTownHall(false);
    }
  }

  function updateProfile(changes: TownHallProfileUpdate) {
    return runMutation(
      () => updateTownHallProfile(changes),
      "No se pudo guardar la configuración del Ayuntamiento.",
    );
  }

  function addSection(title: string) {
    return runMutation(
      () => createTownHallBlock({ block_type: "nav_section", title }),
      "No se pudo crear el apartado.",
    );
  }

  function addItem(sectionId: number, title: string) {
    return runMutation(
      () =>
        createTownHallBlock({
          block_type: "nav_item",
          parent_id: sectionId,
          title,
        }),
      "No se pudo crear el elemento.",
    );
  }

  function renameBlock(blockId: number, title: string) {
    return runMutation(
      () => updateTownHallBlock(blockId, { title }),
      "No se pudo renombrar el apartado.",
    );
  }

  function archiveBlock(blockId: number) {
    return runMutation(
      () => updateTownHallBlock(blockId, { status: "archived" }),
      "No se pudo eliminar el apartado.",
    );
  }

  async function uploadShield(file: File) {
    const uploaded = await runMutation(
      () => uploadTownHallShield(file),
      "No se pudo subir el escudo.",
    );

    if (uploaded) {
      setShieldVersion((version) => version + 1);
    }

    return uploaded;
  }

  // El arrastre necesita respuesta inmediata: se pinta el árbol nuevo y sólo
  // se revierte si el servidor rechaza la reordenación.
  async function reorderNav(nav: TownHallNavSection[]) {
    const previous = townHall;
    if (previous === null) {
      return false;
    }

    setTownHall({ ...previous, nav });
    setTownHallError("");

    try {
      await reorderTownHallBlocks(toPlacements(nav));
      return true;
    } catch (requestError) {
      setTownHall(previous);
      handleRequestError(
        requestError,
        setTownHallError,
        "No se pudo reordenar el menú.",
      );
      return false;
    }
  }

  return {
    townHall,
    isLoadingTownHall,
    isSavingTownHall,
    townHallError,
    shieldVersion,
    loadTownHall,
    uploadShield,
    updateProfile,
    addSection,
    addItem,
    renameBlock,
    archiveBlock,
    reorderNav,
  };
}
