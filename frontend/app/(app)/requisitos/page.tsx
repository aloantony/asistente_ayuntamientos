"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { RequirementsPanel } from "../../components/RequirementsPanel";
import type { OrganizationSummary, Project } from "../../components/types";
import { fetchOrganizations, fetchProjects } from "../../lib/fetchers";
import { shouldShowRequirementsPanel, useSession } from "../../lib/session";
import {
  useRequirementsController,
  type RequirementListFilters,
} from "../../lib/useRequirementsController";

function parseIdParam(value: string | null) {
  const parsed = value ? Number.parseInt(value, 10) : Number.NaN;
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function buildRequisitosSearch(
  filters: RequirementListFilters,
  requirementId: number | null,
) {
  const params = new URLSearchParams();
  if (filters.organizationId) {
    params.set("organizacion", filters.organizationId);
  }
  if (filters.projectId) {
    params.set("proyecto", filters.projectId);
  }
  if (filters.status) {
    params.set("estado", filters.status);
  }
  if (filters.includeArchived) {
    params.set("archivadas", "1");
  }
  if (requirementId !== null) {
    params.set("id", String(requirementId));
  }

  return params.toString();
}

function RequisitosPageInner() {
  const { user, getStoredToken, handleRequestError } = useSession();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requirementsController = useRequirementsController({
    getStoredToken,
    handleRequestError,
  });
  const [organizations, setOrganizations] = useState<OrganizationSummary[]>(
    [],
  );
  const [projects, setProjects] = useState<Project[]>([]);

  const canView = Boolean(user && shouldShowRequirementsPanel(user));

  // La URL es la única fuente de verdad de filtros y selección.
  const urlFilters: RequirementListFilters = {
    organizationId: searchParams.get("organizacion") ?? "",
    projectId: searchParams.get("proyecto") ?? "",
    status: searchParams.get("estado") ?? "",
    includeArchived: searchParams.get("archivadas") === "1",
  };
  const urlRequirementId = parseIdParam(searchParams.get("id"));
  const selectedId = requirementsController.selectedRequirement?.id ?? null;
  // Espejo de la selección para que el efecto de la URL no tenga que volver a
  // ejecutarse (y re-pedir el detalle) cada vez que cambia la selección.
  const selectedIdRef = useRef<number | null>(null);
  selectedIdRef.current = selectedId;
  // Último id SOLICITADO (no resuelto): comparar contra él permite volver a
  // pedir el elemento anterior aunque otra petición siga en vuelo.
  const lastRequestedIdRef = useRef<number | null>(null);

  // Filtros de la URL -> estado de los selectores + recarga del servidor.
  useEffect(() => {
    if (!canView) {
      return;
    }

    requirementsController.setFilterOrganizationId(urlFilters.organizationId);
    requirementsController.setFilterProjectId(urlFilters.projectId);
    requirementsController.setFilterStatus(urlFilters.status);
    requirementsController.setIncludeArchivedRequirements(
      urlFilters.includeArchived,
    );
    void requirementsController.loadRequirements(urlFilters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    user?.id,
    user?.permissions,
    urlFilters.organizationId,
    urlFilters.projectId,
    urlFilters.status,
    urlFilters.includeArchived,
  ]);

  // ?id=N de la URL -> selección (enlaces profundos y atrás/adelante).
  useEffect(() => {
    if (!canView) {
      return;
    }

    if (urlRequirementId !== null) {
      if (urlRequirementId !== lastRequestedIdRef.current) {
        lastRequestedIdRef.current = urlRequirementId;
        void requirementsController.selectRequirement(urlRequirementId);
      }
    } else {
      lastRequestedIdRef.current = null;
      if (selectedIdRef.current !== null) {
        requirementsController.deselectRequirement();
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, urlRequirementId]);

  // Selección -> URL (cubre selecciones hechas dentro del controlador, por
  // ejemplo al crear un requisito). Solo se reemplaza si realmente difiere.
  const previousSelectedIdRef = useRef<number | null>(null);
  useEffect(() => {
    const previousSelectedId = previousSelectedIdRef.current;
    previousSelectedIdRef.current = selectedId;

    if (selectedId !== null) {
      if (selectedId !== urlRequirementId) {
        // Selección ya resuelta dentro del controlador (p. ej. tras crear):
        // se marca como solicitada para que el efecto de la URL no la repita.
        lastRequestedIdRef.current = selectedId;
        const search = buildRequisitosSearch(urlFilters, selectedId);
        router.replace(`/requisitos?${search}`, { scroll: false });
      }
    } else if (previousSelectedId !== null && urlRequirementId !== null) {
      const search = buildRequisitosSearch(urlFilters, null);
      router.replace(search ? `/requisitos?${search}` : "/requisitos", {
        scroll: false,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  useEffect(() => {
    if (!canView) {
      return;
    }

    let isActive = true;

    fetchOrganizations()
      .then((organizationsData) => {
        if (isActive) {
          setOrganizations(organizationsData);
        }
      })
      .catch(() => {
        // The panel falls back to the user's own organizations.
      });

    fetchProjects()
      .then((projectsData) => {
        if (isActive) {
          setProjects(projectsData);
        }
      })
      .catch(() => {
        // The project filter simply stays empty if projects cannot load.
      });

    return () => {
      isActive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  function handleSelectRequirement(requirementId: number) {
    if (requirementId === urlRequirementId) {
      // La URL no cambiaría: recargamos el detalle directamente.
      lastRequestedIdRef.current = requirementId;
      void requirementsController.selectRequirement(requirementId);
      return;
    }

    // push (no replace) para que atrás/adelante recorra las selecciones.
    const search = buildRequisitosSearch(urlFilters, requirementId);
    router.push(`/requisitos?${search}`, { scroll: false });
  }

  // "Aplicar filtros" / "Actualizar": lleva los filtros pendientes a la URL
  // (con la selección actual intacta); si la URL no cambia, recarga.
  function handleRefresh() {
    const pendingFilters: RequirementListFilters = {
      organizationId: requirementsController.filterOrganizationId,
      projectId: requirementsController.filterProjectId,
      status: requirementsController.filterStatus,
      includeArchived: requirementsController.includeArchivedRequirements,
    };
    const unchanged =
      pendingFilters.organizationId === urlFilters.organizationId &&
      pendingFilters.projectId === urlFilters.projectId &&
      pendingFilters.status === urlFilters.status &&
      pendingFilters.includeArchived === urlFilters.includeArchived;
    if (unchanged) {
      void requirementsController.loadRequirements(urlFilters);
      return;
    }

    // push (no replace) para que atrás/adelante recorra los filtros aplicados.
    const search = buildRequisitosSearch(pendingFilters, urlRequirementId);
    router.push(search ? `/requisitos?${search}` : "/requisitos", {
      scroll: false,
    });
  }

  if (!user || !canView) {
    return (
      <div className="workspace">
        <section className="panel">
          <p className="eyebrow">Requisitos</p>
          <h2>Acceso restringido</h2>
          <p className="muted">No tienes permisos sobre requisitos.</p>
        </section>
      </div>
    );
  }

  return (
    <div className="workspace">
      <RequirementsPanel
        user={user}
        organizations={
          organizations.length > 0 ? organizations : user.organizations ?? []
        }
        projects={projects}
        requirements={requirementsController.requirements}
        selectedRequirement={requirementsController.selectedRequirement}
        requirementMessages={requirementsController.requirementMessages}
        isLoadingRequirements={requirementsController.isLoadingRequirements}
        requirementError={requirementsController.requirementError}
        requirementMessage={requirementsController.requirementMessage}
        filterOrganizationId={requirementsController.filterOrganizationId}
        filterProjectId={requirementsController.filterProjectId}
        filterStatus={requirementsController.filterStatus}
        includeArchivedRequirements={
          requirementsController.includeArchivedRequirements
        }
        newRequirement={requirementsController.newRequirement}
        requirementFormError={requirementsController.requirementFormError}
        isCreatingRequirement={requirementsController.isCreatingRequirement}
        requirementEdit={requirementsController.requirementEdit}
        requirementEditError={requirementsController.requirementEditError}
        isUpdatingRequirement={requirementsController.isUpdatingRequirement}
        newRequirementMessageBody={
          requirementsController.newRequirementMessageBody
        }
        newRequirementMessageType={
          requirementsController.newRequirementMessageType
        }
        requirementMessagesError={
          requirementsController.requirementMessagesError
        }
        isCreatingRequirementMessage={
          requirementsController.isCreatingRequirementMessage
        }
        onRefresh={handleRefresh}
        onSelectRequirement={handleSelectRequirement}
        onUpdateNewRequirement={requirementsController.updateNewRequirement}
        onUpdateRequirementEdit={requirementsController.updateRequirementEdit}
        onFilterOrganizationIdChange={
          requirementsController.setFilterOrganizationId
        }
        onFilterProjectIdChange={requirementsController.setFilterProjectId}
        onFilterStatusChange={requirementsController.setFilterStatus}
        onIncludeArchivedRequirementsChange={
          requirementsController.setIncludeArchivedRequirements
        }
        onNewRequirementMessageBodyChange={
          requirementsController.setNewRequirementMessageBody
        }
        onNewRequirementMessageTypeChange={
          requirementsController.setNewRequirementMessageType
        }
        onCreateRequirement={requirementsController.handleCreateRequirement}
        onUpdateRequirement={requirementsController.handleUpdateRequirement}
        onChangeRequirementStatus={
          requirementsController.handleChangeRequirementStatus
        }
        onCreateRequirementMessage={
          requirementsController.handleCreateRequirementMessage
        }
      />
    </div>
  );
}

export default function RequisitosPage() {
  return (
    <Suspense
      fallback={
        <section className="panel">
          <p className="muted">Cargando…</p>
        </section>
      }
    >
      <RequisitosPageInner />
    </Suspense>
  );
}
