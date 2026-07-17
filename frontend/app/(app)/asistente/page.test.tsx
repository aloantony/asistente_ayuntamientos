import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistantConversationDetail } from "../../components/types";
import type { useAssistantControllerContext } from "../../lib/AssistantControllerContext";
import AsistentePage from "./page";

const mocks = vi.hoisted(() => ({
  controller: null as unknown,
  push: vi.fn(),
  replace: vi.fn(),
  searchParams: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: mocks.push,
    replace: mocks.replace,
  }),
  useSearchParams: () => mocks.searchParams,
}));

vi.mock("../../lib/session", () => ({
  useSession: () => ({
    user: {
      id: 7,
      email: "persona@example.test",
      full_name: "Persona de prueba",
      is_active: true,
      is_superuser: false,
      permissions: ["assistant.use"],
    },
  }),
}));

vi.mock("../../lib/AssistantControllerContext", () => ({
  useAssistantControllerContext: () => mocks.controller,
}));

vi.mock("../../components/AssistantPanel", () => ({
  AssistantPanel: () => <div data-testid="assistant-panel" />,
}));

const conversation: AssistantConversationDetail = {
  id: 1,
  title: "Consulta activa",
  status: "active",
  folder_id: null,
  created_at: "2026-07-17T10:00:00Z",
  updated_at: "2026-07-17T10:00:00Z",
  messages: [],
};

type AssistantController = ReturnType<typeof useAssistantControllerContext>;

function createController() {
  return {
    assistantStatus: {
      enabled: true,
    },
    selectedConversation: conversation,
    isLoadingAssistant: false,
    loadAssistant: vi.fn(),
    selectConversation: vi.fn().mockResolvedValue({
      status: "selected",
      conversation: {
        ...conversation,
        id: 2,
      },
    }),
    cancelPendingConversationSelection: vi.fn(),
    deselectConversation: vi.fn(),
    setDraftMessage: vi.fn(),
    startConversationWithDraft: vi.fn().mockResolvedValue({
      ...conversation,
      id: 2,
    }),
  } as unknown as AssistantController;
}

describe("AsistentePage persistent selection", () => {
  beforeEach(() => {
    mocks.controller = createController();
    mocks.push.mockReset();
    mocks.replace.mockReset();
    mocks.searchParams = new URLSearchParams();
  });

  it("restores the persisted conversation when returning through the sidebar", async () => {
    render(<AsistentePage />);

    await waitFor(() => {
      expect(mocks.replace).toHaveBeenCalledWith("/asistente?c=1", {
        scroll: false,
      });
    });
    expect(
      (mocks.controller as AssistantController).deselectConversation,
    ).not.toHaveBeenCalled();
    expect(
      (mocks.controller as AssistantController).selectConversation,
    ).not.toHaveBeenCalled();
  });

  it("does not refetch a persisted conversation already matching the URL", async () => {
    mocks.searchParams = new URLSearchParams("c=1");

    render(<AsistentePage />);

    await waitFor(() => {
      expect(
        (mocks.controller as AssistantController).loadAssistant,
      ).toHaveBeenCalledTimes(1);
    });
    expect(
      (mocks.controller as AssistantController).selectConversation,
    ).not.toHaveBeenCalled();
    expect(
      (mocks.controller as AssistantController).deselectConversation,
    ).not.toHaveBeenCalled();
  });

  it("honors a different conversation requested by a deep link", async () => {
    mocks.searchParams = new URLSearchParams("c=2");

    render(<AsistentePage />);

    await waitFor(() => {
      expect(
        (mocks.controller as AssistantController).selectConversation,
      ).toHaveBeenCalledWith(2);
    });
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    expect(mocks.replace).not.toHaveBeenCalledWith("/asistente?c=1", {
      scroll: false,
    });
  });

  it("returns to the visible chat when a deep link cannot be loaded", async () => {
    mocks.searchParams = new URLSearchParams("c=2");
    const controller = mocks.controller as AssistantController;
    vi.mocked(controller.selectConversation).mockResolvedValue({
      status: "failed",
    });

    render(<AsistentePage />);

    await waitFor(() => {
      expect(mocks.replace).toHaveBeenCalledWith("/asistente?c=1", {
        scroll: false,
      });
    });
  });

  it("does not override navigation when selection is ignored by session handling", async () => {
    mocks.searchParams = new URLSearchParams("c=2");
    const controller = mocks.controller as AssistantController;
    vi.mocked(controller.selectConversation).mockResolvedValue({
      status: "ignored",
    });

    render(<AsistentePage />);

    await waitFor(() => {
      expect(controller.selectConversation).toHaveBeenCalledWith(2);
    });
    expect(mocks.replace).not.toHaveBeenCalledWith("/asistente?c=1", {
      scroll: false,
    });
  });

  it("invalidates a stale selection when history returns to the visible chat", async () => {
    mocks.searchParams = new URLSearchParams("c=2");
    const view = render(<AsistentePage />);

    await waitFor(() => {
      expect(
        (mocks.controller as AssistantController).selectConversation,
      ).toHaveBeenCalledWith(2);
    });

    mocks.searchParams = new URLSearchParams("c=1");
    view.rerender(<AsistentePage />);

    await waitFor(() => {
      expect(
        (mocks.controller as AssistantController)
          .cancelPendingConversationSelection,
      ).toHaveBeenCalledTimes(1);
    });
  });

  it("starts a seeded conversation without restoring the previous URL", async () => {
    mocks.searchParams = new URLSearchParams("q=Nueva consulta");

    const view = render(<AsistentePage />);

    await waitFor(() => {
      expect(
        (mocks.controller as AssistantController).startConversationWithDraft,
      ).toHaveBeenCalledWith("Nueva consulta");
    });
    expect(mocks.replace).toHaveBeenCalledWith("/asistente", {
      scroll: false,
    });
    expect(mocks.replace).not.toHaveBeenCalledWith("/asistente?c=1", {
      scroll: false,
    });

    (mocks.controller as AssistantController).selectedConversation = {
      ...conversation,
      id: 2,
    };
    view.rerender(<AsistentePage />);

    await waitFor(() => {
      expect(mocks.replace).toHaveBeenCalledWith("/asistente?c=2", {
        scroll: false,
      });
    });
  });
});
