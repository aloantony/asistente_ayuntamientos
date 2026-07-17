import type {
  AssistantAction,
  AssistantOpenCanvasUiAction,
} from "../components/types";

const OPERATIONS = new Set([
  "opened",
  "created",
  "updated",
  "restored",
]);

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) > 0;
}

export function parseAssistantCanvasAction(
  value: unknown,
): AssistantOpenCanvasUiAction | null {
  const action = asRecord(value);
  const context = asRecord(action?.context);
  if (
    !action ||
    !context ||
    action.type !== "ui.open_canvas_document" ||
    action.version !== 1 ||
    action.surface !== "document_canvas" ||
    typeof action.id !== "string" ||
    !action.id.trim() ||
    typeof action.title !== "string" ||
    !action.title.trim() ||
    !isPositiveInteger(context.document_id) ||
    !isPositiveInteger(context.conversation_id) ||
    !isPositiveInteger(context.revision) ||
    typeof context.operation !== "string" ||
    !OPERATIONS.has(context.operation)
  ) {
    return null;
  }

  return {
    type: "ui.open_canvas_document",
    version: 1,
    id: action.id,
    surface: "document_canvas",
    title: action.title,
    context: {
      document_id: context.document_id,
      conversation_id: context.conversation_id,
      revision: context.revision,
      operation: context.operation as AssistantOpenCanvasUiAction["context"]["operation"],
    },
  };
}

export function getMessageCanvasActions(
  actions: AssistantAction[],
): AssistantOpenCanvasUiAction[] {
  return actions.flatMap((action) => {
    const parsed = parseAssistantCanvasAction(action.ui_action);
    return parsed ? [parsed] : [];
  });
}
