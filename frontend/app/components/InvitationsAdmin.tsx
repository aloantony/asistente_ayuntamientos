"use client";

import { Copy } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import type {
  OrganizationInvitationCreated,
  User,
} from "./types";
import { adminRequest } from "../lib/api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type InvitationsAdminProps = {
  currentUser: User;
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
};

export function InvitationsAdmin({
  currentUser,
  getStoredToken,
  handleRequestError,
}: InvitationsAdminProps) {
  const organizations = currentUser.organizations ?? [];
  const [organizationId, setOrganizationId] = useState<number | "">("");
  const [email, setEmail] = useState("");
  const [invitationLink, setInvitationLink] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (
      organizationId === "" ||
      !organizations.some((organization) => organization.id === organizationId)
    ) {
      setOrganizationId(organizations[0]?.id ?? "");
    }
  }, [organizationId, organizations]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (organizationId === "") {
      setError("Selecciona una organización.");
      return;
    }

    setError("");
    setMessage("");
    setInvitationLink("");
    setIsSubmitting(true);

    try {
      const invitation = await adminRequest<OrganizationInvitationCreated>(
        `/organizations/${organizationId}/invitations`,
        getStoredToken(),
        "No se pudo crear la invitación.",
        {
          method: "POST",
          body: JSON.stringify({ email }),
        },
      );
      // The bearer secret stays in the URL fragment, which browsers do not
      // send to the frontend server, reverse proxy or referrer targets.
      const link = `${window.location.origin}/invitaciones#${encodeURIComponent(invitation.token)}`;
      setInvitationLink(link);
      setEmail("");
      setMessage(`Invitación creada para ${invitation.email}.`);
    } catch (invitationError) {
      handleRequestError(
        invitationError,
        setError,
        "No se pudo crear la invitación.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  async function copyInvitationLink() {
    setError("");
    try {
      await navigator.clipboard.writeText(invitationLink);
      setMessage("Enlace copiado.");
    } catch (copyError) {
      handleRequestError(
        copyError,
        setError,
        "No se pudo copiar el enlace.",
      );
    }
  }

  return (
    <div className="admin-section">
      <div className="section-header">
        <h3>Invitaciones</h3>
      </div>

      <form className="admin-form" onSubmit={handleSubmit}>
        <div className="form-grid">
          <label>
            Organización
            <select
              onChange={(event) =>
                setOrganizationId(
                  event.target.value ? Number(event.target.value) : "",
                )
              }
              required
              value={organizationId}
            >
              {organizations.length === 0 ? (
                <option value="">Sin organizaciones disponibles</option>
              ) : null}
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>
                  {organization.name}
                </option>
              ))}
            </select>
          </label>

          <label>
            Email
            <input
              autoComplete="email"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
          </label>
        </div>

        <button
          disabled={isSubmitting || organizations.length === 0}
          type="submit"
        >
          {isSubmitting ? "Creando..." : "Crear invitación"}
        </button>
      </form>

      {error ? <p className="error-message">{error}</p> : null}
      {message ? <p className="success-message">{message}</p> : null}
      {invitationLink ? (
        <div className="invitation-link-row">
          <label>
            Enlace de un solo uso
            <input readOnly type="text" value={invitationLink} />
          </label>
          <button
            aria-label="Copiar enlace de invitación"
            className="secondary-button invitation-copy-button"
            onClick={() => void copyInvitationLink()}
            title="Copiar enlace"
            type="button"
          >
            <Copy aria-hidden="true" size={18} />
          </button>
        </div>
      ) : null}
    </div>
  );
}
