# AGENTS.md

Guidance for Codex and other coding agents working in this repository.

## Git-first operating rule

Every coding agent must work with explicit git awareness. Do not edit, run mutating commands, stage, commit, switch branches, or create worktrees until you have checked the live git state.

Start every task with:

```bash
pwd
git status --short --branch
git remote -v
git branch --show-current
```

Finish every coding task with:

```bash
git status --short --branch
git diff --stat HEAD
```

Report the active branch, files changed, tests/commands run, and final git state in the handoff.

## Branch isolation

- Before editing files, creating commits or running repo-mutating commands, check the current Git branch and working tree.
- If the current branch is a shared/base branch, create a unique task branch first, using the `codex/` prefix for Codex work.
- Do not reuse another agent's task branch.
- When switching or creating a branch, preserve existing worktree changes and never revert changes you did not make unless explicitly asked.
- If the working tree contains unrelated or possibly user-owned changes, do not work in place. Create a separate `git worktree` for the new task.

## Recommended local structure

Use the main checkout as the coordination tree:

```text
/home/dev/proyectos/asistente_ayuntamientos
```

Use sibling worktrees for isolated tasks:

```text
/home/dev/proyectos/asistente_ayuntamientos-worktrees/<task-slug>
```

Example:

```bash
mkdir -p /home/dev/proyectos/asistente_ayuntamientos-worktrees
cd /home/dev/proyectos/asistente_ayuntamientos
git fetch origin

git worktree add \
  /home/dev/proyectos/asistente_ayuntamientos-worktrees/fix-invalid-tool-json \
  -b codex/fix-invalid-tool-json-20260623 \
  origin/codex/python-suite-runner-20260618
```

Choose the base branch deliberately:

- Use the agreed integration branch for current product work.
- Use `origin/main` only when the task is independent and `main` is known to be the right base.
- If unsure, stop and ask before creating the branch.

## Commit and PR hygiene

- Keep branches short-lived and scoped to one task.
- Prefer stacked PRs for large features instead of one large PR.
- Use Conventional Commits, for example `fix: normalize assistant tool calls` or `feat: add geolocated information model`.
- Stage only files that belong to the current task. Avoid `git add .` when unrelated changes exist.
- Review `git diff --cached` before committing.
- Do not commit `.hermes/` plans by default. Move durable team documentation to `docs/` if it should be versioned.
- Every PR/handoff must include a real test plan with commands actually run.

## Testing expectations

- Bug fixes require a regression test that fails before the fix when practical.
- Backend assistant/tooling changes should usually run targeted `pytest` first, then the relevant broader suite.
- Frontend changes should run the relevant lint/build checks.
- Never claim tests passed without real command output.

## Assistant embedded-surface review

Every task that adds or materially changes a user-facing workflow, screen,
module, or entity detail must explicitly evaluate whether that functionality
should also be available inside the assistant's embedded-window system.

Before handoff, classify the functionality as one of:

- `embed now`: it should be exposed in the assistant as part of this task;
- `embed later`: it is a valid candidate, but a named dependency or product
  decision prevents including it safely in the current scope;
- `not embeddable`: embedding would not improve the workflow or would create an
  unjustified security, usability, or maintenance cost.

Record the classification and a short reason in the task handoff or PR. A
classification is required even when no embedded-view code changes.

Use these criteria during the review:

- the user benefits from continuing the conversation while viewing or editing
  the functionality;
- the view has bounded, structured context such as an organization, project,
  requirement, or other internal entity ID;
- the existing APIs, authorization rules, and tenant isolation can be reused;
- the workflow remains usable in the desktop dialog and full-screen mobile
  presentation;
- opening or replaying the view can be made deterministic and idempotent.

For `embed now` work:

- extend the closed `open_app_view` surface/context contract in
  `backend/app/assistant/tools.py` and the strict frontend registry/parser in
  `frontend/app/lib/assistantAppViews.ts`;
- prefer extending an existing surface context over creating a nearly
  duplicate surface;
- mount reusable application components inside
  `AssistantEmbeddedWindow`; never execute model-provided HTML, component
  names, paths, or URLs, and do not introduce an iframe escape hatch;
- enforce RBAC, organization isolation, and referenced-entity visibility in
  the backend. A UI descriptor never grants access by itself;
- preserve `call_id`/action identity across text SSE and realtime, suppress
  stale-turn openings, avoid reopening historical actions automatically, and
  keep only bounded, sanitized view context for conversational continuity;
- cover allowed and denied access, malformed context, persistence/replay,
  idempotency, and relevant desktop/mobile behavior with tests or explicit
  verification.

## Database migration safety

- Treat every Alembic revision applied to a persistent database as immutable. Never delete, rename or rewrite it; reconcile mistakes with a successor revision.
- Before a migration drops or transforms data, inspect affected environments and make the migration stop with a clear error when safe automatic handling is not possible.
- Validate both a fresh upgrade and the path from the latest revision already deployed to `head`.
