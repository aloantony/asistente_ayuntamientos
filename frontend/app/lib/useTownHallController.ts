"use client";

import { useCallback, useState } from "react";
import type {
  TownHall,
  TownHallBlockPlacement,
  TownHallNavSection,
  TownHallContent,
  TownHallProfileUpdate,
  TownHallWeather,
} from "../components/types";
import {
  createTownHallBlock,
  fetchTownHall,
  fetchTownHallContent,
  fetchTownHallWeather,
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
  // Obligatoria: el espacio municipal ya sabe qué organización está
  // seleccionada, y un superusuario no tiene "primera organización".
  organizationId: number;
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
  organizationId,
}: UseTownHallControllerArgs) {
  const [townHall, setTownHall] = useState<TownHall | null>(null);
  const [isLoadingTownHall, setIsLoadingTownHall] = useState(false);
  const [townHallError, setTownHallError] = useState("");
  const [isSavingTownHall, setIsSavingTownHall] = useState(false);
  // Se incrementa al sustituir el escudo para invalidar la caché del <img>.
  const [shieldVersion, setShieldVersion] = useState(0);
  const [weather, setWeather] = useState<TownHallWeather | null>(null);
  const [content, setContent] = useState<TownHallContent | null>(null);
  const [isLoadingContent, setIsLoadingContent] = useState(false);

  const loadTownHall = useCallback(async () => {
    setIsLoadingTownHall(true);
    setTownHallError("");

    try {
      setTownHall(await fetchTownHall(organizationId));
    } catch (requestError) {
      handleRequestError(
        requestError,
        setTownHallError,
        "No se pudo cargar el Ayuntamiento.",
      );
    } finally {
      setIsLoadingTownHall(false);
    }
  }, [handleRequestError, organizationId]);

  // Toda mutación recarga el árbol completo: son operaciones puntuales del
  // editor, y releer evita divergencias entre cliente y servidor.
  async function runMutation(mutation: () => Promise<unknown>, fallback: string) {
    setIsSavingTownHall(true);
    setTownHallError("");

    try {
      await mutation();
      setTownHall(await fetchTownHall(organizationId));
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
      () => updateTownHallProfile(changes, organizationId),
      "No se pudo guardar la configuración del Ayuntamiento.",
    );
  }

  function addSection(title: string) {
    return runMutation(
      () => createTownHallBlock({ block_type: "nav_section", title }, organizationId),
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
        }, organizationId),
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

  // La temperatura es accesoria: si el proveedor falla o el bloque está
  // apagado, se queda a null y la barra muestra un guion, sin molestar al
  // usuario con un error.
  const loadWeather = useCallback(async () => {
    try {
      setWeather(await fetchTownHallWeather(organizationId));
    } catch {
      setWeather(null);
    }
  }, [organizationId]);

  // Contenido del apartado abierto. Se pide aparte del árbol porque cambia
  // con la navegación, no con el menú.
  const loadContent = useCallback(
    async (blockId: number) => {
      setIsLoadingContent(true);
      try {
        setContent(await fetchTownHallContent(blockId));
      } catch (requestError) {
        setContent(null);
        handleRequestError(
          requestError,
          setTownHallError,
          "No se pudo cargar el contenido del apartado.",
        );
      } finally {
        setIsLoadingContent(false);
      }
    },
    [handleRequestError],
  );

  // Las mutaciones de contenido releen solo el apartado, no el árbol entero.
  async function runContentMutation(
    blockId: number,
    mutation: () => Promise<unknown>,
    fallback: string,
  ) {
    setIsSavingTownHall(true);
    setTownHallError("");

    try {
      await mutation();
      setContent(await fetchTownHallContent(blockId));
      return true;
    } catch (requestError) {
      handleRequestError(requestError, setTownHallError, fallback);
      return false;
    } finally {
      setIsSavingTownHall(false);
    }
  }

  function addContentItem(blockId: number, title: string) {
    return runContentMutation(
      blockId,
      () =>
        createTownHallBlock(
          { block_type: "item", parent_id: blockId, title },
          organizationId,
        ),
      "No se pudo crear el elemento.",
    );
  }

  function saveContentItem(
    blockId: number,
    itemId: number,
    changes: { title?: string; body?: string | null },
  ) {
    return runContentMutation(
      blockId,
      () => updateTownHallBlock(itemId, changes),
      "No se pudo guardar el elemento.",
    );
  }

  function archiveContentItem(blockId: number, itemId: number) {
    return runContentMutation(
      blockId,
      () => updateTownHallBlock(itemId, { status: "archived" }),
      "No se pudo eliminar el elemento.",
    );
  }

  async function uploadShield(file: File) {
    const uploaded = await runMutation(
      () => uploadTownHallShield(file, organizationId),
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
      await reorderTownHallBlocks(toPlacements(nav), organizationId);
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
    weather,
    content,
    isLoadingContent,
    loadTownHall,
    loadContent,
    addContentItem,
    saveContentItem,
    archiveContentItem,
    loadWeather,
    uploadShield,
    updateProfile,
    addSection,
    addItem,
    renameBlock,
    archiveBlock,
    reorderNav,
  };
}
