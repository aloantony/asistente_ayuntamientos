"use client";

import { useEffect, useState } from "react";
import { fetchOrganizations, fetchProjects } from "../lib/fetchers";
import { shouldShowRequirementsPanel, useSession } from "../lib/session";
import {
  useRequirementsController,
  type RequirementListFilters,
} from "../lib/useRequirementsController";
import { RequirementsPanel } from "./RequirementsPanel";
import type { OrganizationSummary, Project } from "./types";

type EmbeddedRequirementsWorkspaceProps = {
  context: {
    organizationId?: number;
    projectId?: number;
    requirementId?: number;
  };
};

export function EmbeddedRequirementsWorkspace({
  context,
}: EmbeddedRequirementsWorkspaceProps) {
  const { user, getStoredToken, handleRequestError } = useSession();
  const requirementsController = useRequirementsController({
    getStoredToken,
    handleRequestError,
  });
  const [organizations, setOrganizations] = useState<OrganizationSummary[]>(
    [],
  );
  const [projects, setProjects] = useState<Project[]>([]);
  const [appliedFilters, setAppliedFilters] = useState<RequirementListFilters>(
    () => ({
      organizationId: context.organizationId
        ? String(context.organizationId)
        : "",
      projectId: context.projectId ? String(context.projectId) : "",
      status: "",
      includeArchived: false,
      page: 1,
    }),
  );
  const canView = Boolean(user && shouldShowRequirementsPanel(user));

  useEffect(() => {
    requirementsController.setFilterOrganizationId(
      context.organizationId ? String(context.organizationId) : "",
    );
    requirementsController.setFilterProjectId(
      context.projectId ? String(context.projectId) : "",
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context.organizationId, context.projectId]);

  useEffect(() => {
    if (!canView) {
      return;
    }
    void requirementsController.loadRequirements(appliedFilters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    canView,
    appliedFilters.organizationId,
    appliedFilters.projectId,
    appliedFilters.status,
    appliedFilters.includeArchived,
    appliedFilters.page,
  ]);

  useEffect(() => {
    if (canView && context.requirementId) {
      void requirementsController.selectRequirement(context.requirementId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canView, context.requirementId]);

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
      .catch(() => undefined);
    fetchProjects()
      .then((projectsData) => {
        if (isActive) {
          setProjects(projectsData);
        }
      })
      .catch(() => undefined);
    return () => {
      isActive = false;
    };
  }, [canView, user?.id, user?.permissions]);

  const pageCount = Math.max(
    1,
    Math.ceil(
      requirementsController.requirementTotal /
        requirementsController.requirementsPageSize,
    ),
  );

  function handleRefresh() {
    const nextFilters: RequirementListFilters = {
      organizationId: requirementsController.filterOrganizationId,
      projectId: requirementsController.filterProjectId,
      status: requirementsController.filterStatus,
      includeArchived: requirementsController.includeArchivedRequirements,
      page: 1,
    };
    if (
      appliedFilters.page === 1 &&
      appliedFilters.organizationId === nextFilters.organizationId &&
      appliedFilters.projectId === nextFilters.projectId &&
      appliedFilters.status === nextFilters.status &&
      appliedFilters.includeArchived === nextFilters.includeArchived
    ) {
      void requirementsController.loadRequirements(nextFilters);
      return;
    }
    setAppliedFilters(nextFilters);
  }

  function handlePreviousPage() {
    setAppliedFilters((current) => ({
      ...current,
      page: Math.max(1, current.page - 1),
    }));
  }

  function handleNextPage() {
    setAppliedFilters((current) => ({
      ...current,
      page: Math.min(pageCount, current.page + 1),
    }));
  }

  if (!user || !canView) {
    return (
      <div className="workspace">
        <section className="panel">
          <p className="eyebrow">Necesidades</p>
          <h2>Acceso restringido</h2>
          <p className="muted">No tienes permisos sobre necesidades.</p>
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
        requirementPage={appliedFilters.page - 1}
        requirementTotal={requirementsController.requirementTotal}
        requirementPageSize={requirementsController.requirementsPageSize}
        requirementError={requirementsController.requirementError}
        requirementMessage={requirementsController.requirementMessage}
        filterOrganizationId={requirementsController.filterOrganizationId}
        filterProjectId={requirementsController.filterProjectId}
        filterStatus={requirementsController.filterStatus}
        includeArchivedRequirements={
          requirementsController.includeArchivedRequirements
        }
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
        onSelectRequirement={requirementsController.selectRequirement}
        onRequirementPrevPage={handlePreviousPage}
        onRequirementNextPage={handleNextPage}
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
