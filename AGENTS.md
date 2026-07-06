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
- Use the project commit style from `CLAUDE.md`: English, imperative subject, no conventional-commit prefixes; detailed body that references ADRs when relevant. Module milestones follow the pattern `Add <module> v1`.
- Stage only files that belong to the current task. Avoid `git add .` when unrelated changes exist.
- Review `git diff --cached` before committing.
- Do not commit `.hermes/` plans by default. Move durable team documentation to `docs/` if it should be versioned.
- Every non-documentation commit must be followed by a scoped documentation commit that updates the relevant `docs/` living document, ADR, or `README.md` section. If no documentation change is warranted, create or update a docs note explaining why the code-only commit did not change user-facing behavior, architecture, operations, or requirements.
- Keep documentation commits separate from feature/fix commits unless the user explicitly asks for a single combined commit; the normal stack is code/test commit first, docs sync commit immediately after.
- Every PR/handoff must include a real test plan with commands actually run.

## Testing expectations

- Bug fixes require a regression test that fails before the fix when practical.
- Backend assistant/tooling changes should usually run targeted `pytest` first, then the relevant broader suite.
- Frontend changes should run the relevant lint/build checks.
- Never claim tests passed without real command output.
