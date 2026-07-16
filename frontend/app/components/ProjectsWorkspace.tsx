"use client";

import { useEffect, useMemo, useState } from "react";
import {
  fetchAdminGroups,
  fetchAdminUsers,
  fetchOrganizations,
} from "../lib/fetchers";
import { shouldShowProjectsPanel, useSession } from "../lib/session";
import { useProjectsController } from "../lib/useProjectsController";
import { ProjectsPanel } from "./ProjectsPanel";
import {
  userHasPermission,
  type Group,
  type OrganizationSummary,
  type User,
} from "./types";

type ProjectsWorkspaceProps = {
  embeddedContext?: {
    organizationId?: number;
    projectId?: number;
  };
};

export function ProjectsWorkspace({
  embeddedContext,
}: ProjectsWorkspaceProps = {}) {
  const { user, getStoredToken, handleRequestError } = useSession();
  const projectsController = useProjectsController({
    getStoredToken,
    handleRequestError,
    user,
  });
  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [organizations, setOrganizations] = useState<OrganizationSummary[]>(
    [],
  );
  const canOpenWorkspace = Boolean(
    user &&
      (shouldShowProjectsPanel(user) || embeddedContext?.projectId !== undefined),
  );

  useEffect(() => {
    if (!canOpenWorkspace) {
      return;
    }
    void projectsController.loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canOpenWorkspace, user?.id]);

  useEffect(() => {
    if (!user || !canOpenWorkspace) {
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

    if (userHasPermission(user, "projects.manage_members")) {
      fetchAdminUsers()
        .then((usersData) => {
          if (isActive) {
            setAdminUsers(usersData);
          }
        })
        .catch(() => undefined);
      fetchAdminGroups()
        .then((groupsData) => {
          if (isActive) {
            setGroups(groupsData);
          }
        })
        .catch(() => undefined);
    }

    return () => {
      isActive = false;
    };
  }, [canOpenWorkspace, user]);

  const visibleProjects = useMemo(
    () =>
      projectsController.projects.filter(
        (project) =>
          (!embeddedContext?.organizationId ||
            project.organization_id === embeddedContext.organizationId) &&
          (!embeddedContext?.projectId ||
            project.id === embeddedContext.projectId),
      ),
    [
      embeddedContext?.organizationId,
      embeddedContext?.projectId,
      projectsController.projects,
    ],
  );

  if (!user) {
    return null;
  }

  if (!canOpenWorkspace) {
    return (
      <section className="panel">
        <p className="eyebrow">Proyectos</p>
        <h2>Acceso restringido</h2>
        <p className="muted">Esta sección no está disponible para esta cuenta.</p>
      </section>
    );
  }

  return (
    <div className="workspace">
      <ProjectsPanel
        user={user}
        projects={visibleProjects}
        adminUsers={adminUsers}
        groups={groups}
        organizations={
          organizations.length > 0 ? organizations : user.organizations ?? []
        }
        isLoadingProjects={projectsController.isLoadingProjects}
        projectError={projectsController.projectError}
        newProjectName={projectsController.newProjectName}
        newProjectDescription={projectsController.newProjectDescription}
        newProjectStatus={projectsController.newProjectStatus}
        newProjectOrganizationId={projectsController.newProjectOrganizationId}
        projectFormError={projectsController.projectFormError}
        isCreatingProject={projectsController.isCreatingProject}
        projectEdits={projectsController.projectEdits}
        projectEditError={projectsController.projectEditError}
        projectEditMessage={projectsController.projectEditMessage}
        updatingProjectId={projectsController.updatingProjectId}
        projectMembershipProjectId={
          projectsController.projectMembershipProjectId
        }
        projectMembershipUserId={projectsController.projectMembershipUserId}
        projectMembershipGroupId={projectsController.projectMembershipGroupId}
        projectMembershipError={projectsController.projectMembershipError}
        projectMembershipMessage={projectsController.projectMembershipMessage}
        isUpdatingProjectMembership={
          projectsController.isUpdatingProjectMembership
        }
        projectDocuments={projectsController.projectDocuments}
        projectDocumentErrors={projectsController.projectDocumentErrors}
        isLoadingDocuments={projectsController.isLoadingDocuments}
        includeArchivedDocuments={projectsController.includeArchivedDocuments}
        uploadingDocumentProjectId={
          projectsController.uploadingDocumentProjectId
        }
        archivingDocumentId={projectsController.archivingDocumentId}
        documentError={projectsController.documentError}
        documentMessage={projectsController.documentMessage}
        onRefresh={projectsController.loadProjects}
        onNewProjectNameChange={projectsController.setNewProjectName}
        onNewProjectDescriptionChange={
          projectsController.setNewProjectDescription
        }
        onNewProjectStatusChange={projectsController.setNewProjectStatus}
        onNewProjectOrganizationIdChange={
          projectsController.setNewProjectOrganizationId
        }
        onCreateProject={projectsController.handleCreateProject}
        onUpdateProjectEdit={projectsController.updateProjectEdit}
        onUpdateProject={projectsController.handleUpdateProject}
        onProjectMembershipProjectIdChange={
          projectsController.setProjectMembershipProjectId
        }
        onProjectMembershipUserIdChange={
          projectsController.setProjectMembershipUserId
        }
        onProjectMembershipGroupIdChange={
          projectsController.setProjectMembershipGroupId
        }
        onUpdateProjectUserMembership={
          projectsController.updateProjectUserMembership
        }
        onUpdateProjectGroupMembership={
          projectsController.updateProjectGroupMembership
        }
        onUploadDocument={projectsController.handleUploadDocument}
        onDownloadDocument={projectsController.handleDownloadDocument}
        onArchiveDocument={projectsController.handleArchiveDocument}
        onIncludeArchivedDocumentsChange={
          projectsController.handleIncludeArchivedDocumentsChange
        }
      />
    </div>
  );
}
