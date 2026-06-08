"use client";

import { FormEvent, useEffect, useState } from "react";

type User = {
  email: string;
  full_name: string;
  is_superuser: boolean;
};

type LoginResponse = {
  access_token: string;
  token_type: string;
};

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function readApiError(response: Response, fallback: string) {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string") {
      return data.detail;
    }
  } catch {
    // Use the fallback message when the API does not return JSON.
  }

  return fallback;
}

async function fetchCurrentUser(accessToken: string) {
  const response = await fetch(`${API_BASE_URL}/auth/me`, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });

  if (!response.ok) {
    throw new Error(
      await readApiError(
        response,
        "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
      ),
    );
  }

  return (await response.json()) as User;
}

export default function Home() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState("");
  const [isLoadingSession, setIsLoadingSession] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    let isActive = true;
    const token = window.localStorage.getItem("access_token");

    if (!token) {
      setIsLoadingSession(false);
      return () => {
        isActive = false;
      };
    }

    fetchCurrentUser(token)
      .then((currentUser) => {
        if (isActive) {
          setUser(currentUser);
        }
      })
      .catch((sessionError: Error) => {
        window.localStorage.removeItem("access_token");
        if (isActive) {
          setError(sessionError.message);
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

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setIsSubmitting(true);

    try {
      const loginResponse = await fetch(`${API_BASE_URL}/auth/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ email, password }),
      });

      if (!loginResponse.ok) {
        throw new Error(
          await readApiError(
            loginResponse,
            "No se pudo iniciar sesión. Revisa el email y la contraseña.",
          ),
        );
      }

      const loginData = (await loginResponse.json()) as LoginResponse;
      window.localStorage.setItem("access_token", loginData.access_token);

      const currentUser = await fetchCurrentUser(loginData.access_token);
      setUser(currentUser);
      setPassword("");
    } catch (loginError) {
      window.localStorage.removeItem("access_token");
      setUser(null);
      setError(
        loginError instanceof Error
          ? loginError.message
          : "No se pudo iniciar sesión.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleLogout() {
    window.localStorage.removeItem("access_token");
    setUser(null);
    setPassword("");
    setError("");
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
    return (
      <main className="page">
        <section className="panel">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Panel privado</p>
              <h1>Dashboard</h1>
            </div>
            <button
              className="secondary-button"
              type="button"
              onClick={handleLogout}
            >
              Cerrar sesión
            </button>
          </div>

          <dl className="user-details">
            <div>
              <dt>Email</dt>
              <dd>{user.email}</dd>
            </div>
            <div>
              <dt>Nombre completo</dt>
              <dd>{user.full_name}</dd>
            </div>
            <div>
              <dt>Superusuario</dt>
              <dd>{user.is_superuser ? "Sí" : "No"}</dd>
            </div>
          </dl>
        </section>
      </main>
    );
  }

  return (
    <main className="page">
      <section className="panel">
        <p className="eyebrow">Plataforma privada municipal</p>
        <h1>Iniciar sesión</h1>

        <form className="login-form" onSubmit={handleSubmit}>
          <label>
            Email
            <input
              autoComplete="email"
              name="email"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
          </label>

          <label>
            Contraseña
            <input
              autoComplete="current-password"
              name="password"
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>

          {error ? <p className="error-message">{error}</p> : null}

          <button type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Entrando..." : "Entrar"}
          </button>
        </form>
      </section>
    </main>
  );
}
