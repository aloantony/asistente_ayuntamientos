// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  AssistantConversation,
  AssistantConversationFolder,
  AssistantMessageAttachment,
} from "../types";
import {
  MessageAttachmentCard,
  UNCATEGORIZED_FOLDER_ID,
  buildFolderConversationGroups,
  buildRecentConversationGroups,
  formatAttachmentSize,
  normalizeFolderName,
  stableMessageIndex,
} from "./conversationParts";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const NOW = new Date("2026-08-17T12:00:00Z");

function daysAgo(days: number) {
  return new Date(NOW.getTime() - days * 86_400_000).toISOString();
}

function conversation(
  overrides: Partial<AssistantConversation> = {},
): AssistantConversation {
  return {
    id: 1,
    title: "Consulta sobre el padrón",
    status: "active",
    folder_id: null,
    created_at: daysAgo(0.1),
    updated_at: daysAgo(0.1),
    ...overrides,
  };
}

function folder(
  overrides: Partial<AssistantConversationFolder> = {},
): AssistantConversationFolder {
  return {
    id: 10,
    name: "Urbanismo",
    sort_order: 0,
    created_at: daysAgo(30),
    updated_at: daysAgo(30),
    ...overrides,
  };
}

function attachment(
  overrides: Partial<AssistantMessageAttachment> = {},
): AssistantMessageAttachment {
  return {
    id: 1,
    document_id: 44,
    project_id: 2,
    project_name: "Licencias",
    filename: "licencia-obra.pdf",
    content_type: "application/pdf",
    size_bytes: 2048,
    context_status: "ready",
    context_char_count: 1200,
    ...overrides,
  };
}

describe("normalizeFolderName", () => {
  it("colapsa los espacios que el usuario no ve al teclear", () => {
    expect(normalizeFolderName("  Obras   y   servicios ")).toBe(
      "Obras y servicios",
    );
  });
});

describe("stableMessageIndex", () => {
  it("devuelve siempre el mismo índice para la misma semilla", () => {
    expect(stableMessageIndex(1234, 7)).toBe(stableMessageIndex("1234", 7));
  });

  it("se mantiene dentro del rango pedido", () => {
    for (const seed of [0, 1, 99, "conversacion-12", "ñ"]) {
      const index = stableMessageIndex(seed, 5);
      expect(index).toBeGreaterThanOrEqual(0);
      expect(index).toBeLessThan(5);
    }
  });
});

describe("buildRecentConversationGroups", () => {
  it("reparte por antigüedad y saca las archivadas del reparto", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);

    const groups = buildRecentConversationGroups([
      conversation({ id: 1, updated_at: daysAgo(0.2) }),
      conversation({ id: 2, updated_at: daysAgo(3) }),
      conversation({ id: 3, updated_at: daysAgo(40) }),
      // Archivada de hoy: manda el estado, no la fecha.
      conversation({ id: 4, status: "archived", updated_at: daysAgo(0.1) }),
    ]);

    expect(
      groups.map((group) => [
        group.label,
        group.conversations.map((item) => item.id),
      ]),
    ).toEqual([
      ["Archivadas", [4]],
      ["Hoy", [1]],
      ["Últimos 7 días", [2]],
      ["Anteriores", [3]],
    ]);
  });

  it("no pinta grupos vacíos", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);

    const groups = buildRecentConversationGroups([conversation({ id: 1 })]);

    expect(groups.map((group) => group.label)).toEqual(["Hoy"]);
  });

  it("manda al fondo lo que trae una fecha ilegible en vez de romperse", () => {
    const groups = buildRecentConversationGroups([
      conversation({ id: 1, updated_at: "no es una fecha" }),
    ]);

    expect(groups.map((group) => group.label)).toEqual(["Anteriores"]);
  });
});

describe("buildFolderConversationGroups", () => {
  it("respeta el orden de las carpetas y deja «Sin carpeta» al final", () => {
    const groups = buildFolderConversationGroups(
      [
        conversation({ id: 1 }),
        conversation({ id: 2 }),
        conversation({ id: 3 }),
      ],
      [folder({ id: 10, name: "Urbanismo" }), folder({ id: 11, name: "Padrón" })],
      { 1: 11, 2: 10 },
    );

    expect(
      groups.map((group) => [
        group.label,
        group.conversations.map((item) => item.id),
      ]),
    ).toEqual([
      ["Urbanismo", [2]],
      ["Padrón", [1]],
      ["Sin carpeta", [3]],
    ]);
  });

  it("recoge en «Sin carpeta» lo asignado a una carpeta que ya no existe", () => {
    // Borrar una carpeta no puede hacer desaparecer conversaciones.
    const groups = buildFolderConversationGroups(
      [conversation({ id: 1 })],
      [folder({ id: 10 })],
      { 1: 99 },
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].id).toBe(UNCATEGORIZED_FOLDER_ID);
    expect(groups[0].conversations.map((item) => item.id)).toEqual([1]);
  });
});

describe("formatAttachmentSize", () => {
  it("no redondea a cero un archivo que sí ocupa", () => {
    expect(formatAttachmentSize(1)).toBe("1 B");
    expect(formatAttachmentSize(1025)).toBe("2 KB");
  });

  it("cambia de unidad en el umbral", () => {
    expect(formatAttachmentSize(1023)).toBe("1023 B");
    expect(formatAttachmentSize(1024)).toBe("1 KB");
    expect(formatAttachmentSize(1024 * 1024)).toBe("1.0 MB");
  });
});

describe("MessageAttachmentCard", () => {
  it("describe el adjunto con su tamaño y qué se hizo con él", () => {
    render(
      <MessageAttachmentCard
        attachment={attachment()}
        onLoadPreview={async () => ""}
        onOpen={async () => {}}
      />,
    );

    expect(screen.getByText("licencia-obra.pdf")).toBeTruthy();
    expect(screen.getByText(/2 KB/)).toBeTruthy();
    expect(screen.getByText(/Texto usado solo en este turno/)).toBeTruthy();
  });

  it("no ofrece vista previa de lo que no es una imagen", () => {
    render(
      <MessageAttachmentCard
        attachment={attachment()}
        onLoadPreview={async () => ""}
        onOpen={async () => {}}
      />,
    );

    expect(screen.queryByLabelText(/Cargar vista previa/)).toBeNull();
  });

  it("carga la vista previa de una imagen a petición", async () => {
    const onLoadPreview = vi.fn(async () => "blob:preview");
    render(
      <MessageAttachmentCard
        attachment={attachment({
          content_type: "image/png",
          filename: "fachada.png",
        })}
        onLoadPreview={onLoadPreview}
        onOpen={async () => {}}
      />,
    );

    fireEvent.click(screen.getByLabelText("Cargar vista previa de fachada.png"));

    expect(onLoadPreview).toHaveBeenCalledWith(44);
    const image = await screen.findByAltText("Vista previa de fachada.png");
    expect(image.getAttribute("src")).toBe("blob:preview");
  });

  it("deja reintentar cuando la vista previa falla", async () => {
    const onLoadPreview = vi
      .fn<(documentId: number) => Promise<string>>()
      .mockRejectedValueOnce(new Error("403"))
      .mockResolvedValueOnce("blob:preview");
    render(
      <MessageAttachmentCard
        attachment={attachment({
          content_type: "image/png",
          filename: "fachada.png",
        })}
        onLoadPreview={onLoadPreview}
        onOpen={async () => {}}
      />,
    );

    const button = screen.getByLabelText("Cargar vista previa de fachada.png");
    fireEvent.click(button);
    await screen.findByTitle("Reintentar vista previa");

    fireEvent.click(button);
    const image = await screen.findByAltText("Vista previa de fachada.png");
    expect(image.getAttribute("src")).toBe("blob:preview");
    expect(onLoadPreview).toHaveBeenCalledTimes(2);
  });
});
