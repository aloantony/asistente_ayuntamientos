"use client";

import { useState, type FormEvent } from "react";
import { Dashboard } from "../../components/Dashboard";
import { adminRequest } from "../../lib/api";
import { useSession } from "../../lib/session";

export default function CuentaPage() {
  const { user, getStoredToken, handleRequestError, logout } = useSession();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newPasswordConfirmation, setNewPasswordConfirmation] = useState("");
  const [changePasswordError, setChangePasswordError] = useState("");
  const [changePasswordMessage, setChangePasswordMessage] = useState("");
  const [isChangingPassword, setIsChangingPassword] = useState(false);

  async function handleChangePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setChangePasswordError("");
    setChangePasswordMessage("");

    if (newPassword.length < 8) {
      setChangePasswordError(
        "La nueva contraseña debe tener al menos 8 caracteres.",
      );
      return;
    }

    if (newPassword !== newPasswordConfirmation) {
      setChangePasswordError(
        "La nueva contraseña y su confirmación no coinciden.",
      );
      return;
    }

    setIsChangingPassword(true);

    try {
      await adminRequest<{ detail: string }>(
        "/auth/change-password",
        getStoredToken(),
        "No se pudo cambiar la contraseña.",
        {
          method: "POST",
          body: JSON.stringify({
            current_password: currentPassword,
            new_password: newPassword,
          }),
        },
      );

      setCurrentPassword("");
      setNewPassword("");
      setNewPasswordConfirmation("");
      setChangePasswordMessage("Contraseña actualizada.");
    } catch (changeError) {
      handleRequestError(
        changeError,
        setChangePasswordError,
        "No se pudo cambiar la contraseña.",
      );
    } finally {
      setIsChangingPassword(false);
    }
  }

  if (!user) {
    return null;
  }

  return (
    <div className="workspace">
      <Dashboard
        user={user}
        currentPassword={currentPassword}
        newPassword={newPassword}
        newPasswordConfirmation={newPasswordConfirmation}
        changePasswordError={changePasswordError}
        changePasswordMessage={changePasswordMessage}
        isChangingPassword={isChangingPassword}
        onLogout={logout}
        onCurrentPasswordChange={setCurrentPassword}
        onNewPasswordChange={setNewPassword}
        onNewPasswordConfirmationChange={setNewPasswordConfirmation}
        onChangePassword={handleChangePassword}
      />
    </div>
  );
}
