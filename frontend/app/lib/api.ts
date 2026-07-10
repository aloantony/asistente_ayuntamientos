import type {
  AssistantStreamDone,
  AssistantStreamMessageStart,
  AssistantStreamToolActivity,
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
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
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
  // In that public-origin case, fall back to same-origin relative API routes.
  if (typeof window !== "undefined" && isLoopbackApiBaseUrl(trimmed)) {
    return isLoopbackHostname(window.location.hostname) ? trimmed : "";
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
  if (detail.startsWith("Permission required:")) {
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
    case "Only superusers can create global user accounts":
      return "Solo un superusuario puede crear cuentas de usuario globales.";
    case "Only superusers can update global user accounts":
      return "Solo un superusuario puede modificar los datos globales de una cuenta.";
    case "Only superusers can delete user accounts":
      return "Solo un superusuario puede eliminar cuentas de usuario.";
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
    case "Organization not found":
      return "No se encontró la organización indicada.";
    case "Organization access denied":
      return "No tienes acceso a esa organización.";
    case "Municipality not found":
      return "No se encontró el municipio indicado.";
    case "Municipality already exists":
      return "Ya existe un municipio con ese código INE.";
    case "Municipality is archived":
      return "No se puede usar un municipio archivado.";
    case "Ordinance not found":
      return "No se encontró la ordenanza indicada.";
    case "Required ordinance fields cannot be null":
      return "Los campos obligatorios de la ordenanza no pueden estar vacíos.";
    case "Official legal source not found":
      return "No se encontró la fuente oficial.";
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
  onMessageStart?: (event: AssistantStreamMessageStart) => void;
  onTextDelta?: (text: string) => void;
  onToolActivity?: (event: AssistantStreamToolActivity) => void;
  onDone?: (event: AssistantStreamDone) => void;
};

export async function streamAssistantMessage(
  conversationId: number,
  content: string,
  accessToken: string,
  handlers: AssistantStreamHandlers,
  inputMode: "text" | "voice" = "text",
) {
  const response = await performAdminRequest(
    `/assistant/conversations/${conversationId}/messages/stream`,
    accessToken,
    "El asistente no ha podido responder.",
    { method: "POST", body: JSON.stringify({ content, input_mode: inputMode }) },
  );

  if (!response.body) {
    throw new ApiRequestError("El asistente no ha podido responder.", 0);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      dispatchAssistantStreamFrame(frame, handlers);
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) {
    dispatchAssistantStreamFrame(buffer, handlers);
  }
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
    case "message_start":
      handlers.onMessageStart?.(data as AssistantStreamMessageStart);
      break;
    case "text_delta":
      if (typeof data.text === "string") {
        handlers.onTextDelta?.(data.text);
      }
      break;
    case "tool_activity":
      handlers.onToolActivity?.(data as AssistantStreamToolActivity);
      break;
    case "done":
      handlers.onDone?.(data as AssistantStreamDone);
      break;
    case "error":
      throw new ApiRequestError(
        translateApiDetail(
          typeof data.detail === "string" ? data.detail : "",
          "El asistente no ha podido responder.",
        ),
        500,
      );
    default:
      break;
  }
}

export function getErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

export function isAuthError(error: unknown) {
  return error instanceof ApiRequestError && error.status === 401;
}
