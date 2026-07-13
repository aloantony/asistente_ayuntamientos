"use client";

import { useEffect, useRef, useState } from "react";
import type {
  AssistantAction,
  AssistantConversation,
  AssistantConversationDetail,
  AssistantConversationFolder,
  AssistantMemoryCategory,
  AssistantMemoryEntry,
  AssistantMemorySensitivity,
  AssistantMemoryStatus,
  AssistantStatus,
  AssistantStreamToolActivity,
  AssistantVoiceState,
} from "../components/types";
import {
  adminRequest,
  createAssistantRealtimeSession,
  persistAssistantRealtimeTurn,
  sendAssistantRealtimeToolCall,
  streamAssistantMessage,
  streamAssistantVoiceTurn,
  synthesizeAssistantSpeech,
} from "./api";
import {
  RealtimeVoiceSession,
  type RealtimeServerEvent,
} from "./realtimeVoice";
import {
  createSentenceChunker,
  createSpeechPlayer,
  flattenMarkdownForSpeech,
} from "./voice";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseAssistantControllerArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  onRequirementsChanged?: () => void;
};

type AssistantAudioTranscription = {
  text: string;
};

type AssistantInputMode = "text" | "voice";

type SendMessageOptions = {
  contentOverride?: string;
  inputMode?: AssistantInputMode;
};

type RealtimeTurnDraft = {
  conversationId: number;
  userTempId: number;
  assistantTempId: number;
  userMessageId: number | null;
  userText: string;
  assistantText: string;
  actions: AssistantAction[];
  pendingToolCalls: number;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function parseRealtimeToolArguments(value: unknown): Record<string, unknown> {
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value) as unknown;
      return asRecord(parsed) ?? {};
    } catch {
      return {};
    }
  }
  return asRecord(value) ?? {};
}

function realtimeEventText(
  event: RealtimeServerEvent,
  keys: string[],
): string {
  for (const key of keys) {
    const value = event[key];
    if (typeof value === "string") {
      return value;
    }
  }
  return "";
}

function realtimeToolCallFromEvent(event: RealtimeServerEvent) {
  const eventType = event.type;
  if (eventType === "response.output_item.done") {
    const item = asRecord(event.item);
    if (item?.type !== "function_call") {
      return null;
    }
    const name = typeof item.name === "string" ? item.name : "";
    const callId = typeof item.call_id === "string" ? item.call_id : "";
    if (!name || !callId) {
      return null;
    }
    return {
      name,
      callId,
      arguments: parseRealtimeToolArguments(item.arguments),
    };
  }

  if (eventType === "response.function_call_arguments.done") {
    const name = typeof event.name === "string" ? event.name : "";
    const callId = typeof event.call_id === "string" ? event.call_id : "";
    if (!name || !callId) {
      return null;
    }
    return {
      name,
      callId,
      arguments: parseRealtimeToolArguments(event.arguments),
    };
  }

  return null;
}

function toSummary(detail: AssistantConversationDetail): AssistantConversation {
  return {
    id: detail.id,
    title: detail.title,
    status: detail.status,
    folder_id: detail.folder_id,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
  };
}

export function useAssistantController({
  getStoredToken,
  handleRequestError,
  onRequirementsChanged,
}: UseAssistantControllerArgs) {
  const [assistantStatus, setAssistantStatus] =
    useState<AssistantStatus | null>(null);
  const [conversations, setConversations] = useState<AssistantConversation[]>(
    [],
  );
  const [conversationFolders, setConversationFolders] = useState<
    AssistantConversationFolder[]
  >([]);
  const [memoryEntries, setMemoryEntries] = useState<AssistantMemoryEntry[]>(
    [],
  );
  const [selectedConversation, setSelectedConversation] =
    useState<AssistantConversationDetail | null>(null);
  const [draftMessage, setDraftMessage] = useState("");
  const [includeArchivedConversations, setIncludeArchivedConversations] =
    useState(false);
  const [isLoadingAssistant, setIsLoadingAssistant] = useState(false);
  const [isSendingMessage, setIsSendingMessage] = useState(false);
  const [assistantError, setAssistantError] = useState("");
  const [voiceState, setVoiceState] = useState<AssistantVoiceState>("idle");
  const [realtimeVoiceActive, setRealtimeVoiceActive] = useState(false);
  const [realtimeVoiceFallback, setRealtimeVoiceFallback] = useState(false);
  const [voiceModeEnabled, setVoiceModeEnabledState] = useState(() => {
    if (typeof window === "undefined") {
      return false;
    }
    return window.localStorage.getItem("assistant.voice.mode") === "true";
  });
  // Voice dialogue is always hands-free ("Jarvis" mode): no per-user toggle.
  // Kept as a constant so the loop / silence / barge-in logic can read it.
  const handsFreeEnabled = true;
  const [isSpeaking, setIsSpeaking] = useState(false);
  // Mirrors the selected conversation id so async callbacks can check
  // whether the user navigated away while a request was in flight.
  const selectedIdRef = useRef<number | null>(null);
  const speechPlayerRef = useRef<ReturnType<typeof createSpeechPlayer> | null>(
    null,
  );
  const realtimeVoiceSessionRef = useRef<RealtimeVoiceSession | null>(null);
  const realtimeTurnRef = useRef<RealtimeTurnDraft | null>(null);
  const realtimeTempIdRef = useRef(-10000);
  const realtimePersistingRef = useRef(false);
  // Set true when the user interrupts (barge-in / stop) so a turn that is still
  // streaming does not resume audio after its playback was cut.
  const speechInterruptedRef = useRef(false);

  if (speechPlayerRef.current === null) {
    speechPlayerRef.current = createSpeechPlayer({
      synthesize: (text, signal) =>
        synthesizeAssistantSpeech(text, getStoredToken(), signal),
    });
  }

  useEffect(() => {
    return speechPlayerRef.current?.subscribe(setIsSpeaking);
  }, []);

  useEffect(() => {
    window.localStorage.setItem(
      "assistant.voice.mode",
      voiceModeEnabled ? "true" : "false",
    );
  }, [voiceModeEnabled]);

  useEffect(() => {
    return () => {
      speechPlayerRef.current?.stop();
      realtimeVoiceSessionRef.current?.stop();
    };
  }, []);

  function applySelectedConversation(
    detail: AssistantConversationDetail | null,
  ) {
    const nextId = detail?.id ?? null;
    if (selectedIdRef.current !== nextId) {
      speechPlayerRef.current?.stop();
      stopRealtimeVoice({ interrupted: true });
      setVoiceState("idle");
    }
    selectedIdRef.current = nextId;
    setSelectedConversation(detail);
  }

  function setVoiceModeEnabled(enabled: boolean) {
    setVoiceModeEnabledState(enabled);
    if (enabled) {
      setRealtimeVoiceFallback(false);
    }
    if (!enabled) {
      stopRealtimeVoice({ interrupted: true });
      speechPlayerRef.current?.stop();
      setVoiceState("idle");
    }
  }

  function stopSpeaking() {
    speechInterruptedRef.current = true;
    if (realtimeVoiceSessionRef.current) {
      stopRealtimeVoice({ interrupted: true });
      return;
    }
    setVoiceState("interrupted");
    speechPlayerRef.current?.stop();
  }

  function clearAssistantState() {
    setAssistantStatus(null);
    setConversations([]);
    setConversationFolders([]);
    setMemoryEntries([]);
    applySelectedConversation(null);
    setDraftMessage("");
    setIncludeArchivedConversations(false);
    setIsLoadingAssistant(false);
    setIsSendingMessage(false);
    setAssistantError("");
    setVoiceState("idle");
    stopRealtimeVoice({ interrupted: true });
    speechPlayerRef.current?.stop();
  }

  async function loadAssistant(
    includeArchived: boolean = includeArchivedConversations,
  ) {
    setIsLoadingAssistant(true);
    setAssistantError("");

    try {
      const token = getStoredToken();
      const conversationsPath = includeArchived
        ? "/assistant/conversations?include_archived=true"
        : "/assistant/conversations";
      const [status, conversationList, conversationFoldersList, pendingMemory] =
        await Promise.all([
        adminRequest<AssistantStatus>(
          "/assistant/status",
          token,
          "No se pudo consultar el estado del asistente.",
        ),
        adminRequest<AssistantConversation[]>(
          conversationsPath,
          token,
          "No se pudieron cargar las conversaciones.",
        ),
        adminRequest<AssistantConversationFolder[]>(
          "/assistant/conversation-folders",
          token,
          "No se pudieron cargar las carpetas.",
        ).catch(() => []),
        adminRequest<AssistantMemoryEntry[]>(
          "/assistant/memory?status=proposed",
          token,
          "No se pudieron cargar las propuestas de memoria.",
        ),
      ]);
      setAssistantStatus(status);
      setConversations(conversationList);
      setConversationFolders(conversationFoldersList);
      setMemoryEntries(pendingMemory);
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo cargar el asistente.",
      );
    } finally {
      setIsLoadingAssistant(false);
    }
  }

  function toggleIncludeArchivedConversations(includeArchived: boolean) {
    setIncludeArchivedConversations(includeArchived);
    void loadAssistant(includeArchived);
  }

  function deselectConversation() {
    if (selectedIdRef.current === null) {
      return;
    }

    applySelectedConversation(null);
    setDraftMessage("");
  }

  async function selectConversation(conversationId: number) {
    setAssistantError("");

    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo abrir la conversación.",
      );
      if (selectedIdRef.current !== detail.id) {
        setDraftMessage("");
      }
      applySelectedConversation(detail);
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo abrir la conversación.",
      );
    }
  }

  async function startConversation() {
    setAssistantError("");

    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        "/assistant/conversations",
        getStoredToken(),
        "No se pudo crear la conversación.",
        { method: "POST", body: JSON.stringify({}) },
      );
      setDraftMessage("");
      applySelectedConversation(detail);
      setConversations((existing) => [toSummary(detail), ...existing]);
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo crear la conversación.",
      );
    }
  }

  async function sendMessage(options: SendMessageOptions = {}) {
    const content = (options.contentOverride ?? draftMessage).trim();
    const inputMode = options.inputMode ?? "text";
    const usesDraft = options.contentOverride === undefined;
    if (!content || !selectedConversation || isSendingMessage) {
      return;
    }
    const conversationId = selectedConversation.id;

    speechInterruptedRef.current = false;
    speechPlayerRef.current?.stop();
    setIsSendingMessage(true);
    setAssistantError("");

    let streamedUserMessageId: number | null = null;
    const speakAssistantText = (text: string) => {
      if (speechInterruptedRef.current) {
        return;
      }
      const speechText = flattenMarkdownForSpeech(text);
      if (!speechText) {
        return;
      }
      void speechPlayerRef.current?.speak(speechText).catch((speechError) => {
        if (selectedIdRef.current === conversationId) {
          handleRequestError(
            speechError,
            setAssistantError,
            "No se pudo reproducir la voz del asistente.",
          );
        }
      });
    };
    const sentenceChunker =
      inputMode === "voice" && handsFreeEnabled
        ? createSentenceChunker(speakAssistantText)
        : null;

    // Optimistic echo so the user sees their message while the agent works.
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: [
              ...current.messages,
              {
                id: -1,
                role: "user",
                content,
                actions: [],
                agent_key: null,
                routing: null,
                created_at: new Date().toISOString(),
              },
              {
                id: -2,
                role: "assistant",
                content: "",
                actions: [],
                agent_key: "anacleto",
                routing: null,
                created_at: new Date().toISOString(),
              },
            ],
          }
        : current,
    );
    if (usesDraft) {
      setDraftMessage("");
    }

    try {
      await streamAssistantMessage(conversationId, content, getStoredToken(), {
        onMessageStart: (event) => {
          streamedUserMessageId = event.user_message_id;
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  messages: current.messages.map((message) =>
                    message.id === -1
                      ? { ...message, id: event.user_message_id }
                      : message,
                  ),
                }
              : current,
          );
        },
        onTextDelta: (text) => {
          sentenceChunker?.push(text);
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  messages: current.messages.map((message) =>
                    message.id === -2
                      ? { ...message, content: `${message.content}${text}` }
                      : message,
                  ),
                }
              : current,
          );
        },
        onToolActivity: (event) => {
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  messages: current.messages.map((message) =>
                    message.id === -2
                      ? {
                          ...message,
                          actions: updateStreamingActions(
                            message.actions,
                            event,
                          ),
                        }
                      : message,
                  ),
                }
              : current,
          );
        },
        onDone: (event) => {
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  ...event.conversation,
                  messages: [
                    ...current.messages
                      .filter((message) => message.id !== -2)
                      .map((message) =>
                        message.id === -1 && streamedUserMessageId !== null
                          ? { ...message, id: streamedUserMessageId }
                          : message,
                      ),
                    event.message,
                  ],
                }
              : current,
          );
          setConversations((existing) => [
            event.conversation,
            ...existing.filter(
              (conversation) => conversation.id !== event.conversation.id,
            ),
          ]);
          const mutatingTools = new Set(
            assistantStatus?.tools
              .filter((tool) => !tool.read_only)
              .map((tool) => tool.name) ?? [],
          );
          const hasMutatingAction = event.message.actions.some(
            (action) => action.ok && mutatingTools.has(action.tool),
          );
          if (hasMutatingAction) {
            onRequirementsChanged?.();
          }
          if (sentenceChunker) {
            sentenceChunker.flush();
          } else if (inputMode === "voice") {
            speakAssistantText(event.message.content);
          }
        },
      }, inputMode);
    } catch (requestError) {
      // Drop the optimistic echo from this conversation only; the backend
      // may have persisted the user message, so a reload shows it again.
      setSelectedConversation((current) =>
        current && current.id === conversationId
          ? {
              ...current,
              messages: current.messages.filter(
                (message) => message.id !== -1 && message.id !== -2,
              ),
            }
          : current,
      );
      if (selectedIdRef.current === conversationId) {
        if (usesDraft) {
          setDraftMessage(content);
        }
        void selectConversation(conversationId);
      }
      handleRequestError(
        requestError,
        setAssistantError,
        "El asistente no ha podido responder.",
      );
    } finally {
      setIsSendingMessage(false);
    }
  }

  function updateStreamingActions(
    actions: AssistantAction[],
    event: AssistantStreamToolActivity,
  ): AssistantAction[] {
    if (event.status === "started") {
      return [
        ...actions,
        {
          tool: event.tool,
          ok: false,
          input: event.input,
          result: "",
          status: "started",
        },
      ];
    }

    const next = [...actions];
    let pendingIndex = -1;
    for (let index = next.length - 1; index >= 0; index -= 1) {
      if (next[index].tool === event.tool && next[index].status === "started") {
        pendingIndex = index;
        break;
      }
    }
    const finishedAction: AssistantAction = {
      tool: event.tool,
      ok: Boolean(event.ok),
      input: event.input,
      result: event.result ?? "",
      status: "finished",
    };
    if (pendingIndex >= 0) {
      next[pendingIndex] = finishedAction;
      return next;
    }
    return [...next, finishedAction];
  }

  async function transcribeAudio(audio: Blob) {
    const formData = new FormData();
    formData.append("file", audio, "anacleto-audio.webm");
    const transcription = await adminRequest<AssistantAudioTranscription>(
      "/assistant/audio-transcriptions",
      getStoredToken(),
      "No se pudo transcribir el audio.",
      { method: "POST", body: formData },
    );
    return transcription.text;
  }

  async function sendVoiceAudio(audio: Blob) {
    if (!selectedConversation || isSendingMessage || audio.size === 0) {
      return;
    }
    const conversationId = selectedConversation.id;

    speechInterruptedRef.current = false;
    speechPlayerRef.current?.stop();
    setIsSendingMessage(true);
    setAssistantError("");
    setVoiceState("transcribing");

    let streamedUserMessageId: number | null = null;
    let transcriptReceived = false;
    const speakAssistantText = (text: string) => {
      if (speechInterruptedRef.current) {
        return;
      }
      const speechText = flattenMarkdownForSpeech(text);
      if (!speechText) {
        return;
      }
      void speechPlayerRef.current?.speak(speechText).catch((speechError) => {
        if (selectedIdRef.current === conversationId) {
          handleRequestError(
            speechError,
            setAssistantError,
            "No se pudo reproducir la voz del asistente.",
          );
        }
      });
    };
    const sentenceChunker = createSentenceChunker(speakAssistantText);

    const insertTranscribedTurn = (content: string) => {
      transcriptReceived = true;
      setSelectedConversation((current) =>
        current && current.id === conversationId
          ? {
              ...current,
              messages: [
                ...current.messages,
                {
                  id: -1,
                  role: "user",
                  content,
                  actions: [],
                  agent_key: null,
                  routing: null,
                  created_at: new Date().toISOString(),
                },
                {
                  id: -2,
                  role: "assistant",
                  content: "",
                  actions: [],
                  agent_key: "anacleto",
                  routing: null,
                  created_at: new Date().toISOString(),
                },
              ],
            }
          : current,
      );
    };

    try {
      await streamAssistantVoiceTurn(conversationId, audio, getStoredToken(), {
        onVoiceState: (event) => {
          setVoiceState(event.state);
        },
        onTranscriptFinal: (event) => {
          insertTranscribedTurn(event.text);
        },
        onMessageStart: (event) => {
          streamedUserMessageId = event.user_message_id;
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  messages: current.messages.map((message) =>
                    message.id === -1
                      ? { ...message, id: event.user_message_id }
                      : message,
                  ),
                }
              : current,
          );
        },
        onTextDelta: (text) => {
          sentenceChunker.push(text);
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  messages: current.messages.map((message) =>
                    message.id === -2
                      ? { ...message, content: `${message.content}${text}` }
                      : message,
                  ),
                }
              : current,
          );
        },
        onToolActivity: (event) => {
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  messages: current.messages.map((message) =>
                    message.id === -2
                      ? {
                          ...message,
                          actions: updateStreamingActions(
                            message.actions,
                            event,
                          ),
                        }
                      : message,
                  ),
                }
              : current,
          );
        },
        onDone: (event) => {
          setSelectedConversation((current) =>
            current && current.id === conversationId
              ? {
                  ...current,
                  ...event.conversation,
                  messages: [
                    ...current.messages
                      .filter((message) => message.id !== -2)
                      .map((message) =>
                        message.id === -1 && streamedUserMessageId !== null
                          ? { ...message, id: streamedUserMessageId }
                          : message,
                      ),
                    event.message,
                  ],
                }
              : current,
          );
          setConversations((existing) => [
            event.conversation,
            ...existing.filter(
              (conversation) => conversation.id !== event.conversation.id,
            ),
          ]);
          const mutatingTools = new Set(
            assistantStatus?.tools
              .filter((tool) => !tool.read_only)
              .map((tool) => tool.name) ?? [],
          );
          const hasMutatingAction = event.message.actions.some(
            (action) => action.ok && mutatingTools.has(action.tool),
          );
          if (hasMutatingAction) {
            onRequirementsChanged?.();
          }
          sentenceChunker.flush();
        },
      });
    } catch (requestError) {
      setVoiceState("error");
      setSelectedConversation((current) =>
        current && current.id === conversationId
          ? {
              ...current,
              messages: current.messages.filter(
                (message) => message.id !== -1 && message.id !== -2,
              ),
            }
          : current,
      );
      if (selectedIdRef.current === conversationId && transcriptReceived) {
        void selectConversation(conversationId);
      }
      handleRequestError(
        requestError,
        setAssistantError,
        "El asistente no ha podido responder.",
      );
    } finally {
      setIsSendingMessage(false);
      setVoiceState((current) => (current === "error" ? "error" : "idle"));
    }
  }

  function getOrCreateRealtimeTurn(conversationId: number): RealtimeTurnDraft {
    const existing = realtimeTurnRef.current;
    if (existing?.conversationId === conversationId) {
      return existing;
    }

    const userTempId = realtimeTempIdRef.current--;
    const assistantTempId = realtimeTempIdRef.current--;
    const now = new Date().toISOString();
    const draft: RealtimeTurnDraft = {
      conversationId,
      userTempId,
      assistantTempId,
      userMessageId: null,
      userText: "",
      assistantText: "",
      actions: [],
      pendingToolCalls: 0,
    };
    realtimeTurnRef.current = draft;
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: [
              ...current.messages,
              {
                id: userTempId,
                role: "user",
                content: "",
                actions: [],
                agent_key: null,
                routing: null,
                created_at: now,
              },
              {
                id: assistantTempId,
                role: "assistant",
                content: "",
                actions: [],
                agent_key: "anacleto",
                routing: null,
                created_at: now,
              },
            ],
          }
        : current,
    );
    return draft;
  }

  function updateRealtimeUserText(conversationId: number, text: string) {
    const draft = getOrCreateRealtimeTurn(conversationId);
    draft.userText = text;
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: current.messages.map((message) =>
              message.id === draft.userTempId ||
              (draft.userMessageId !== null && message.id === draft.userMessageId)
                ? { ...message, content: text }
                : message,
            ),
          }
        : current,
    );
  }

  function appendRealtimeAssistantText(conversationId: number, text: string) {
    if (!text) {
      return;
    }
    const draft = getOrCreateRealtimeTurn(conversationId);
    draft.assistantText = `${draft.assistantText}${text}`;
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: current.messages.map((message) =>
              message.id === draft.assistantTempId
                ? { ...message, content: `${message.content}${text}` }
                : message,
            ),
          }
        : current,
    );
  }

  function setRealtimeAssistantText(conversationId: number, text: string) {
    const draft = getOrCreateRealtimeTurn(conversationId);
    draft.assistantText = text;
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: current.messages.map((message) =>
              message.id === draft.assistantTempId
                ? { ...message, content: text }
                : message,
            ),
          }
        : current,
    );
  }

  function updateRealtimeActions(
    conversationId: number,
    action: AssistantAction,
  ) {
    const draft = getOrCreateRealtimeTurn(conversationId);
    const existingIndex = draft.actions.findIndex(
      (candidate) =>
        candidate.tool === action.tool && candidate.status === "started",
    );
    if (existingIndex >= 0) {
      draft.actions[existingIndex] = action;
    } else {
      draft.actions.push(action);
    }
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: current.messages.map((message) =>
              message.id === draft.assistantTempId
                ? { ...message, actions: [...draft.actions] }
                : message,
            ),
          }
        : current,
    );
  }

  function replaceRealtimeUserMessage(
    conversationId: number,
    userMessageId: number,
  ) {
    const draft = realtimeTurnRef.current;
    if (!draft || draft.conversationId !== conversationId) {
      return;
    }
    draft.userMessageId = userMessageId;
    setSelectedConversation((current) =>
      current && current.id === conversationId
        ? {
            ...current,
            messages: current.messages.map((message) =>
              message.id === draft.userTempId
                ? { ...message, id: userMessageId }
                : message,
            ),
          }
        : current,
    );
  }

  async function persistCurrentRealtimeTurn(
    conversationId: number,
    options: { interrupted?: boolean } = {},
  ) {
    const draft = realtimeTurnRef.current;
    if (
      !draft ||
      draft.conversationId !== conversationId ||
      draft.pendingToolCalls > 0 ||
      realtimePersistingRef.current
    ) {
      return;
    }

    const userText = draft.userText.trim();
    const assistantText = draft.assistantText.trim();
    if (!userText && !assistantText && draft.actions.length === 0) {
      realtimeTurnRef.current = null;
      return;
    }

    realtimePersistingRef.current = true;
    try {
      const result = await persistAssistantRealtimeTurn(
        conversationId,
        {
          user_text: userText || null,
          assistant_text: assistantText || null,
          actions: draft.actions,
          user_message_id: draft.userMessageId,
          interrupted: Boolean(options.interrupted),
        },
        getStoredToken(),
      );
      setSelectedConversation((current) => {
        if (!current || current.id !== conversationId) {
          return current;
        }
        const persistedIds = new Set(
          [
            draft.userTempId,
            draft.assistantTempId,
            result.user_message?.id,
            result.assistant_message?.id,
          ].filter((value): value is number => typeof value === "number"),
        );
        const messages = current.messages.filter(
          (message) => !persistedIds.has(message.id),
        );
        if (result.user_message) {
          messages.push(result.user_message);
        }
        if (result.assistant_message) {
          messages.push(result.assistant_message);
        }
        return {
          ...current,
          ...result.conversation,
          messages,
        };
      });
      setConversations((existing) => [
        result.conversation,
        ...existing.filter(
          (conversation) => conversation.id !== result.conversation.id,
        ),
      ]);
      const mutatingTools = new Set(
        assistantStatus?.tools
          .filter((tool) => !tool.read_only)
          .map((tool) => tool.name) ?? [],
      );
      const hasMutatingAction = draft.actions.some(
        (action) => action.ok && mutatingTools.has(action.tool),
      );
      if (hasMutatingAction) {
        onRequirementsChanged?.();
      }
      if (realtimeTurnRef.current === draft) {
        realtimeTurnRef.current = null;
      }
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo guardar el turno de voz.",
      );
    } finally {
      realtimePersistingRef.current = false;
    }
  }

  async function handleRealtimeToolCall(
    conversationId: number,
    toolCall: {
      callId: string;
      name: string;
      arguments: Record<string, unknown>;
    },
  ) {
    const draft = getOrCreateRealtimeTurn(conversationId);
    draft.pendingToolCalls += 1;
    setVoiceState("tool_running");
    updateRealtimeActions(conversationId, {
      tool: toolCall.name,
      ok: false,
      input: toolCall.arguments,
      result: "",
      status: "started",
    });

    try {
      const result = await sendAssistantRealtimeToolCall(
        conversationId,
        {
          call_id: toolCall.callId,
          name: toolCall.name,
          arguments: toolCall.arguments,
          user_transcript: draft.userText || null,
          user_message_id: draft.userMessageId,
        },
        getStoredToken(),
      );
      replaceRealtimeUserMessage(conversationId, result.user_message.id);
      updateRealtimeActions(conversationId, {
        ...result.action,
        status: "finished",
      });
      realtimeVoiceSessionRef.current?.sendFunctionOutput(
        result.call_id,
        result.output,
      );
    } catch (requestError) {
      updateRealtimeActions(conversationId, {
        tool: toolCall.name,
        ok: false,
        input: toolCall.arguments,
        result:
          requestError instanceof Error
            ? requestError.message
            : "No se pudo ejecutar la herramienta.",
        status: "finished",
      });
      realtimeVoiceSessionRef.current?.sendFunctionOutput(
        toolCall.callId,
        JSON.stringify({
          error: "No se pudo ejecutar la herramienta solicitada.",
        }),
      );
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo ejecutar la herramienta de voz.",
      );
    } finally {
      draft.pendingToolCalls = Math.max(0, draft.pendingToolCalls - 1);
      setVoiceState((current) =>
        current === "tool_running" ? "thinking" : current,
      );
    }
  }

  function handleRealtimeServerEvent(
    conversationId: number,
    event: RealtimeServerEvent,
  ) {
    const eventType = event.type;
    const toolCall = realtimeToolCallFromEvent(event);
    if (toolCall) {
      void handleRealtimeToolCall(conversationId, toolCall);
      return;
    }

    switch (eventType) {
      case "input_audio_buffer.speech_started":
        getOrCreateRealtimeTurn(conversationId);
        setVoiceState("user_speaking");
        break;
      case "input_audio_buffer.speech_stopped":
        setVoiceState("thinking");
        break;
      case "conversation.item.input_audio_transcription.delta": {
        const delta = realtimeEventText(event, ["delta"]);
        if (delta) {
          const draft = getOrCreateRealtimeTurn(conversationId);
          updateRealtimeUserText(conversationId, `${draft.userText}${delta}`);
        }
        break;
      }
      case "conversation.item.input_audio_transcription.completed": {
        const transcript = realtimeEventText(event, ["transcript"]);
        if (transcript) {
          updateRealtimeUserText(conversationId, transcript);
        }
        break;
      }
      case "response.output_audio_transcript.delta":
      case "response.audio_transcript.delta":
      case "response.output_text.delta":
      case "response.text.delta":
        setVoiceState("responding");
        appendRealtimeAssistantText(
          conversationId,
          realtimeEventText(event, ["delta", "text"]),
        );
        break;
      case "response.output_audio_transcript.done":
      case "response.audio_transcript.done": {
        const transcript = realtimeEventText(event, ["transcript", "text"]);
        if (transcript) {
          setRealtimeAssistantText(conversationId, transcript);
        }
        break;
      }
      case "response.created":
        setVoiceState("thinking");
        break;
      case "response.done":
        setVoiceState("listening");
        void persistCurrentRealtimeTurn(conversationId);
        break;
      case "error":
        setVoiceState("error");
        setAssistantError("La sesión de voz ha devuelto un error.");
        break;
      default:
        break;
    }
  }

  async function startRealtimeVoice() {
    if (
      !selectedConversation ||
      selectedConversation.status !== "active" ||
      realtimeVoiceSessionRef.current
    ) {
      return;
    }
    if (!assistantStatus?.realtime_voice_enabled) {
      setRealtimeVoiceFallback(true);
      return;
    }

    const conversationId = selectedConversation.id;
    setAssistantError("");
    setVoiceState("connecting");
    setRealtimeVoiceFallback(false);

    try {
      const session = await createAssistantRealtimeSession(
        conversationId,
        getStoredToken(),
      );
      const realtimeSession = await RealtimeVoiceSession.start(session, {
        onOpen: () => {
          if (selectedIdRef.current === conversationId) {
            setRealtimeVoiceActive(true);
            setVoiceState("listening");
          }
        },
        onClose: () => {
          setRealtimeVoiceActive(false);
          if (selectedIdRef.current === conversationId) {
            setVoiceState("idle");
          }
        },
        onEvent: (event) => {
          if (selectedIdRef.current === conversationId) {
            handleRealtimeServerEvent(conversationId, event);
          }
        },
        onError: (error) => {
          if (selectedIdRef.current === conversationId) {
            setVoiceState("error");
            handleRequestError(
              error,
              setAssistantError,
              "La voz en tiempo real ha fallado.",
            );
          }
        },
      });
      if (selectedIdRef.current !== conversationId) {
        realtimeSession.stop();
        return;
      }
      realtimeVoiceSessionRef.current = realtimeSession;
      setRealtimeVoiceActive(true);
      setVoiceState("listening");
    } catch (requestError) {
      setRealtimeVoiceActive(false);
      setRealtimeVoiceFallback(true);
      setVoiceState("error");
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo iniciar la voz en tiempo real.",
      );
    }
  }

  function stopRealtimeVoice(options: { interrupted?: boolean } = {}) {
    const session = realtimeVoiceSessionRef.current;
    realtimeVoiceSessionRef.current = null;
    if (session) {
      session.stop();
    }
    setRealtimeVoiceActive(false);
    const conversationId = realtimeTurnRef.current?.conversationId;
    if (conversationId !== undefined) {
      void persistCurrentRealtimeTurn(conversationId, {
        interrupted: options.interrupted,
      });
    }
    setVoiceState(options.interrupted ? "interrupted" : "idle");
  }

  async function archiveConversation(conversationId: number) {
    setAssistantError("");

    try {
      await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo archivar la conversación.",
        { method: "PATCH", body: JSON.stringify({ status: "archived" }) },
      );

      if (includeArchivedConversations) {
        // The archived conversation stays visible with its new status.
        await loadAssistant();
        if (selectedIdRef.current === conversationId) {
          await selectConversation(conversationId);
        }
        return;
      }

      setConversations((existing) =>
        existing.filter((conversation) => conversation.id !== conversationId),
      );
      if (selectedIdRef.current === conversationId) {
        applySelectedConversation(null);
        setDraftMessage("");
      }
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo archivar la conversación.",
      );
    }
  }

  async function restoreConversation(conversationId: number) {
    setAssistantError("");

    try {
      await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo restaurar la conversación.",
        { method: "PATCH", body: JSON.stringify({ status: "active" }) },
      );

      await loadAssistant();
      if (selectedIdRef.current === conversationId) {
        await selectConversation(conversationId);
      }
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo restaurar la conversación.",
      );
    }
  }

  async function renameConversation(conversationId: number, title: string) {
    const normalizedTitle = title.trim();
    if (!normalizedTitle) {
      return;
    }
    setAssistantError("");

    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo renombrar la conversación.",
        { method: "PATCH", body: JSON.stringify({ title: normalizedTitle }) },
      );

      setConversations((existing) =>
        existing.map((conversation) =>
          conversation.id === detail.id ? toSummary(detail) : conversation,
        ),
      );
      setSelectedConversation((current) =>
        current && current.id === detail.id ? detail : current,
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo renombrar la conversación.",
      );
      throw requestError;
    }
  }

  async function loadMemoryEntries(status: AssistantMemoryStatus = "proposed") {
    setAssistantError("");

    try {
      const entries = await adminRequest<AssistantMemoryEntry[]>(
        `/assistant/memory?status=${status}`,
        getStoredToken(),
        "No se pudieron cargar las propuestas de memoria.",
      );
      setMemoryEntries(entries);
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudieron cargar las propuestas de memoria.",
      );
    }
  }

  async function assignConversationFolder(
    conversationId: number,
    folderId: number | null,
  ) {
    setAssistantError("");
    try {
      const detail = await adminRequest<AssistantConversationDetail>(
        `/assistant/conversations/${conversationId}`,
        getStoredToken(),
        "No se pudo mover la conversación.",
        { method: "PATCH", body: JSON.stringify({ folder_id: folderId }) },
      );
      setConversations((existing) =>
        existing.map((conversation) =>
          conversation.id === detail.id ? toSummary(detail) : conversation,
        ),
      );
      setSelectedConversation((current) =>
        current && current.id === detail.id ? detail : current,
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo mover la conversación.",
      );
    }
  }

  async function createConversationFolder(name: string) {
    const normalizedName = name.trim();
    if (!normalizedName) {
      return null;
    }
    setAssistantError("");
    try {
      const folder = await adminRequest<AssistantConversationFolder>(
        "/assistant/conversation-folders",
        getStoredToken(),
        "No se pudo crear la carpeta.",
        { method: "POST", body: JSON.stringify({ name: normalizedName }) },
      );
      setConversationFolders((existing) => [...existing, folder]);
      return folder;
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo crear la carpeta.",
      );
      return null;
    }
  }

  async function renameConversationFolder(folderId: number, name: string) {
    const normalizedName = name.trim();
    if (!normalizedName) {
      return;
    }
    setAssistantError("");
    try {
      const folder = await adminRequest<AssistantConversationFolder>(
        `/assistant/conversation-folders/${folderId}`,
        getStoredToken(),
        "No se pudo renombrar la carpeta.",
        { method: "PATCH", body: JSON.stringify({ name: normalizedName }) },
      );
      setConversationFolders((existing) =>
        existing.map((candidate) =>
          candidate.id === folder.id ? folder : candidate,
        ),
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo renombrar la carpeta.",
      );
    }
  }

  async function deleteConversationFolder(folderId: number) {
    setAssistantError("");
    try {
      await adminRequest<void>(
        `/assistant/conversation-folders/${folderId}`,
        getStoredToken(),
        "No se pudo eliminar la carpeta.",
        { method: "DELETE" },
      );
      setConversationFolders((existing) =>
        existing.filter((folder) => folder.id !== folderId),
      );
      setConversations((existing) =>
        existing.map((conversation) =>
          conversation.folder_id === folderId
            ? { ...conversation, folder_id: null }
            : conversation,
        ),
      );
      setSelectedConversation((current) =>
        current && current.folder_id === folderId
          ? { ...current, folder_id: null }
          : current,
      );
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo eliminar la carpeta.",
      );
    }
  }

  async function updateMemoryEntry(
    entryId: number,
    updates: {
      category?: AssistantMemoryCategory;
      content?: string;
      status?: AssistantMemoryStatus;
      sensitivity?: AssistantMemorySensitivity;
      review_notes?: string;
    },
  ) {
    setAssistantError("");

    try {
      await adminRequest<AssistantMemoryEntry>(
        `/assistant/memory/${entryId}`,
        getStoredToken(),
        "No se pudo actualizar la memoria.",
        { method: "PATCH", body: JSON.stringify(updates) },
      );
      await loadMemoryEntries();
    } catch (requestError) {
      handleRequestError(
        requestError,
        setAssistantError,
        "No se pudo actualizar la memoria.",
      );
    }
  }

  return {
    assistantStatus,
    conversations,
    conversationFolders,
    memoryEntries,
    selectedConversation,
    draftMessage,
    includeArchivedConversations,
    voiceModeEnabled,
    handsFreeEnabled,
    voiceState,
    realtimeVoiceActive,
    realtimeVoiceFallback,
    isLoadingAssistant,
    isSendingMessage,
    isSpeaking,
    assistantError,
    setDraftMessage,
    setVoiceModeEnabled,
    loadAssistant,
    loadMemoryEntries,
    toggleIncludeArchivedConversations,
    selectConversation,
    deselectConversation,
    startConversation,
    sendMessage,
    sendVoiceAudio,
    startRealtimeVoice,
    stopRealtimeVoice,
    stopSpeaking,
    transcribeAudio,
    archiveConversation,
    restoreConversation,
    renameConversation,
    assignConversationFolder,
    createConversationFolder,
    renameConversationFolder,
    deleteConversationFolder,
    updateMemoryEntry,
    clearAssistantState,
  };
}
