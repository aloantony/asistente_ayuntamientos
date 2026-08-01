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

export type OrdinanceCurationStatus =
  | "approved"
  | "pending_review"
  | "needs_changes"
  | "rejected";

export type OfficialLegalSourceType =
  | "boe"
  | "bop"
  | "autonomic"
  | "municipal"
  | "other";

export type OfficialLegalSourceStatus = "active" | "archived";

export type OrdinanceImportJobStatus =
  | "draft"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type OrdinanceImportItemStatus =
  | "discovered"
  | "fetching"
  | "extracted"
  | "pending_review"
  | "approved"
  | "rejected"
  | "duplicate"
  | "failed";

export type OrdinanceReviewDecision = "approve" | "needs_changes" | "reject";

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
  population_reference_year: number | null;
  population_source_url: string | null;
  population_source_sha256: string | null;
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
  curation_status: OrdinanceCurationStatus;
  import_job_id: number | null;
  source_hash: string | null;
  extraction_status: string;
  confidence_score: number | null;
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

export type GeoEntityType = "requirement" | "project" | "asset";
export type GeoLocationRole = "primary" | "affected_area" | "reference";
export type GeoReviewStatus = "draft" | "proposed" | "reviewed" | "rejected";
export type AssetTaxonomyStatus = "active" | "archived";
export type AssetStatus = "active" | "inactive" | "retired" | "archived";
export type AssetConditionStatus = "good" | "fair" | "poor" | "unknown";

export type MunicipalAssetCategory = {
  id: number;
  organization_id: number;
  code: string;
  name: string;
  description: string | null;
  color: string | null;
  sort_order: number;
  status: AssetTaxonomyStatus;
  created_by_id: number | null;
  updated_by_id: number | null;
  created_at: string;
  updated_at: string;
};

export type MunicipalAssetType = {
  id: number;
  organization_id: number;
  category_id: number;
  code: string;
  name: string;
  description: string | null;
  sort_order: number;
  status: AssetTaxonomyStatus;
  created_by_id: number | null;
  updated_by_id: number | null;
  created_at: string;
  updated_at: string;
  category: MunicipalAssetCategory;
};

export type GeoLocation = {
  id: number;
  organization_id: number | null;
  municipality_id: number | null;
  label: string;
  geometry_type: "point" | "line" | "polygon";
  geometry_json: string;
  latitude: number | null;
  longitude: number | null;
  address_text: string | null;
  place_name: string | null;
  cadastral_reference: string | null;
  source: string;
  confidence: number | null;
  review_status: GeoReviewStatus;
  created_at: string;
  updated_at: string;
};

export type MunicipalAsset = {
  id: number;
  organization_id: number;
  municipality_id: number;
  asset_type_id: number;
  location_id: number | null;
  code: string | null;
  name: string;
  description: string | null;
  status: AssetStatus;
  condition_status: AssetConditionStatus;
  material: string | null;
  dimensions: string | null;
  installed_on: string | null;
  last_inspected_on: string | null;
  notes: string | null;
  created_by_id: number | null;
  updated_by_id: number | null;
  created_at: string;
  updated_at: string;
  asset_type: MunicipalAssetType;
  location: GeoLocation | null;
};

export type MaintenanceOrderStatus =
  | "planned"
  | "scheduled"
  | "in_progress"
  | "completed"
  | "cancelled";

export type MaintenanceOrderPriority = "low" | "normal" | "high" | "urgent";

export type MaintenanceType =
  | "preventive"
  | "corrective"
  | "inspection"
  | "cleaning"
  | "other";

export type MaintenanceEventType = "created" | "updated" | "transition";

export type MaintenanceAssetSummary = {
  id: number;
  code: string | null;
  name: string;
  status: AssetStatus;
};

export type MaintenanceAssigneeSummary = {
  id: number;
  full_name: string;
};

export type MaintenanceOrder = {
  id: number;
  organization_id: number;
  municipality_id: number;
  asset_id: number;
  title: string;
  description: string | null;
  maintenance_type: MaintenanceType;
  priority: MaintenanceOrderPriority;
  status: MaintenanceOrderStatus;
  scheduled_for: string | null;
  estimated_minutes: number | null;
  assigned_to_id: number | null;
  created_by_id: number | null;
  updated_by_id: number | null;
  created_at: string;
  updated_at: string;
  asset: MaintenanceAssetSummary;
  assigned_to: MaintenanceAssigneeSummary | null;
};

export type MaintenanceOrderEvent = {
  id: number;
  order_id: number;
  organization_id: number;
  event_type: MaintenanceEventType;
  from_status: MaintenanceOrderStatus | null;
  to_status: MaintenanceOrderStatus | null;
  changed_fields: string[];
  note: string | null;
  actor_id: number | null;
  created_at: string;
};

export type MaintenanceOrderDetail = MaintenanceOrder & {
  events: MaintenanceOrderEvent[];
};

export type MaintenanceOrderCreate = {
  asset_id: number;
  title: string;
  description?: string | null;
  maintenance_type?: MaintenanceType;
  priority?: MaintenanceOrderPriority;
  scheduled_for?: string | null;
  estimated_minutes?: number | null;
  assigned_to_id?: number | null;
};

export type MaintenanceOrderUpdate = Omit<
  Partial<MaintenanceOrderCreate>,
  "asset_id"
>;

export type MaintenanceOrderTransition = {
  status: MaintenanceOrderStatus;
  note?: string | null;
  scheduled_for?: string | null;
};

export type GeoMapItem = {
  entity_type: GeoEntityType;
  entity_id: number;
  role: GeoLocationRole;
  title: string;
  subtitle: string | null;
  status: string;
  priority: string | null;
  organization_id: number;
  organization_name: string;
  detail_path: string;
  location: GeoLocation;
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
  curation_status: OrdinanceCurationStatus;
  text_content: string;
  notes: string;
  legal_review_notes: string;
};

export type OfficialLegalSource = {
  id: number;
  name: string;
  base_url: string;
  domain: string;
  source_type: OfficialLegalSourceType;
  status: OfficialLegalSourceStatus;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type OrdinanceImportSourceInput = {
  url: string;
  municipality_id: number | null;
  official_source_id: number | null;
  title: string | null;
};

export type OrdinanceReviewChecklistItem = {
  key: string;
  label: string;
  passed: boolean;
  detail: string | null;
};

export type OrdinanceReviewReport = {
  id: number;
  ordinance_id: number;
  import_item_id: number | null;
  status: string;
  proposed_decision: OrdinanceReviewDecision;
  confidence_score: number;
  checklist: OrdinanceReviewChecklistItem[];
  summary: string | null;
  doubts: string | null;
  reviewed_by_agent: boolean;
  reviewed_by_id: number | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type OrdinanceImportItem = {
  id: number;
  job_id: number;
  municipality_id: number | null;
  official_source_id: number | null;
  ordinance_id: number | null;
  source_url: string;
  source_title: string | null;
  status: OrdinanceImportItemStatus;
  source_hash: string | null;
  extracted_metadata: Record<string, unknown> | null;
  confidence_score: number | null;
  error_message: string | null;
  ordinance: Ordinance | null;
  review_reports: OrdinanceReviewReport[];
  created_at: string;
  updated_at: string;
};

export type OrdinanceImportJob = {
  id: number;
  title: string;
  description: string | null;
  topic: string | null;
  subtopic: string | null;
  search_query: string | null;
  municipality_ids: number[];
  official_source_ids: number[];
  source_urls: OrdinanceImportSourceInput[];
  review_criteria: string;
  source_policy: "official_only";
  status: OrdinanceImportJobStatus;
  created_by_id: number | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  item_count: number;
  items?: OrdinanceImportItem[];
  created_at: string;
  updated_at: string;
};

export type OrdinanceComparisonEntry = {
  municipality_id: number;
  municipality_name: string;
  ordinance_id: number;
  title: string;
  topic: string;
  subtopic: string | null;
  status: OrdinanceStatus;
  curation_status: OrdinanceCurationStatus;
  approval_date: string | null;
  publication_date: string | null;
  effective_date: string | null;
  source_url: string | null;
  summary: string | null;
  confidence_score: number | null;
};

export type OrdinanceComparisonMunicipality = {
  id: number;
  name: string;
  province: string;
};

export type OrdinanceComparisonRow = {
  topic: string;
  subtopic: string | null;
  entries: OrdinanceComparisonEntry[];
};

export type OrdinanceComparison = {
  municipality_ids: number[];
  municipalities: OrdinanceComparisonMunicipality[];
  include_pending: boolean;
  include_inactive: boolean;
  rows: OrdinanceComparisonRow[];
};

export type OrdinanceResultScope =
  | "fragments"
  | "ordinances"
  | "municipalities";

export type OrdinanceSemanticSearchResult = {
  chunk_id: number;
  ordinance_id: number;
  title: string;
  municipality_id: number;
  municipality_name: string;
  province: string;
  population: number | null;
  topic: string;
  status: OrdinanceStatus;
  curation_status: OrdinanceCurationStatus;
  approval_date: string | null;
  publication_date: string | null;
  effective_date: string | null;
  chunk_index: number;
  heading: string | null;
  citation: string | null;
  source_locator: string | null;
  text: string;
  text_truncated: boolean;
  source_url: string | null;
  score: number;
};

export type OrdinancePopulationFilter = {
  applied: boolean;
  gte: number | null;
  lt: number | null;
  eligible_municipalities: number;
  municipalities_with_population: number;
  municipalities_without_population: number;
  coverage_complete: boolean;
};

export type OrdinanceLegalStatusFilter = {
  include_inactive: boolean;
  excluded_statuses: OrdinanceStatus[];
};

export type OrdinanceSearchPage = {
  query: string;
  result_scope: OrdinanceResultScope;
  limit: number;
  offset: number;
  returned: number;
  total_matches: number;
  has_more: boolean;
  next_offset: number | null;
  corpus_scan_complete: boolean;
  search_backend: "pgvector" | "python";
  topic_filter_mode: "none" | "preference" | "strict";
  legal_status_filter: OrdinanceLegalStatusFilter;
  population_filter: OrdinancePopulationFilter;
  eligible_chunks: number;
  results: OrdinanceSemanticSearchResult[];
};

export type OrdinanceLegalChunk = {
  id: number;
  ordinance_id: number;
  import_item_id: number | null;
  chunk_index: number;
  heading: string | null;
  citation: string | null;
  text: string;
  source_url: string | null;
  source_locator: string | null;
  review_status: "pending_review" | "approved" | "rejected";
  embedding_model: string | null;
  embedding_status: "pending" | "ready" | "failed" | "disabled";
  embedded_at: string | null;
  created_at: string;
  updated_at: string;
};

export type TelegramLinkStatus = {
  linked: boolean;
  status: string | null;
  telegram_username: string | null;
  linked_at: string | null;
  revoked_at: string | null;
};

export type TelegramLinkCode = {
  code: string;
  expires_at: string;
  ttl_seconds: number;
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

export const ORDINANCE_CURATION_STATUSES: OrdinanceCurationStatus[] = [
  "approved",
  "pending_review",
  "needs_changes",
  "rejected",
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
  unknown: "Vigencia desconocida",
  archived: "Archivada",
};

const ORDINANCE_CURATION_STATUS_LABELS: Record<
  OrdinanceCurationStatus,
  string
> = {
  approved: "Aprobada",
  pending_review: "Pendiente de revisión",
  needs_changes: "Necesita cambios",
  rejected: "Rechazada",
};

const ORDINANCE_IMPORT_JOB_STATUS_LABELS: Record<
  OrdinanceImportJobStatus,
  string
> = {
  draft: "Borrador",
  queued: "En cola",
  running: "En ejecución",
  completed: "Completado",
  failed: "Fallido",
  cancelled: "Cancelado",
};

const ORDINANCE_IMPORT_ITEM_STATUS_LABELS: Record<
  OrdinanceImportItemStatus,
  string
> = {
  discovered: "Descubierta",
  fetching: "Descargando",
  extracted: "Extraída",
  pending_review: "Pendiente de revisión",
  approved: "Aprobada",
  rejected: "Rechazada",
  duplicate: "Duplicada",
  failed: "Fallida",
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

export function formatOrdinanceCurationStatus(
  status: OrdinanceCurationStatus,
) {
  return ORDINANCE_CURATION_STATUS_LABELS[status];
}

export function formatOrdinanceImportJobStatus(
  status: OrdinanceImportJobStatus,
) {
  return ORDINANCE_IMPORT_JOB_STATUS_LABELS[status];
}

export function formatOrdinanceImportItemStatus(
  status: OrdinanceImportItemStatus,
) {
  return ORDINANCE_IMPORT_ITEM_STATUS_LABELS[status];
}

export function formatMunicipalityType(municipalityType: MunicipalityType) {
  return MUNICIPALITY_TYPE_LABELS[municipalityType];
}

export function formatRuralUrbanProfile(profile: RuralUrbanProfile) {
  return RURAL_URBAN_PROFILE_LABELS[profile];
}

export type AssistantStatus = {
  enabled: boolean;
  runtime: string;
  model: string;
  runtime_healthy: boolean | null;
  speech_transcription_enabled: boolean;
  speech_synthesis_enabled: boolean;
  speech_synthesis_max_chars: number;
  realtime_voice_enabled: boolean;
  realtime_voice_provider: "openai" | null;
  realtime_voice_model: string | null;
  tools: AssistantTool[];
};

export type AssistantTool = {
  name: string;
  label: string;
  read_only: boolean;
  domain: string;
  required_permission: string | null;
};

export type AssistantMemoryCategory =
  | "protocol"
  | "preference"
  | "context"
  | "decision"
  | "open_question";

export type AssistantMemoryStatus =
  | "proposed"
  | "approved"
  | "rejected"
  | "archived"
  | "blocked";

export type AssistantMemorySensitivity =
  | "normal"
  | "personal"
  | "sensitive"
  | "legal";

export type AssistantMemoryUser = {
  id: number;
  email: string;
  full_name: string;
};

export type AssistantMemoryEntry = {
  id: number;
  organization_id: number;
  organization: OrganizationSummary;
  category: AssistantMemoryCategory;
  content: string;
  status: AssistantMemoryStatus;
  sensitivity: AssistantMemorySensitivity;
  source_conversation_id: number | null;
  source_message_id: number | null;
  proposed_by_id: number | null;
  reviewed_by_id: number | null;
  review_notes: string | null;
  reviewed_at: string | null;
  proposed_by: AssistantMemoryUser | null;
  reviewed_by: AssistantMemoryUser | null;
  created_at: string;
  updated_at: string;
};

export type AssistantAdminFeedbackCategory =
  | "bug"
  | "improvement"
  | "missing_capability"
  | "data_issue"
  | "ux"
  | "other";

export type AssistantAdminFeedbackStatus =
  | "submitted"
  | "reviewed"
  | "dismissed"
  | "archived";

export type AssistantAdminFeedbackPriority =
  | "low"
  | "medium"
  | "high"
  | "urgent";

export type AssistantAdminFeedback = {
  id: number;
  organization_id: number | null;
  organization: OrganizationSummary | null;
  category: AssistantAdminFeedbackCategory;
  title: string;
  description: string;
  priority: AssistantAdminFeedbackPriority;
  status: AssistantAdminFeedbackStatus;
  source_conversation_id: number | null;
  source_message_id: number | null;
  submitted_by_id: number | null;
  reviewed_by_id: number | null;
  review_notes: string | null;
  reviewed_at: string | null;
  submitted_by: AssistantMemoryUser | null;
  reviewed_by: AssistantMemoryUser | null;
  created_at: string;
  updated_at: string;
};

export type AssistantAction = {
  call_id?: string;
  tool: string;
  ok: boolean;
  input: Record<string, unknown>;
  result: string;
  status?: "started" | "finished";
};

export type AssistantMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  actions: AssistantAction[];
  agent_key: string | null;
  routing: Record<string, unknown> | null;
  created_at: string;
};

export type AssistantConversation = {
  id: number;
  title: string;
  status: "active" | "archived";
  folder_id: number | null;
  created_at: string;
  updated_at: string;
};

export type AssistantConversationDetail = AssistantConversation & {
  messages: AssistantMessage[];
};

export type AssistantStreamMessageStart = {
  conversation_id: number;
  user_message_id: number;
};

export type AssistantStreamToolActivity = {
  tool: string;
  status: "started" | "finished";
  input: Record<string, unknown>;
  ok?: boolean;
  result?: string;
};

export type AssistantStreamDone = {
  message: AssistantMessage;
  conversation: AssistantConversation;
};

export type AssistantVoiceState =
  | "idle"
  | "connecting"
  | "listening"
  | "user_speaking"
  | "transcribing"
  | "thinking"
  | "tool_running"
  | "responding"
  | "done"
  | "interrupted"
  | "error";

export type AssistantStreamVoiceState = {
  state: AssistantVoiceState;
};

export type AssistantStreamTranscriptFinal = {
  text: string;
};

export type AssistantRealtimeSession = {
  client_secret: string;
  client_secret_expires_at: number | null;
  provider: "openai";
  model: string;
  voice: string;
  realtime_url: string;
};

export type AssistantRealtimeTurnStartRequest = {
  turn_id: string;
  user_text: string;
};

export type AssistantRealtimeTurnStartResult = {
  turn_id: string;
  user_message: AssistantMessage;
  replayed: boolean;
};

export type AssistantRealtimeToolCallRequest = {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
};

export type AssistantRealtimeToolCallResult = {
  call_id: string;
  ok: boolean;
  output: string;
  action: AssistantAction;
  user_message: AssistantMessage;
  confirmation_prompt: string | null;
  replayed: boolean;
};

export type AssistantRealtimeResponseStatus =
  | "completed"
  | "cancelled"
  | "failed"
  | "incomplete";

export type AssistantRealtimeTurnCompleteRequest = {
  response_id: string;
  response_status: AssistantRealtimeResponseStatus;
  assistant_text?: string | null;
  interrupted: boolean;
};

export type AssistantRealtimeTurnResult = {
  conversation: AssistantConversation;
  user_message: AssistantMessage | null;
  assistant_message: AssistantMessage | null;
  confirmation_prompt: string | null;
  confirmation_delivery_required: boolean;
  replayed: boolean;
};

export type AssistantConversationFolder = {
  id: number;
  name: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
};

export const ASSISTANT_TOOL_LABELS: Record<string, string> = {
  list_organizations: "Consultar organizaciones",
  list_projects: "Consultar proyectos",
  get_map_items: "Consultar mapa",
  list_requirements: "Consultar necesidades",
  get_requirement: "Leer necesidad",
  create_requirement: "Crear necesidad",
  update_requirement: "Actualizar necesidad",
  add_requirement_message: "Añadir nota a necesidad",
  propose_memory_entry: "Proponer memoria",
  propose_transversal_feature: "Proponer funcionalidad transversal",
  list_available_transversal_features: "Consultar funcionalidades disponibles",
  record_transversal_feature_acceptance: "Registrar activación transversal",
  web_search: "Buscar en web",
};

export function formatAssistantTool(
  tool: string,
  toolLabels?: Record<string, string>,
) {
  return toolLabels?.[tool] ?? ASSISTANT_TOOL_LABELS[tool] ?? tool;
}

export const ASSISTANT_MEMORY_CATEGORY_LABELS: Record<
  AssistantMemoryCategory,
  string
> = {
  protocol: "Protocolo",
  preference: "Preferencia",
  context: "Contexto",
  decision: "Decisión",
  open_question: "Duda abierta",
};

export const ASSISTANT_MEMORY_SENSITIVITY_LABELS: Record<
  AssistantMemorySensitivity,
  string
> = {
  normal: "Normal",
  personal: "Personal",
  sensitive: "Sensible",
  legal: "Legal",
};

export const ASSISTANT_MEMORY_STATUSES: AssistantMemoryStatus[] = [
  "proposed",
  "approved",
  "rejected",
  "blocked",
  "archived",
];

export const ASSISTANT_MEMORY_CATEGORIES: AssistantMemoryCategory[] = [
  "protocol",
  "preference",
  "context",
  "decision",
  "open_question",
];

export const ASSISTANT_MEMORY_SENSITIVITIES: AssistantMemorySensitivity[] = [
  "normal",
  "personal",
  "sensitive",
  "legal",
];

export const ASSISTANT_MEMORY_STATUS_LABELS: Record<
  AssistantMemoryStatus,
  string
> = {
  proposed: "Propuesta",
  approved: "Aprobada",
  rejected: "Rechazada",
  blocked: "Bloqueada",
  archived: "Archivada",
};

export const ASSISTANT_ADMIN_FEEDBACK_STATUSES: AssistantAdminFeedbackStatus[] = [
  "submitted",
  "reviewed",
  "dismissed",
  "archived",
];

export const ASSISTANT_ADMIN_FEEDBACK_PRIORITIES: AssistantAdminFeedbackPriority[] = [
  "low",
  "medium",
  "high",
  "urgent",
];

export const ASSISTANT_ADMIN_FEEDBACK_CATEGORY_LABELS: Record<
  AssistantAdminFeedbackCategory,
  string
> = {
  bug: "Error",
  improvement: "Mejora",
  missing_capability: "Capacidad ausente",
  data_issue: "Problema de datos",
  ux: "Experiencia de uso",
  other: "Otro",
};

export const ASSISTANT_ADMIN_FEEDBACK_STATUS_LABELS: Record<
  AssistantAdminFeedbackStatus,
  string
> = {
  submitted: "Pendiente",
  reviewed: "Revisado",
  dismissed: "Descartado",
  archived: "Archivado",
};

export const ASSISTANT_ADMIN_FEEDBACK_PRIORITY_LABELS: Record<
  AssistantAdminFeedbackPriority,
  string
> = {
  low: "Baja",
  medium: "Media",
  high: "Alta",
  urgent: "Urgente",
};

export function formatAssistantMemoryStatus(status: AssistantMemoryStatus) {
  return ASSISTANT_MEMORY_STATUS_LABELS[status];
}

export function formatAssistantAdminFeedbackCategory(
  category: AssistantAdminFeedbackCategory,
) {
  return ASSISTANT_ADMIN_FEEDBACK_CATEGORY_LABELS[category];
}

export function formatAssistantAdminFeedbackStatus(
  status: AssistantAdminFeedbackStatus,
) {
  return ASSISTANT_ADMIN_FEEDBACK_STATUS_LABELS[status];
}

export function formatAssistantAdminFeedbackPriority(
  priority: AssistantAdminFeedbackPriority,
) {
  return ASSISTANT_ADMIN_FEEDBACK_PRIORITY_LABELS[priority];
}

export function userHasPermission(user: User, permissionCode: string) {
  return (
    user.is_superuser || (user.permissions ?? []).includes(permissionCode)
  );
}

// Ayuntamiento: perfil del municipio y navegación configurable de su barra.
// Los apartados y elementos son bloques del mismo árbol genérico (ver
// ADR-030), por eso comparten forma.
export type TownHallProfile = {
  display_name: string | null;
  weather_enabled: boolean;
  weather_location: string | null;
  has_shield: boolean;
};

export type TownHallWeather = {
  temperature_celsius: number;
  location: string;
};

export type TownHallNavItem = {
  id: number;
  title: string;
  position: number;
};

export type TownHallNavSection = TownHallNavItem & {
  items: TownHallNavItem[];
};

export type TownHall = {
  organization_id: number;
  organization_name: string;
  profile: TownHallProfile;
  nav: TownHallNavSection[];
};

export type TownHallProfileUpdate = {
  display_name?: string | null;
  weather_enabled?: boolean;
  weather_location?: string | null;
};

export type TownHallBlockCreate = {
  block_type: "nav_section" | "nav_item" | "item";
  parent_id?: number | null;
  title: string;
};

export type TownHallBlockUpdate = {
  title?: string;
  body?: string | null;
  layout?: TownHallSectionLayout;
  fields?: TownHallContentField[];
  status?: "active" | "archived";
};

export type TownHallBlock = {
  id: number;
  organization_id: number;
  parent_id: number | null;
  block_type: string;
  title: string;
  position: number;
  status: string;
};

export type TownHallBlockPlacement = {
  id: number;
  parent_id?: number | null;
  position: number;
};

// Contenido de un apartado: la lista de elementos con su cuerpo (Fase B1 del
// prototipo, ver docs/diseno-ayuntamiento-prototipo.md).
// Cómo se presenta un apartado: prosa por defecto, o listas de nombre y
// número como los teléfonos del prototipo. En ambos el elemento guarda la
// etiqueta en `title` y el valor en `body`.
export type TownHallSectionLayout = "text" | "contacts" | "people";

export type TownHallContentField = {
  label: string;
  value: string;
};

export type TownHallContentItem = {
  id: number;
  title: string;
  body: string | null;
  position: number;
  fields: TownHallContentField[];
};

export type TownHallContent = {
  block_id: number;
  title: string;
  parent_title: string | null;
  layout: TownHallSectionLayout;
  items: TownHallContentItem[];
};
