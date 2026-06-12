"use client";

import { AdminPanel } from "./components/AdminPanel";
import { AssistantPanel } from "./components/AssistantPanel";
import { Dashboard } from "./components/Dashboard";
import { LoginForm } from "./components/LoginForm";
import { ProjectsPanel } from "./components/ProjectsPanel";
import { RequirementsPanel } from "./components/RequirementsPanel";
import { useHomeController } from "./lib/useHomeController";

export default function Home() {
  const {
    isLoadingSession,
    user,
    loginFormProps,
    dashboardProps,
    projectsPanelProps,
    requirementsPanelProps,
    adminPanelProps,
    assistantPanelProps,
  } = useHomeController();

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

  if (user && dashboardProps && projectsPanelProps) {
    return (
      <main className="page app-page">
        <div className="workspace">
          <Dashboard {...dashboardProps} />
          {assistantPanelProps ? (
            <AssistantPanel {...assistantPanelProps} />
          ) : null}
          {requirementsPanelProps ? (
            <RequirementsPanel {...requirementsPanelProps} />
          ) : null}
          <ProjectsPanel {...projectsPanelProps} />

          {adminPanelProps ? (
            <AdminPanel {...adminPanelProps} />
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

  return <LoginForm {...loginFormProps} />;
}
