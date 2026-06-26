"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef } from "react";
import { AssistantPanel } from "../../components/AssistantPanel";
import { userHasPermission } from "../../components/types";
import { useSession } from "../../lib/session";
import { useAssistantController } from "../../lib/useAssistantController";

function parseConversationParam(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function AsistentePageInner() {
  const { user, getStoredToken, handleRequestError } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();
  const assistantController = useAssistantController({
    getStoredToken,
    handleRequestError,
  });

  const canUseAssistant = Boolean(
    user && userHasPermission(user, "assistant.use"),
  );
  // La URL (?c=N) es la única fuente de verdad de la conversación abierta.
  const urlConversationId = parseConversationParam(searchParams.get("c"));
  const selectedId = assistantController.selectedConversation?.id ?? null;
  // Espejo de la selección para que el efecto de la URL no tenga que volver a
  // ejecutarse (y re-pedir la conversación) cada vez que cambia la selección.
  const selectedIdRef = useRef<number | null>(null);
  selectedIdRef.current = selectedId;
  // Último id SOLICITADO (no resuelto): comparar contra él permite volver a
  // pedir la conversación anterior aunque otra petición siga en vuelo.
  const lastRequestedIdRef = useRef<number | null>(null);

  // El panel de inicio (y la barra superior) abren el asistente con el texto ya
  // escrito vía ?q=. Se vuelca una sola vez en el borrador y se limpia el
  // parámetro de la URL para que no reaparezca al navegar atrás/adelante.
  const seededQueryRef = useRef(false);
  useEffect(() => {
    if (seededQueryRef.current) {
      return;
    }
    const seededQuery = searchParams.get("q");
    if (!seededQuery) {
      return;
    }
    seededQueryRef.current = true;
    assistantController.setDraftMessage(seededQuery);
    const params = new URLSearchParams(searchParams.toString());
    params.delete("q");
    const rest = params.toString();
    router.replace(rest ? `/asistente?${rest}` : "/asistente", {
      scroll: false,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  useEffect(() => {
    if (!canUseAssistant) {
      return;
    }

    void assistantController.loadAssistant();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  // ?c=N de la URL -> selección (enlaces profundos y atrás/adelante).
  useEffect(() => {
    if (!canUseAssistant) {
      return;
    }

    if (urlConversationId !== null) {
      if (urlConversationId !== lastRequestedIdRef.current) {
        lastRequestedIdRef.current = urlConversationId;
        void assistantController.selectConversation(urlConversationId);
      }
    } else {
      lastRequestedIdRef.current = null;
      if (selectedIdRef.current !== null) {
        assistantController.deselectConversation();
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, urlConversationId]);

  // Selección -> URL (cubre la conversación nueva, archivar, clics en la
  // lista…). Solo se reemplaza si el parámetro realmente difiere.
  const previousSelectedIdRef = useRef<number | null>(null);
  useEffect(() => {
    const previousSelectedId = previousSelectedIdRef.current;
    previousSelectedIdRef.current = selectedId;

    if (selectedId !== null) {
      if (selectedId !== urlConversationId) {
        // Selección ya resuelta dentro del controlador (p. ej. conversación
        // nueva): se marca como solicitada para que el efecto de la URL no
        // vuelva a pedirla.
        lastRequestedIdRef.current = selectedId;
        router.replace(`/asistente?c=${selectedId}`, { scroll: false });
      }
    } else if (previousSelectedId !== null && urlConversationId !== null) {
      router.replace("/asistente", { scroll: false });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // Clic del usuario en la lista: viaja por la URL con push para que
  // atrás/adelante recorra las conversaciones; si la URL no cambiaría
  // (re-clic), se recarga el detalle directamente.
  function handleSelectConversation(conversationId: number) {
    if (conversationId === urlConversationId) {
      lastRequestedIdRef.current = conversationId;
      void assistantController.selectConversation(conversationId);
      return;
    }

    router.push(`/asistente?c=${conversationId}`, { scroll: false });
  }

  if (!user || !canUseAssistant) {
    return (
      <div className="workspace">
        <section className="panel">
          <p className="eyebrow">Asistente</p>
          <h2>Acceso restringido</h2>
          <p className="muted">No tienes permisos para usar el asistente.</p>
        </section>
      </div>
    );
  }

  return (
    <div className="workspace assistant-workspace">
      <AssistantPanel
        assistantStatus={assistantController.assistantStatus}
        conversations={assistantController.conversations}
        currentUser={user}
        memoryEntries={assistantController.memoryEntries}
        selectedConversation={assistantController.selectedConversation}
        draftMessage={assistantController.draftMessage}
        isLoadingAssistant={assistantController.isLoadingAssistant}
        isSendingMessage={assistantController.isSendingMessage}
        assistantError={assistantController.assistantError}
        includeArchivedConversations={
          assistantController.includeArchivedConversations
        }
        onDraftMessageChange={assistantController.setDraftMessage}
        onSelectConversation={handleSelectConversation}
        onStartConversation={assistantController.startConversation}
        onSendMessage={assistantController.sendMessage}
        onArchiveConversation={assistantController.archiveConversation}
        onRestoreConversation={assistantController.restoreConversation}
        onRenameConversation={assistantController.renameConversation}
        onUpdateMemoryEntry={assistantController.updateMemoryEntry}
        onIncludeArchivedConversationsChange={
          assistantController.toggleIncludeArchivedConversations
        }
      />
    </div>
  );
}

export default function AsistentePage() {
  return (
    <Suspense
      fallback={
        <section className="panel">
          <p className="muted">Cargando…</p>
        </section>
      }
    >
      <AsistentePageInner />
    </Suspense>
  );
}
