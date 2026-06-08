"use client";

import { FormEvent, useEffect, useState } from "react";

type User = {
  id: number;
  email: string;
  full_name: string;
  is_active: boolean;
  is_superuser: boolean;
};

type Group = {
  id: number;
  name: string;
  description: string | null;
};

type LoginResponse = {
  access_token: string;
  token_type: string;
};

type MembershipResponse = {
  group_id: number;
  user_id: number;
  detail: string;
};

class ApiRequestError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function translateApiDetail(detail: string, fallback: string) {
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
    case "User not found":
      return "No se encontró el usuario indicado.";
    case "Group not found":
      return "No se encontró el grupo indicado.";
    default:
      return detail || fallback;
  }
}

async function readApiError(response: Response, fallback: string) {
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

async function fetchCurrentUser(accessToken: string) {
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

async function adminRequest<T>(
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

function getErrorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function isAuthError(error: unknown) {
  return error instanceof ApiRequestError && error.status === 401;
}

export default function Home() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState("");
  const [isLoadingSession, setIsLoadingSession] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [isLoadingAdmin, setIsLoadingAdmin] = useState(false);
  const [adminError, setAdminError] = useState("");

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserFullName, setNewUserFullName] = useState("");
  const [newUserIsActive, setNewUserIsActive] = useState(true);
  const [newUserIsSuperuser, setNewUserIsSuperuser] = useState(false);
  const [userFormError, setUserFormError] = useState("");
  const [isCreatingUser, setIsCreatingUser] = useState(false);

  const [newGroupName, setNewGroupName] = useState("");
  const [newGroupDescription, setNewGroupDescription] = useState("");
  const [groupFormError, setGroupFormError] = useState("");
  const [isCreatingGroup, setIsCreatingGroup] = useState(false);

  const [membershipUserId, setMembershipUserId] = useState("");
  const [membershipGroupId, setMembershipGroupId] = useState("");
  const [membershipError, setMembershipError] = useState("");
  const [membershipMessage, setMembershipMessage] = useState("");
  const [isUpdatingMembership, setIsUpdatingMembership] = useState(false);

  function clearAdminState() {
    setAdminUsers([]);
    setGroups([]);
    setAdminError("");
    setUserFormError("");
    setGroupFormError("");
    setMembershipError("");
    setMembershipMessage("");
  }

  function handleSessionExpired(message: string) {
    window.localStorage.removeItem("access_token");
    setUser(null);
    setPassword("");
    clearAdminState();
    setError(message);
  }

  function getStoredToken() {
    const token = window.localStorage.getItem("access_token");
    if (!token) {
      throw new ApiRequestError(
        "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
        401,
      );
    }

    return token;
  }

  async function loadAdminData() {
    setIsLoadingAdmin(true);
    setAdminError("");

    try {
      const token = getStoredToken();
      const [usersData, groupsData] = await Promise.all([
        adminRequest<User[]>(
          "/admin/users",
          token,
          "No se pudo cargar la lista de usuarios.",
        ),
        adminRequest<Group[]>(
          "/admin/groups",
          token,
          "No se pudo cargar la lista de grupos.",
        ),
      ]);

      setAdminUsers(usersData);
      setGroups(groupsData);
    } catch (adminLoadError) {
      if (isAuthError(adminLoadError)) {
        handleSessionExpired(
          getErrorMessage(
            adminLoadError,
            "La sesión ha caducado o no es válida. Inicia sesión de nuevo.",
          ),
        );
        return;
      }

      setAdminError(
        getErrorMessage(
          adminLoadError,
          "No se pudo cargar la información de administración.",
        ),
      );
    } finally {
      setIsLoadingAdmin(false);
    }
  }

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

  useEffect(() => {
    if (!user?.is_superuser) {
      return;
    }

    loadAdminData();
  }, [user?.is_superuser]);

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
        throw new ApiRequestError(
          await readApiError(
            loginResponse,
            "No se pudo iniciar sesión. Revisa el email y la contraseña.",
          ),
          loginResponse.status,
        );
      }

      const loginData = (await loginResponse.json()) as LoginResponse;
      window.localStorage.setItem("access_token", loginData.access_token);

      const currentUser = await fetchCurrentUser(loginData.access_token);
      setUser(currentUser);
      setPassword("");
      setError("");
    } catch (loginError) {
      window.localStorage.removeItem("access_token");
      setUser(null);
      clearAdminState();
      setError(
        getErrorMessage(loginError, "No se pudo iniciar sesión."),
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
    clearAdminState();
  }

  function handleAdminError(
    requestError: unknown,
    setMessage: (message: string) => void,
    fallback: string,
  ) {
    const message = getErrorMessage(requestError, fallback);

    if (isAuthError(requestError)) {
      handleSessionExpired(message);
      return;
    }

    setMessage(message);
  }

  async function handleCreateUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setUserFormError("");
    setIsCreatingUser(true);

    try {
      const token = getStoredToken();
      await adminRequest<User>(
        "/admin/users",
        token,
        "No se pudo crear el usuario.",
        {
          method: "POST",
          body: JSON.stringify({
            email: newUserEmail,
            password: newUserPassword,
            full_name: newUserFullName,
            is_active: newUserIsActive,
            is_superuser: newUserIsSuperuser,
          }),
        },
      );

      setNewUserEmail("");
      setNewUserPassword("");
      setNewUserFullName("");
      setNewUserIsActive(true);
      setNewUserIsSuperuser(false);
      await loadAdminData();
    } catch (createError) {
      handleAdminError(
        createError,
        setUserFormError,
        "No se pudo crear el usuario.",
      );
    } finally {
      setIsCreatingUser(false);
    }
  }

  async function handleCreateGroup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setGroupFormError("");
    setIsCreatingGroup(true);

    try {
      const token = getStoredToken();
      await adminRequest<Group>(
        "/admin/groups",
        token,
        "No se pudo crear el grupo.",
        {
          method: "POST",
          body: JSON.stringify({
            name: newGroupName,
            description: newGroupDescription.trim() || null,
          }),
        },
      );

      setNewGroupName("");
      setNewGroupDescription("");
      await loadAdminData();
    } catch (createError) {
      handleAdminError(
        createError,
        setGroupFormError,
        "No se pudo crear el grupo.",
      );
    } finally {
      setIsCreatingGroup(false);
    }
  }

  async function updateMembership(action: "add" | "remove") {
    setMembershipError("");
    setMembershipMessage("");

    const userId = Number.parseInt(membershipUserId, 10);
    const groupId = Number.parseInt(membershipGroupId, 10);

    if (!Number.isInteger(userId) || !Number.isInteger(groupId)) {
      setMembershipError("Selecciona un usuario y un grupo.");
      return;
    }

    setIsUpdatingMembership(true);

    try {
      const token = getStoredToken();
      await adminRequest<MembershipResponse>(
        `/admin/groups/${groupId}/users/${userId}`,
        token,
        "No se pudo actualizar la pertenencia al grupo.",
        {
          method: action === "add" ? "POST" : "DELETE",
        },
      );

      setMembershipMessage(
        action === "add"
          ? "Usuario añadido al grupo."
          : "Usuario eliminado del grupo.",
      );
    } catch (membershipUpdateError) {
      handleAdminError(
        membershipUpdateError,
        setMembershipError,
        "No se pudo actualizar la pertenencia al grupo.",
      );
    } finally {
      setIsUpdatingMembership(false);
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
    return (
      <main className="page app-page">
        <div className="workspace">
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

          {user.is_superuser ? (
            <section className="panel admin-panel">
              <div className="panel-header">
                <div>
                  <p className="eyebrow">Administración</p>
                  <h2>Usuarios y grupos</h2>
                </div>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={loadAdminData}
                  disabled={isLoadingAdmin}
                >
                  {isLoadingAdmin ? "Cargando..." : "Actualizar"}
                </button>
              </div>

              {adminError ? (
                <p className="error-message">{adminError}</p>
              ) : null}

              <div className="admin-section">
                <div className="section-header">
                  <h3>Usuarios</h3>
                  {isLoadingAdmin ? (
                    <p className="small-muted">Cargando usuarios.</p>
                  ) : null}
                </div>

                <div className="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Email</th>
                        <th>Nombre completo</th>
                        <th>Activo</th>
                        <th>Superusuario</th>
                      </tr>
                    </thead>
                    <tbody>
                      {adminUsers.length > 0 ? (
                        adminUsers.map((adminUser) => (
                          <tr key={adminUser.id}>
                            <td>{adminUser.id}</td>
                            <td>{adminUser.email}</td>
                            <td>{adminUser.full_name}</td>
                            <td>{adminUser.is_active ? "Sí" : "No"}</td>
                            <td>{adminUser.is_superuser ? "Sí" : "No"}</td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td colSpan={5}>No hay usuarios para mostrar.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                <form className="admin-form" onSubmit={handleCreateUser}>
                  <h4>Crear usuario</h4>
                  <div className="form-grid">
                    <label>
                      Email
                      <input
                        autoComplete="email"
                        name="new-user-email"
                        onChange={(event) =>
                          setNewUserEmail(event.target.value)
                        }
                        required
                        type="email"
                        value={newUserEmail}
                      />
                    </label>

                    <label>
                      Contraseña
                      <input
                        autoComplete="new-password"
                        minLength={8}
                        name="new-user-password"
                        onChange={(event) =>
                          setNewUserPassword(event.target.value)
                        }
                        required
                        type="password"
                        value={newUserPassword}
                      />
                    </label>

                    <label>
                      Nombre completo
                      <input
                        name="new-user-full-name"
                        onChange={(event) =>
                          setNewUserFullName(event.target.value)
                        }
                        required
                        type="text"
                        value={newUserFullName}
                      />
                    </label>
                  </div>

                  <div className="checkbox-row">
                    <label className="checkbox-label">
                      <input
                        checked={newUserIsActive}
                        onChange={(event) =>
                          setNewUserIsActive(event.target.checked)
                        }
                        type="checkbox"
                      />
                      Activo
                    </label>
                    <label className="checkbox-label">
                      <input
                        checked={newUserIsSuperuser}
                        onChange={(event) =>
                          setNewUserIsSuperuser(event.target.checked)
                        }
                        type="checkbox"
                      />
                      Superusuario
                    </label>
                  </div>

                  {userFormError ? (
                    <p className="error-message">{userFormError}</p>
                  ) : null}

                  <button type="submit" disabled={isCreatingUser}>
                    {isCreatingUser ? "Creando..." : "Crear usuario"}
                  </button>
                </form>
              </div>

              <div className="admin-section">
                <div className="section-header">
                  <h3>Grupos</h3>
                  {isLoadingAdmin ? (
                    <p className="small-muted">Cargando grupos.</p>
                  ) : null}
                </div>

                <div className="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Nombre</th>
                        <th>Descripción</th>
                      </tr>
                    </thead>
                    <tbody>
                      {groups.length > 0 ? (
                        groups.map((group) => (
                          <tr key={group.id}>
                            <td>{group.id}</td>
                            <td>{group.name}</td>
                            <td>{group.description || "Sin descripción"}</td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td colSpan={3}>No hay grupos para mostrar.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                <form className="admin-form" onSubmit={handleCreateGroup}>
                  <h4>Crear grupo</h4>
                  <div className="form-grid">
                    <label>
                      Nombre
                      <input
                        name="new-group-name"
                        onChange={(event) =>
                          setNewGroupName(event.target.value)
                        }
                        required
                        type="text"
                        value={newGroupName}
                      />
                    </label>

                    <label>
                      Descripción
                      <textarea
                        name="new-group-description"
                        onChange={(event) =>
                          setNewGroupDescription(event.target.value)
                        }
                        rows={3}
                        value={newGroupDescription}
                      />
                    </label>
                  </div>

                  {groupFormError ? (
                    <p className="error-message">{groupFormError}</p>
                  ) : null}

                  <button type="submit" disabled={isCreatingGroup}>
                    {isCreatingGroup ? "Creando..." : "Crear grupo"}
                  </button>
                </form>
              </div>

              <div className="admin-section">
                <h3>Pertenencia a grupos</h3>

                <div className="membership-controls">
                  <label>
                    Usuario
                    <select
                      onChange={(event) =>
                        setMembershipUserId(event.target.value)
                      }
                      value={membershipUserId}
                    >
                      <option value="">Selecciona un usuario</option>
                      {adminUsers.map((adminUser) => (
                        <option key={adminUser.id} value={adminUser.id}>
                          {adminUser.id} - {adminUser.email}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label>
                    Grupo
                    <select
                      onChange={(event) =>
                        setMembershipGroupId(event.target.value)
                      }
                      value={membershipGroupId}
                    >
                      <option value="">Selecciona un grupo</option>
                      {groups.map((group) => (
                        <option key={group.id} value={group.id}>
                          {group.id} - {group.name}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                {membershipError ? (
                  <p className="error-message">{membershipError}</p>
                ) : null}
                {membershipMessage ? (
                  <p className="success-message">{membershipMessage}</p>
                ) : null}

                <div className="button-row">
                  <button
                    type="button"
                    onClick={() => updateMembership("add")}
                    disabled={isUpdatingMembership}
                  >
                    Añadir al grupo
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => updateMembership("remove")}
                    disabled={isUpdatingMembership}
                  >
                    Quitar del grupo
                  </button>
                </div>
              </div>
            </section>
          ) : (
            <section className="panel">
              <p className="eyebrow">Administración</p>
              <h2>Acceso restringido</h2>
              <p className="muted">No tienes permisos de administración.</p>
            </section>
          )}
        </div>
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
