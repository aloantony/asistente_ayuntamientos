# AGENTS.md

Guidance for Codex and other coding agents working in this repository.

## Branch isolation

- Before editing files, creating commits or running repo-mutating commands, check the current Git branch.
- If the current branch is a shared/base branch, create a unique task branch first, using the `codex/` prefix for Codex work.
- Do not reuse another agent's task branch.
- When switching or creating a branch, preserve existing worktree changes and never revert changes you did not make unless explicitly asked.
