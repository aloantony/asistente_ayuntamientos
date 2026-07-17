"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import type { User } from "../components/types";
import { userHasPermission } from "../components/types";
import {
  API_BASE_URL,
  fetchCurrentUser,
  getErrorMessage,
  isAuthError,
} from "./api";
import {
  ADMIN_PANEL_PERMISSIONS,
  canViewOrdinanceLibrary,
  canUseMemoryReview,
  canUseProductReview,
  PROJECT_PERMISSIONS,
} from "./permissions";
import { runNavigationGuards } from "./navigationGuards";

export const REQUIREMENT_PERMISSIONS = [
  "requirements.view",
  "requirements.create",
  "requirements.edit",
  "requirements.review",
  "requirements.archive",
  "requirements.manage",
];

export function hasAnyPermission(user: User, permissionCodes: string[]) {
  return permissionCodes.some((permissionCode) =>
    userHasPermission(user, permissionCode),
  );
}

export function shouldShowAdminPanel(user: User) {
  return (
    hasAnyPermission(user, ADMIN_PANEL_PERMISSIONS) ||
    canUseProductReview(user) ||
    canUseMemoryReview(user)
  );
}

export function shouldShowRequirementsPanel(user: User) {
  return hasAnyPermission(user, REQUIREMENT_PERMISSIONS);
}

export function shouldShowProjectsPanel(user: User) {
  return hasAnyPermission(user, PROJECT_PERMISSIONS);
}

// Sección de aterrizaje según los permisos del usuario; la usan el login
// (para llegar en un solo salto) y la página raíz "/" como respaldo.
export function getDefaultRouteForUser(user: User) {
  if (userHasPermission(user, "assistant.use")) {
    return "/asistente";
  }
  if (shouldShowRequirementsPanel(user)) {
    return "/requisitos";
  }
  if (shouldShowProjectsPanel(user)) {
    return "/proyectos";
  }
  if (canViewOrdinanceLibrary(user)) {
    return "/ordenanzas";
  }
  return "/cuenta";
}

// Valida que ?next sea una ruta interna ("/algo") para evitar redirecciones
// abiertas tras el login. El parser WHATWG elimina tabuladores y saltos de
// línea y normaliza "\" a "/", así que "/\\evil.com" o "/\t/evil.com"
// acabarían siendo "//evil.com" (protocol-relative): se normaliza igual
// antes de validar.
export function sanitizeNextPath(value: string | null) {
  if (!value) {
    return null;
  }
  const normalized = value.replace(/[\t\n\r]/g, "");
  if (
    normalized.startsWith("/") &&
    !normalized.startsWith("//") &&
    !normalized.startsWith("/\\")
  ) {
    return normalized;
  }
  return null;
}

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type SessionContextValue = {
  user: User | null;
  setUser: Dispatch<SetStateAction<User | null>>;
  isLoadingSession: boolean;
  /** Fallo no-auth (red, 5xx) al restaurar la sesión; lo muestra el login. */
  sessionError: string;
  handleRequestError: RequestErrorHandler;
  logout: (options?: { expired?: boolean }) => Promise<void>;
  getStoredToken: () => string;
};

const SessionContext = createContext<SessionContextValue | null>(null);

// Se activa cuando logout()/handleRequestError ya han navegado a /login con
// sus propios parámetros (expired, next); el guard del (app) layout lo
// consume para no sobrescribir esa URL con su propia redirección.
let hasPendingLoginRedirect = false;

export function consumePendingLoginRedirect() {
  const pending = hasPendingLoginRedirect;
  hasPendingLoginRedirect = false;
  return pending;
}

// /login?expired=1[&next=...]: el aviso de sesión caducada y la vuelta a la
// página en la que estaba el usuario cuando el backend devolvió un 401.
function buildExpiredLoginUrl() {
  const params = new URLSearchParams({ expired: "1" });
  const target = window.location.pathname + window.location.search;
  if (
    target.startsWith("/") &&
    !target.startsWith("//") &&
    target !== "/" &&
    !target.startsWith("/login")
  ) {
    params.set("next", target);
  }
  return `/login?${params.toString()}`;
}

function requestLogout() {
  // Fire-and-forget: the cookie is httpOnly so only the backend can clear
  // it; local state is reset regardless of whether this call succeeds.
  void fetch(`${API_BASE_URL}/auth/logout`, {
    method: "POST",
    credentials: "include",
  }).catch(() => undefined);
}

function getStoredToken() {
  // Auth now travels in an httpOnly cookie, so there is no token to read.
  // The function is kept so the controllers keep their signatures.
  return "";
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [isLoadingSession, setIsLoadingSession] = useState(true);
  const [sessionError, setSessionError] = useState("");

  useEffect(() => {
    let isActive = true;

    fetchCurrentUser("")
      .then((currentUser) => {
        if (isActive) {
          setUser(currentUser);
        }
      })
      .catch((restoreError) => {
        // Un 401 solo significa que no hay cookie de sesión activa. Cualquier
        // otro fallo (red, 5xx) se guarda para que el login explique por qué
        // un usuario con cookie válida ha acabado allí.
        if (isActive && !isAuthError(restoreError)) {
          setSessionError(
            getErrorMessage(restoreError, "No se pudo comprobar la sesión."),
          );
        }
      })
      .finally(() => {
        if (isActive) {
          setIsLoadingSession(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, []);

  async function logout(options: { expired?: boolean } = {}) {
    if (!options.expired && !(await runNavigationGuards())) {
      return;
    }
    requestLogout();
    setUser(null);
    hasPendingLoginRedirect = true;
    // Route pages unmount on navigation, so their state clears naturally.
    router.replace(options.expired ? buildExpiredLoginUrl() : "/login");
  }

  function handleRequestError(
    requestError: unknown,
    setMessage: (message: string) => void,
    fallback: string,
  ) {
    if (isAuthError(requestError)) {
      void logout({ expired: true });
      return;
    }

    setMessage(getErrorMessage(requestError, fallback));
  }

  return (
    <SessionContext.Provider
      value={{
        user,
        setUser,
        isLoadingSession,
        sessionError,
        handleRequestError,
        logout,
        getStoredToken,
      }}
    >
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  const context = useContext(SessionContext);
  if (!context) {
    throw new Error("useSession debe usarse dentro de <SessionProvider>.");
  }

  return context;
}
