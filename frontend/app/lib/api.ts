import type {
  AssistantRealtimeSession,
  AssistantRealtimeToolCallRequest,
  AssistantRealtimeToolCallResult,
  AssistantRealtimeTurnCompleteRequest,
  AssistantRealtimeTurnResult,
  AssistantRealtimeTurnStartRequest,
  AssistantRealtimeTurnStartResult,
  AssistantStreamDone,
  AssistantStreamMessageStart,
  AssistantStreamTranscriptFinal,
  AssistantStreamToolActivity,
  AssistantStreamVoiceState,
  TownHall,
  TownHallBlock,
  TownHallBlockCreate,
  TownHallBlockPlacement,
  TownHallBlockUpdate,
  TownHallProfile,
  TownHallContent,
  TownHallProfileUpdate,
  TownHallWeather,
  User,
} from "../components/types";

export class ApiRequestError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

const CONFIGURED_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

function isLoopbackHostname(hostname: string) {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "::1" ||
    hostname === "[::1]"
  );
}

function isLoopbackApiBaseUrl(value: string) {
  try {
    return isLoopbackHostname(new URL(value).hostname);
  } catch {
    return false;
  }
}

function normalizeApiBaseUrl(value: string) {
  const trimmed = value.trim().replace(/\/$/, "");
  if (!trimmed) {
    return "";
  }

  // NEXT_PUBLIC_API_BASE_URL is baked into the Next.js bundle at build time.
  // Local builds commonly set it to localhost, but a browser opening a public
  // tunnel would then call its *own* localhost and fail with "Failed to fetch".
  // In a loopback browser, keep the hostname aligned with the page so the
  // httpOnly session cookie is stored and sent to the same site.
  // In a public-origin case, fall back to same-origin relative API routes.
  if (typeof window !== "undefined" && isLoopbackApiBaseUrl(trimmed)) {
    if (!isLoopbackHostname(window.location.hostname)) {
      return "";
    }

    const apiUrl = new URL(trimmed);
    apiUrl.hostname = window.location.hostname;
    return apiUrl.toString().replace(/\/$/, "");
  }

  return trimmed;
}

export const API_BASE_URL = normalizeApiBaseUrl(CONFIGURED_API_BASE_URL);

function translateProviderError(detail: string) {
  const normalized = detail.toLowerCase();

  if (
    normalized.includes("http 429") ||
    normalized.includes("usage limit") ||
    normalized.includes("rate limit") ||
    normalized.includes("too many requests") ||
    normalized.includes("quota") ||
    normalized.includes("insufficient_quota")
  ) {
    return "No se ha podido obtener respuesta porque se ha alcanzado el límite de uso del proveedor. Inténtalo de nuevo más tarde.";
  }

  if (
    normalized.includes("api call failed") ||
    (normalized.includes("after ") && normalized.includes(" retries")) ||
    normalized.includes("runtime error") ||
    normalized.includes("provider error")
  ) {
    return "El asistente no ha podido obtener respuesta del proveedor. Inténtalo de nuevo más tarde.";
  }

  return null;
}

function translateApiDetail(detail: string, fallback: string) {
  if (
    detail.startsWith("Permission required:") ||
    detail.startsWith("Permissions required:")
  ) {
    return "No tienes el permiso necesario para esta acción.";
  }

  const providerError = translateProviderError(detail);
  if (providerError) {
    console.warn("Assistant provider error hidden from user:", detail);
    return providerError;
  }

  switch (detail) {
    case "Incorrect email or password":
      return "No se pudo iniciar sesión. Revisa el email y la contraseña.";
    case "Current password is incorrect":
      return "La contraseña actual no es correcta.";
    case "Too many login attempts":
      return "Demasiados intentos de inicio de sesión. Espera un minuto e inténtalo de nuevo.";
    case "Too many password attempts":
      return "Demasiados intentos de contraseña. Espera un minuto e inténtalo de nuevo.";
    case "Only superusers can reset a superuser password":
      return "Solo un superusuario puede restablecer la contraseña de un superusuario.";
    case "Inactive user":
      return "El usuario está inactivo.";
    case "Could not validate credentials":
      return "La sesión ha caducado o no es válida. Inicia sesión de nuevo.";
    case "Superuser privileges required":
      return "No tienes permisos de administración.";
    case "User already exists":
      return "Ya existe un usuario con ese email.";
    case "Group already exists":
      return "Ya existe un grupo con ese nombre.";
    case "Role already exists":
      return "Ya existe un rol con ese nombre.";
    case "User not found":
      return "No se encontró el usuario indicado.";
    case "Group not found":
      return "No se encontró el grupo indicado.";
    case "Role not found":
      return "No se encontró el rol indicado.";
    case "Permission not found":
      return "No se encontró el permiso indicado.";
    case "Project not found":
      return "No se encontró el proyecto indicado.";
    case "Project access denied":
      return "No tienes acceso a ese proyecto.";
    case "Document not found":
      return "No se encontró el documento indicado.";
    case "Document file not found":
      return "No se encontró el archivo del documento.";
    case "Unsupported document content type":
      return "El tipo de archivo no está permitido.";
    case "Document exceeds maximum upload size":
      return "El archivo supera el tamaño máximo permitido.";
    case "Empty document upload":
      return "El archivo está vacío.";
    case "Invalid document storage key":
      return "No se pudo guardar el documento.";
    case "Document project organization mismatch":
      return "El documento no coincide con la organización del proyecto.";
    case "Requirement not found":
      return "No se encontró la necesidad indicada.";
    case "Requirement message not found":
      return "No se encontró el mensaje de la necesidad.";
    case "Requirement access denied":
      return "No tienes acceso a esa necesidad.";
    case "Requirement status does not allow content edits":
      return "El estado de la necesidad no permite editar su contenido.";
    case "Project does not belong to the requirement organization":
      return "El proyecto no pertenece a la organización de la necesidad.";
    case "Attachment not found":
      return "No se encontró el adjunto.";
    case "Unsupported attachment content type":
      return "Ese tipo de archivo no está permitido: PDF, imagen o audio.";
    case "Attachment exceeds maximum upload size":
      return "El archivo supera el tamaño máximo permitido.";
    case "Empty attachment upload":
      return "El archivo está vacío.";
    case "Invalid attachment storage key":
      return "No se pudo guardar el adjunto.";
    case "Too many attachments":
      return "Este elemento ya tiene demasiados adjuntos.";
    case "Only content items carry a series":
      return "Solo los elementos pueden tener datos de serie.";
    case "Only content items carry fields":
      return "Solo los elementos pueden tener campos.";
    case "Only sections carry a layout":
      return "Solo los apartados tienen formato.";
    case "Only content items carry a body":
      return "Solo los elementos pueden tener texto.";
    case "Block not found":
      return "No se encontró el apartado del menú.";
    case "A navigation section cannot have a parent":
      return "Un apartado principal no puede colgar de otro apartado.";
    case "A navigation item requires a parent section":
      return "Un elemento del menú tiene que colgar de un apartado.";
    case "Parent must be an active navigation section of the same organization":
      return "El apartado de destino no es válido.";
    case "Duplicate block in reorder payload":
      return "No se pudo reordenar el menú: hay elementos repetidos.";
    case "Weather block is disabled":
      return "El bloque de temperatura está desactivado.";
    case "Weather provider unavailable":
      return "No se pudo consultar la temperatura del municipio.";
    case "Shield not found":
      return "Este municipio todavía no tiene escudo.";
    case "Unsupported shield content type":
      return "El escudo tiene que ser una imagen PNG, JPG, SVG o WebP.";
    case "Shield exceeds maximum upload size":
      return "La imagen del escudo supera el tamaño máximo permitido.";
    case "Empty shield upload":
      return "La imagen del escudo está vacía.";
    case "Invalid shield storage key":
      return "No se pudo guardar el escudo.";
    case "User has no organization":
      return "Tu usuario no pertenece a ninguna organización.";
    case "organization_id is required":
      return "Indica la organización para ver su Ayuntamiento.";
    case "Organization not found":
      return "No se encontró la organización indicada.";
    case "Organization access denied":
      return "No tienes acceso a esa organización.";
    case "Organization is not available for asset inventory reads":
      return "La organización ya no está disponible para consultar su inventario.";
    case "Organization must be active to modify asset inventory":
      return "La organización debe estar activa para modificar su inventario.";
    case "Organization must have a municipality to modify asset inventory":
      return "La organización necesita un municipio asociado para modificar su inventario.";
    case "Organization municipality must be active to modify asset inventory":
      return "El municipio asociado debe estar activo para modificar el inventario.";
    case "Entity access denied":
      return "No tienes acceso al elemento indicado.";
    case "Asset changed while assigning its location":
      return "El activo cambió mientras se guardaba su ubicación. Vuelve a buscarlo e inténtalo de nuevo.";
    case "Asset municipality does not match organization municipality":
      return "El activo no pertenece al municipio actual de la organización.";
    case "Location organization does not match asset organization":
      return "La ubicación no pertenece a la misma organización que el activo.";
    case "Location municipality does not match asset municipality":
      return "La ubicación no pertenece al mismo municipio que el activo.";
    case "Asset location update conflicts with existing data":
      return "No se pudo guardar la ubicación porque el activo cambió. Actualiza el inventario e inténtalo de nuevo.";
    case "Asset locations only support the primary role":
      return "Los activos solo admiten una ubicación principal.";
    case "Municipality not found":
      return "No se encontró el municipio indicado.";
    case "Municipality already exists":
      return "Ya existe un municipio con ese código INE.";
    case "Municipality is archived":
      return "No se puede usar un municipio archivado.";
    case "Ordinance not found":
      return "No se encontró la ordenanza indicada.";
    case "Ordinance semantic search is unavailable":
      return "La búsqueda semántica de ordenanzas no está disponible ahora mismo. Inténtalo de nuevo más tarde.";
    case "Ordinance search query is too short":
      return "Escribe al menos dos caracteres para buscar ordenanzas.";
    case "population_gte must be lower than population_lt":
      return "La población mínima debe ser menor que el límite superior.";
    case "Comparison needs between 1 and 20 municipalities":
      return "Selecciona entre uno y veinte municipios para comparar.";
    case "Changed ordinance content requires a separate review":
      return "El contenido jurídico ha cambiado. Guárdalo primero y apruébalo después en una revisión separada.";
    case "Imported ordinance review must use the import item endpoint":
      return "Las ordenanzas importadas deben revisarse desde su elemento de importación.";
    case "Import item is not pending review":
      return "Este elemento de importación ya no está pendiente de revisión.";
    case "Import item does not own this ordinance review":
      return "El elemento de importación no puede revisar esa ordenanza.";
    case "El texto de la ordenanza supera el máximo de fragmentos buscables.":
      return "El texto es demasiado extenso para indexarlo de forma segura. Divídelo o revisa el límite configurado.";
    case "Required ordinance fields cannot be null":
      return "Los campos obligatorios de la ordenanza no pueden estar vacíos.";
    case "Official legal source not found":
      return "No se encontró la fuente oficial.";
    case "Invalid official legal source definition":
      return "La fuente oficial no tiene una URL y un dominio seguros y coherentes.";
    case "Import job needs seed URLs or search query with municipalities":
      return "La importación necesita URLs semilla o una búsqueda con municipios.";
    case "Import job needs active official sources":
      return "La importación necesita fuentes oficiales activas.";
    case "Import source URL is not official":
      return "La URL no pertenece a una fuente oficial permitida.";
    case "Ordinance import job not found":
      return "No se encontró la importación.";
    case "Ordinance import item not found":
      return "No se encontró el elemento importado.";
    case "Import queue is unavailable":
      return "La cola de importación no está disponible.";
    case "Import item has no ordinance":
      return "El elemento importado no tiene ordenanza asociada.";
    case "Telegram is not enabled":
      return "Telegram no está habilitado en este servidor.";
    case "Invalid Telegram webhook secret":
      return "El secreto del webhook de Telegram no es válido.";
    case "User does not belong to the group organization":
      return "El usuario no pertenece a la organización del grupo.";
    case "User does not belong to the project organization":
      return "El usuario no pertenece a la organización del proyecto.";
    case "Group does not belong to the project organization":
      return "El grupo no pertenece a la organización del proyecto.";
    case "Cannot remove yourself from an organization":
      return "No puedes quitarte a ti mismo de una organización.";
    case "Cannot remove the last active superuser from all organizations":
      return "No puedes quitar al último superusuario activo de todas las organizaciones.";
    case "Cannot move group because a group user is outside the target organization":
      return "No se puede mover el grupo porque tiene usuarios fuera de la organización destino.";
    case "Cannot move group because it is assigned to a project outside the target organization":
      return "No se puede mover el grupo porque está asignado a proyectos de otra organización.";
    case "Cannot delete your own account":
      return "No puedes eliminar tu propia cuenta.";
    case "Cannot delete the last active superuser":
      return "No puedes eliminar el último superusuario activo.";
    case "Cannot demote the last active superuser":
      return "No puedes desactivar ni degradar al último superusuario activo.";
    case "Only superusers can change superuser status":
      return "Solo un superusuario puede conceder o retirar el estado de superusuario.";
    case "Assistant is not configured":
      return "El asistente no está configurado en este servidor.";
    case "Assistant request failed":
      return "El asistente no ha podido procesar la petición. Inténtalo de nuevo.";
    case "Audio transcription is not available":
      return "La transcripción de voz no está disponible ahora mismo.";
    case "Audio transcription returned no text":
      return "No he detectado texto en el audio. Prueba con una nota un poco más clara.";
    case "Speech synthesis is not available":
      return "La voz del asistente no está disponible ahora mismo.";
    case "Speech text is too long":
      return "La respuesta es demasiado larga para leerla en voz alta.";
    case "Conversation is archived":
      return "La conversación está archivada.";
    case "Conversation not found":
      return "No se encontró la conversación.";
    case "Assistant conversation folder already exists":
      return "Ya existe una carpeta con ese nombre.";
    case "Conversation folder not found":
      return "No se encontró la carpeta.";
    case "Assistant memory entry not found":
      return "No se encontró la entrada de memoria.";
    case "Assistant admin feedback not found":
      return "No se encontró el feedback de producto.";
    case "Assistant memory entry was modified by another reviewer":
      return "Otra persona ha modificado esta entrada. Actualiza la bandeja y revisa la versión nueva antes de continuar.";
    case "Assistant admin feedback was modified by another reviewer":
      return "Otra persona ha modificado este feedback. Actualiza la bandeja y revisa la versión nueva antes de continuar.";
    case "Invalid assistant memory status transition":
      return "Ese cambio de estado de memoria ya no está permitido. Actualiza la bandeja.";
    case "Sensitive assistant memory approval requires explicit confirmation":
      return "Confirma expresamente la revisión del contenido sensible antes de aprobar esta memoria.";
    case "Invalid assistant admin feedback status transition":
      return "Ese cambio de estado del feedback ya no está permitido. Actualiza la bandeja.";
    case "Transversal feature not found":
      return "No se encontró la funcionalidad transversal.";
    case "Transversal feature adoption not found":
      return "No se encontró la activación transversal.";
    case "Transversal feature is not available":
      return "La funcionalidad transversal no está disponible.";
    default:
      return detail || fallback;
  }
}

export async function readApiError(response: Response, fallback: string) {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string") {
      return translateApiDetail(data.detail, fallback);
    }
  } catch {
    // Use the fallback message when the API does not return JSON.
  }

  return fallback;
}

export async function fetchCurrentUser(accessToken: string) {
  const headers = new Headers();
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }

  const response = await fetch(`${API_BASE_URL}/auth/me`, {
    credentials: "include",
    headers,
  });

  if (!response.ok) {
    throw new ApiRequestError(
      await readApiError(
        response,
        "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
      ),
      response.status,
    );
  }

  return (await response.json()) as User;
}

async function performAdminRequest(
  path: string,
  accessToken: string,
  fallbackError: string,
  options: RequestInit = {},
) {
  const headers = new Headers(options.headers);
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }

  const isFormData =
    typeof FormData !== "undefined" && options.body instanceof FormData;
  if (options.body && !headers.has("Content-Type") && !isFormData) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    credentials: "include",
    headers,
  });

  if (!response.ok) {
    throw new ApiRequestError(
      await readApiError(response, fallbackError),
      response.status,
    );
  }

  return response;
}

export async function adminRequest<T>(
  path: string,
  accessToken: string,
  fallbackError: string,
  options: RequestInit = {},
) {
  const response = await performAdminRequest(
    path,
    accessToken,
    fallbackError,
    options,
  );

  return (await response.json()) as T;
}

export async function adminRequestWithTotal<T>(
  path: string,
  accessToken: string,
  fallbackError: string,
  options: RequestInit = {},
) {
  const response = await performAdminRequest(
    path,
    accessToken,
    fallbackError,
    options,
  );

  const items = (await response.json()) as T;
  const totalHeader = response.headers.get("X-Total-Count");
  const parsedTotal =
    totalHeader === null ? Number.NaN : Number.parseInt(totalHeader, 10);
  const total = Number.isFinite(parsedTotal)
    ? parsedTotal
    : Array.isArray(items)
      ? items.length
      : 0;

  return { items, total };
}

type AssistantStreamHandlers = {
  onVoiceState?: (event: AssistantStreamVoiceState) => void;
  onTranscriptFinal?: (event: AssistantStreamTranscriptFinal) => void;
  onMessageStart?: (event: AssistantStreamMessageStart) => void;
  onTextDelta?: (text: string) => void;
  onTextReset?: (text: string) => void;
  onToolActivity?: (event: AssistantStreamToolActivity) => void;
  onDone?: (event: AssistantStreamDone) => void;
};

type AssistantStreamTerminalEvent =
  | { type: "done"; event: AssistantStreamDone }
  | { type: "error"; error: ApiRequestError };

const ASSISTANT_STREAM_INACTIVITY_TIMEOUT_MS = 90_000;
const ASSISTANT_STREAM_TIMEOUT_MESSAGE =
  "El asistente ha tardado demasiado en responder. Inténtalo de nuevo.";

async function consumeAssistantStream(
  request: (signal: AbortSignal) => Promise<Response>,
  handlers: AssistantStreamHandlers,
  signal?: AbortSignal,
) {
  const streamController = new AbortController();
  let inactivityTimeout: ReturnType<typeof setTimeout> | null = null;
  let timedOut = false;

  const abortFromParent = () => streamController.abort(signal?.reason);
  const resetInactivityWatchdog = () => {
    if (inactivityTimeout !== null) {
      clearTimeout(inactivityTimeout);
    }
    inactivityTimeout = setTimeout(() => {
      timedOut = true;
      streamController.abort(
        new DOMException(ASSISTANT_STREAM_TIMEOUT_MESSAGE, "TimeoutError"),
      );
    }, ASSISTANT_STREAM_INACTIVITY_TIMEOUT_MS);
  };

  if (signal?.aborted) {
    abortFromParent();
  } else {
    signal?.addEventListener("abort", abortFromParent, { once: true });
  }
  resetInactivityWatchdog();

  try {
    const response = await request(streamController.signal);
    if (!response.body) {
      throw new ApiRequestError("El asistente no ha podido responder.", 0);
    }
    resetInactivityWatchdog();
    await readAssistantStream(response, handlers, resetInactivityWatchdog);
  } catch (requestError) {
    if (timedOut && !signal?.aborted) {
      throw new ApiRequestError(ASSISTANT_STREAM_TIMEOUT_MESSAGE, 408);
    }
    throw requestError;
  } finally {
    if (inactivityTimeout !== null) {
      clearTimeout(inactivityTimeout);
    }
    signal?.removeEventListener("abort", abortFromParent);
    if (!streamController.signal.aborted) {
      streamController.abort();
    }
  }
}

export async function streamAssistantMessage(
  conversationId: number,
  content: string,
  accessToken: string,
  handlers: AssistantStreamHandlers,
  inputMode: "text" | "voice" = "text",
  signal?: AbortSignal,
) {
  await consumeAssistantStream(
    (streamSignal) =>
      performAdminRequest(
        `/assistant/conversations/${conversationId}/messages/stream`,
        accessToken,
        "El asistente no ha podido responder.",
        {
          method: "POST",
          body: JSON.stringify({ content, input_mode: inputMode }),
          signal: streamSignal,
        },
      ),
    handlers,
    signal,
  );
}

export async function streamAssistantVoiceTurn(
  conversationId: number,
  audio: Blob,
  accessToken: string,
  handlers: AssistantStreamHandlers,
  signal?: AbortSignal,
) {
  const formData = new FormData();
  formData.append("file", audio, "anacleto-audio.webm");
  await consumeAssistantStream(
    (streamSignal) =>
      performAdminRequest(
        `/assistant/conversations/${conversationId}/voice-turns/stream`,
        accessToken,
        "El asistente no ha podido responder.",
        { method: "POST", body: formData, signal: streamSignal },
      ),
    handlers,
    signal,
  );
}

export async function createAssistantRealtimeSession(
  conversationId: number,
  accessToken: string,
  signal?: AbortSignal,
) {
  return adminRequest<AssistantRealtimeSession>(
    `/assistant/conversations/${conversationId}/realtime/session`,
    accessToken,
    "No se pudo iniciar la voz en tiempo real.",
    { method: "POST", body: JSON.stringify({}), signal },
  );
}

export async function sendAssistantRealtimeToolCall(
  conversationId: number,
  turnId: string,
  payload: AssistantRealtimeToolCallRequest,
  accessToken: string,
  signal?: AbortSignal,
) {
  return adminRequest<AssistantRealtimeToolCallResult>(
    `/assistant/conversations/${conversationId}/realtime/turns/${turnId}/tool-calls`,
    accessToken,
    "No se pudo ejecutar la herramienta de voz.",
    { method: "POST", body: JSON.stringify(payload), signal },
  );
}

export async function startAssistantRealtimeTurn(
  conversationId: number,
  payload: AssistantRealtimeTurnStartRequest,
  accessToken: string,
  signal?: AbortSignal,
) {
  return adminRequest<AssistantRealtimeTurnStartResult>(
    `/assistant/conversations/${conversationId}/realtime/turns/start`,
    accessToken,
    "No se pudo iniciar el turno de voz.",
    { method: "POST", body: JSON.stringify(payload), signal },
  );
}

export async function completeAssistantRealtimeTurn(
  conversationId: number,
  turnId: string,
  payload: AssistantRealtimeTurnCompleteRequest,
  accessToken: string,
  signal?: AbortSignal,
) {
  return adminRequest<AssistantRealtimeTurnResult>(
    `/assistant/conversations/${conversationId}/realtime/turns/${turnId}/complete`,
    accessToken,
    "No se pudo guardar el turno de voz.",
    { method: "POST", body: JSON.stringify(payload), signal },
  );
}

async function readAssistantStream(
  response: Response,
  handlers: AssistantStreamHandlers,
  onActivity: () => void,
) {
  if (!response.body) {
    throw new ApiRequestError("El asistente no ha podido responder.", 0);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminalEvent: AssistantStreamTerminalEvent | null = null;

  const dispatchFrame = (frame: string) => {
    const nextTerminalEvent = dispatchAssistantStreamFrame(
      frame,
      terminalEvent ? {} : handlers,
    );
    if (!nextTerminalEvent) {
      return null;
    }
    if (terminalEvent) {
      throw new ApiRequestError(
        "El asistente ha enviado una respuesta no válida. Inténtalo de nuevo.",
        0,
      );
    }
    return nextTerminalEvent;
  };

  try {
    while (!terminalEvent) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      onActivity();
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        terminalEvent = dispatchFrame(frame) ?? terminalEvent;
      }
    }

    if (!terminalEvent) {
      buffer += decoder.decode();
      if (buffer.trim()) {
        terminalEvent = dispatchFrame(buffer) ?? terminalEvent;
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }

  if (!terminalEvent) {
    throw new ApiRequestError(
      "La respuesta del asistente se ha interrumpido antes de terminar. Inténtalo de nuevo.",
      0,
    );
  }

  if (terminalEvent.type === "error") {
    throw terminalEvent.error;
  }

  handlers.onDone?.(terminalEvent.event);
}

export async function synthesizeAssistantSpeech(
  text: string,
  accessToken: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await performAdminRequest(
    "/assistant/speech",
    accessToken,
    "No se pudo generar la voz del asistente.",
    { method: "POST", body: JSON.stringify({ text }), signal },
  );

  return response.blob();
}

function dispatchAssistantStreamFrame(
  frame: string,
  handlers: AssistantStreamHandlers,
) {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  const rawData = dataLines.join("\n");
  const data = rawData ? (JSON.parse(rawData) as Record<string, unknown>) : {};
  switch (eventName) {
    case "voice_state":
      if (typeof data.state === "string") {
        handlers.onVoiceState?.(data as AssistantStreamVoiceState);
      }
      break;
    case "transcript_final":
      if (typeof data.text === "string") {
        handlers.onTranscriptFinal?.(data as AssistantStreamTranscriptFinal);
      }
      break;
    case "message_start":
      handlers.onMessageStart?.(data as AssistantStreamMessageStart);
      break;
    case "text_delta":
      if (typeof data.text === "string") {
        handlers.onTextDelta?.(data.text);
      }
      break;
    case "text_reset":
      if (typeof data.text === "string") {
        handlers.onTextReset?.(data.text);
      }
      break;
    case "tool_activity":
      handlers.onToolActivity?.(data as AssistantStreamToolActivity);
      break;
    case "done":
      return { type: "done" as const, event: data as AssistantStreamDone };
    case "error":
      return {
        type: "error" as const,
        error: new ApiRequestError(
          translateApiDetail(
            typeof data.detail === "string" ? data.detail : "",
            "El asistente no ha podido responder.",
          ),
          500,
        ),
      };
    default:
      break;
  }

  return null;
}

export function getErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

export function isAuthError(error: unknown) {
  return error instanceof ApiRequestError && error.status === 401;
}

// Ayuntamiento: perfil del municipio y árbol de navegación configurable. La
// organización viaja opcionalmente; sin ella el backend usa la primera del
// usuario, la misma convención que aplica el menú lateral.
function townHallPath(path: string, organizationId?: number) {
  return organizationId === undefined
    ? path
    : `${path}${path.includes("?") ? "&" : "?"}organization_id=${organizationId}`;
}

export function fetchTownHall(organizationId?: number) {
  return adminRequest<TownHall>(
    townHallPath("/town-hall", organizationId),
    "",
    "No se pudo cargar el Ayuntamiento.",
  );
}

// El bloque de temperatura se pide aparte de la pantalla para que una caída
// del proveedor no impida cargar el Ayuntamiento.
export function fetchTownHallWeather(organizationId?: number) {
  return adminRequest<TownHallWeather>(
    townHallPath("/town-hall/weather", organizationId),
    "",
    "No se pudo consultar la temperatura.",
  );
}

export function updateTownHallProfile(
  changes: TownHallProfileUpdate,
  organizationId?: number,
) {
  return adminRequest<TownHallProfile>(
    townHallPath("/town-hall/profile", organizationId),
    "",
    "No se pudo guardar la configuración del Ayuntamiento.",
    { method: "PATCH", body: JSON.stringify(changes) },
  );
}

export function createTownHallBlock(
  block: TownHallBlockCreate,
  organizationId?: number,
) {
  return adminRequest<TownHallBlock>(
    townHallPath("/town-hall/blocks", organizationId),
    "",
    "No se pudo crear el apartado del menú.",
    { method: "POST", body: JSON.stringify(block) },
  );
}

export function updateTownHallBlock(
  blockId: number,
  changes: TownHallBlockUpdate,
) {
  return adminRequest<TownHallBlock>(
    `/town-hall/blocks/${blockId}`,
    "",
    "No se pudo actualizar el apartado del menú.",
    { method: "PATCH", body: JSON.stringify(changes) },
  );
}

export function reorderTownHallBlocks(
  placements: TownHallBlockPlacement[],
  organizationId?: number,
) {
  return adminRequest<TownHallBlock[]>(
    townHallPath("/town-hall/blocks/reorder", organizationId),
    "",
    "No se pudo reordenar el menú.",
    { method: "POST", body: JSON.stringify({ placements }) },
  );
}

export function fetchTownHallContent(blockId: number) {
  return adminRequest<TownHallContent>(
    `/town-hall/blocks/${blockId}/content`,
    "",
    "No se pudo cargar el contenido del apartado.",
  );
}

export function uploadTownHallAttachment(blockId: number, file: File) {
  const body = new FormData();
  body.append("file", file);

  return adminRequest<TownHallContent>(
    `/town-hall/blocks/${blockId}/attachments`,
    "",
    "No se pudo subir el adjunto.",
    { method: "POST", body },
  );
}

export function deleteTownHallAttachment(blockId: number, index: number) {
  return adminRequest<TownHallContent>(
    `/town-hall/blocks/${blockId}/attachments/${index}`,
    "",
    "No se pudo eliminar el adjunto.",
    { method: "DELETE" },
  );
}

// Descarga directa: la cookie de sesión viaja sola porque backend y frontend
// comparten host (ADR-010).
export function townHallAttachmentUrl(blockId: number, index: number) {
  return `${API_BASE_URL}/town-hall/blocks/${blockId}/attachments/${index}`;
}

export function uploadTownHallShield(file: File, organizationId?: number) {
  const body = new FormData();
  body.append("file", file);

  return adminRequest<TownHallProfile>(
    townHallPath("/town-hall/shield", organizationId),
    "",
    "No se pudo subir el escudo.",
    { method: "POST", body },
  );
}

// El escudo se pinta con <img>: la cookie de sesión viaja sola porque backend
// y frontend comparten host (ADR-010). `version` fuerza a saltarse la caché
// tras sustituirlo, ya que la URL es siempre la misma.
export function townHallShieldUrl(version: number, organizationId?: number) {
  const path = townHallPath("/town-hall/shield", organizationId);
  return `${API_BASE_URL}${path}${path.includes("?") ? "&" : "?"}v=${version}`;
}
