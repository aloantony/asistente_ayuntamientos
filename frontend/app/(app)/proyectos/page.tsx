"use client";

import { useEffect, useState } from "react";
import { ProjectsPanel } from "../../components/ProjectsPanel";
import type {
  Group,
  OrganizationSummary,
  User,
} from "../../components/types";
import { userHasPermission } from "../../components/types";
import {
  fetchAdminGroups,
  fetchAdminUsers,
  fetchOrganizations,
} from "../../lib/fetchers";
import { shouldShowProjectsPanel, useSession } from "../../lib/session";
import { useProjectsController } from "../../lib/useProjectsController";

export default function ProyectosPage() {
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

  useEffect(() => {
    if (!user || !shouldShowProjectsPanel(user)) {
      return;
    }

    void projectsController.loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  useEffect(() => {
    if (!user || !shouldShowProjectsPanel(user)) {
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

    // The membership widget is permission-gated, so users without the
    // permission simply keep the empty lists.
    if (userHasPermission(user, "projects.manage_members")) {
      fetchAdminUsers()
        .then((usersData) => {
          if (isActive) {
            setAdminUsers(usersData);
          }
        })
        .catch(() => {
          // The membership selector stays empty if users cannot load.
        });

      fetchAdminGroups()
        .then((groupsData) => {
          if (isActive) {
            setGroups(groupsData);
          }
        })
        .catch(() => {
          // The membership selector stays empty if groups cannot load.
        });
    }

    return () => {
      isActive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, user?.permissions]);

  if (!user) {
    return null;
  }

  if (!shouldShowProjectsPanel(user)) {
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
        projects={projectsController.projects}
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
