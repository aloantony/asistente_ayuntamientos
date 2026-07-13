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
  AssistantRealtimeResponseStatus,
  AssistantStatus,
  AssistantStreamToolActivity,
  AssistantVoiceState,
} from "../components/types";
import {
  adminRequest,
  ApiRequestError,
  completeAssistantRealtimeTurn,
  createAssistantRealtimeSession,
  sendAssistantRealtimeToolCall,
  startAssistantRealtimeTurn,
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
  clientTurnId: string;
  conversationId: number;
  session: RealtimeVoiceSession | null;
  inputItemId: string | null;
  userTempId: number;
  assistantTempId: number;
  userMessageId: number | null;
  userText: string;
  transcriptionCompleted: boolean;
  transcriptionError: Error | null;
  transcriptionReady: Deferred<void>;
  initialResponseRequested: boolean;
  startPromise: Promise<void> | null;
  assistantText: string;
  actions: AssistantAction[];
  toolCalls: Map<string, RealtimeToolCallRecord>;
  responseCycles: Map<string, RealtimeResponseCycle>;
  responseOrder: string[];
  responseRequestIds: Set<string>;
  requestAbortController: AbortController;
  terminalResponseId: string;
  terminalRecoveryPromise: Promise<void> | null;
  operationQueue: Promise<void>;
  confirmationDeliveryRetries: number;
  hasCompletionPayload: boolean;
  completePromise: Promise<void> | null;
  interrupted: boolean;
  terminationScheduled: boolean;
  finalized: boolean;
};

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (reason?: unknown) => void;
};

type RealtimeToolCallRecord = {
  callId: string;
  itemId: string;
  responseId: string;
  name: string;
  arguments: Record<string, unknown>;
  signature: string;
  invalid: boolean;
  status: "registered" | "running" | "completed";
  output: string | null;
  outputSent: boolean;
  confirmationPrompt: string | null;
};

type RealtimeResponseCycle = {
  responseId: string;
  status: AssistantRealtimeResponseStatus | null;
  done: boolean;
  toolCallIds: string[];
  assistantText: string;
  confirmationPrompt: string | null;
  hasAudioOutput: boolean;
  audioPlaybackStopped: boolean;
  audioPlaybackInterrupted: boolean;
  continuationRequested: boolean;
  completionPayload: {
    response_id: string;
    response_status: AssistantRealtimeResponseStatus;
    assistant_text: string | null;
    interrupted: boolean;
  } | null;
  processingAttempts: number;
  processingPromise: Promise<void> | null;
};

type ParsedRealtimeToolCall = {
  callId: string;
  itemId: string;
  responseId: string;
  name: string;
  arguments: Record<string, unknown>;
  signature: string;
  invalid: boolean;
};

type RealtimeResponseRequest = {
  draft: RealtimeTurnDraft;
  confirmationPrompt: string | null;
};

const MAX_CONFIRMATION_DELIVERY_RETRIES = 2;
const MAX_REALTIME_HTTP_ATTEMPTS = 3;
const MAX_REALTIME_PROCESSING_ATTEMPTS = 2;
const MAX_IGNORED_REALTIME_RESPONSES = 100;
const REALTIME_HTTP_TIMEOUT_MS = 15_000;
const REALTIME_TOOL_HTTP_TIMEOUT_MS = 125_000;
const REALTIME_TOOL_RECOVERY_TIMEOUT_MS = 130_000;
const REALTIME_TERMINAL_HTTP_TIMEOUT_MS = 10_000;
const REALTIME_TERMINAL_RECOVERY_TIMEOUT_MS = 140_000;
const REALTIME_TOOL_CALL_IN_PROGRESS_MESSAGE =
  "Realtime tool call is already in progress";
const REALTIME_TURN_TOOL_IN_PROGRESS_MESSAGE =
  "Realtime tool call is still in progress";

class RealtimeToolCallAmbiguousError extends Error {
  constructor() {
    super(
      "La llamada de herramienta sigue en curso y no se pudo confirmar su resultado.",
    );
    this.name = "RealtimeToolCallAmbiguousError";
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function createDeferred<T>(): Deferred<T> {
  let resolve!: Deferred<T>["resolve"];
  let reject!: Deferred<T>["reject"];
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  void promise.catch(() => undefined);
  return { promise, resolve, reject };
}

function parseRealtimeToolArguments(value: unknown): {
  arguments: Record<string, unknown>;
  valid: boolean;
} {
  if (typeof value === "string" && value.trim()) {
    try {
      const parsed = JSON.parse(value) as unknown;
      const argumentsValue = asRecord(parsed);
      return {
        arguments: argumentsValue ?? {},
        valid: argumentsValue !== null,
      };
    } catch {
      return { arguments: {}, valid: false };
    }
  }
  const argumentsValue = asRecord(value);
  return {
    arguments: argumentsValue ?? {},
    valid: argumentsValue !== null,
  };
}

function canonicalRealtimeValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonicalRealtimeValue);
  }
  const record = asRecord(value);
  if (!record) {
    return value;
  }
  return Object.fromEntries(
    Object.keys(record)
      .sort()
      .map((key) => [key, canonicalRealtimeValue(record[key])]),
  );
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

function realtimeToolCallFromEvent(
  event: RealtimeServerEvent,
): ParsedRealtimeToolCall | null {
  const eventType = event.type;
  if (eventType === "response.output_item.done") {
    const item = asRecord(event.item);
    if (item?.type !== "function_call") {
      return null;
    }
    const name = typeof item.name === "string" ? item.name : "";
    const callId = typeof item.call_id === "string" ? item.call_id : "";
    const itemId = typeof item.id === "string" ? item.id : "";
    const responseId =
      typeof event.response_id === "string" ? event.response_id : "";
    if (!name || !callId || !itemId || !responseId) {
      return null;
    }
    const parsed = parseRealtimeToolArguments(item.arguments);
    return {
      name,
      callId,
      itemId,
      responseId,
      arguments: parsed.arguments,
      signature: `${name}:${JSON.stringify(
        canonicalRealtimeValue(parsed.arguments),
      )}`,
      invalid: !parsed.valid,
    };
  }

  if (eventType === "response.function_call_arguments.done") {
    const name = typeof event.name === "string" ? event.name : "";
    const callId = typeof event.call_id === "string" ? event.call_id : "";
    const itemId = typeof event.item_id === "string" ? event.item_id : "";
    const responseId =
      typeof event.response_id === "string" ? event.response_id : "";
    if (!name || !callId || !itemId || !responseId) {
      return null;
    }
    const parsed = parseRealtimeToolArguments(event.arguments);
    return {
      name,
      callId,
      itemId,
      responseId,
      arguments: parsed.arguments,
      signature: `${name}:${JSON.stringify(
        canonicalRealtimeValue(parsed.arguments),
      )}`,
      invalid: !parsed.valid,
    };
  }

  return null;
}

function realtimeEventId(event: RealtimeServerEvent, key: string): string {
  const value = event[key];
  return typeof value === "string" ? value : "";
}

function realtimeResponseDone(event: RealtimeServerEvent): {
  responseId: string;
  status: AssistantRealtimeResponseStatus;
} | null {
  const response = asRecord(event.response);
  const responseId =
    (typeof response?.id === "string" ? response.id : "") ||
    realtimeEventId(event, "response_id");
  const status = response?.status;
  if (
    !responseId ||
    (status !== "completed" &&
      status !== "cancelled" &&
      status !== "failed" &&
      status !== "incomplete")
  ) {
    return null;
  }
  return { responseId, status };
}

function realtimeResponseHasAudioOutput(event: RealtimeServerEvent): boolean {
  const response = asRecord(event.response);
  const output = response?.output;
  if (!Array.isArray(output)) {
    return false;
  }
  return output.some((item) => {
    const content = asRecord(item)?.content;
    return (
      Array.isArray(content) &&
      content.some((part) => {
        const type = asRecord(part)?.type;
        return type === "audio" || type === "output_audio";
      })
    );
  });
}

function confirmationDeliveryInstructions(prompt: string): string {
  return (
    "Lee exactamente el texto siguiente, sin resumirlo, parafrasearlo, " +
    "comentarlo ni añadir nada antes o después. Texto exacto:\n\n" +
    prompt
  );
}

function shouldRetryRealtimeRequest(
  error: unknown,
  options: {
    retryConflict?: (error: ApiRequestError) => boolean;
  } = {},
): boolean {
  return (
    !(error instanceof ApiRequestError) ||
    error.status === 0 ||
    (error.status === 409 && options.retryConflict?.(error) === true) ||
    error.status === 502 ||
    error.status === 503 ||
    error.status === 504
  );
}

function isRealtimeToolCallInProgress(error: ApiRequestError): boolean {
  return (
    error.status === 409 &&
    error.message === REALTIME_TOOL_CALL_IN_PROGRESS_MESSAGE
  );
}

function isRealtimeTurnToolInProgress(error: ApiRequestError): boolean {
  return (
    error.status === 409 &&
    error.message === REALTIME_TURN_TOOL_IN_PROGRESS_MESSAGE
  );
}

async function retryRealtimeRequest<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  options: {
    attempts?: number;
    overallTimeoutMs?: number;
    retryConflict?: (error: ApiRequestError) => boolean;
    signal?: AbortSignal;
    timeoutMs?: number;
  } = {},
): Promise<T> {
  const attempts = options.attempts ?? MAX_REALTIME_HTTP_ATTEMPTS;
  const timeoutMs = options.timeoutMs ?? REALTIME_HTTP_TIMEOUT_MS;
  const deadline =
    options.overallTimeoutMs === undefined
      ? null
      : Date.now() + options.overallTimeoutMs;
  let lastError: unknown = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    options.signal?.throwIfAborted();
    const remainingMs = deadline === null ? timeoutMs : deadline - Date.now();
    if (remainingMs <= 0) {
      throw (
        lastError ??
        new DOMException(
          "La solicitud realtime agotó su presupuesto total.",
          "TimeoutError",
        )
      );
    }
    const attemptController = new AbortController();
    const abortFromParent = () =>
      attemptController.abort(options.signal?.reason);
    options.signal?.addEventListener("abort", abortFromParent, { once: true });
    const timeout = window.setTimeout(
      () =>
        attemptController.abort(
          new DOMException("La solicitud realtime agotó el tiempo.", "TimeoutError"),
        ),
      Math.min(timeoutMs, remainingMs),
    );
    try {
      return await operation(attemptController.signal);
    } catch (requestError) {
      lastError = requestError;
      if (options.signal?.aborted) {
        throw options.signal.reason ?? requestError;
      }
      if (
        attempt + 1 >= attempts ||
        !shouldRetryRealtimeRequest(requestError, options)
      ) {
        throw requestError;
      }
      const retryBudgetMs =
        deadline === null ? Number.POSITIVE_INFINITY : deadline - Date.now();
      if (retryBudgetMs <= 0) {
        throw requestError;
      }
      await new Promise<void>((resolve, reject) => {
        const retryTimeout = window.setTimeout(
          () => {
            options.signal?.removeEventListener("abort", abortRetry);
            resolve();
          },
          Math.min(2_000, 250 * (attempt + 1), retryBudgetMs),
        );
        function abortRetry() {
          window.clearTimeout(retryTimeout);
          reject(options.signal?.reason ?? requestError);
        }
        if (options.signal?.aborted) {
          abortRetry();
          return;
        }
        options.signal?.addEventListener("abort", abortRetry, { once: true });
      });
    } finally {
      window.clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abortFromParent);
      if (!attemptController.signal.aborted) {
        attemptController.abort();
      }
    }
  }
  throw lastError;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function realtimeResponseMetadata(
  event: RealtimeServerEvent,
): Record<string, unknown> | null {
  return asRecord(asRecord(event.response)?.metadata);
}

function realtimeErrorRequestId(event: RealtimeServerEvent): string {
  const requestId = asRecord(event.error)?.event_id;
  return typeof requestId === "string" ? requestId : "";
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
  const [realtimeServerClosurePending, setRealtimeServerClosurePending] =
    useState(false);
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
  const realtimeConnectionAbortRef = useRef<AbortController | null>(null);
  const realtimeConnectionGenerationRef = useRef(0);
  const realtimeTurnRef = useRef<RealtimeTurnDraft | null>(null);
  const realtimeTurnDraftsRef = useRef(
    new Map<string, RealtimeTurnDraft>(),
  );
  const ignoredRealtimeTurnIdsRef = useRef(new Set<string>());
  const realtimeInputDraftsRef = useRef(
    new Map<string, RealtimeTurnDraft>(),
  );
  const ignoredRealtimeInputIdsRef = useRef(new Set<string>());
  const realtimeResponseDraftsRef = useRef(
    new Map<string, RealtimeTurnDraft>(),
  );
  const realtimeResponseRequestsRef = useRef(
    new Map<string, RealtimeResponseRequest>(),
  );
  const realtimeServerClosuresRef = useRef(new Set<string>());
  const realtimeServerClosureWaitersRef = useRef(new Set<() => void>());
  const ignoredRealtimeResponseIdsRef = useRef(new Set<string>());
  const realtimeUnmountCleanupRef = useRef<() => void>(() => undefined);
  const realtimeTempIdRef = useRef(-10000);
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

  realtimeUnmountCleanupRef.current = () => {
    speechPlayerRef.current?.stop();
    realtimeConnectionGenerationRef.current += 1;
    realtimeConnectionAbortRef.current?.abort();
    realtimeConnectionAbortRef.current = null;
    const realtimeSession = realtimeVoiceSessionRef.current;
    realtimeVoiceSessionRef.current = null;
    realtimeSession?.stop();
    cancelRealtimeDraftsForSession(realtimeSession);
  };

  useEffect(() => () => realtimeUnmountCleanupRef.current(), []);

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
    if (
      !content ||
      !selectedConversation ||
      isSendingMessage ||
      realtimeServerClosuresRef.current.size > 0
    ) {
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
    if (
      !selectedConversation ||
      isSendingMessage ||
      audio.size === 0 ||
      realtimeServerClosuresRef.current.size > 0
    ) {
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

  function createRealtimeTurn(
    conversationId: number,
    inputItemId: string | null = null,
  ): RealtimeTurnDraft {
    const userTempId = realtimeTempIdRef.current--;
    const assistantTempId = realtimeTempIdRef.current--;
    const now = new Date().toISOString();
    const draft: RealtimeTurnDraft = {
      clientTurnId: crypto.randomUUID(),
      conversationId,
      session: realtimeVoiceSessionRef.current,
      inputItemId,
      userTempId,
      assistantTempId,
      userMessageId: null,
      userText: "",
      transcriptionCompleted: false,
      transcriptionError: null,
      transcriptionReady: createDeferred<void>(),
      initialResponseRequested: false,
      startPromise: null,
      assistantText: "",
      actions: [],
      toolCalls: new Map(),
      responseCycles: new Map(),
      responseOrder: [],
      responseRequestIds: new Set(),
      requestAbortController: new AbortController(),
      terminalResponseId: `client_interrupted_${crypto.randomUUID()}`,
      terminalRecoveryPromise: null,
      operationQueue: Promise.resolve(),
      confirmationDeliveryRetries: 0,
      hasCompletionPayload: false,
      completePromise: null,
      interrupted: false,
      terminationScheduled: false,
      finalized: false,
    };
    realtimeTurnRef.current = draft;
    realtimeTurnDraftsRef.current.set(draft.clientTurnId, draft);
    if (inputItemId) {
      realtimeInputDraftsRef.current.set(inputItemId, draft);
    }
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

  function isCurrentRealtimeDraft(draft: RealtimeTurnDraft): boolean {
    return (
      realtimeTurnRef.current === draft &&
      selectedIdRef.current === draft.conversationId &&
      realtimeVoiceSessionRef.current === draft.session &&
      draft.session !== null &&
      !draft.session.isClosed &&
      !draft.finalized
    );
  }

  function getOrCreateRealtimeTurn(
    conversationId: number,
    inputItemId: string | null = null,
  ): RealtimeTurnDraft {
    if (inputItemId) {
      const mapped = realtimeInputDraftsRef.current.get(inputItemId);
      if (mapped) {
        return mapped;
      }
    }
    const existing = realtimeTurnRef.current;
    if (
      existing?.conversationId === conversationId &&
      !existing.finalized &&
      (!inputItemId ||
        !existing.inputItemId ||
        existing.inputItemId === inputItemId)
    ) {
      if (!existing.session) {
        existing.session = realtimeVoiceSessionRef.current;
      }
      if (inputItemId && !existing.inputItemId) {
        existing.inputItemId = inputItemId;
        realtimeInputDraftsRef.current.set(inputItemId, existing);
      }
      return existing;
    }
    if (existing && !existing.finalized) {
      existing.interrupted = true;
      scheduleRealtimeDraftTermination(existing);
    }
    return createRealtimeTurn(conversationId, inputItemId);
  }

  function realtimeDraftForInput(
    conversationId: number,
    event: RealtimeServerEvent,
  ): RealtimeTurnDraft | null {
    const inputItemId = realtimeEventId(event, "item_id");
    if (inputItemId && ignoredRealtimeInputIdsRef.current.has(inputItemId)) {
      return null;
    }
    return getOrCreateRealtimeTurn(conversationId, inputItemId || null);
  }

  function ensureRealtimeResponseCycle(
    draft: RealtimeTurnDraft,
    responseId: string,
  ): RealtimeResponseCycle {
    const existing = draft.responseCycles.get(responseId);
    if (existing) {
      return existing;
    }
    const cycle: RealtimeResponseCycle = {
      responseId,
      status: null,
      done: false,
      toolCallIds: [],
      assistantText: "",
      confirmationPrompt: null,
      hasAudioOutput: false,
      audioPlaybackStopped: false,
      audioPlaybackInterrupted: false,
      continuationRequested: false,
      completionPayload: null,
      processingAttempts: 0,
      processingPromise: null,
    };
    draft.responseCycles.set(responseId, cycle);
    draft.responseOrder.push(responseId);
    realtimeResponseDraftsRef.current.set(responseId, draft);
    return cycle;
  }

  function realtimeDraftForResponse(
    conversationId: number,
    responseId: string,
  ): RealtimeTurnDraft | null {
    if (ignoredRealtimeResponseIdsRef.current.has(responseId)) {
      return null;
    }
    const mapped = realtimeResponseDraftsRef.current.get(responseId);
    if (mapped?.conversationId === conversationId) {
      return mapped;
    }
    return null;
  }

  function updateRealtimeUserText(draft: RealtimeTurnDraft, text: string) {
    if (draft.finalized) {
      return;
    }
    draft.userText = text;
    setSelectedConversation((current) =>
      current && current.id === draft.conversationId
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

  function syncRealtimeAssistantText(draft: RealtimeTurnDraft) {
    if (draft.finalized) {
      return;
    }
    draft.assistantText = draft.responseOrder
      .map((responseId) => draft.responseCycles.get(responseId)?.assistantText)
      .filter((text): text is string => Boolean(text?.trim()))
      .join("\n\n");
    setSelectedConversation((current) =>
      current && current.id === draft.conversationId
        ? {
            ...current,
            messages: current.messages.map((message) =>
              message.id === draft.assistantTempId
                ? { ...message, content: draft.assistantText }
                : message,
            ),
          }
        : current,
    );
  }

  function appendRealtimeAssistantText(
    draft: RealtimeTurnDraft,
    responseId: string,
    text: string,
  ) {
    if (!text || draft.finalized) {
      return;
    }
    const cycle = ensureRealtimeResponseCycle(draft, responseId);
    cycle.assistantText = `${cycle.assistantText}${text}`;
    syncRealtimeAssistantText(draft);
  }

  function setRealtimeAssistantText(
    draft: RealtimeTurnDraft,
    responseId: string,
    text: string,
  ) {
    if (draft.finalized) {
      return;
    }
    ensureRealtimeResponseCycle(draft, responseId).assistantText = text;
    syncRealtimeAssistantText(draft);
  }

  function updateRealtimeAction(
    draft: RealtimeTurnDraft,
    callId: string,
    action: AssistantAction,
  ) {
    if (draft.finalized) {
      return;
    }
    const nextAction = { ...action, call_id: callId };
    const existingIndex = draft.actions.findIndex(
      (candidate) => candidate.call_id === callId,
    );
    if (existingIndex >= 0) {
      draft.actions[existingIndex] = nextAction;
    } else {
      draft.actions.push(nextAction);
    }
    setSelectedConversation((current) =>
      current && current.id === draft.conversationId
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
    draft: RealtimeTurnDraft,
    userMessage: AssistantConversationDetail["messages"][number],
  ) {
    if (
      draft.userMessageId !== null &&
      draft.userMessageId !== userMessage.id
    ) {
      throw new Error("El servidor devolvió otro mensaje para el mismo turno.");
    }
    draft.userMessageId = userMessage.id;
    setSelectedConversation((current) =>
      current && current.id === draft.conversationId
        ? {
            ...current,
            messages: current.messages.map((message) =>
              message.id === draft.userTempId || message.id === userMessage.id
                ? userMessage
                : message,
            ),
          }
        : current,
    );
  }

  async function ensureRealtimeTurnStarted(draft: RealtimeTurnDraft) {
    if (draft.startPromise) {
      return draft.startPromise;
    }
    const startPromise = (async () => {
      await draft.transcriptionReady.promise;
      if (draft.transcriptionError) {
        throw draft.transcriptionError;
      }
      const userText = draft.userText.trim();
      if (!userText) {
        throw new Error("La transcripción final del turno está vacía.");
      }
      const result = await retryRealtimeRequest(
        (signal) =>
        startAssistantRealtimeTurn(
          draft.conversationId,
          { turn_id: draft.clientTurnId, user_text: userText },
          getStoredToken(),
          signal,
        ),
        { signal: draft.requestAbortController.signal },
      );
      if (result.turn_id !== draft.clientTurnId) {
        throw new Error("El servidor devolvió un identificador de turno distinto.");
      }
      replaceRealtimeUserMessage(draft, result.user_message);
    })();
    draft.startPromise = startPromise;
    try {
      await startPromise;
    } catch (error) {
      if (draft.startPromise === startPromise) {
        draft.startPromise = null;
      }
      throw error;
    }
  }

  function registerRealtimeToolCall(
    conversationId: number,
    toolCall: ParsedRealtimeToolCall,
  ) {
    const draft = realtimeDraftForResponse(conversationId, toolCall.responseId);
    if (!draft || draft.finalized) {
      return;
    }
    const cycle = ensureRealtimeResponseCycle(draft, toolCall.responseId);
    const existing = draft.toolCalls.get(toolCall.callId);
    if (existing) {
      if (
        existing.signature !== toolCall.signature ||
        existing.responseId !== toolCall.responseId ||
        existing.itemId !== toolCall.itemId
      ) {
        existing.invalid = true;
        if (isCurrentRealtimeDraft(draft)) {
          setAssistantError(
            "La sesión de voz intentó reutilizar una llamada con otros datos.",
          );
        }
      }
      return;
    }
    const record: RealtimeToolCallRecord = {
      ...toolCall,
      status: "registered",
      output: null,
      outputSent: false,
      confirmationPrompt: null,
    };
    draft.toolCalls.set(record.callId, record);
    cycle.toolCallIds.push(record.callId);
    updateRealtimeAction(draft, record.callId, {
      tool: record.name,
      ok: false,
      input: record.arguments,
      result: "",
      status: "started",
    });
  }

  async function postRealtimeToolCallWithReplay(
    draft: RealtimeTurnDraft,
    record: RealtimeToolCallRecord,
  ) {
    try {
      return await retryRealtimeRequest(
        (signal) =>
          sendAssistantRealtimeToolCall(
            draft.conversationId,
            draft.clientTurnId,
            {
              call_id: record.callId,
              name: record.name,
              arguments: record.arguments,
            },
            getStoredToken(),
            signal,
          ),
        {
          attempts: Number.POSITIVE_INFINITY,
          overallTimeoutMs: REALTIME_TOOL_RECOVERY_TIMEOUT_MS,
          retryConflict: isRealtimeToolCallInProgress,
          signal: draft.requestAbortController.signal,
          timeoutMs: REALTIME_TOOL_HTTP_TIMEOUT_MS,
        },
      );
    } catch (requestError) {
      if (
        !draft.requestAbortController.signal.aborted &&
        shouldRetryRealtimeRequest(requestError, {
          retryConflict: isRealtimeToolCallInProgress,
        })
      ) {
        throw new RealtimeToolCallAmbiguousError();
      }
      throw requestError;
    }
  }

  async function executeRealtimeToolCall(
    draft: RealtimeTurnDraft,
    record: RealtimeToolCallRecord,
  ) {
    if (record.status === "completed") {
      return;
    }
    record.status = "running";
    if (isCurrentRealtimeDraft(draft)) {
      setVoiceState("tool_running");
    }
    if (record.invalid) {
      record.output = JSON.stringify({
        error: "La llamada de herramienta recibida no es válida.",
      });
      record.status = "completed";
      updateRealtimeAction(draft, record.callId, {
        tool: record.name,
        ok: false,
        input: record.arguments,
        result: "La llamada recibida no es válida.",
        status: "finished",
      });
      return;
    }

    try {
      await ensureRealtimeTurnStarted(draft);
      const result = await postRealtimeToolCallWithReplay(draft, record);
      if (result.call_id !== record.callId) {
        throw new Error("El servidor devolvió otra llamada de herramienta.");
      }
      replaceRealtimeUserMessage(draft, result.user_message);
      record.output = result.output;
      record.confirmationPrompt = result.confirmation_prompt;
      updateRealtimeAction(draft, record.callId, {
        ...result.action,
        status: "finished",
      });
    } catch (requestError) {
      if (draft.requestAbortController.signal.aborted) {
        throw requestError;
      }
      if (requestError instanceof RealtimeToolCallAmbiguousError) {
        draft.interrupted = true;
        throw requestError;
      }
      const message =
        requestError instanceof Error
          ? requestError.message
          : "No se pudo ejecutar la herramienta.";
      record.output = JSON.stringify({
        error: "No se pudo ejecutar la herramienta solicitada.",
      });
      updateRealtimeAction(draft, record.callId, {
        tool: record.name,
        ok: false,
        input: record.arguments,
        result: message,
        status: "finished",
      });
      if (isCurrentRealtimeDraft(draft)) {
        handleRequestError(
          requestError,
          setAssistantError,
          "No se pudo ejecutar la herramienta de voz.",
        );
      }
    } finally {
      if (record.output !== null) {
        record.status = "completed";
      }
    }
  }

  function setRealtimeServerClosure(
    draft: RealtimeTurnDraft,
    pending: boolean,
  ) {
    const closures = realtimeServerClosuresRef.current;
    if (pending) {
      closures.add(draft.clientTurnId);
    } else {
      closures.delete(draft.clientTurnId);
    }
    setRealtimeServerClosurePending(closures.size > 0);
    if (!pending) {
      const waiters = [...realtimeServerClosureWaitersRef.current];
      realtimeServerClosureWaitersRef.current.clear();
      waiters.forEach((resolve) => resolve());
    }
  }

  async function waitForOtherRealtimeServerClosures(
    draft: RealtimeTurnDraft,
  ) {
    while (
      [...realtimeServerClosuresRef.current].some(
        (turnId) => turnId !== draft.clientTurnId,
      )
    ) {
      await new Promise<void>((resolve) =>
        realtimeServerClosureWaitersRef.current.add(resolve),
      );
      if (draft.finalized || draft.interrupted) {
        throw new Error("El turno de voz terminó antes de poder responder.");
      }
    }
  }

  async function requestRealtimeResponse(
    draft: RealtimeTurnDraft,
    confirmationPrompt: string | null = null,
  ) {
    await waitForOtherRealtimeServerClosures(draft);
    if (draft.finalized) {
      return;
    }
    const session = draft.session;
    if (!session) {
      throw new Error("La sesión de voz ya no está conectada.");
    }
    if (
      session.isClosed ||
      session !== realtimeVoiceSessionRef.current ||
      draft.interrupted
    ) {
      throw new Error("La sesión de voz ya no está activa para este turno.");
    }
    const requestId = `response_${crypto.randomUUID()}`;
    draft.responseRequestIds.add(requestId);
    realtimeResponseRequestsRef.current.set(requestId, {
      draft,
      confirmationPrompt,
    });
    try {
      session.requestResponse(
        confirmationPrompt
          ? {
              eventId: requestId,
              instructions: confirmationDeliveryInstructions(
                confirmationPrompt,
              ),
              metadata: {
                client_request_id: requestId,
                client_turn_id: draft.clientTurnId,
              },
              toolChoice: "none",
            }
          : {
              eventId: requestId,
              metadata: {
                client_request_id: requestId,
                client_turn_id: draft.clientTurnId,
              },
            },
      );
      session.setInputEnabled(true);
      setRealtimeServerClosure(draft, false);
    } catch (error) {
      draft.responseRequestIds.delete(requestId);
      realtimeResponseRequestsRef.current.delete(requestId);
      throw error;
    }
    return requestId;
  }

  function rememberIgnoredRealtimeResponse(responseId: string) {
    const ignored = ignoredRealtimeResponseIdsRef.current;
    ignored.add(responseId);
    while (ignored.size > MAX_IGNORED_REALTIME_RESPONSES) {
      const oldest = ignored.values().next().value;
      if (typeof oldest !== "string") {
        break;
      }
      ignored.delete(oldest);
    }
  }

  function rememberIgnoredRealtimeInput(inputItemId: string) {
    const ignored = ignoredRealtimeInputIdsRef.current;
    ignored.add(inputItemId);
    while (ignored.size > MAX_IGNORED_REALTIME_RESPONSES) {
      const oldest = ignored.values().next().value;
      if (typeof oldest !== "string") {
        break;
      }
      ignored.delete(oldest);
    }
  }

  function rememberIgnoredRealtimeTurn(turnId: string) {
    const ignored = ignoredRealtimeTurnIdsRef.current;
    ignored.add(turnId);
    while (ignored.size > MAX_IGNORED_REALTIME_RESPONSES) {
      const oldest = ignored.values().next().value;
      if (typeof oldest !== "string") {
        break;
      }
      ignored.delete(oldest);
    }
  }

  function cleanupRealtimeDraft(draft: RealtimeTurnDraft) {
    draft.finalized = true;
    if (!draft.requestAbortController.signal.aborted) {
      draft.requestAbortController.abort(
        new DOMException("El turno de voz ha finalizado.", "AbortError"),
      );
    }
    setRealtimeServerClosure(draft, false);
    if (realtimeTurnRef.current === draft) {
      realtimeTurnRef.current = null;
    }
    if (draft.inputItemId) {
      realtimeInputDraftsRef.current.delete(draft.inputItemId);
      rememberIgnoredRealtimeInput(draft.inputItemId);
    }
    realtimeTurnDraftsRef.current.delete(draft.clientTurnId);
    rememberIgnoredRealtimeTurn(draft.clientTurnId);
    for (const responseId of draft.responseCycles.keys()) {
      if (realtimeResponseDraftsRef.current.get(responseId) === draft) {
        realtimeResponseDraftsRef.current.delete(responseId);
      }
      rememberIgnoredRealtimeResponse(responseId);
    }
    for (const requestId of draft.responseRequestIds) {
      if (
        realtimeResponseRequestsRef.current.get(requestId)?.draft === draft
      ) {
        realtimeResponseRequestsRef.current.delete(requestId);
      }
    }
    draft.responseRequestIds.clear();
  }

  function discardRealtimeDraft(draft: RealtimeTurnDraft) {
    if (draft.finalized) {
      return;
    }
    setSelectedConversation((current) =>
      current && current.id === draft.conversationId
        ? {
            ...current,
            messages: current.messages.filter(
              (message) =>
                message.id !== draft.userTempId &&
                message.id !== draft.assistantTempId,
            ),
          }
        : current,
    );
    cleanupRealtimeDraft(draft);
  }

  function cancelRealtimeDraft(draft: RealtimeTurnDraft) {
    if (draft.finalized) {
      return;
    }
    draft.interrupted = true;
    if (!draft.requestAbortController.signal.aborted) {
      draft.requestAbortController.abort(
        new DOMException("El turno de voz fue cancelado.", "AbortError"),
      );
    }
    void attemptTerminalRealtimeCompletion(draft);
  }

  function failRealtimeTranscription(
    draft: RealtimeTurnDraft,
    error: Error,
  ) {
    if (!draft.transcriptionCompleted) {
      draft.transcriptionCompleted = true;
      draft.transcriptionError = error;
      draft.transcriptionReady.reject(error);
    }
    draft.interrupted = true;
    discardRealtimeDraft(draft);
  }

  function scheduleRealtimeDraftTermination(draft: RealtimeTurnDraft) {
    if (draft.finalized || draft.terminationScheduled) {
      return;
    }
    draft.interrupted = true;
    if (
      !draft.transcriptionCompleted ||
      draft.transcriptionError ||
      !draft.userText.trim()
    ) {
      failRealtimeTranscription(
        draft,
        draft.transcriptionError ??
          new Error("El turno de voz terminó antes de transcribirse."),
      );
      return;
    }
    draft.terminationScheduled = true;
    setRealtimeServerClosure(draft, true);
    const syntheticCycle = ensureRealtimeResponseCycle(
      draft,
      draft.terminalResponseId,
    );
    syntheticCycle.done = true;
    syntheticCycle.status = "cancelled";
    scheduleRealtimeResponseCycle(draft, syntheticCycle);
  }

  function applyCompletedRealtimeTurn(
    draft: RealtimeTurnDraft,
    result: Awaited<ReturnType<typeof completeAssistantRealtimeTurn>>,
  ) {
    const shouldResumeListening =
      draft.session !== null &&
      draft.session === realtimeVoiceSessionRef.current &&
      !draft.session.isClosed;
    setSelectedConversation((current) => {
      if (!current || current.id !== draft.conversationId) {
        return current;
      }
      let insertedUser = false;
      let insertedAssistant = false;
      const messages = current.messages.flatMap((message) => {
        if (
          message.id === draft.userTempId ||
          (draft.userMessageId !== null && message.id === draft.userMessageId)
        ) {
          if (!result.user_message || insertedUser) {
            return [];
          }
          insertedUser = true;
          return [result.user_message];
        }
        if (message.id === draft.assistantTempId) {
          if (!result.assistant_message || insertedAssistant) {
            return [];
          }
          insertedAssistant = true;
          return [result.assistant_message];
        }
        if (
          message.id === result.user_message?.id ||
          message.id === result.assistant_message?.id
        ) {
          return [];
        }
        return [message];
      });
      if (result.user_message && !insertedUser) {
        messages.push(result.user_message);
      }
      if (result.assistant_message && !insertedAssistant) {
        messages.push(result.assistant_message);
      }
      return { ...current, ...result.conversation, messages };
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
    if (
      result.assistant_message?.actions.some(
        (action) => action.ok && mutatingTools.has(action.tool),
      )
    ) {
      onRequirementsChanged?.();
    }
    cleanupRealtimeDraft(draft);
    if (shouldResumeListening) {
      draft.session?.setInputEnabled(true);
      setVoiceState("listening");
    }
  }

  async function completeRealtimeResponse(
    draft: RealtimeTurnDraft,
    cycle: RealtimeResponseCycle,
  ) {
    if (draft.finalized || !cycle.status) {
      return;
    }
    const responseStatus = cycle.status;
    if (draft.completePromise) {
      await draft.completePromise;
    }
    if (draft.finalized) {
      return;
    }
    const completePromise = (async () => {
      await ensureRealtimeTurnStarted(draft);
      let payload = cycle.completionPayload;
      if (!payload) {
        const assistantText = (
          draft.hasCompletionPayload
            ? cycle.assistantText
            : draft.assistantText
        ).trim();
        payload = {
          response_id: cycle.responseId,
          response_status: responseStatus,
          assistant_text: assistantText || null,
          interrupted: draft.interrupted || responseStatus !== "completed",
        };
        cycle.completionPayload = payload;
        draft.hasCompletionPayload = true;
      }
      const result = await retryRealtimeRequest(
        (signal) =>
          completeAssistantRealtimeTurn(
          draft.conversationId,
          draft.clientTurnId,
          payload,
          getStoredToken(),
          signal,
        ),
        { signal: draft.requestAbortController.signal },
      );
      if (result.confirmation_delivery_required) {
        if (!result.confirmation_prompt) {
          throw new Error(
            "El servidor pidió entregar una confirmación sin incluir su texto.",
          );
        }
        if (
          draft.confirmationDeliveryRetries >=
          MAX_CONFIRMATION_DELIVERY_RETRIES
        ) {
          draft.interrupted = true;
          throw new Error(
            "No se pudo entregar literalmente la confirmación de seguridad.",
          );
        }
        if (!isCurrentRealtimeDraft(draft)) {
          draft.interrupted = true;
          scheduleRealtimeDraftTermination(draft);
          return;
        }
        draft.confirmationDeliveryRetries += 1;
        await requestRealtimeResponse(draft, result.confirmation_prompt);
        return;
      }
      applyCompletedRealtimeTurn(draft, result);
    })();
    draft.completePromise = completePromise;
    try {
      await completePromise;
    } finally {
      if (draft.completePromise === completePromise) {
        draft.completePromise = null;
      }
    }
  }

  async function attemptTerminalRealtimeCompletion(
    draft: RealtimeTurnDraft,
  ): Promise<boolean> {
    if (draft.finalized) {
      return true;
    }
    if (
      !draft.transcriptionCompleted ||
      draft.transcriptionError ||
      !draft.userText.trim()
    ) {
      discardRealtimeDraft(draft);
      return false;
    }
    if (draft.terminalRecoveryPromise) {
      await draft.terminalRecoveryPromise;
      return draft.finalized;
    }

    draft.interrupted = true;
    if (!draft.requestAbortController.signal.aborted) {
      draft.requestAbortController.abort(
        new DOMException("El turno de voz requiere cierre terminal.", "AbortError"),
      );
    }
    setRealtimeServerClosure(draft, true);
    const terminalController = new AbortController();
    const terminalTimeout = window.setTimeout(
      () =>
        terminalController.abort(
          new DOMException(
            "El cierre del turno realtime agotó el tiempo.",
            "TimeoutError",
          ),
        ),
      REALTIME_TERMINAL_RECOVERY_TIMEOUT_MS,
    );
    const recoveryPromise = (async () => {
      try {
        const userText = draft.userText.trim();
        const startResult = await retryRealtimeRequest(
          (signal) =>
            startAssistantRealtimeTurn(
              draft.conversationId,
              { turn_id: draft.clientTurnId, user_text: userText },
              getStoredToken(),
              signal,
          ),
          {
            attempts: 2,
            signal: terminalController.signal,
            timeoutMs: REALTIME_TERMINAL_HTTP_TIMEOUT_MS,
          },
        );
        if (startResult.turn_id !== draft.clientTurnId) {
          throw new Error(
            "El servidor devolvió otro identificador al cerrar el turno realtime.",
          );
        }
        if (draft.finalized) {
          return;
        }
        replaceRealtimeUserMessage(draft, startResult.user_message);
        const payload = {
          response_id: draft.terminalResponseId,
          response_status: "cancelled" as const,
          assistant_text: draft.assistantText.trim() || null,
          interrupted: true,
        };
        const result = await retryRealtimeRequest(
          (signal) =>
            completeAssistantRealtimeTurn(
              draft.conversationId,
              draft.clientTurnId,
              payload,
              getStoredToken(),
              signal,
          ),
          {
            attempts: Number.POSITIVE_INFINITY,
            overallTimeoutMs: REALTIME_TOOL_RECOVERY_TIMEOUT_MS,
            retryConflict: isRealtimeTurnToolInProgress,
            signal: terminalController.signal,
            timeoutMs: REALTIME_TERMINAL_HTTP_TIMEOUT_MS,
          },
        );
        if (result.confirmation_delivery_required) {
          throw new Error(
            "El servidor no pudo cerrar el turno realtime interrumpido.",
          );
        }
        applyCompletedRealtimeTurn(draft, result);
      } catch {
        if (!draft.finalized) {
          discardRealtimeDraft(draft);
          if (selectedIdRef.current === draft.conversationId) {
            void selectConversation(draft.conversationId);
          }
        }
      } finally {
        window.clearTimeout(terminalTimeout);
      }
    })();
    draft.terminalRecoveryPromise = recoveryPromise;
    try {
      await recoveryPromise;
    } finally {
      if (draft.terminalRecoveryPromise === recoveryPromise) {
        draft.terminalRecoveryPromise = null;
      }
    }
    return draft.finalized;
  }

  async function processRealtimeResponseCycle(
    draft: RealtimeTurnDraft,
    cycle: RealtimeResponseCycle,
  ) {
    if (draft.finalized || !cycle.done || !cycle.status) {
      return;
    }
    if (cycle.status !== "completed" || cycle.toolCallIds.length === 0) {
      await completeRealtimeResponse(draft, cycle);
      return;
    }

    for (const callId of cycle.toolCallIds) {
      const record = draft.toolCalls.get(callId);
      if (record) {
        await executeRealtimeToolCall(draft, record);
      }
    }
    if (draft.finalized || cycle.continuationRequested) {
      return;
    }
    if (!isCurrentRealtimeDraft(draft)) {
      draft.interrupted = true;
      await completeRealtimeResponse(draft, cycle);
      return;
    }

    const confirmationPrompts = new Set(
      cycle.toolCallIds
        .map((callId) => draft.toolCalls.get(callId)?.confirmationPrompt)
        .filter((prompt): prompt is string => Boolean(prompt)),
    );
    if (confirmationPrompts.size > 1) {
      throw new Error(
        "El servidor devolvió varias confirmaciones distintas en un turno.",
      );
    }
    const session = draft.session;
    if (!session) {
      throw new Error("La sesión de voz se cerró antes de entregar el resultado.");
    }
    for (const callId of cycle.toolCallIds) {
      const record = draft.toolCalls.get(callId);
      if (!record || record.outputSent || record.output === null) {
        continue;
      }
      session.sendFunctionOutputItem(record.callId, record.output);
      record.outputSent = true;
    }
    await requestRealtimeResponse(
      draft,
      confirmationPrompts.values().next().value ?? null,
    );
    cycle.continuationRequested = true;
    if (isCurrentRealtimeDraft(draft)) {
      setVoiceState("thinking");
    }
  }

  function scheduleRealtimeResponseCycle(
    draft: RealtimeTurnDraft,
    cycle: RealtimeResponseCycle,
  ) {
    if (
      cycle.processingPromise ||
      draft.finalized ||
      cycle.processingAttempts >= MAX_REALTIME_PROCESSING_ATTEMPTS
    ) {
      return;
    }
    cycle.processingAttempts += 1;
    const processingPromise = draft.operationQueue.then(() =>
      processRealtimeResponseCycle(draft, cycle),
    );
    cycle.processingPromise = processingPromise;
    draft.operationQueue = processingPromise.catch(() => undefined);
    void processingPromise.catch(async (requestError) => {
      if (cycle.processingPromise === processingPromise) {
        cycle.processingPromise = null;
      }
      const shouldRetry =
        !draft.finalized &&
        !draft.requestAbortController.signal.aborted &&
        !(requestError instanceof RealtimeToolCallAmbiguousError) &&
        cycle.processingAttempts < MAX_REALTIME_PROCESSING_ATTEMPTS &&
        shouldRetryRealtimeRequest(requestError);
      if (shouldRetry) {
        window.setTimeout(
          () => scheduleRealtimeResponseCycle(draft, cycle),
          500,
        );
        return;
      }
      draft.interrupted = true;
      const shouldCloseSession = isCurrentRealtimeDraft(draft);
      if (shouldCloseSession) {
        handleRequestError(
          requestError,
          setAssistantError,
          "No se pudo completar el turno de voz en tiempo real.",
        );
      }
      if (!draft.finalized) {
        await attemptTerminalRealtimeCompletion(draft);
      }
      if (shouldCloseSession) {
        stopRealtimeVoice({ interrupted: true });
        setVoiceState("error");
      }
    });
  }

  function scheduleDeliverableRealtimeResponseCycle(
    draft: RealtimeTurnDraft,
    cycle: RealtimeResponseCycle,
  ) {
    if (draft.finalized || !cycle.done || !cycle.status) {
      return;
    }
    if (cycle.status === "completed" && cycle.confirmationPrompt !== null) {
      if (!cycle.hasAudioOutput) {
        cycle.audioPlaybackInterrupted = true;
        draft.interrupted = true;
      } else if (
        !cycle.audioPlaybackStopped &&
        !cycle.audioPlaybackInterrupted
      ) {
        if (isCurrentRealtimeDraft(draft)) {
          setVoiceState("responding");
        }
        return;
      }
    }
    if (
      draft.terminationScheduled &&
      cycle.responseId !== draft.terminalResponseId
    ) {
      return;
    }
    if (isCurrentRealtimeDraft(draft)) {
      draft.session?.setInputEnabled(false);
      setVoiceState(
        cycle.toolCallIds.length > 0 ? "tool_running" : "thinking",
      );
    }
    scheduleRealtimeResponseCycle(draft, cycle);
  }

  function handleRealtimeServerEvent(
    conversationId: number,
    event: RealtimeServerEvent,
  ) {
    const eventType = event.type;
    const toolCall = realtimeToolCallFromEvent(event);
    if (toolCall) {
      registerRealtimeToolCall(conversationId, toolCall);
      return;
    }

    switch (eventType) {
      case "input_audio_buffer.speech_started": {
        const currentDraft = realtimeTurnRef.current;
        let interruptedPlayback = false;
        if (currentDraft && !currentDraft.finalized) {
          for (const cycle of currentDraft.responseCycles.values()) {
            if (cycle.hasAudioOutput && !cycle.audioPlaybackStopped) {
              cycle.audioPlaybackInterrupted = true;
              interruptedPlayback = true;
            }
          }
        }
        const inputItemId = realtimeEventId(event, "item_id") || null;
        if (currentDraft && interruptedPlayback) {
          currentDraft.interrupted = true;
          scheduleRealtimeDraftTermination(currentDraft);
          createRealtimeTurn(conversationId, inputItemId);
        } else {
          getOrCreateRealtimeTurn(conversationId, inputItemId);
        }
        setVoiceState("user_speaking");
        break;
      }
      case "input_audio_buffer.speech_stopped":
        setVoiceState("thinking");
        break;
      case "conversation.item.input_audio_transcription.delta": {
        const delta = realtimeEventText(event, ["delta"]);
        if (delta) {
          const draft = realtimeDraftForInput(conversationId, event);
          if (draft) {
            updateRealtimeUserText(draft, `${draft.userText}${delta}`);
          }
        }
        break;
      }
      case "conversation.item.input_audio_transcription.completed": {
        const draft = realtimeDraftForInput(conversationId, event);
        if (!draft || draft.transcriptionCompleted) {
          break;
        }
        const transcript = realtimeEventText(event, ["transcript"]);
        if (!transcript.trim()) {
          const error = new Error("La transcripción final del turno está vacía.");
          failRealtimeTranscription(draft, error);
          stopRealtimeVoice({ interrupted: true });
          setAssistantError(error.message);
          setVoiceState("error");
          break;
        }
        updateRealtimeUserText(draft, transcript);
        draft.transcriptionCompleted = true;
        draft.transcriptionReady.resolve(undefined);
        if (!draft.initialResponseRequested) {
          draft.initialResponseRequested = true;
          draft.session?.setInputEnabled(false);
          void (async () => {
            try {
              await requestRealtimeResponse(draft);
              if (isCurrentRealtimeDraft(draft)) {
                setVoiceState("thinking");
              }
            } catch (requestError) {
              draft.interrupted = true;
              scheduleRealtimeDraftTermination(draft);
              stopRealtimeVoice({ interrupted: true });
              handleRequestError(
                requestError,
                setAssistantError,
                "No se pudo iniciar la respuesta de voz.",
              );
              setVoiceState("error");
            }
          })();
        }
        break;
      }
      case "conversation.item.input_audio_transcription.failed": {
        const draft = realtimeDraftForInput(conversationId, event);
        if (!draft) {
          break;
        }
        const providerMessage = asRecord(event.error)?.message;
        const error = new Error(
          typeof providerMessage === "string" && providerMessage
            ? providerMessage
            : "No se pudo transcribir el turno de voz.",
        );
        failRealtimeTranscription(draft, error);
        stopRealtimeVoice({ interrupted: true });
        setAssistantError(error.message);
        setVoiceState("error");
        break;
      }
      case "response.created": {
        const response = asRecord(event.response);
        const responseId =
          (typeof response?.id === "string" ? response.id : "") ||
          realtimeEventId(event, "response_id");
        if (!responseId) {
          setVoiceState("error");
          setAssistantError("La sesión de voz creó una respuesta sin identificador.");
          stopRealtimeVoice({ interrupted: true });
          break;
        }
        const existingDraft = realtimeResponseDraftsRef.current.get(responseId);
        if (existingDraft) {
          break;
        }
        const metadata = realtimeResponseMetadata(event);
        const turnId = metadata?.client_turn_id;
        const requestId = metadata?.client_request_id;
        if (
          typeof turnId === "string" &&
          ignoredRealtimeTurnIdsRef.current.has(turnId)
        ) {
          rememberIgnoredRealtimeResponse(responseId);
          break;
        }
        const turnDraft =
          typeof turnId === "string"
            ? realtimeTurnDraftsRef.current.get(turnId)
            : undefined;
        const requestRecord =
          typeof requestId === "string"
            ? realtimeResponseRequestsRef.current.get(requestId)
            : undefined;
        if (
          typeof turnId !== "string" ||
          typeof requestId !== "string" ||
          !turnDraft ||
          !requestRecord ||
          turnDraft !== requestRecord.draft ||
          turnDraft.conversationId !== conversationId ||
          turnDraft.finalized
        ) {
          rememberIgnoredRealtimeResponse(responseId);
          setVoiceState("error");
          setAssistantError(
            "La sesión de voz devolvió una respuesta sin correlación de turno.",
          );
          stopRealtimeVoice({ interrupted: true });
          break;
        }
        turnDraft.responseRequestIds.delete(requestId);
        realtimeResponseRequestsRef.current.delete(requestId);
        realtimeResponseDraftsRef.current.set(responseId, turnDraft);
        ensureRealtimeResponseCycle(
          turnDraft,
          responseId,
        ).confirmationPrompt = requestRecord.confirmationPrompt;
        if (isCurrentRealtimeDraft(turnDraft)) {
          setVoiceState("thinking");
        }
        break;
      }
      case "response.output_audio_transcript.delta":
      case "response.audio_transcript.delta":
      case "response.output_text.delta":
      case "response.text.delta": {
        const responseId = realtimeEventId(event, "response_id");
        if (responseId) {
          const draft = realtimeDraftForResponse(conversationId, responseId);
          if (!draft) {
            break;
          }
          if (
            eventType === "response.output_audio_transcript.delta" ||
            eventType === "response.audio_transcript.delta"
          ) {
            ensureRealtimeResponseCycle(draft, responseId).hasAudioOutput = true;
          }
          if (isCurrentRealtimeDraft(draft)) {
            setVoiceState("responding");
          }
          appendRealtimeAssistantText(
            draft,
            responseId,
            realtimeEventText(event, ["delta", "text"]),
          );
        }
        break;
      }
      case "response.output_audio_transcript.done":
      case "response.audio_transcript.done":
      case "response.output_text.done":
      case "response.text.done": {
        const responseId = realtimeEventId(event, "response_id");
        const transcript = realtimeEventText(event, ["transcript", "text"]);
        if (responseId && transcript) {
          const draft = realtimeDraftForResponse(conversationId, responseId);
          if (draft) {
            if (
              eventType === "response.output_audio_transcript.done" ||
              eventType === "response.audio_transcript.done"
            ) {
              ensureRealtimeResponseCycle(draft, responseId).hasAudioOutput = true;
            }
            setRealtimeAssistantText(draft, responseId, transcript);
          }
        }
        break;
      }
      case "response.output_audio.delta":
      case "response.output_audio.done":
      case "response.audio.delta":
      case "response.audio.done": {
        const responseId = realtimeEventId(event, "response_id");
        if (!responseId) {
          break;
        }
        const draft = realtimeDraftForResponse(conversationId, responseId);
        if (!draft) {
          break;
        }
        ensureRealtimeResponseCycle(draft, responseId).hasAudioOutput = true;
        if (isCurrentRealtimeDraft(draft)) {
          setVoiceState("responding");
        }
        break;
      }
      case "output_audio_buffer.stopped":
      case "output_audio_buffer.cleared": {
        const responseId = realtimeEventId(event, "response_id");
        if (!responseId) {
          break;
        }
        const draft = realtimeDraftForResponse(conversationId, responseId);
        if (!draft) {
          break;
        }
        const cycle = ensureRealtimeResponseCycle(draft, responseId);
        if (eventType === "output_audio_buffer.stopped") {
          cycle.audioPlaybackStopped = true;
        } else {
          cycle.audioPlaybackInterrupted = true;
          draft.interrupted = true;
        }
        scheduleDeliverableRealtimeResponseCycle(draft, cycle);
        break;
      }
      case "response.done": {
        const completed = realtimeResponseDone(event);
        if (!completed) {
          setVoiceState("error");
          setAssistantError("La sesión de voz devolvió una respuesta inválida.");
          break;
        }
        const draft = realtimeDraftForResponse(
          conversationId,
          completed.responseId,
        );
        if (!draft) {
          break;
        }
        const cycle = ensureRealtimeResponseCycle(
          draft,
          completed.responseId,
        );
        cycle.hasAudioOutput =
          cycle.hasAudioOutput || realtimeResponseHasAudioOutput(event);
        cycle.status = completed.status;
        cycle.done = true;
        if (completed.status !== "completed") {
          draft.interrupted = true;
          cycle.audioPlaybackInterrupted = true;
        }
        setRealtimeServerClosure(draft, true);
        scheduleDeliverableRealtimeResponseCycle(draft, cycle);
        break;
      }
      case "error": {
        const requestId = realtimeErrorRequestId(event);
        const requestRecord = requestId
          ? realtimeResponseRequestsRef.current.get(requestId)
          : undefined;
        if (requestRecord) {
          requestRecord.draft.responseRequestIds.delete(requestId);
          realtimeResponseRequestsRef.current.delete(requestId);
          requestRecord.draft.interrupted = true;
          scheduleRealtimeDraftTermination(requestRecord.draft);
        }
        stopRealtimeVoice({ interrupted: true });
        setVoiceState("error");
        setAssistantError("La sesión de voz ha devuelto un error.");
        break;
      }
      default:
        break;
    }
  }

  function cancelRealtimeDraftsForSession(
    session: RealtimeVoiceSession | null,
  ) {
    if (!session) {
      return;
    }
    for (const draft of realtimeTurnDraftsRef.current.values()) {
      if (draft.session === session && !draft.finalized) {
        cancelRealtimeDraft(draft);
      }
    }
  }

  async function startRealtimeVoice() {
    if (
      !selectedConversation ||
      selectedConversation.status !== "active" ||
      realtimeVoiceSessionRef.current ||
      realtimeConnectionAbortRef.current
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
    const generation = realtimeConnectionGenerationRef.current + 1;
    realtimeConnectionGenerationRef.current = generation;
    const connectionAbort = new AbortController();
    realtimeConnectionAbortRef.current = connectionAbort;
    let connectedSession: RealtimeVoiceSession | null = null;

    const isActiveGeneration = () =>
      realtimeConnectionGenerationRef.current === generation &&
      selectedIdRef.current === conversationId &&
      !connectionAbort.signal.aborted;

    const closeActiveSession = (error?: unknown) => {
      if (!isActiveGeneration()) {
        return;
      }
      realtimeConnectionGenerationRef.current += 1;
      realtimeConnectionAbortRef.current = null;
      connectionAbort.abort();
      const session = connectedSession ?? realtimeVoiceSessionRef.current;
      if (realtimeVoiceSessionRef.current === session) {
        realtimeVoiceSessionRef.current = null;
      }
      session?.stop();
      cancelRealtimeDraftsForSession(session);
      setRealtimeVoiceActive(false);
      setVoiceState("error");
      if (error) {
        handleRequestError(
          error,
          setAssistantError,
          "La voz en tiempo real ha fallado.",
        );
      } else {
        setAssistantError("La sesión de voz se ha cerrado inesperadamente.");
      }
    };

    try {
      const session = await createAssistantRealtimeSession(
        conversationId,
        getStoredToken(),
        connectionAbort.signal,
      );
      connectionAbort.signal.throwIfAborted();
      connectedSession = await RealtimeVoiceSession.start(
        session,
        {
          onOpen: () => {
            if (
              isActiveGeneration() &&
              realtimeVoiceSessionRef.current === connectedSession
            ) {
              setRealtimeVoiceActive(true);
              setVoiceState("listening");
            }
          },
          onClose: () => closeActiveSession(),
          onEvent: (event) => {
            if (
              isActiveGeneration() &&
              realtimeVoiceSessionRef.current === connectedSession
            ) {
              handleRealtimeServerEvent(conversationId, event);
            }
          },
          onError: (error) => closeActiveSession(error),
        },
        connectionAbort.signal,
      );
      if (!isActiveGeneration() || connectedSession.isClosed) {
        connectedSession.stop();
        return;
      }
      realtimeVoiceSessionRef.current = connectedSession;
      realtimeConnectionAbortRef.current = null;
      setRealtimeVoiceActive(true);
      setVoiceState("listening");
    } catch (requestError) {
      connectedSession?.stop();
      if (realtimeConnectionGenerationRef.current !== generation) {
        return;
      }
      realtimeConnectionAbortRef.current = null;
      if (isAbortError(requestError) || connectionAbort.signal.aborted) {
        setRealtimeVoiceActive(false);
        setVoiceState("idle");
        return;
      }
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
    realtimeConnectionGenerationRef.current += 1;
    const connectionAbort = realtimeConnectionAbortRef.current;
    realtimeConnectionAbortRef.current = null;
    connectionAbort?.abort();
    const session = realtimeVoiceSessionRef.current;
    realtimeVoiceSessionRef.current = null;
    session?.stop();
    cancelRealtimeDraftsForSession(session);
    setRealtimeVoiceActive(false);
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
    isSendingMessage: isSendingMessage || realtimeServerClosurePending,
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
