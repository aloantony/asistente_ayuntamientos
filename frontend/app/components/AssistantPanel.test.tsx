import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState, type ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssistantPanel } from "./AssistantPanel";

const conversation = {
  id: 1,
  title: "Consulta activa",
  status: "active" as const,
  folder_id: null,
  created_at: "2026-07-17T10:00:00Z",
  updated_at: "2026-07-17T10:00:00Z",
  messages: [],
};

const assistantStatus = {
  enabled: true,
  runtime: "test",
  model: "test-model",
  runtime_healthy: true,
  speech_transcription_enabled: false,
  speech_synthesis_enabled: false,
  speech_synthesis_max_chars: 3000,
  realtime_voice_enabled: false,
  realtime_voice_provider: null,
  realtime_voice_model: null,
  web_page_reader_enabled: false,
  realtime_web_page_reader_enabled: false,
  tools: [],
};

const originalMediaDevicesDescriptor = Object.getOwnPropertyDescriptor(
  navigator,
  "mediaDevices",
);

afterEach(() => {
  vi.unstubAllGlobals();
  if (originalMediaDevicesDescriptor) {
    Object.defineProperty(
      navigator,
      "mediaDevices",
      originalMediaDevicesDescriptor,
    );
  } else {
    Reflect.deleteProperty(navigator, "mediaDevices");
  }
});

function enableFakeMicrophone(getUserMedia: ReturnType<typeof vi.fn>) {
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
}

function createBaseProps() {
  return {
    assistantStatus,
    conversations: [conversation],
    conversationFolders: [],
    currentUser: {
      id: 7,
      email: "persona@example.test",
      full_name: "Persona de prueba",
      is_active: true,
      is_superuser: false,
      permissions: ["assistant.use"],
    },
    selectedConversation: conversation,
    attachmentProjects: [],
    availableAttachments: [],
    selectedAttachments: [],
    isLoadingAttachments: false,
    isUploadingAttachment: false,
    attachmentError: "",
    voiceModeEnabled: false,
    handsFreeEnabled: true,
    voiceState: "idle" as const,
    realtimeVoiceActive: false,
    realtimeVoiceFallback: false,
    isLoadingAssistant: false,
    isSpeaking: false,
    assistantError: "",
    includeArchivedConversations: false,
    onLoadAttachmentLibrary: vi.fn().mockResolvedValue(undefined),
    onToggleAttachment: vi.fn(),
    onRemoveAttachment: vi.fn(),
    onUploadAttachment: vi.fn().mockResolvedValue(undefined),
    onLoadAttachmentPreview: vi.fn().mockResolvedValue(""),
    onOpenAttachment: vi.fn().mockResolvedValue(undefined),
    onVoiceModeChange: vi.fn(),
    onSelectConversation: vi.fn(),
    onStartConversation: vi.fn(),
    onSendVoiceAudio: vi.fn().mockResolvedValue(undefined),
    onStartRealtimeVoice: vi.fn().mockResolvedValue(undefined),
    onStopRealtimeVoice: vi.fn(),
    onStopSpeaking: vi.fn(),
    onTranscribeAudio: vi.fn().mockResolvedValue(""),
    onArchiveConversation: vi.fn(),
    onRestoreConversation: vi.fn(),
    onRenameConversation: vi.fn().mockResolvedValue(undefined),
    onAssignConversationFolder: vi.fn().mockResolvedValue(undefined),
    onCreateConversationFolder: vi.fn().mockResolvedValue(null),
    onRenameConversationFolder: vi.fn().mockResolvedValue(undefined),
    onDeleteConversationFolder: vi.fn().mockResolvedValue(undefined),
    onIncludeArchivedConversationsChange: vi.fn(),
  } satisfies Omit<
    ComponentProps<typeof AssistantPanel>,
    | "draftMessage"
    | "isSendingMessage"
    | "onDraftMessageChange"
    | "onSendMessage"
    | "onStopMessageGeneration"
  >;
}

function ControlledAssistantPanel({
  isSendingMessage,
  onSendMessage,
  onStopMessageGeneration,
}: {
  isSendingMessage: boolean;
  onSendMessage: () => void;
  onStopMessageGeneration: () => void;
}) {
  const [draftMessage, setDraftMessage] = useState("");

  return (
    <AssistantPanel
      {...createBaseProps()}
      draftMessage={draftMessage}
      isSendingMessage={isSendingMessage}
      onDraftMessageChange={setDraftMessage}
      onSendMessage={onSendMessage}
      onStopMessageGeneration={onStopMessageGeneration}
    />
  );
}

describe("AssistantPanel composer", () => {
  it("keeps the next draft editable while blocking a concurrent send", async () => {
    const user = userEvent.setup();
    const onSendMessage = vi.fn();
    const onStopMessageGeneration = vi.fn();
    const { rerender } = render(
      <ControlledAssistantPanel
        isSendingMessage
        onSendMessage={onSendMessage}
        onStopMessageGeneration={onStopMessageGeneration}
      />,
    );

    const textarea = screen.getByPlaceholderText(
      "Escribe tu consulta o pide un borrador…",
    );
    expect(textarea).toBeEnabled();

    await user.type(textarea, "Siguiente consulta");
    expect(textarea).toHaveValue("Siguiente consulta");

    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    fireEvent.submit(textarea.closest("form")!);
    expect(onSendMessage).not.toHaveBeenCalled();

    expect(
      screen.getByRole("button", { name: "Detener generación" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Enviar" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Detener generación" }),
    );
    expect(onStopMessageGeneration).toHaveBeenCalledTimes(1);

    rerender(
      <ControlledAssistantPanel
        isSendingMessage={false}
        onSendMessage={onSendMessage}
        onStopMessageGeneration={onStopMessageGeneration}
      />,
    );

    expect(textarea).toHaveValue("Siguiente consulta");
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    expect(onSendMessage).toHaveBeenCalledTimes(1);
  });

  it("closes voice resources on navigation without stopping the text turn", () => {
    const onStopMessageGeneration = vi.fn();
    const onStopRealtimeVoice = vi.fn();
    const onStopSpeaking = vi.fn();
    const view = render(
      <AssistantPanel
        {...createBaseProps()}
        draftMessage=""
        isSendingMessage
        onDraftMessageChange={vi.fn()}
        onSendMessage={vi.fn()}
        onStopMessageGeneration={onStopMessageGeneration}
        onStopRealtimeVoice={onStopRealtimeVoice}
        onStopSpeaking={onStopSpeaking}
      />,
    );

    onStopRealtimeVoice.mockClear();
    onStopSpeaking.mockClear();
    view.unmount();

    expect(onStopRealtimeVoice).toHaveBeenCalledWith({ interrupted: true });
    expect(onStopSpeaking).toHaveBeenCalledTimes(1);
    expect(onStopMessageGeneration).not.toHaveBeenCalled();
  });

  it("discards a partial recording when navigation unmounts the panel", async () => {
    const user = userEvent.setup();
    const stopTrack = vi.fn();
    const stream = {
      getTracks: () => [{ stop: stopTrack }],
    } as unknown as MediaStream;
    enableFakeMicrophone(vi.fn().mockResolvedValue(stream));
    const recorders: FakeMediaRecorder[] = [];

    class FakeMediaRecorder {
      mimeType = "audio/webm";
      state: "inactive" | "recording" | "paused" = "inactive";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;

      constructor(_stream: MediaStream) {
        void _stream;
        recorders.push(this);
      }

      start() {
        this.state = "recording";
      }

      stop() {
        this.state = "inactive";
        this.ondataavailable?.({
          data: new Blob(["partial voice"]),
        } as BlobEvent);
        this.onstop?.();
      }
    }

    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    const onTranscribeAudio = vi.fn().mockResolvedValue("texto parcial");
    const view = render(
      <AssistantPanel
        {...createBaseProps()}
        assistantStatus={{
          ...assistantStatus,
          speech_transcription_enabled: true,
        }}
        draftMessage=""
        isSendingMessage={false}
        onDraftMessageChange={vi.fn()}
        onSendMessage={vi.fn()}
        onStopMessageGeneration={vi.fn()}
        onTranscribeAudio={onTranscribeAudio}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Grabar audio" })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: "Grabar audio" }));
    await waitFor(() => expect(recorders).toHaveLength(1));

    view.unmount();

    expect(onTranscribeAudio).not.toHaveBeenCalled();
    expect(stopTrack).toHaveBeenCalled();
  });

  it("does not start recording when mic access resolves after navigation", async () => {
    const user = userEvent.setup();
    let resolveStream!: (stream: MediaStream) => void;
    const pendingStream = new Promise<MediaStream>((resolve) => {
      resolveStream = resolve;
    });
    const getUserMedia = vi.fn().mockReturnValue(pendingStream);
    enableFakeMicrophone(getUserMedia);
    const started = vi.fn();

    class FakeMediaRecorder {
      mimeType = "audio/webm";
      state: "inactive" | "recording" | "paused" = "inactive";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;

      start() {
        this.state = "recording";
        started();
      }

      stop() {
        this.state = "inactive";
      }
    }

    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    const view = render(
      <StrictMode>
        <AssistantPanel
          {...createBaseProps()}
          assistantStatus={{
            ...assistantStatus,
            speech_transcription_enabled: true,
          }}
          draftMessage=""
          isSendingMessage={false}
          onDraftMessageChange={vi.fn()}
          onSendMessage={vi.fn()}
          onStopMessageGeneration={vi.fn()}
        />
      </StrictMode>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Grabar audio" })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: "Grabar audio" }));
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
    view.unmount();

    const stopTrack = vi.fn();
    await act(async () => {
      resolveStream({
        getTracks: () => [{ stop: stopTrack }],
      } as unknown as MediaStream);
      await pendingStream;
    });

    expect(started).not.toHaveBeenCalled();
    expect(stopTrack).toHaveBeenCalledTimes(1);
  });

  it("ignores a transcription that resolves after navigation", async () => {
    const user = userEvent.setup();
    const stream = {
      getTracks: () => [{ stop: vi.fn() }],
    } as unknown as MediaStream;
    enableFakeMicrophone(vi.fn().mockResolvedValue(stream));

    class FakeMediaRecorder {
      mimeType = "audio/webm";
      state: "inactive" | "recording" | "paused" = "inactive";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;

      start() {
        this.state = "recording";
      }

      stop() {
        this.state = "inactive";
        this.ondataavailable?.({
          data: new Blob(["voice note"]),
        } as BlobEvent);
        this.onstop?.();
      }
    }

    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    let resolveTranscript!: (transcript: string) => void;
    const pendingTranscript = new Promise<string>((resolve) => {
      resolveTranscript = resolve;
    });
    const onTranscribeAudio = vi.fn().mockReturnValue(pendingTranscript);
    const onDraftMessageChange = vi.fn();
    const view = render(
      <AssistantPanel
        {...createBaseProps()}
        assistantStatus={{
          ...assistantStatus,
          speech_transcription_enabled: true,
        }}
        draftMessage=""
        isSendingMessage={false}
        onDraftMessageChange={onDraftMessageChange}
        onSendMessage={vi.fn()}
        onStopMessageGeneration={vi.fn()}
        onTranscribeAudio={onTranscribeAudio}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Grabar audio" }));
    await user.click(await screen.findByRole("button", { name: "Detener voz" }));
    await waitFor(() => expect(onTranscribeAudio).toHaveBeenCalledTimes(1));
    view.unmount();

    await act(async () => {
      resolveTranscript("texto tardío");
      await pendingTranscript;
    });

    expect(onDraftMessageChange).not.toHaveBeenCalled();
  });
});
