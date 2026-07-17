import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AssistantAttachmentCandidate,
  AssistantConversationDetail,
  AssistantMessage,
  AssistantStreamDone,
} from "../components/types";
import { ApiRequestError, streamAssistantMessage } from "./api";
import {
  AssistantControllerProvider,
  useAssistantControllerContext,
} from "./AssistantControllerContext";

const mocks = vi.hoisted(() => ({
  adminRequest: vi.fn(),
  handleRequestError: vi.fn(),
  streamAssistantMessage: vi.fn(),
}));

vi.mock("./session", () => ({
  useSession: () => ({
    user: {
      id: 7,
      email: "persona@example.test",
      full_name: "Persona de prueba",
      is_active: true,
      is_superuser: false,
      permissions: ["assistant.use"],
    },
    getStoredToken: () => "",
    handleRequestError: mocks.handleRequestError,
  }),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    adminRequest: mocks.adminRequest,
    streamAssistantMessage: mocks.streamAssistantMessage,
  };
});

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
};

type StreamHandlers = Parameters<typeof streamAssistantMessage>[3];

type StreamRequest = {
  deferred: Deferred<void>;
  handlers: StreamHandlers;
  signal: AbortSignal;
};

const conversation: AssistantConversationDetail = {
  id: 1,
  title: "Consulta activa",
  status: "active",
  folder_id: null,
  created_at: "2026-07-17T10:00:00Z",
  updated_at: "2026-07-17T10:00:00Z",
  messages: [],
};

const attachmentCandidate = {
  document: {
    id: 91,
  },
  project: {
    id: 12,
  },
} as unknown as AssistantAttachmentCandidate;

function createDeferred<T>(): Deferred<T> {
  let resolve!: Deferred<T>["resolve"];
  let reject!: Deferred<T>["reject"];
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

function createMessage(
  id: number,
  role: AssistantMessage["role"],
  content: string,
): AssistantMessage {
  return {
    id,
    role,
    content,
    actions: [],
    attachments: [],
    agent_key: role === "assistant" ? "anacleto" : null,
    routing: null,
    created_at: "2026-07-17T10:00:00Z",
  };
}

function createDoneEvent(): AssistantStreamDone {
  return {
    conversation: {
      ...conversation,
      updated_at: "2026-07-17T10:00:01Z",
    },
    user_message: createMessage(11, "user", "Primera consulta"),
    message: createMessage(12, "assistant", "Respuesta terminada"),
  };
}

function ControllerProbe() {
  const controller = useAssistantControllerContext();
  const lastMessage = controller.selectedConversation?.messages.at(-1);
  const [selectionStatus, setSelectionStatus] = useState("idle");

  return (
    <div>
      <output data-testid="conversation-id">
        {controller.selectedConversation?.id ?? "none"}
      </output>
      <output data-testid="sending-state">
        {controller.isSendingMessage ? "sending" : "idle"}
      </output>
      <output data-testid="last-message">{lastMessage?.content ?? ""}</output>
      <output data-testid="selected-attachments">
        {controller.selectedAttachments
          .map((attachment) => attachment.document.id)
          .join(",")}
      </output>
      <output data-testid="selection-status">{selectionStatus}</output>
      <textarea
        aria-label="Borrador persistente"
        onChange={(event) => controller.setDraftMessage(event.target.value)}
        value={controller.draftMessage}
      />
      <button onClick={() => void controller.startConversation()} type="button">
        Iniciar conversación
      </button>
      <button onClick={() => void controller.sendMessage()} type="button">
        Enviar mensaje
      </button>
      <button
        onClick={() => {
          void controller
            .selectConversation(2)
            .then((selection) => setSelectionStatus(selection.status));
        }}
        type="button"
      >
        Abrir segunda conversación
      </button>
      <button
        onClick={() => controller.cancelPendingConversationSelection()}
        type="button"
      >
        Mantener conversación visible
      </button>
      <button
        onClick={() => controller.toggleAttachment(attachmentCandidate)}
        type="button"
      >
        Adjuntar prueba
      </button>
    </div>
  );
}

function PersistentHarness({
  children,
  showAssistant,
}: {
  children?: ReactNode;
  showAssistant: boolean;
}) {
  return (
    <AssistantControllerProvider>
      {showAssistant ? <ControllerProbe /> : children ?? <p>Otra sección</p>}
    </AssistantControllerProvider>
  );
}

describe("AssistantControllerProvider", () => {
  let streamRequests: StreamRequest[];

  beforeEach(() => {
    streamRequests = [];
    mocks.adminRequest.mockReset();
    mocks.handleRequestError.mockReset();
    mocks.streamAssistantMessage.mockReset();

    mocks.adminRequest.mockImplementation(
      async (path: string) => {
        if (path === "/assistant/conversations") {
          return conversation;
        }
        if (path === "/assistant/conversations/1") {
          return conversation;
        }
        throw new Error(`Unexpected request: ${path}`);
      },
    );
    mocks.handleRequestError.mockImplementation(
      (
        _error: unknown,
        setMessage: (message: string) => void,
        fallback: string,
      ) => setMessage(fallback),
    );
    mocks.streamAssistantMessage.mockImplementation(
      (
        _conversationId: number,
        _content: string,
        _accessToken: string,
        handlers: StreamHandlers,
        _inputMode: "text" | "voice",
        signal?: AbortSignal,
      ) => {
        if (!signal) {
          throw new Error("Expected an abort signal");
        }
        const deferred = createDeferred<void>();
        signal.addEventListener(
          "abort",
          () => {
            deferred.reject(new DOMException("Aborted", "AbortError"));
          },
          { once: true },
        );
        streamRequests.push({ deferred, handlers, signal });
        return deferred.promise;
      },
    );
  });

  it("keeps a text turn alive across tab hiding and internal navigation", async () => {
    const user = userEvent.setup();
    const view = render(
      <StrictMode>
        <PersistentHarness showAssistant />
      </StrictMode>,
    );

    await user.click(screen.getByRole("button", { name: "Iniciar conversación" }));
    await waitFor(() => {
      expect(screen.getByTestId("conversation-id")).toHaveTextContent("1");
    });

    const draft = screen.getByRole("textbox", { name: "Borrador persistente" });
    await user.click(screen.getByRole("button", { name: "Adjuntar prueba" }));
    expect(screen.getByTestId("selected-attachments")).toHaveTextContent("91");
    await user.type(draft, "Primera consulta");
    await user.click(screen.getByRole("button", { name: "Enviar mensaje" }));
    await waitFor(() => expect(streamRequests).toHaveLength(1));
    expect(screen.getByTestId("sending-state")).toHaveTextContent("sending");

    await user.type(draft, "Siguiente consulta");
    expect(draft).toHaveValue("Siguiente consulta");

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(streamRequests[0].signal.aborted).toBe(false);

    view.rerender(
      <StrictMode>
        <PersistentHarness showAssistant={false} />
      </StrictMode>,
    );
    expect(streamRequests[0].signal.aborted).toBe(false);

    await act(async () => {
      streamRequests[0].handlers.onDone?.(createDoneEvent());
      streamRequests[0].deferred.resolve();
      await streamRequests[0].deferred.promise;
    });

    view.rerender(
      <StrictMode>
        <PersistentHarness showAssistant />
      </StrictMode>,
    );
    expect(
      screen.getByRole("textbox", { name: "Borrador persistente" }),
    ).toHaveValue("Siguiente consulta");
    expect(screen.getByTestId("last-message")).toHaveTextContent(
      "Respuesta terminada",
    );
    expect(screen.getByTestId("sending-state")).toHaveTextContent("idle");

    await user.click(screen.getByRole("button", { name: "Enviar mensaje" }));
    await waitFor(() => expect(streamRequests).toHaveLength(2));
    const requestCountBeforeUnmount = mocks.adminRequest.mock.calls.length;
    await act(async () => {
      view.unmount();
      await Promise.resolve();
    });
    expect(streamRequests[1].signal.aborted).toBe(true);
    expect(mocks.adminRequest).toHaveBeenCalledTimes(requestCountBeforeUnmount);
  });

  it("does not overwrite a new draft when the previous stream fails", async () => {
    const user = userEvent.setup();
    render(<PersistentHarness showAssistant />);

    await user.click(screen.getByRole("button", { name: "Iniciar conversación" }));
    await waitFor(() => {
      expect(screen.getByTestId("conversation-id")).toHaveTextContent("1");
    });

    const draft = screen.getByRole("textbox", { name: "Borrador persistente" });
    await user.click(screen.getByRole("button", { name: "Adjuntar prueba" }));
    expect(screen.getByTestId("selected-attachments")).toHaveTextContent("91");
    await user.type(draft, "Primera consulta");
    await user.click(screen.getByRole("button", { name: "Enviar mensaje" }));
    await waitFor(() => expect(streamRequests).toHaveLength(1));
    await user.type(draft, "Borrador nuevo");

    await act(async () => {
      streamRequests[0].deferred.reject(new Error("network failure"));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(draft).toHaveValue("Borrador nuevo");
      expect(screen.getByTestId("sending-state")).toHaveTextContent("idle");
      expect(screen.getByTestId("selected-attachments")).toBeEmptyDOMElement();
    });
  });

  it("lets a pending navigation win when the previous text turn fails", async () => {
    const user = userEvent.setup();
    render(<PersistentHarness showAssistant />);

    await user.click(screen.getByRole("button", { name: "Iniciar conversación" }));
    await waitFor(() => {
      expect(screen.getByTestId("conversation-id")).toHaveTextContent("1");
    });

    const draft = screen.getByRole("textbox", { name: "Borrador persistente" });
    await user.type(draft, "Consulta que falla");
    await user.click(screen.getByRole("button", { name: "Enviar mensaje" }));
    await waitFor(() => expect(streamRequests).toHaveLength(1));

    const pendingConversation = createDeferred<AssistantConversationDetail>();
    mocks.adminRequest.mockImplementation(async (path: string) => {
      if (path === "/assistant/conversations/2") {
        return pendingConversation.promise;
      }
      if (path === "/assistant/conversations/1") {
        return conversation;
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    await user.click(
      screen.getByRole("button", { name: "Abrir segunda conversación" }),
    );

    await act(async () => {
      streamRequests[0].deferred.reject(new Error("network failure"));
      await Promise.resolve();
      pendingConversation.resolve({ ...conversation, id: 2 });
      await pendingConversation.promise;
    });

    await waitFor(() => {
      expect(screen.getByTestId("conversation-id")).toHaveTextContent("2");
    });
    expect(mocks.adminRequest).not.toHaveBeenCalledWith(
      "/assistant/conversations/1",
      expect.anything(),
      expect.anything(),
    );
  });

  it("ignores a pending selection after it is explicitly cancelled", async () => {
    const user = userEvent.setup();
    render(<PersistentHarness showAssistant />);

    await user.click(screen.getByRole("button", { name: "Iniciar conversación" }));
    await waitFor(() => {
      expect(screen.getByTestId("conversation-id")).toHaveTextContent("1");
    });

    const pendingConversation = createDeferred<AssistantConversationDetail>();
    mocks.adminRequest.mockImplementation(async (path: string) => {
      if (path === "/assistant/conversations/2") {
        return pendingConversation.promise;
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    await user.click(
      screen.getByRole("button", { name: "Abrir segunda conversación" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Mantener conversación visible" }),
    );

    await act(async () => {
      pendingConversation.resolve({ ...conversation, id: 2 });
      await pendingConversation.promise;
    });

    expect(screen.getByTestId("conversation-id")).toHaveTextContent("1");
  });

  it("classifies an authentication failure as ignored selection", async () => {
    const user = userEvent.setup();
    render(<PersistentHarness showAssistant />);

    mocks.adminRequest.mockImplementation(async (path: string) => {
      if (path === "/assistant/conversations/2") {
        throw new ApiRequestError("Sesión caducada", 401);
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    await user.click(
      screen.getByRole("button", { name: "Abrir segunda conversación" }),
    );

    await waitFor(() => {
      expect(screen.getByTestId("selection-status")).toHaveTextContent(
        "ignored",
      );
    });
    expect(mocks.handleRequestError).toHaveBeenCalledWith(
      expect.any(ApiRequestError),
      expect.any(Function),
      "No se pudo abrir la conversación.",
    );
  });
});
