# Assistant-First Discovery Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Convert the assistant into the main product entry point without freezing predefined flows: users ask freely, the assistant captures needs, exposes work state, and turns repeated patterns into reusable product intelligence.

**Architecture:** Keep the existing chat pipeline (`/assistant/conversations/{id}/messages` -> `run_agent_turn` -> tools -> audited assistant reply). Do not introduce rigid workflow tables yet. Start by improving terminology, prompts, state visibility, and UI around the existing conversation state/actions. Keep `Requirement` as the internal model for now, but present it as “Necesidad” in user-facing surfaces.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, Next.js, React, TypeScript, existing pytest backend suite, existing frontend build.

---

## Current context

Relevant files inspected:

- Backend assistant route: `backend/app/assistant/routes.py`
- Backend assistant service/tool loop: `backend/app/assistant/service.py`
- Backend assistant agent registry: `backend/app/assistant/agents.py`
- Backend assistant tools: `backend/app/assistant/tools.py`
- Backend assistant models: `backend/app/assistant/models.py`
- Backend assistant schemas: `backend/app/assistant/schemas.py`
- Frontend assistant controller: `frontend/app/lib/useAssistantController.ts`
- Frontend assistant panel: `frontend/app/components/AssistantPanel.tsx`
- Frontend assistant page: `frontend/app/(app)/asistente/page.tsx`
- Permission catalog: `backend/app/rbac/permissions.py`
- Project overview: `README.md`

Current message flow:

1. Frontend sends `POST /assistant/conversations/{conversation_id}/messages`.
2. Backend validates user, permission, conversation ownership/status, and runtime availability.
3. `run_agent_turn` persists the user message.
4. Backend first tries deterministic handling in `try_handle_direct_turn`.
5. If not handled, planner/router chooses an agent.
6. System prompt and tools are built.
7. Gateway calls Anthropic or Hermes Agent.
8. Tool requests are executed by backend with RBAC.
9. Assistant reply is persisted with `actions`, `agent_key`, and `routing`.
10. Frontend replaces optimistic state with the returned full conversation.

Product decision behind this plan:

- The assistant should not start from predefined flows.
- The assistant should discover needs through usage.
- “Requisitos” are better presented as “Necesidades” to municipal users.
- Structured modules remain behind the chat as capabilities and audit/state stores.

---

## Non-goals for this phase

- Do not rename database tables from `requirements` to `needs` yet.
- Do not create a full workflow engine.
- Do not hard-code 5 fixed flows.
- Do not remove existing module screens.
- Do not let the LLM bypass backend permissions or mutate data directly.
- Do not send documents or sensitive municipal data to external AI APIs outside the existing gateway rules.

---

## Proposed approach

Implement a first “assistant-first discovery” layer in four small increments:

1. User-facing terminology: “Requisitos” -> “Necesidades”.
2. Backend assistant prompt alignment: the agent captures “necesidades”, not “requisitos” in product language.
3. Conversation work state: expose a safe summary of current conversation state/actions to the UI.
4. Assistant workspace UI: show what the chat is building: selected organization, pending step, actions, needs created/updated, and next expected input.

This keeps the architecture flexible while making the assistant feel like the center of the product.

---

## Task 1: Rename user-facing “Requisitos” copy to “Necesidades” in frontend

**Objective:** Make the product language match the discovery model while keeping internal API/model names stable.

**Files:**
- Modify: `frontend/app/components/AssistantPanel.tsx`
- Modify: `frontend/app/components/RequirementsPanel.tsx`
- Modify: `frontend/app/(app)/requisitos/page.tsx`
- Modify: any nearby frontend component containing visible strings with “Requisito(s)”

**Steps:**

1. Search frontend visible copy:
   - `search_files("Requisito|requisito|Requisitos|requisitos", path="frontend/app", file_glob="*.tsx")`
2. Replace user-facing labels only:
   - “Requisitos” -> “Necesidades”
   - “Requisito” -> “Necesidad”
   - “Nuevo requisito” -> “Nueva necesidad”
   - “Captura de requisitos” -> “Captura de necesidades”
3. Do not rename TypeScript types, API paths, variables, or backend concepts in this task.
4. Keep admin/dev-only wording if it clearly refers to internal implementation, but prefer visible municipal language.

**Validation:**

Run:

```bash
npm --prefix frontend run build
```

Expected: build succeeds.

---

## Task 2: Rename assistant capability labels from requirements to needs

**Objective:** Make the assistant UI capabilities reflect product wording.

**Files:**
- Modify: `frontend/app/components/AssistantPanel.tsx`

**Known area:**
- `buildCapabilities` currently labels the capability as `Requisitos` around `frontend/app/components/AssistantPanel.tsx:183-188`.

**Steps:**

1. Change capability label:
   - `label: "Requisitos"` -> `label: "Necesidades"`
2. Change detail:
   - `"Lectura y borradores"` can remain, or become `"Captura y borradores"`.
3. Ensure action icons still work: the implementation can keep checking `tool.includes("requirement")` internally.

**Validation:**

Run:

```bash
npm --prefix frontend run build
```

Expected: build succeeds.

---

## Task 3: Align backend assistant prompts with “necesidades” language

**Objective:** Preserve the internal `Requirement` model while instructing the assistant to speak in terms of “necesidades” to users.

**Files:**
- Modify: `backend/app/assistant/agents.py`
- Modify: `backend/app/assistant/service.py`

**Steps:**

1. In `REQUIREMENTS_INTAKE_INSTRUCTIONS`, change user-facing wording:
   - “capturar requisitos” -> “capturar necesidades o requisitos funcionales”
   - “Crea los requisitos...” -> “Crea las necesidades...” while clarifying they are stored as requirement drafts internally only if needed for developer comments.
2. In `CONSULTATION_INSTRUCTIONS`, change visible wording to “necesidades registradas”.
3. In direct deterministic replies in `service.py`, change visible strings:
   - `organization_prompt`
   - `requirement_content_prompt`
   - `delegated_requirement_content_reply`
   - `create_requirement_organization_prompt`
   - direct list/create replies
4. Keep function names unchanged in this task.
5. Be careful with logic relying on normalized keyword `requisito`. Add support for `necesidad` without removing `requisito`.

**Implementation detail:**

Update keyword detectors such as:

- `is_list_requirements_request`
- `is_empty_requirements_followup`
- `is_create_test_requirement_request`
- `is_create_another_requirement_request`
- `is_create_requirement_capability_question`
- `recent_assistant_requested_requirement_content`

They should match both “requisito(s)” and “necesidad(es)”. A small helper avoids repetition:

```python
def mentions_need_or_requirement(normalized: str) -> bool:
    return any(
        word in normalized
        for word in {"requisito", "requisitos", "necesidad", "necesidades"}
    )
```

Then replace direct checks like:

```python
if "requisito" not in normalized:
    return False
```

with:

```python
if not mentions_need_or_requirement(normalized):
    return False
```

**Validation:**

Run:

```bash
python3 -m compileall -q backend/app backend/alembic
```

Expected: no compile errors.

---

## Task 4: Add backend tests for “necesidad” synonyms in deterministic assistant handling

**Objective:** Prevent regressions where the deterministic layer only understands “requisito”.

**Files:**
- Modify: `backend/tests/test_assistant.py`

**Steps:**

1. Find existing assistant tests for listing/creating requirements.
2. Add tests using “necesidad” language, for example:
   - “quiero crear una necesidad”
   - “lista las necesidades”
   - “no hay necesidades” if a matching follow-up test exists
3. Assert behavior mirrors the existing `requisito` tests.
4. Do not require the LLM runtime for these tests if existing direct handling can cover them.

**Validation:**

Run targeted tests:

```bash
docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/test_assistant.py -q"
```

Expected: assistant tests pass.

---

## Task 5: Expose a safe conversation work-state summary in the API

**Objective:** Let the frontend show what the assistant is currently working on without exposing raw internal state blindly.

**Files:**
- Modify: `backend/app/assistant/schemas.py`
- Modify: `backend/app/assistant/routes.py`
- Modify: `backend/app/assistant/service.py` or create `backend/app/assistant/state_summary.py`

**Preferred minimal design:**

Add a computed field to `AssistantConversationDetail`:

```python
class AssistantConversationStateSummary(BaseModel):
    selected_organization_id: int | None = None
    pending_action_type: str | None = None
    pending_action_label: str | None = None
    draft_title: str | None = None
    draft_problem: str | None = None
```

Then:

```python
class AssistantConversationDetail(AssistantConversationRead):
    messages: list[AssistantMessageRead]
    state_summary: AssistantConversationStateSummary | None = None
```

Because `AssistantConversationDetail` currently uses `from_attributes`, the easiest safe implementation may be to build response objects explicitly in route functions instead of relying only on ORM serialization. If that is too invasive, add a serializer helper in `routes.py`:

```python
def serialize_conversation_detail(conversation: AssistantConversation) -> AssistantConversationDetail:
    return AssistantConversationDetail.model_validate(conversation).model_copy(
        update={"state_summary": build_state_summary(conversation)}
    )
```

Use it in:

- `create_conversation`
- `get_conversation`
- `update_conversation`
- `send_message`

**State mapping rules:**

- Parse `conversation.state` with existing `load_conversation_state` helper or a duplicate safe parser if import direction requires it.
- Expose only:
  - selected organization id
  - pending action type
  - pending draft title/problem
  - friendly pending label
- Do not expose arbitrary raw state JSON.

Suggested labels:

- `list_requirements` -> “Consultar necesidades”
- `create_requirement_organization` -> “Elegir organización”
- `create_requirement_content` -> “Completar necesidad”
- `create_requirement_retry` -> “Reintentar creación de necesidad”
- `create_test_requirement_organization` -> “Elegir organización”
- `create_test_requirement_content` -> “Completar necesidad de prueba”
- `create_test_requirement_confirm` -> “Confirmar creación”

**Validation:**

Run:

```bash
python3 -m compileall -q backend/app backend/alembic
```

Run targeted assistant tests and add/adjust schema assertions if needed.

---

## Task 6: Add frontend types for conversation state summary

**Objective:** Type the new API field so the assistant UI can render conversation work state safely.

**Files:**
- Modify: `frontend/app/components/types.ts`

**Steps:**

1. Add a type matching backend schema:

```ts
export type AssistantConversationStateSummary = {
  selected_organization_id: number | null;
  pending_action_type: string | null;
  pending_action_label: string | null;
  draft_title: string | null;
  draft_problem: string | null;
};
```

2. Add to `AssistantConversationDetail`:

```ts
state_summary?: AssistantConversationStateSummary | null;
```

**Validation:**

Run:

```bash
npm --prefix frontend run build
```

Expected: TypeScript build succeeds.

---

## Task 7: Add an assistant work-state panel next to/below the chat

**Objective:** Make the chat feel like the primary interface by showing structured progress generated by the conversation.

**Files:**
- Modify: `frontend/app/components/AssistantPanel.tsx`

**Design:**

Add a compact “Trabajo actual” / “Estado del trabajo” panel using existing `selectedConversation` data.

Show:

- active conversation title
- selected organization id if present
- pending action label if present
- draft title/problem if present
- latest successful tool actions

Use existing message `actions`; no new endpoint is needed beyond `state_summary`.

Suggested helper functions:

```ts
function getRecentActions(conversation: AssistantConversationDetail | null) {
  return conversation?.messages
    .flatMap((message) => message.actions.map((action) => ({ ...action, messageId: message.id })))
    .filter((action) => action.ok)
    .slice(-5) ?? [];
}
```

Keep it simple. Do not create a complex workflow board yet.

**UX copy:**

- If no conversation selected: “Abre o inicia una conversación para empezar.”
- If no pending action/actions: “Aún no hay trabajo estructurado en esta conversación.”
- For pending action: “El asistente está esperando: {pending_action_label}”

**Validation:**

Run:

```bash
npm --prefix frontend run build
```

Expected: build succeeds.

---

## Task 8: Update assistant prompt to favor discovery over predefined flows

**Objective:** Make the model behave as a discovery assistant rather than a fixed workflow selector.

**Files:**
- Modify: `backend/app/assistant/service.py`
- Modify: `backend/app/assistant/agents.py`

**Prompt principle to add:**

In `COMMON_SYSTEM_PROMPT`, add wording like:

```text
- No fuerces flujos predefinidos. Parte de lo que el usuario pide, pregunta lo mínimo necesario y estructura la necesidad si el sistema todavía no tiene una herramienta específica para resolverla.
- Cuando detectes una necesidad operativa nueva o repetible, intenta capturarla como borrador antes de prometer una automatización inexistente.
```

In `REQUIREMENTS_INTAKE_INSTRUCTIONS`, strengthen:

```text
- Tu objetivo no es encajar al usuario en un formulario, sino descubrir necesidades reales. Si la petición todavía no puede ejecutarse con herramientas disponibles, captura la necesidad con suficiente contexto para que pueda revisarse después.
```

**Validation:**

Run:

```bash
python3 -m compileall -q backend/app backend/alembic
```

Then run assistant tests.

---

## Task 9: Add backend test coverage for state summary

**Objective:** Ensure the new `state_summary` field is stable and does not expose raw state.

**Files:**
- Modify: `backend/tests/test_assistant.py`

**Test cases:**

1. New conversation has `state_summary` null or empty fields.
2. A turn that asks for missing need content sets `pending_action_label` to a friendly value.
3. Raw `conversation.state` keys not included in response.
4. Existing message fields still serialize correctly.

**Validation:**

Run:

```bash
docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/test_assistant.py -q"
```

Expected: tests pass.

---

## Task 10: Add frontend build verification and visual sanity pass

**Objective:** Verify the assistant page still builds and the new panel does not break interaction.

**Files:**
- No planned file changes beyond previous tasks.

**Steps:**

1. Run frontend build:

```bash
npm --prefix frontend run build
```

2. If local stack is already running, manually verify:
   - open `/asistente`
   - create conversation
   - send “quiero crear una necesidad”
   - confirm the optimistic message appears
   - confirm backend response appears
   - confirm “Trabajo actual” panel shows pending action/draft when applicable

3. If local stack is not running, do not start destructive commands; rely on build + backend tests.

---

## Task 11: Update README product wording lightly

**Objective:** Make project docs match the product direction without over-documenting implementation details.

**Files:**
- Modify: `README.md`

**Steps:**

1. In project overview, clarify:
   - Requirements are user-facing “necesidades funcionales/operativas”.
   - Chat is intended as the primary entry point for discovering work.
2. Avoid claiming completed capabilities not implemented yet.
3. Keep current technical setup unchanged.

Suggested minimal addition near README section 2:

```md
The assistant-first direction means users should be able to express needs in natural language; the system captures them as structured needs, uses available tools when safe, and lets repeated patterns evolve into reusable capabilities.
```

Translate/adapt to Spanish or keep README style consistent with existing English.

**Validation:**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

---

## Final validation

Run the relevant checks:

```bash
python3 -m compileall -q backend/app backend/alembic
npm --prefix frontend run build
git diff --check
```

Run backend assistant tests:

```bash
docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/test_assistant.py -q"
```

If the full backend suite is affordable, run:

```bash
docker compose run --rm -T -v "$(pwd)/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q"
```

---

## Risks and tradeoffs

1. **Terminology mismatch:** Internally the model remains `Requirement`, externally “Necesidad”. This is acceptable short-term but should be documented to avoid developer confusion.
2. **Overexposing state:** Do not return raw `conversation.state`; only return a curated summary.
3. **Prompt-only behavior is not guaranteed:** Prompt changes improve behavior but do not enforce product logic. Critical actions must remain backend/tool-governed.
4. **UI complexity:** The work-state panel should be simple. Avoid building a workflow engine visually before the product has discovered actual patterns.
5. **Tests may depend on exact Spanish strings:** Prefer assertions on behavior and key substrings rather than full long replies.

---

## Open questions

1. Should the sidebar/menu route stay `/requisitos` while displaying “Necesidades”, or should there be a redirect/new route `/necesidades` later?
2. Should `state_summary.selected_organization_id` include organization name? If yes, backend must resolve it safely against visible organizations.
3. Should “funcionalidades transversales” get a visible review panel soon, or stay admin-only until more needs are collected?
4. Should Telegram receive the same “necesidad” terminology immediately in bot replies? Current deterministic backend copy changes should affect it if Telegram uses the same service path.

---

## Recommended implementation order

1. Tasks 1-4: terminology and tests for “necesidad”.
2. Tasks 5-7: state summary API and work-state UI.
3. Tasks 8-9: prompt/discovery behavior and backend tests.
4. Tasks 10-11: validation and README alignment.

This sequence keeps changes small, testable, and aligned with the assistant-first discovery direction.
