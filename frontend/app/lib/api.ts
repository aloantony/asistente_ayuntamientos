import type { User } from "../components/types";

export class ApiRequestError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function translateApiDetail(detail: string, fallback: string) {
  if (detail.startsWith("Permission required:")) {
    return "No tienes el permiso necesario para esta acción.";
  }

  switch (detail) {
    case "Incorrect email or password":
      return "No se pudo iniciar sesión. Revisa el email y la contraseña.";
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
      return "No se encontró el requisito indicado.";
    case "Requirement message not found":
      return "No se encontró el mensaje del requisito.";
    case "Requirement access denied":
      return "No tienes acceso a ese requisito.";
    case "Requirement status does not allow content edits":
      return "El estado del requisito no permite editar su contenido.";
    case "Project does not belong to the requirement organization":
      return "El proyecto no pertenece a la organización del requisito.";
    case "Organization not found":
      return "No se encontró la organización indicada.";
    case "Organization access denied":
      return "No tienes acceso a esa organización.";
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
  const response = await fetch(`${API_BASE_URL}/auth/me`, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
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

export async function adminRequest<T>(
  path: string,
  accessToken: string,
  fallbackError: string,
  options: RequestInit = {},
) {
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${accessToken}`);

  const isFormData =
    typeof FormData !== "undefined" && options.body instanceof FormData;
  if (options.body && !headers.has("Content-Type") && !isFormData) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    throw new ApiRequestError(
      await readApiError(response, fallbackError),
      response.status,
    );
  }

  return (await response.json()) as T;
}

export function getErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

export function isAuthError(error: unknown) {
  return error instanceof ApiRequestError && error.status === 401;
}
