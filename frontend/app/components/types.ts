export type UserGroupSummary = {
  id: number;
  name: string;
};

export type GroupUserSummary = {
  id: number;
  email: string;
  full_name: string;
};

export type User = {
  id: number;
  email: string;
  full_name: string;
  is_active: boolean;
  is_superuser: boolean;
  groups?: UserGroupSummary[];
};

export type Group = {
  id: number;
  name: string;
  description: string | null;
  users?: GroupUserSummary[];
};

export type ProjectStatus = "active" | "paused" | "completed" | "archived";

export type Project = {
  id: number;
  name: string;
  description: string | null;
  status: ProjectStatus;
  users: GroupUserSummary[];
  groups: UserGroupSummary[];
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
};

export type ProjectEditState = {
  name: string;
  description: string;
  status: ProjectStatus;
};

export type MembershipAction = "add" | "remove";

export const PROJECT_STATUSES: ProjectStatus[] = [
  "active",
  "paused",
  "completed",
  "archived",
];

const PROJECT_STATUS_LABELS: Record<ProjectStatus, string> = {
  active: "Activo",
  paused: "Pausado",
  completed: "Completado",
  archived: "Archivado",
};

export function formatUserOption(adminUser: User) {
  return `${adminUser.full_name} (${adminUser.email})`;
}

export function formatProjectStatus(status: ProjectStatus) {
  return PROJECT_STATUS_LABELS[status];
}
