"use client";

import {
  type Dispatch,
  type FormEvent,
  type SetStateAction,
  useState,
} from "react";
import type {
  User,
  UserDeleteResponse,
  UserEditState,
} from "../../components/types";
import { adminRequest } from "../api";

type RequestErrorHandler = (
  requestError: unknown,
  setMessage: (message: string) => void,
  fallback: string,
) => void;

type UseUsersAdminArgs = {
  getStoredToken: () => string;
  handleRequestError: RequestErrorHandler;
  user: User | null;
  setUser: Dispatch<SetStateAction<User | null>>;
};

function buildUserEditState(users: User[]) {
  return users.reduce<Record<number, UserEditState>>((edits, adminUser) => {
    edits[adminUser.id] = {
      full_name: adminUser.full_name,
      is_active: adminUser.is_active,
      is_superuser: adminUser.is_superuser,
    };
    return edits;
  }, {});
}

export function useUsersAdmin({
  getStoredToken,
  handleRequestError,
  user,
  setUser,
}: UseUsersAdminArgs) {
  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  const [isLoadingUsers, setIsLoadingUsers] = useState(false);
  const [usersError, setUsersError] = useState("");

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserFullName, setNewUserFullName] = useState("");
  const [newUserIsActive, setNewUserIsActive] = useState(true);
  const [newUserIsSuperuser, setNewUserIsSuperuser] = useState(false);
  const [userFormError, setUserFormError] = useState("");
  const [isCreatingUser, setIsCreatingUser] = useState(false);
  const [userEdits, setUserEdits] = useState<Record<number, UserEditState>>(
    {},
  );
  const [userEditError, setUserEditError] = useState("");
  const [userEditMessage, setUserEditMessage] = useState("");
  const [updatingUserId, setUpdatingUserId] = useState<number | null>(null);
  const [deletingUserId, setDeletingUserId] = useState<number | null>(null);
  const [userPasswordResets, setUserPasswordResets] = useState<
    Record<number, string>
  >({});
  const [resettingPasswordUserId, setResettingPasswordUserId] = useState<
    number | null
  >(null);

  async function loadUsers() {
    setIsLoadingUsers(true);
    setUsersError("");

    try {
      const token = getStoredToken();
      const usersData = await adminRequest<User[]>(
        "/admin/users",
        token,
        "No se pudo cargar la lista de usuarios.",
      );

      setAdminUsers(usersData);
      setUserEdits(buildUserEditState(usersData));
    } catch (loadError) {
      handleRequestError(
        loadError,
        setUsersError,
        "No se pudo cargar la lista de usuarios.",
      );
    } finally {
      setIsLoadingUsers(false);
    }
  }

  function updateUserEdit(userId: number, updates: Partial<UserEditState>) {
    setUserEdits((currentEdits) => {
      const currentEdit = currentEdits[userId];
      if (!currentEdit) {
        return currentEdits;
      }

      return {
        ...currentEdits,
        [userId]: {
          ...currentEdit,
          ...updates,
        },
      };
    });
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
      await loadUsers();
    } catch (createError) {
      handleRequestError(
        createError,
        setUserFormError,
        "No se pudo crear el usuario.",
      );
    } finally {
      setIsCreatingUser(false);
    }
  }

  async function handleUpdateUser(userId: number) {
    const edit = userEdits[userId];
    if (!edit) {
      setUserEditError("No se pudo encontrar el usuario para editar.");
      return;
    }

    const fullName = edit.full_name.trim();
    if (!fullName) {
      setUserEditError("El nombre completo no puede estar vacío.");
      setUserEditMessage("");
      return;
    }

    setUserEditError("");
    setUserEditMessage("");
    setUpdatingUserId(userId);

    try {
      const token = getStoredToken();
      const updatedUser = await adminRequest<User>(
        `/admin/users/${userId}`,
        token,
        "No se pudo actualizar el usuario.",
        {
          method: "PATCH",
          body: JSON.stringify({
            full_name: fullName,
            is_active: edit.is_active,
            is_superuser: edit.is_superuser,
          }),
        },
      );

      setUserEditMessage("Usuario actualizado.");

      // Si el usuario editado es el de la sesión, la sesión se actualiza y
      // los permisos de las rutas de administración reaccionan solos.
      if (user?.id === updatedUser.id) {
        setUser(updatedUser);
      }

      await loadUsers();
    } catch (updateError) {
      handleRequestError(
        updateError,
        setUserEditError,
        "No se pudo actualizar el usuario.",
      );
    } finally {
      setUpdatingUserId(null);
    }
  }

  async function handleDeleteUser(adminUser: User) {
    setUserEditError("");
    setUserEditMessage("");

    if (user?.id === adminUser.id) {
      setUserEditError("No puedes eliminar tu propia cuenta.");
      return;
    }

    const confirmed = window.confirm(
      `¿Eliminar el usuario ${adminUser.email}? Esta acción no se puede deshacer.`,
    );
    if (!confirmed) {
      return;
    }

    setDeletingUserId(adminUser.id);

    try {
      const token = getStoredToken();
      await adminRequest<UserDeleteResponse>(
        `/admin/users/${adminUser.id}`,
        token,
        "No se pudo eliminar el usuario.",
        {
          method: "DELETE",
        },
      );

      setUserEditMessage("Usuario eliminado.");
      await loadUsers();
    } catch (deleteError) {
      handleRequestError(
        deleteError,
        setUserEditError,
        "No se pudo eliminar el usuario.",
      );
    } finally {
      setDeletingUserId(null);
    }
  }

  function updateUserPasswordReset(userId: number, password: string) {
    setUserPasswordResets((current) => ({
      ...current,
      [userId]: password,
    }));
  }

  async function handleResetUserPassword(userId: number) {
    const password = userPasswordResets[userId] ?? "";

    setUserEditError("");
    setUserEditMessage("");

    if (password.length < 8) {
      setUserEditError(
        "La nueva contraseña debe tener al menos 8 caracteres.",
      );
      return;
    }

    setResettingPasswordUserId(userId);

    try {
      const token = getStoredToken();
      await adminRequest<User>(
        `/admin/users/${userId}`,
        token,
        "No se pudo restablecer la contraseña.",
        {
          method: "PATCH",
          body: JSON.stringify({ password }),
        },
      );

      setUserPasswordResets((current) => {
        const nextResets = { ...current };
        delete nextResets[userId];
        return nextResets;
      });
      setUserEditMessage("Contraseña restablecida.");
    } catch (resetError) {
      handleRequestError(
        resetError,
        setUserEditError,
        "No se pudo restablecer la contraseña.",
      );
    } finally {
      setResettingPasswordUserId(null);
    }
  }

  return {
    adminUsers,
    isLoadingUsers,
    usersError,
    newUserEmail,
    newUserPassword,
    newUserFullName,
    newUserIsActive,
    newUserIsSuperuser,
    userFormError,
    isCreatingUser,
    userEdits,
    userEditError,
    userEditMessage,
    updatingUserId,
    deletingUserId,
    userPasswordResets,
    resettingPasswordUserId,
    setNewUserEmail,
    setNewUserPassword,
    setNewUserFullName,
    setNewUserIsActive,
    setNewUserIsSuperuser,
    loadUsers,
    updateUserEdit,
    handleCreateUser,
    handleUpdateUser,
    handleDeleteUser,
    updateUserPasswordReset,
    handleResetUserPassword,
  };
}
