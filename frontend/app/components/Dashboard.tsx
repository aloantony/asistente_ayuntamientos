import type { User } from "./types";

type DashboardProps = {
  user: User;
  onLogout: () => void;
};

export function Dashboard({ user, onLogout }: DashboardProps) {
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
    </section>
  );
}
