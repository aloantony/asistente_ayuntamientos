"use client";

import { Copy, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  OrganizationInvitationCreated,
  OrganizationInvitation,
  User,
} from "./types";
import {
  adminRequest,
  adminRequestWithTotal,
  getErrorMessage,
} from "../lib/api";

const INVITATIONS_PAGE_SIZE = 50;

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
  const organizations = useMemo(
    () => currentUser.organizations ?? [],
    [currentUser.organizations],
  );
  const [organizationId, setOrganizationId] = useState<number | "">("");
  const [email, setEmail] = useState("");
  const [invitationLink, setInvitationLink] = useState<{
    invitationId: number;
    organizationId: number;
    organizationName: string;
    url: string;
  } | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [invitations, setInvitations] = useState<OrganizationInvitation[]>([]);
  const [invitationPage, setInvitationPage] = useState(0);
  const [invitationTotal, setInvitationTotal] = useState(0);
  const [invitationRefresh, setInvitationRefresh] = useState(0);
  const [isLoadingInvitations, setIsLoadingInvitations] = useState(false);
  const [revokingInvitationId, setRevokingInvitationId] = useState<number | null>(
    null,
  );

  useEffect(() => {
    if (
      organizationId === "" ||
      !organizations.some((organization) => organization.id === organizationId)
    ) {
      setOrganizationId(organizations[0]?.id ?? "");
    }
  }, [organizationId, organizations]);

  useEffect(() => {
    setInvitationPage(0);
    setInvitationLink(null);
    setMessage("");
  }, [organizationId]);

  useEffect(() => {
    if (organizationId === "") {
      setInvitations([]);
      setInvitationTotal(0);
      return;
    }
    const controller = new AbortController();
    setInvitations([]);
    setInvitationTotal(0);
    setError("");
    setIsLoadingInvitations(true);
    adminRequestWithTotal<OrganizationInvitation[]>(
      `/organizations/${organizationId}/invitations?limit=${INVITATIONS_PAGE_SIZE}&offset=${invitationPage * INVITATIONS_PAGE_SIZE}`,
      getStoredToken(),
      "No se pudieron cargar las invitaciones.",
      { signal: controller.signal },
    )
      .then(({ items, total }) => {
        if (!controller.signal.aborted) {
          setInvitations(items);
          setInvitationTotal(total);
        }
      })
      .catch((loadError) => {
        if (!controller.signal.aborted) {
          setError(
            getErrorMessage(loadError, "No se pudieron cargar las invitaciones."),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoadingInvitations(false);
        }
      });
    return () => controller.abort();
  }, [getStoredToken, invitationPage, invitationRefresh, organizationId]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (organizationId === "") {
      setError("Selecciona una organización.");
      return;
    }
    const requestedOrganizationId = organizationId;
    const requestedOrganizationName =
      organizations.find(
        (organization) => organization.id === requestedOrganizationId,
      )?.name ?? `Organización ${requestedOrganizationId}`;

    setError("");
    setMessage("");
    setInvitationLink(null);
    setIsSubmitting(true);

    try {
      const invitation = await adminRequest<OrganizationInvitationCreated>(
        `/organizations/${requestedOrganizationId}/invitations`,
        getStoredToken(),
        "No se pudo crear la invitación.",
        {
          method: "POST",
          body: JSON.stringify({ email }),
        },
      );
      const { token, ...invitationItem } = invitation;
      // The bearer secret stays in the URL fragment, which browsers do not
      // send to the frontend server, reverse proxy or referrer targets.
      const link = `${window.location.origin}/invitaciones#${encodeURIComponent(token)}`;
      setInvitationLink({
        invitationId: invitation.id,
        organizationId: requestedOrganizationId,
        organizationName: requestedOrganizationName,
        url: link,
      });
      setInvitationPage(0);
      setInvitationRefresh((current) => current + 1);
      setEmail("");
      setMessage(
        `Invitación creada para ${invitationItem.email} en ${requestedOrganizationName}.`,
      );
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

  async function revokeInvitation(invitationId: number) {
    if (organizationId === "") {
      return;
    }
    setError("");
    setRevokingInvitationId(invitationId);
    try {
      await adminRequest(
        `/organizations/${organizationId}/invitations/${invitationId}`,
        getStoredToken(),
        "No se pudo revocar la invitación.",
        { method: "DELETE" },
      );
      setInvitations((current) =>
        current.map((invitation) =>
          invitation.id === invitationId
            ? { ...invitation, revoked_at: new Date().toISOString() }
            : invitation,
        ),
      );
      setInvitationLink((current) =>
        current?.invitationId === invitationId ? null : current,
      );
      setMessage("Invitación revocada.");
    } catch (revokeError) {
      handleRequestError(
        revokeError,
        setError,
        "No se pudo revocar la invitación.",
      );
    } finally {
      setRevokingInvitationId(null);
    }
  }

  function invitationStatus(invitation: OrganizationInvitation) {
    if (invitation.accepted_at) {
      return "Aceptada";
    }
    if (invitation.revoked_at) {
      return "Revocada";
    }
    if (new Date(invitation.expires_at).getTime() <= Date.now()) {
      return "Caducada";
    }
    return "Pendiente";
  }

  async function copyInvitationLink() {
    setError("");
    try {
      if (!invitationLink) {
        return;
      }
      await navigator.clipboard.writeText(invitationLink.url);
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
              disabled={isSubmitting || revokingInvitationId !== null}
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
            Enlace de un solo uso para {invitationLink.organizationName}
            <input readOnly type="text" value={invitationLink.url} />
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

      <div className="section-header invitation-list-header">
        <h4>Historial</h4>
      </div>
      {isLoadingInvitations ? (
        <p className="muted">Cargando invitaciones...</p>
      ) : invitations.length === 0 ? (
        <p className="muted">No hay invitaciones en esta organización.</p>
      ) : (
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Estado</th>
                <th>Caduca</th>
                <th aria-label="Acciones" />
              </tr>
            </thead>
            <tbody>
              {invitations.map((invitation) => {
                const currentStatus = invitationStatus(invitation);
                return (
                  <tr key={invitation.id}>
                    <td>{invitation.email}</td>
                    <td>{currentStatus}</td>
                    <td>{new Date(invitation.expires_at).toLocaleString("es-ES")}</td>
                    <td>
                      {currentStatus === "Pendiente" ? (
                        <button
                          aria-label={`Revocar invitación de ${invitation.email}`}
                          className="secondary-button invitation-copy-button"
                          disabled={revokingInvitationId === invitation.id}
                          onClick={() => void revokeInvitation(invitation.id)}
                          title="Revocar invitación"
                          type="button"
                        >
                          <Trash2 aria-hidden="true" size={18} />
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {invitationTotal > INVITATIONS_PAGE_SIZE ? (
        <div className="pager-row">
          <button
            className="secondary-button"
            disabled={invitationPage === 0 || isLoadingInvitations}
            onClick={() => setInvitationPage((current) => Math.max(0, current - 1))}
            type="button"
          >
            Anterior
          </button>
          <span className="pager-status">
            Página {invitationPage + 1} de{" "}
            {Math.ceil(invitationTotal / INVITATIONS_PAGE_SIZE)} ({invitationTotal} en
            total)
          </span>
          <button
            className="secondary-button"
            disabled={
              (invitationPage + 1) * INVITATIONS_PAGE_SIZE >= invitationTotal ||
              isLoadingInvitations
            }
            onClick={() => setInvitationPage((current) => current + 1)}
            type="button"
          >
            Siguiente
          </button>
        </div>
      ) : null}
    </div>
  );
}
