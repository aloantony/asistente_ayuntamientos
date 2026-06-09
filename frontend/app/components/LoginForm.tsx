import type { FormEvent } from "react";

type LoginFormProps = {
  email: string;
  password: string;
  error: string;
  isSubmitting: boolean;
  onEmailChange: (email: string) => void;
  onPasswordChange: (password: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
};

export function LoginForm({
  email,
  password,
  error,
  isSubmitting,
  onEmailChange,
  onPasswordChange,
  onSubmit,
}: LoginFormProps) {
  return (
    <main className="page">
      <section className="panel">
        <p className="eyebrow">Plataforma privada municipal</p>
        <h1>Iniciar sesión</h1>

        <form className="login-form" onSubmit={onSubmit}>
          <label>
            Email
            <input
              autoComplete="email"
              name="email"
              onChange={(event) => onEmailChange(event.target.value)}
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
              onChange={(event) => onPasswordChange(event.target.value)}
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
