import type { FormEvent } from "react";
import type { TelegramLinkCode, TelegramLinkStatus, User } from "./types";

type DashboardProps = {
  user: User;
  currentPassword: string;
  newPassword: string;
  newPasswordConfirmation: string;
  changePasswordError: string;
  changePasswordMessage: string;
  isChangingPassword: boolean;
  telegramStatus: TelegramLinkStatus | null;
  telegramCode: TelegramLinkCode | null;
  telegramError: string;
  telegramMessage: string;
  isTelegramBusy: boolean;
  onLogout: () => void;
  onCurrentPasswordChange: (currentPassword: string) => void;
  onNewPasswordChange: (newPassword: string) => void;
  onNewPasswordConfirmationChange: (newPasswordConfirmation: string) => void;
  onChangePassword: (event: FormEvent<HTMLFormElement>) => void;
  onCreateTelegramCode: () => void;
  onRevokeTelegramLink: () => void;
};

export function Dashboard({
  user,
  currentPassword,
  newPassword,
  newPasswordConfirmation,
  changePasswordError,
  changePasswordMessage,
  isChangingPassword,
  telegramStatus,
  telegramCode,
  telegramError,
  telegramMessage,
  isTelegramBusy,
  onLogout,
  onCurrentPasswordChange,
  onNewPasswordChange,
  onNewPasswordConfirmationChange,
  onChangePassword,
  onCreateTelegramCode,
  onRevokeTelegramLink,
}: DashboardProps) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Panel privado</p>
          <h1>Dashboard</h1>
        </div>
        <button className="secondary-button" type="button" onClick={onLogout}>
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
        <div>
          <dt>Organizaciones</dt>
          <dd>
            {(user.organizations ?? []).length > 0
              ? (user.organizations ?? [])
                  .map((organization) => organization.name)
                  .join(", ")
              : "Sin organizaciones"}
          </dd>
        </div>
      </dl>

      <details className="change-password-section">
        <summary>Cambiar contraseña</summary>
        <form className="change-password-form" onSubmit={onChangePassword}>
          <div className="form-grid">
            <label>
              Contraseña actual
              <input
                autoComplete="current-password"
                name="current-password"
                onChange={(event) =>
                  onCurrentPasswordChange(event.target.value)
                }
                required
                type="password"
                value={currentPassword}
              />
            </label>

            <label>
              Nueva contraseña
              <input
                autoComplete="new-password"
                minLength={8}
                name="new-password"
                onChange={(event) => onNewPasswordChange(event.target.value)}
                required
                type="password"
                value={newPassword}
              />
            </label>

            <label>
              Confirmar nueva contraseña
              <input
                autoComplete="new-password"
                minLength={8}
                name="new-password-confirmation"
                onChange={(event) =>
                  onNewPasswordConfirmationChange(event.target.value)
                }
                required
                type="password"
                value={newPasswordConfirmation}
              />
            </label>
          </div>

          {changePasswordError ? (
            <p className="error-message">{changePasswordError}</p>
          ) : null}
          {changePasswordMessage ? (
            <p className="success-message">{changePasswordMessage}</p>
          ) : null}

          <button type="submit" disabled={isChangingPassword}>
            {isChangingPassword ? "Guardando..." : "Cambiar contraseña"}
          </button>
        </form>
      </details>

      <details className="change-password-section">
        <summary>Telegram</summary>
        <div className="telegram-link-panel">
          <dl className="user-details">
            <div>
              <dt>Estado</dt>
              <dd>
                {telegramStatus?.linked
                  ? `Vinculado${
                      telegramStatus.telegram_username
                        ? ` a @${telegramStatus.telegram_username}`
                        : ""
                    }`
                  : "Sin vincular"}
              </dd>
            </div>
          </dl>

          {telegramCode ? (
            <p className="success-message">
              Código: <strong>{telegramCode.code}</strong>
            </p>
          ) : null}
          {telegramError ? (
            <p className="error-message">{telegramError}</p>
          ) : null}
          {telegramMessage ? (
            <p className="success-message">{telegramMessage}</p>
          ) : null}

          <div className="button-row">
            <button
              type="button"
              onClick={onCreateTelegramCode}
              disabled={isTelegramBusy}
            >
              Generar código
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={onRevokeTelegramLink}
              disabled={isTelegramBusy || !telegramStatus?.linked}
            >
              Revocar vínculo
            </button>
          </div>
        </div>
      </details>
    </section>
  );
}
