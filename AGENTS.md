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

## Parallel Codex sessions

The main checkout is the coordination tree. Keep it clean and do not implement
product changes there. Every writing session owns exactly one task branch and
one sibling worktree; two sessions must never write in the same worktree.

Use the repository helper from the coordination tree:

```bash
scripts/codex-session audit
scripts/codex-session create <task-slug> --owner <session-id> --base origin/main
scripts/codex-session list
```

- `origin/main` is the default base for independent work. Pass the actual
  dependency branch with `--base` for stacked work.
- A worktree remains reserved while its session is `active`, `paused`, or
  `blocked`. Another session may inspect it only after an explicit handoff and
  must not edit it.
- Finish a worker turn with `scripts/codex-session handoff --status ...`. The
  `ready` status requires a clean tree, a synchronized task-branch upstream,
  and at least one recorded test command.
- The helper never pushes, merges, deletes branches, or removes worktrees.
  Cleanup requires a separate audit, confirmation that the work is integrated
  or intentionally discarded, and explicit user approval.

### Shared development runtime

There is one long-lived Docker Compose stack because its services bind fixed
ports and persistent volumes. The coordination session owns it by default.
Before a worker rebuilds services, runs development migrations, or performs a
visual full-stack check, it must acquire the runtime lease:

```bash
scripts/codex-session runtime acquire --owner <session-id> --task <task-slug>
scripts/codex-session runtime status
scripts/codex-session runtime release --owner <session-id>
```

Workers without the lease must not run `docker compose up`, `down`, or `build`,
must not migrate the development database, and must not mutate development
document storage. Backend pytest runs may share the existing PostgreSQL service
because the harness creates a unique `app_test_<uuid>` database and temporary
document directory for each process. From a worker worktree, use the
coordination Compose file without starting dependencies:

```bash
docker compose \
  -f /home/dev/proyectos/asistente_ayuntamientos/docker-compose.yml \
  run --rm -T --no-deps -v "$PWD/backend:/app" backend \
  sh -c "pip install -q -r requirements.txt -r requirements-dev.txt && python -m pytest tests -q"
```

Frontend lint, type-check, and build commands run inside the worker worktree
and do not require the runtime lease.

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

## Database migration safety

- Treat every Alembic revision applied to a persistent database as immutable. Never delete, rename or rewrite it; reconcile mistakes with a successor revision.
- Before a migration drops or transforms data, inspect affected environments and make the migration stop with a clear error when safe automatic handling is not possible.
- Validate both a fresh upgrade and the path from the latest revision already deployed to `head`.
