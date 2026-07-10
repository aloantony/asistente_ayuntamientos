"use client";

import Link from "next/link";
import Image from "next/image";
import { useEffect, useState, type FormEvent } from "react";
import { adminRequest, getErrorMessage } from "../../lib/api";

type InvitationPreview = {
  email: string;
  organization_name: string;
  expires_at: string;
  requires_registration: boolean;
};

type InvitationAccepted = {
  detail: string;
  organization_id: number;
  organization_name: string;
};

export default function AcceptInvitationPage() {
  const [token, setToken] = useState("");
  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [acceptedOrganization, setAcceptedOrganization] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    const fragmentToken = window.location.hash.slice(1);
    if (fragmentToken) {
      window.history.replaceState(null, "", "/invitaciones");
    }
    if (fragmentToken) {
      try {
        setToken(decodeURIComponent(fragmentToken));
      } catch {
        setError("La invitación no está disponible.");
        setIsLoading(false);
      }
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
  }, [token]);

  async function handleAccept(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!preview) {
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
          body: JSON.stringify({
            token,
            ...(preview.requires_registration
              ? { full_name: fullName, password }
              : {}),
          }),
        },
      );
      setAcceptedOrganization(accepted.organization_name);
      setPassword("");
      window.history.replaceState(null, "", "/invitaciones");
    } catch (acceptError) {
      setError(
        getErrorMessage(acceptError, "No se pudo aceptar la invitación."),
      );
    } finally {
      setIsSubmitting(false);
    }
  }

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
            <Link className="auth-action-link" href="/login">
              Iniciar sesión
            </Link>
          </>
        ) : isLoading ? (
          <>
            <p className="eyebrow">Acceso municipal</p>
            <h1>Comprobando invitación</h1>
          </>
        ) : preview ? (
          <>
            <p className="eyebrow">Invitación a organización</p>
            <h1>{preview.organization_name}</h1>
            <form className="login-form" onSubmit={handleAccept}>
              <label>
                Email
                <input readOnly type="email" value={preview.email} />
              </label>

              {preview.requires_registration ? (
                <>
                  <label>
                    Nombre completo
                    <input
                      autoComplete="name"
                      maxLength={255}
                      onChange={(event) => setFullName(event.target.value)}
                      required
                      type="text"
                      value={fullName}
                    />
                  </label>
                  <label>
                    Contraseña
                    <input
                      autoComplete="new-password"
                      maxLength={1024}
                      minLength={8}
                      onChange={(event) => setPassword(event.target.value)}
                      required
                      type="password"
                      value={password}
                    />
                  </label>
                </>
              ) : null}

              {error ? (
                <p className="error-message" role="alert">
                  {error}
                </p>
              ) : null}
              <button disabled={isSubmitting} type="submit">
                {isSubmitting ? "Aceptando..." : "Aceptar invitación"}
              </button>
            </form>
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
            <Link className="auth-action-link" href="/login">
              Ir al inicio de sesión
            </Link>
          </>
        )}
      </section>
    </main>
  );
}
