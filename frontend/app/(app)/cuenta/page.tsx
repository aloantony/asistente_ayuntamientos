"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Dashboard } from "../../components/Dashboard";
import type {
  TelegramLinkCode,
  TelegramLinkStatus,
} from "../../components/types";
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
  const [telegramStatus, setTelegramStatus] =
    useState<TelegramLinkStatus | null>(null);
  const [telegramCode, setTelegramCode] = useState<TelegramLinkCode | null>(
    null,
  );
  const [telegramError, setTelegramError] = useState("");
  const [telegramMessage, setTelegramMessage] = useState("");
  const [isTelegramBusy, setIsTelegramBusy] = useState(false);

  useEffect(() => {
    if (!user) {
      return;
    }
    void loadTelegramStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

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

  async function loadTelegramStatus() {
    try {
      const status = await adminRequest<TelegramLinkStatus>(
        "/telegram/link",
        getStoredToken(),
        "No se pudo cargar el estado de Telegram.",
      );
      setTelegramStatus(status);
    } catch (statusError) {
      handleRequestError(
        statusError,
        setTelegramError,
        "No se pudo cargar el estado de Telegram.",
      );
    }
  }

  async function handleCreateTelegramCode() {
    setTelegramError("");
    setTelegramMessage("");
    setTelegramCode(null);
    setIsTelegramBusy(true);
    try {
      const code = await adminRequest<TelegramLinkCode>(
        "/telegram/link-codes",
        getStoredToken(),
        "No se pudo generar el código de Telegram.",
        { method: "POST" },
      );
      setTelegramCode(code);
      setTelegramMessage("Código generado.");
    } catch (codeError) {
      handleRequestError(
        codeError,
        setTelegramError,
        "No se pudo generar el código de Telegram.",
      );
    } finally {
      setIsTelegramBusy(false);
    }
  }

  async function handleRevokeTelegramLink() {
    setTelegramError("");
    setTelegramMessage("");
    setIsTelegramBusy(true);
    try {
      const status = await adminRequest<TelegramLinkStatus>(
        "/telegram/link",
        getStoredToken(),
        "No se pudo revocar Telegram.",
        { method: "DELETE" },
      );
      setTelegramStatus(status);
      setTelegramCode(null);
      setTelegramMessage("Vínculo revocado.");
    } catch (revokeError) {
      handleRequestError(
        revokeError,
        setTelegramError,
        "No se pudo revocar Telegram.",
      );
    } finally {
      setIsTelegramBusy(false);
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
        telegramStatus={telegramStatus}
        telegramCode={telegramCode}
        telegramError={telegramError}
        telegramMessage={telegramMessage}
        isTelegramBusy={isTelegramBusy}
        onLogout={logout}
        onCurrentPasswordChange={setCurrentPassword}
        onNewPasswordChange={setNewPassword}
        onNewPasswordConfirmationChange={setNewPasswordConfirmation}
        onChangePassword={handleChangePassword}
        onCreateTelegramCode={handleCreateTelegramCode}
        onRevokeTelegramLink={handleRevokeTelegramLink}
      />
    </div>
  );
}
