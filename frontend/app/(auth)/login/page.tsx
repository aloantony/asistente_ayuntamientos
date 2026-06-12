"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";
import { LoginForm } from "../../components/LoginForm";
import {
  API_BASE_URL,
  ApiRequestError,
  fetchCurrentUser,
  getErrorMessage,
  readApiError,
} from "../../lib/api";
import {
  getDefaultRouteForUser,
  sanitizeNextPath,
  useSession,
} from "../../lib/session";

const SESSION_EXPIRED_MESSAGE =
  "La sesión ha caducado o no es válida. Inicia sesión de nuevo.";

function LoginPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, setUser, isLoadingSession, sessionError } = useSession();
  // ?next solo se acepta como ruta interna para evitar redirecciones abiertas.
  const nextPath = sanitizeNextPath(searchParams.get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  // ?expired=1 lo pone el funnel de 401: la sesión caducó en mitad del uso.
  const [error, setError] = useState(
    searchParams.get("expired") === "1" ? SESSION_EXPIRED_MESSAGE : "",
  );
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!isLoadingSession && user) {
      // Vuelve al destino solicitado o aterriza en la sección por defecto
      // del usuario en un solo salto (sin pasar por "/").
      router.replace(nextPath ?? getDefaultRouteForUser(user));
    }
  }, [isLoadingSession, user, router, nextPath]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setIsSubmitting(true);

    try {
      const loginResponse = await fetch(`${API_BASE_URL}/auth/login`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ email, password }),
      });

      if (!loginResponse.ok) {
        throw new ApiRequestError(
          await readApiError(
            loginResponse,
            "No se pudo iniciar sesión. Revisa el email y la contraseña.",
          ),
          loginResponse.status,
        );
      }

      // The session lives in the httpOnly cookie set by the backend.
      const currentUser = await fetchCurrentUser("");
      setUser(currentUser);
      setPassword("");
      setError("");
    } catch (loginError) {
      setUser(null);
      setError(getErrorMessage(loginError, "No se pudo iniciar sesión."));
    } finally {
      setIsSubmitting(false);
    }
  }

  if (isLoadingSession) {
    return (
      <main className="page">
        <section className="panel">
          <p className="eyebrow">Plataforma privada municipal</p>
          <h1>Comprobando sesión</h1>
          <p className="muted">Validando tus credenciales guardadas.</p>
        </section>
      </main>
    );
  }

  if (user) {
    // The effect above redirects to the app shell.
    return null;
  }

  return (
    <LoginForm
      email={email}
      password={password}
      // El fallo de restauración de sesión (red, 5xx) se muestra cuando no
      // hay un error local más reciente que lo sustituya.
      error={error || sessionError}
      isSubmitting={isSubmitting}
      onEmailChange={setEmail}
      onPasswordChange={setPassword}
      onSubmit={handleSubmit}
    />
  );
}

export default function LoginPage() {
  // useSearchParams exige un límite de Suspense durante el prerender.
  return (
    <Suspense
      fallback={
        <main className="page">
          <section className="panel">
            <p className="eyebrow">Plataforma privada municipal</p>
            <h1>Comprobando sesión</h1>
            <p className="muted">Validando tus credenciales guardadas.</p>
          </section>
        </main>
      }
    >
      <LoginPageInner />
    </Suspense>
  );
}
