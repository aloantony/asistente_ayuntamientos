"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";
import {
  adminRequest,
  API_BASE_URL,
  ApiRequestError,
  getErrorMessage,
  isAuthError,
} from "../../lib/api";
import { useSession } from "../../lib/session";

const INVITATION_TOKEN_STORAGE_KEY = "organization-invitation-token";

type InvitationPreview = {
  email: string;
  organization_name: string;
  expires_at: string;
};

type InvitationAccepted = {
  detail: string;
  organization_id: number;
  organization_name: string;
};

export default function AcceptInvitationPage() {
  const { user, setUser, isLoadingSession } = useSession();
  const [token, setToken] = useState("");
  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [acceptedOrganization, setAcceptedOrganization] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [canRetryPreview, setCanRetryPreview] = useState(false);

  useEffect(() => {
    const fragmentToken = window.location.hash.slice(1);
    if (fragmentToken) {
      window.history.replaceState(null, "", "/invitaciones");
      try {
        const decodedToken = decodeURIComponent(fragmentToken);
        sessionStorage.setItem(INVITATION_TOKEN_STORAGE_KEY, decodedToken);
        setToken(decodedToken);
      } catch {
        sessionStorage.removeItem(INVITATION_TOKEN_STORAGE_KEY);
        setError("La invitación no está disponible.");
        setIsLoading(false);
      }
      return;
    }

    const storedToken = sessionStorage.getItem(INVITATION_TOKEN_STORAGE_KEY);
    if (storedToken) {
      setToken(storedToken);
      return;
    }
    setError("La invitación no está disponible.");
    setIsLoading(false);
  }, []);

  useEffect(() => {
    if (!token) {
      return;
    }
    let isActive = true;
    setIsLoading(true);
    setError("");
    setCanRetryPreview(false);
    adminRequest<InvitationPreview>(
      "/auth/invitations/preview",
      "",
      "La invitación no está disponible.",
      {
        method: "POST",
        body: JSON.stringify({ token }),
      },
    )
      .then((invitation) => {
        if (isActive) {
          setPreview(invitation);
        }
      })
      .catch((previewError) => {
        if (isActive) {
          const isTerminalError =
            previewError instanceof ApiRequestError &&
            [410, 422].includes(previewError.status);
          if (isTerminalError) {
            sessionStorage.removeItem(INVITATION_TOKEN_STORAGE_KEY);
          }
          setCanRetryPreview(!isTerminalError);
          setError(
            getErrorMessage(previewError, "La invitación no está disponible."),
          );
        }
      })
      .finally(() => {
        if (isActive) {
          setIsLoading(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, [previewAttempt, token]);

  async function handleAccept() {
    if (!preview || !user) {
      return;
    }
    setError("");
    setIsSubmitting(true);

    try {
      const accepted = await adminRequest<InvitationAccepted>(
        "/auth/invitations/accept",
        "",
        "No se pudo aceptar la invitación.",
        {
          method: "POST",
          body: JSON.stringify({ token }),
        },
      );
      setAcceptedOrganization(accepted.organization_name);
      sessionStorage.removeItem(INVITATION_TOKEN_STORAGE_KEY);
      window.history.replaceState(null, "", "/invitaciones");
    } catch (acceptError) {
      if (isAuthError(acceptError)) {
        setUser(null);
      }
      if (
        acceptError instanceof ApiRequestError &&
        [410, 422].includes(acceptError.status)
      ) {
        sessionStorage.removeItem(INVITATION_TOKEN_STORAGE_KEY);
        setPreview(null);
      }
      setError(
        getErrorMessage(acceptError, "No se pudo aceptar la invitación."),
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  async function switchAccount() {
    try {
      const response = await fetch(`${API_BASE_URL}/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok && response.status !== 401) {
        throw new Error("logout failed");
      }
      setUser(null);
      window.location.assign("/login?next=%2Finvitaciones");
    } catch {
      setError("No se pudo cerrar la sesión activa.");
    }
  }

  const identityMatches =
    user && preview
      ? user.email.trim().toLowerCase() === preview.email.trim().toLowerCase()
      : false;

  return (
    <main className="page">
      <section className="panel login-panel invitation-panel">
        <div className="login-brand">
          <span className="app-brand-star" aria-hidden="true">
            <Image
              alt=""
              height={56}
              src="/brand/logo-principal.svg"
              width={56}
            />
          </span>
          <span className="login-brand-name">Asistente Anacleto</span>
        </div>

        {acceptedOrganization ? (
          <>
            <p className="eyebrow">Invitación aceptada</p>
            <h1>{acceptedOrganization}</h1>
            <p className="success-message" role="status">
              Tu acceso a la organización ya está preparado.
            </p>
            <button
              className="auth-action-link"
              onClick={() => window.location.assign("/")}
              type="button"
            >
              Continuar
            </button>
          </>
        ) : isLoading || isLoadingSession ? (
          <>
            <p className="eyebrow">Acceso municipal</p>
            <h1>Comprobando invitación</h1>
          </>
        ) : preview ? (
          <>
            <p className="eyebrow">Invitación a organización</p>
            <h1>{preview.organization_name}</h1>
            <div className="login-form">
              <label>
                Email invitado
                <input readOnly type="email" value={preview.email} />
              </label>

              {!user ? (
                <Link
                  className="auth-action-link"
                  href="/login?next=%2Finvitaciones"
                >
                  Iniciar sesión para aceptar
                </Link>
              ) : !identityMatches ? (
                <>
                  <p className="error-message" role="alert">
                    La sesión activa no corresponde al email invitado.
                  </p>
                  <button onClick={() => void switchAccount()} type="button">
                    Cambiar de cuenta
                  </button>
                </>
              ) : (
                <button
                  disabled={isSubmitting}
                  onClick={() => void handleAccept()}
                  type="button"
                >
                  {isSubmitting ? "Aceptando..." : "Aceptar invitación"}
                </button>
              )}

              {error ? (
                <p className="error-message" role="alert">
                  {error}
                </p>
              ) : null}
            </div>
          </>
        ) : (
          <>
            <p className="eyebrow">Acceso municipal</p>
            <h1>Invitación no disponible</h1>
            {error ? (
              <p className="error-message" role="alert">
                {error}
              </p>
            ) : null}
            {canRetryPreview ? (
              <button
                className="auth-action-link"
                onClick={() => setPreviewAttempt((current) => current + 1)}
                type="button"
              >
                Reintentar
              </button>
            ) : null}
            <Link className="auth-action-link" href="/login">
              Ir al inicio de sesión
            </Link>
          </>
        )}
      </section>
    </main>
  );
}
