export type UserGroupSummary = {
  id: number;
  name: string;
  organization?: OrganizationSummary;
};

export type RoleSummary = {
  id: number;
  name: string;
};

export type GroupUserSummary = {
  id: number;
  email: string;
  full_name: string;
};

export type OrganizationStatus = "active" | "paused" | "archived";

export type OrganizationSummary = {
  id: number;
  name: string;
  status: OrganizationStatus;
};

export type OrganizationUserSummary = {
  id: number;
  email: string;
  full_name: string;
  is_active: boolean;
  is_superuser: boolean;
};

export type Organization = OrganizationSummary & {
  description: string | null;
  users: OrganizationUserSummary[];
  created_at: string;
  updated_at: string;
};

export type User = {
  id: number;
  email: string;
  full_name: string;
  is_active: boolean;
  is_superuser: boolean;
  permissions?: string[];
  organizations?: OrganizationSummary[];
  groups?: UserGroupSummary[];
};

export type Group = {
  id: number;
  name: string;
  description: string | null;
  organization_id: number;
  organization: OrganizationSummary;
  users?: GroupUserSummary[];
  roles?: RoleSummary[];
};

export type Permission = {
  id: number;
  code: string;
  description: string | null;
  created_at: string;
  updated_at: string;
};

export type Role = {
  id: number;
  name: string;
  description: string | null;
  permissions: Permission[];
  created_at: string;
  updated_at: string;
};

export type ProjectStatus = "active" | "paused" | "completed" | "archived";

export type DocumentStatus = "active" | "archived";

export type Project = {
  id: number;
  name: string;
  description: string | null;
  status: ProjectStatus;
  organization_id: number;
  organization: OrganizationSummary;
  users: GroupUserSummary[];
  groups: UserGroupSummary[];
  created_at: string;
  updated_at: string;
};

export type Document = {
  id: number;
  organization_id: number;
  project_id: number;
  original_filename: string;
  stored_filename: string;
  storage_backend: string;
  storage_key: string;
  content_type: string;
  size_bytes: number;
  checksum_sha256: string;
  status: DocumentStatus;
  uploaded_by_id: number | null;
  created_at: string;
  updated_at: string;
};

export type LoginResponse = {
  access_token: string;
  token_type: string;
};

export type MembershipResponse = {
  group_id: number;
  user_id: number;
  detail: string;
};

export type UserDeleteResponse = {
  user_id: number;
  detail: string;
};

export type GroupDeleteResponse = {
  group_id: number;
  detail: string;
};

export type UserEditState = {
  full_name: string;
  is_active: boolean;
  is_superuser: boolean;
};

export type GroupEditState = {
  name: string;
  description: string;
  organization_id: number;
};

export type OrganizationEditState = {
  name: string;
  description: string;
  status: OrganizationStatus;
};

export type RoleEditState = {
  name: string;
  description: string;
};

export type ProjectEditState = {
  name: string;
  description: string;
  status: ProjectStatus;
};

export type MembershipAction = "add" | "remove";

export type RoleDeleteResponse = {
  role_id: number;
  detail: string;
};

export type RolePermissionResponse = {
  role_id: number;
  permission_id: number;
  detail: string;
};

export type GroupRoleResponse = {
  group_id: number;
  role_id: number;
  detail: string;
};

export type PermissionBootstrapResponse = {
  created_codes: string[];
  permissions: Permission[];
  detail: string;
};

export type OrganizationMembershipResponse = {
  organization_id: number;
  user_id: number;
  detail: string;
};

export const PROJECT_STATUSES: ProjectStatus[] = [
  "active",
  "paused",
  "completed",
  "archived",
];

export const DOCUMENT_STATUSES: DocumentStatus[] = ["active", "archived"];

export const ORGANIZATION_STATUSES: OrganizationStatus[] = [
  "active",
  "paused",
  "archived",
];

const PROJECT_STATUS_LABELS: Record<ProjectStatus, string> = {
  active: "Activo",
  paused: "Pausado",
  completed: "Completado",
  archived: "Archivado",
};

const DOCUMENT_STATUS_LABELS: Record<DocumentStatus, string> = {
  active: "Activo",
  archived: "Archivado",
};

const ORGANIZATION_STATUS_LABELS: Record<OrganizationStatus, string> = {
  active: "Activo",
  paused: "Pausado",
  archived: "Archivado",
};

export function formatUserOption(adminUser: User) {
  return `${adminUser.full_name} (${adminUser.email})`;
}

export function formatOrganizationOption(organization: OrganizationSummary) {
  return `${organization.name} (${formatOrganizationStatus(organization.status)})`;
}

export function formatProjectStatus(status: ProjectStatus) {
  return PROJECT_STATUS_LABELS[status];
}

export function formatDocumentStatus(status: DocumentStatus) {
  return DOCUMENT_STATUS_LABELS[status];
}

export function formatFileSize(sizeBytes: number) {
  if (sizeBytes < 1024) {
    return `${sizeBytes} B`;
  }

  if (sizeBytes < 1024 * 1024) {
    return `${(sizeBytes / 1024).toFixed(1)} KB`;
  }

  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatOrganizationStatus(status: OrganizationStatus) {
  return ORGANIZATION_STATUS_LABELS[status];
}

export function userHasPermission(user: User, permissionCode: string) {
  return (
    user.is_superuser || (user.permissions ?? []).includes(permissionCode)
  );
}
