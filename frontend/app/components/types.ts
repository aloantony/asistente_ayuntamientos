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

export type MunicipalityStatus = "active" | "archived";

export type OrdinanceType =
  | "ordinance"
  | "regulation"
  | "bylaw"
  | "tax_ordinance"
  | "urban_planning"
  | "other";

export type OrdinanceStatus =
  | "active"
  | "repealed"
  | "partially_repealed"
  | "superseded"
  | "unknown"
  | "archived";

export type MunicipalityType =
  | "municipality"
  | "minor_local_entity"
  | "district"
  | "other";

export type RuralUrbanProfile =
  | "rural"
  | "semi_rural"
  | "urban"
  | "mixed"
  | "unknown";

export type MunicipalitySummary = {
  id: number;
  name: string;
  province: string;
  autonomous_community: string;
};

export type Municipality = MunicipalitySummary & {
  country: string;
  ine_code: string | null;
  population: number | null;
  surface_km2: number | null;
  density: number | null;
  postal_codes: string | null;
  municipality_type: MunicipalityType;
  rural_urban_profile: RuralUrbanProfile;
  economic_profile: string | null;
  tourism_profile: string | null;
  geographic_notes: string | null;
  administrative_notes: string | null;
  status: MunicipalityStatus;
  created_at: string;
  updated_at: string;
};

export type OrdinanceDocumentSummary = {
  id: number;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  status: DocumentStatus;
};

export type Ordinance = {
  id: number;
  municipality_id: number;
  document_id: number | null;
  title: string;
  topic: string;
  subtopic: string | null;
  ordinance_type: OrdinanceType;
  summary: string | null;
  source_url: string | null;
  official_bulletin: string | null;
  bulletin_number: string | null;
  approval_date: string | null;
  publication_date: string | null;
  effective_date: string | null;
  status: OrdinanceStatus;
  // Only present in the GET /ordinances/{id} detail; list items omit it.
  text_content?: string | null;
  notes: string | null;
  legal_review_notes: string | null;
  created_by_id: number | null;
  updated_by_id: number | null;
  municipality: MunicipalitySummary;
  document?: OrdinanceDocumentSummary | null;
  created_at: string;
  updated_at: string;
};

export type OrganizationSummary = {
  id: number;
  name: string;
  status: OrganizationStatus;
  municipality_id?: number | null;
  municipality?: MunicipalitySummary | null;
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
  municipality_id: number | null;
  municipality: MunicipalitySummary | null;
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

export type RequirementPriority = "low" | "medium" | "high" | "urgent";

export type RequirementStatus =
  | "draft"
  | "submitted"
  | "in_review"
  | "needs_clarification"
  | "accepted"
  | "rejected"
  | "converted"
  | "archived";

export type RequirementSourceType =
  | "manual"
  | "conversation"
  | "phone_call"
  | "meeting"
  | "other";

export type RequirementMessageType =
  | "note"
  | "question"
  | "answer"
  | "clarification"
  | "decision";

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

export type RequirementOrganizationSummary = {
  id: number;
  name: string;
};

export type RequirementProjectSummary = {
  id: number;
  name: string;
};

export type RequirementUserSummary = {
  id: number;
  email: string;
  full_name: string;
};

export type Requirement = {
  id: number;
  organization_id: number;
  project_id: number | null;
  title: string;
  summary: string | null;
  problem: string | null;
  current_process: string | null;
  desired_process: string | null;
  affected_users: string | null;
  involved_documents: string | null;
  data_sensitivity_notes: string | null;
  legal_notes: string | null;
  acceptance_criteria: string | null;
  open_questions: string | null;
  priority: RequirementPriority;
  status: RequirementStatus;
  source_type: RequirementSourceType;
  created_by_id: number | null;
  reviewed_by_id: number | null;
  organization: RequirementOrganizationSummary;
  project: RequirementProjectSummary | null;
  created_by: RequirementUserSummary | null;
  reviewed_by: RequirementUserSummary | null;
  created_at: string;
  updated_at: string;
};

export type RequirementMessage = {
  id: number;
  requirement_id: number;
  author_id: number | null;
  body: string;
  message_type: RequirementMessageType;
  author: RequirementUserSummary | null;
  created_at: string;
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
  municipality_id: string;
  status: OrganizationStatus;
};

export type MunicipalityEditState = {
  name: string;
  province: string;
  autonomous_community: string;
  country: string;
  ine_code: string;
  population: string;
  surface_km2: string;
  density: string;
  postal_codes: string;
  municipality_type: MunicipalityType;
  rural_urban_profile: RuralUrbanProfile;
  economic_profile: string;
  tourism_profile: string;
  geographic_notes: string;
  administrative_notes: string;
  status: MunicipalityStatus;
};

export type OrdinanceEditState = {
  municipality_id: string;
  document_id: string;
  title: string;
  topic: string;
  subtopic: string;
  ordinance_type: OrdinanceType;
  summary: string;
  source_url: string;
  official_bulletin: string;
  bulletin_number: string;
  approval_date: string;
  publication_date: string;
  effective_date: string;
  status: OrdinanceStatus;
  text_content: string;
  notes: string;
  legal_review_notes: string;
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

export type RequirementFormState = {
  organization_id: string;
  project_id: string;
  title: string;
  summary: string;
  problem: string;
  current_process: string;
  desired_process: string;
  affected_users: string;
  involved_documents: string;
  data_sensitivity_notes: string;
  legal_notes: string;
  acceptance_criteria: string;
  open_questions: string;
  priority: RequirementPriority;
  source_type: RequirementSourceType;
};

export type RequirementEditState = Omit<
  RequirementFormState,
  "organization_id"
> & {
  status: RequirementStatus;
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

export const REQUIREMENT_PRIORITIES: RequirementPriority[] = [
  "low",
  "medium",
  "high",
  "urgent",
];

export const REQUIREMENT_STATUSES: RequirementStatus[] = [
  "draft",
  "submitted",
  "in_review",
  "needs_clarification",
  "accepted",
  "rejected",
  "converted",
  "archived",
];

export const REQUIREMENT_SOURCE_TYPES: RequirementSourceType[] = [
  "manual",
  "conversation",
  "phone_call",
  "meeting",
  "other",
];

export const REQUIREMENT_MESSAGE_TYPES: RequirementMessageType[] = [
  "note",
  "question",
  "answer",
  "clarification",
  "decision",
];

export const ORGANIZATION_STATUSES: OrganizationStatus[] = [
  "active",
  "paused",
  "archived",
];

export const MUNICIPALITY_STATUSES: MunicipalityStatus[] = [
  "active",
  "archived",
];

export const ORDINANCE_TYPES: OrdinanceType[] = [
  "ordinance",
  "regulation",
  "bylaw",
  "tax_ordinance",
  "urban_planning",
  "other",
];

export const ORDINANCE_STATUSES: OrdinanceStatus[] = [
  "active",
  "repealed",
  "partially_repealed",
  "superseded",
  "unknown",
  "archived",
];

export const MUNICIPALITY_TYPES: MunicipalityType[] = [
  "municipality",
  "minor_local_entity",
  "district",
  "other",
];

export const RURAL_URBAN_PROFILES: RuralUrbanProfile[] = [
  "rural",
  "semi_rural",
  "urban",
  "mixed",
  "unknown",
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

const REQUIREMENT_PRIORITY_LABELS: Record<RequirementPriority, string> = {
  low: "Baja",
  medium: "Media",
  high: "Alta",
  urgent: "Urgente",
};

const REQUIREMENT_STATUS_LABELS: Record<RequirementStatus, string> = {
  draft: "Borrador",
  submitted: "Pendiente",
  in_review: "En revisión",
  needs_clarification: "Necesita aclaración",
  accepted: "Aceptado",
  rejected: "Rechazado",
  converted: "Convertido",
  archived: "Archivado",
};

const REQUIREMENT_SOURCE_TYPE_LABELS: Record<RequirementSourceType, string> = {
  manual: "Manual",
  conversation: "Conversación",
  phone_call: "Llamada",
  meeting: "Reunión",
  other: "Otro",
};

const REQUIREMENT_MESSAGE_TYPE_LABELS: Record<RequirementMessageType, string> = {
  note: "Nota",
  question: "Pregunta",
  answer: "Respuesta",
  clarification: "Aclaración",
  decision: "Decisión",
};

const ORGANIZATION_STATUS_LABELS: Record<OrganizationStatus, string> = {
  active: "Activo",
  paused: "Pausado",
  archived: "Archivado",
};

const MUNICIPALITY_STATUS_LABELS: Record<MunicipalityStatus, string> = {
  active: "Activo",
  archived: "Archivado",
};

const ORDINANCE_TYPE_LABELS: Record<OrdinanceType, string> = {
  ordinance: "Ordenanza",
  regulation: "Reglamento",
  bylaw: "Bando",
  tax_ordinance: "Ordenanza fiscal",
  urban_planning: "Urbanismo",
  other: "Otra",
};

const ORDINANCE_STATUS_LABELS: Record<OrdinanceStatus, string> = {
  active: "Activa",
  repealed: "Derogada",
  partially_repealed: "Parcialmente derogada",
  superseded: "Sustituida",
  unknown: "Desconocida",
  archived: "Archivada",
};

const MUNICIPALITY_TYPE_LABELS: Record<MunicipalityType, string> = {
  municipality: "Municipio",
  minor_local_entity: "Entidad local menor",
  district: "Distrito",
  other: "Otro",
};

const RURAL_URBAN_PROFILE_LABELS: Record<RuralUrbanProfile, string> = {
  rural: "Rural",
  semi_rural: "Semirrural",
  urban: "Urbano",
  mixed: "Mixto",
  unknown: "Desconocido",
};

export function formatUserOption(adminUser: User) {
  return `${adminUser.full_name} (${adminUser.email})`;
}

export function formatOrganizationOption(organization: OrganizationSummary) {
  return `${organization.name} (${formatOrganizationStatus(organization.status)})`;
}

export function formatMunicipalityOption(municipality: MunicipalitySummary) {
  return `${municipality.name} - ${municipality.province}`;
}

export function formatProjectStatus(status: ProjectStatus) {
  return PROJECT_STATUS_LABELS[status];
}

export function formatDocumentStatus(status: DocumentStatus) {
  return DOCUMENT_STATUS_LABELS[status];
}

export function formatRequirementPriority(priority: RequirementPriority) {
  return REQUIREMENT_PRIORITY_LABELS[priority];
}

export function formatRequirementStatus(status: RequirementStatus) {
  return REQUIREMENT_STATUS_LABELS[status];
}

export function formatRequirementSourceType(sourceType: RequirementSourceType) {
  return REQUIREMENT_SOURCE_TYPE_LABELS[sourceType];
}

export function formatRequirementMessageType(
  messageType: RequirementMessageType,
) {
  return REQUIREMENT_MESSAGE_TYPE_LABELS[messageType];
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

export function formatMunicipalityStatus(status: MunicipalityStatus) {
  return MUNICIPALITY_STATUS_LABELS[status];
}

export function formatOrdinanceType(ordinanceType: OrdinanceType) {
  return ORDINANCE_TYPE_LABELS[ordinanceType];
}

export function formatOrdinanceStatus(status: OrdinanceStatus) {
  return ORDINANCE_STATUS_LABELS[status];
}

export function formatMunicipalityType(municipalityType: MunicipalityType) {
  return MUNICIPALITY_TYPE_LABELS[municipalityType];
}

export function formatRuralUrbanProfile(profile: RuralUrbanProfile) {
  return RURAL_URBAN_PROFILE_LABELS[profile];
}

export type AssistantStatus = {
  enabled: boolean;
  model: string;
};

export type AssistantAction = {
  tool: string;
  ok: boolean;
  input: Record<string, unknown>;
  result: string;
};

export type AssistantMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  actions: AssistantAction[];
  created_at: string;
};

export type AssistantConversation = {
  id: number;
  title: string;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
};

export type AssistantConversationDetail = AssistantConversation & {
  messages: AssistantMessage[];
};

export const ASSISTANT_TOOL_LABELS: Record<string, string> = {
  list_organizations: "Consultar organizaciones",
  list_projects: "Consultar proyectos",
  list_requirements: "Consultar requisitos",
  get_requirement: "Leer requisito",
  create_requirement: "Crear requisito",
  update_requirement: "Actualizar requisito",
  add_requirement_message: "Añadir nota a requisito",
};

export function formatAssistantTool(tool: string) {
  return ASSISTANT_TOOL_LABELS[tool] ?? tool;
}

export function userHasPermission(user: User, permissionCode: string) {
  return (
    user.is_superuser || (user.permissions ?? []).includes(permissionCode)
  );
}
