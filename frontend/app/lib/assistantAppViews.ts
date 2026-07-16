import type { AssistantAction, AssistantUiAction } from "../components/types";

type SharedAppView = {
  id: string;
  title: string;
};

export type AssistantMapAppView = SharedAppView & {
  surface: "map";
  context: {
    organizationId?: number;
    entityType?: "requirement" | "project";
    entityId?: number;
  };
};

export type AssistantRequirementsAppView = SharedAppView & {
  surface: "requirements";
  context: {
    organizationId?: number;
    projectId?: number;
    requirementId?: number;
  };
};

export type AssistantProjectsAppView = SharedAppView & {
  surface: "projects";
  context: {
    organizationId?: number;
    projectId?: number;
  };
};

export type AssistantAppView =
  | AssistantMapAppView
  | AssistantRequirementsAppView
  | AssistantProjectsAppView;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parsePositiveInteger(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : undefined;
}

function hasOnlyKeys(
  record: Record<string, unknown>,
  allowedKeys: ReadonlySet<string>,
) {
  return Object.keys(record).every((key) => allowedKeys.has(key));
}

function parseEnvelope(value: unknown): AssistantUiAction | null {
  const action = asRecord(value);
  const context = asRecord(action?.context);
  if (
    action?.type !== "ui.open_embedded" ||
    action.version !== 1 ||
    typeof action.id !== "string" ||
    !/^[A-Za-z0-9:_-]+$/.test(action.id) ||
    action.id.length < 1 ||
    action.id.length > 128 ||
    typeof action.surface !== "string" ||
    typeof action.title !== "string" ||
    [...action.title].length < 1 ||
    [...action.title].length > 255 ||
    context === null
  ) {
    return null;
  }
  return {
    type: action.type,
    version: action.version,
    id: action.id,
    surface: action.surface,
    title: action.title,
    context,
  };
}

export function parseAssistantAppView(value: unknown): AssistantAppView | null {
  const action = parseEnvelope(value);
  if (!action) {
    return null;
  }

  const organizationId = parsePositiveInteger(action.context.organization_id);
  if (
    action.context.organization_id !== undefined &&
    organizationId === undefined
  ) {
    return null;
  }

  if (action.surface === "map") {
    if (
      !hasOnlyKeys(
        action.context,
        new Set(["organization_id", "entity_type", "entity_id"]),
      )
    ) {
      return null;
    }
    const entityType =
      action.context.entity_type === "requirement" ||
      action.context.entity_type === "project"
        ? action.context.entity_type
        : undefined;
    const entityId = parsePositiveInteger(action.context.entity_id);
    if (
      (action.context.entity_type !== undefined && entityType === undefined) ||
      (action.context.entity_id !== undefined && entityId === undefined) ||
      (entityType === undefined) !== (entityId === undefined)
    ) {
      return null;
    }
    return {
      id: action.id,
      title: action.title,
      surface: "map",
      context: {
        ...(organizationId ? { organizationId } : {}),
        ...(entityType ? { entityType } : {}),
        ...(entityId ? { entityId } : {}),
      },
    };
  }

  if (action.surface === "requirements") {
    if (
      !hasOnlyKeys(
        action.context,
        new Set(["organization_id", "project_id", "requirement_id"]),
      )
    ) {
      return null;
    }
    const projectId = parsePositiveInteger(action.context.project_id);
    const requirementId = parsePositiveInteger(action.context.requirement_id);
    if (
      (action.context.project_id !== undefined && projectId === undefined) ||
      (action.context.requirement_id !== undefined &&
        requirementId === undefined)
    ) {
      return null;
    }
    return {
      id: action.id,
      title: action.title,
      surface: "requirements",
      context: {
        ...(organizationId ? { organizationId } : {}),
        ...(projectId ? { projectId } : {}),
        ...(requirementId ? { requirementId } : {}),
      },
    };
  }

  if (action.surface === "projects") {
    if (
      !hasOnlyKeys(
        action.context,
        new Set(["organization_id", "project_id"]),
      )
    ) {
      return null;
    }
    const projectId = parsePositiveInteger(action.context.project_id);
    if (action.context.project_id !== undefined && projectId === undefined) {
      return null;
    }
    return {
      id: action.id,
      title: action.title,
      surface: "projects",
      context: {
        ...(organizationId ? { organizationId } : {}),
        ...(projectId ? { projectId } : {}),
      },
    };
  }

  return null;
}

export function getAssistantAppViewFromAction(
  action: AssistantAction,
): AssistantAppView | null {
  if (
    action.tool !== "open_app_view" ||
    !action.ok ||
    action.status === "started"
  ) {
    return null;
  }
  return parseAssistantAppView(action.ui_action);
}

export function buildAssistantAppViewHref(view: AssistantAppView) {
  const params = new URLSearchParams();
  if (view.surface === "map") {
    if (view.context.entityType && view.context.entityId) {
      params.set("entity_type", view.context.entityType);
      params.set("entity_id", String(view.context.entityId));
      params.set("zoom", "17");
    }
    return params.size ? `/mapa?${params.toString()}` : "/mapa";
  }
  if (view.surface === "requirements") {
    if (view.context.organizationId) {
      params.set("organizacion", String(view.context.organizationId));
    }
    if (view.context.projectId) {
      params.set("proyecto", String(view.context.projectId));
    }
    if (view.context.requirementId) {
      params.set("id", String(view.context.requirementId));
    }
    return params.size ? `/requisitos?${params.toString()}` : "/requisitos";
  }
  return "/proyectos";
}
