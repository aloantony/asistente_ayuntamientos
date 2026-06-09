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

  if (options.body && !headers.has("Content-Type")) {
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
