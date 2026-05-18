# Ralph Loop

Ralph Loop is a domain-agnostic automated iteration wrapper for Kiro CLI. It drives an agent through a persistent loop until explicit completion criteria are met, rather than relying on a single long-running session that drifts as context accumulates.

Each iteration is a fresh, short-lived Kiro CLI session that works on exactly one task under exactly one persona. The filesystem (`tasks.json`, `specs/`, `personas/`, `SUMMARY.md`, `pending_tasks.json`, logs under `logs/`) is the durable state between iterations; in-memory agent context is discarded. Git acts as an append-only audit trail of working-tree changes. The loop keeps going until every task passes validation, a budget cap is hit, or the run is blocked on unresolved dependencies.

## Quickstart

Install the package in editable mode with development dependencies:

```bash
uv sync            # or: pip install -e ".[dev]"
```

Then scaffold a project and run the loop:

```bash
# 1. Scaffold SUMMARY.md, tasks.json, pending_tasks.json, ralph.config.json,
#    specs/, and personas/ in the current directory.
ralph init

# 2. Edit SUMMARY.md with your project brief, and adjust ralph.config.json
#    (at minimum, make sure fallback_persona points at a persona file).

# 3. Invoke the planner persona to populate tasks.json from SUMMARY.md.
ralph init-tasks

# 4. Run the loop until every task passes validation, the budget caps hit,
#    or the run is blocked on unresolved dependencies.
ralph run

# 5. Rewind the working tree to the commit for an earlier iteration.
ralph rollback 7
```

## Directory layout

`ralph init` scaffolds the following layout in the target directory:

```
<project-root>/
├── SUMMARY.md              # Project brief (freeform Markdown)
├── tasks.json              # Durable task list; schema in schemas/tasks.schema.json
├── pending_tasks.json      # Tasks spilled by budget caps; re-admitted next run
├── ralph.config.json       # Runtime configuration (see Configuration reference)
├── specs/                  # One Task_Spec Markdown file per task
│   └── <task-id>.md
├── personas/               # One YAML (or Markdown+frontmatter) file per persona
│   └── writer.yaml
└── logs/                   # Created on first run
    ├── run-<uuid>.log
    ├── iter-0001.log
    └── summary-<uuid>.json
```

## Configuration reference

`ralph.config.json` is parsed into the `Config` Pydantic model. Only `fallback_persona` is required; every other field has a documented default.

| Field | Default | Requirement |
| --- | --- | --- |
| `tasks_path` | `"tasks.json"` | 15.3 |
| `summary_path` | `"SUMMARY.md"` | 15.3 |
| `personas_dir` | `"personas/"` | 15.3 |
| `specs_dir` | `"specs/"` | 15.3 |
| `pending_tasks_path` | `"pending_tasks.json"` | 15.5 |
| `log_dir` | `"logs/"` | 15.3 |
| `fallback_persona` | *(required)* | 15.3, 4.7 |
| `escalation_persona` | `null` | 5.5, 15.3 |
| `escalation_threshold` | `3` | 5.5 |
| `planner_persona` | `null` | 17.1, 15.3 |
| `automatic_planner` | `false` | 15.7, 17.3 |
| `orchestrator_llm_command` | `null` (falls back to `kiro_cli_command`) | 15.3, 15.4 |
| `orchestrator_model_id` | `null` | 15.3 |
| `max_iterations` | `50` | 10.1 |
| `max_retries_per_task` | `5` | 10.2 |
| `wall_clock_timeout_ms` | `3_600_000` (1 hour) | 10.4 |
| `validation_timeout_ms` | `300_000` (5 minutes) | 7.13 |
| `kiro_cli_command` | `"kiro-cli"` | 15.3 |
| `per_iteration_task_creation_budget` | `10` | 10.6 |
| `per_run_task_creation_budget` | `100` | 10.7 |
| `max_creation_chain_depth` | `5` | 10.8 |
| `max_context_tokens` | `32_000` | 6.7 |
| `git_integration_enabled` | `true` | 13.1, 15.6 |
| `model_pricing` | `{}` | 12.3, 12.4 |

CLI flags on `ralph run` override `max_iterations`, `max_retries_per_task`, `wall_clock_timeout_ms`, and `personas_dir` for a single run (R15.2). `ralph init-tasks` accepts `--personas-dir` for the same reason — useful when you want to point at a shared persona set (like `templates/book/personas/`) without copying files into the project.

## Personas

Personas live under `personas/` (one file per persona). A persona is a named role with a description, a prompt template, optional instructions, optional tool restrictions, and an optional default pass condition for `persona_review` checks. YAML and Markdown-with-YAML-frontmatter files are both supported.

The Orchestrator picks one persona per iteration. Selection follows four paths (R4.10):

1. **Explicit** — the task's `target_persona` names a persona that exists.
2. **LLM** — the Orchestrator asks the configured LLM to pick a persona from the registry's `PersonaDescription` list.
3. **Fallback** — the LLM's choice is missing or invalid, so `fallback_persona` is used.
4. **Escalation** — the task has hit `escalation_threshold` retries; `escalation_persona` takes over.

`persona_review` validation checks resolve their pass condition in two steps (R7.7): a per-check `pass_condition` in the Task_Spec wins, otherwise the reviewing persona's `default_persona_review_pass_condition` is used. A check with neither marks the executing task stuck (R7.8).

A ready-to-use book-authoring persona set ships at `templates/book/personas/` (Writer, Reviewer, Editor, FactChecker, Outline, Planner).

## Task specs

Every task in `tasks.json` points at a Task_Spec file under `specs/`. A Task_Spec is Markdown with a YAML frontmatter block. Required frontmatter fields are `id`, `title`, and at least one `validation` entry (R18.1).

```markdown
---
id: chapter-01-draft
title: Draft Chapter 1
target_persona: Writer
tags: [prose, chapter]
depends_on: [outline-v1]
validation:
  - type: file_exists
    name: chapter-file-present
    paths: [chapters/chapter-01.md]
  - type: persona_review
    name: reviewer-signoff
    persona: Reviewer
context_files: [chapters/chapter-01.md]
---

## Objective
Draft chapter 1 from the outline.

## Context References
- outline v1 (see `specs/outline-v1.md`)

## Instructions
Write ~3000 words of prose.

## Notes
Mirror the tone of the sample chapter.
```

## Validation checks

Three check types are supported, keyed by the `type` field on each `validation` entry:

- **`shell`** — runs one or more shell commands. A non-zero exit on any command marks the check `fail` (R7.2, R7.5).
- **`file_exists`** — asserts every path in `paths` exists on disk after the iteration (R7.4, R7.11).
- **`persona_review`** — invokes a reviewing persona with the iteration context and parses a pass/fail verdict against the resolved pass condition (R7.3, R7.6, R7.7).

A check that runs longer than `validation_timeout_ms` is terminated and reported as a timeout (R7.13).

## Running, resumption, budgets

Each iteration of `ralph run` does: select next eligible task → pick persona → compose context → flip task to `in_progress` → invoke Kiro CLI → validate → process task creation → update status → commit. The loop stops when every task passes (exit 0), every non-passing task is stuck or blocked (exit 1), or a budget cap fires (exit 3).

Interrupting with `SIGTERM` or `Ctrl+C` is safe. On the next startup, any task still marked `in_progress` is reset to `failing` without incrementing `retry_count` (R14.3), and the next iteration is flagged `resumed_from_interruption` so the persona sees a resumed-from-interruption notice in its context (R14.5).

Four budget knobs bound each run:

- `max_iterations` — hard cap on the number of iterations (R10.1).
- `max_retries_per_task` — per-task retry cap; exceeded tasks become `stuck` (R10.2).
- `wall_clock_timeout_ms` — end-to-end wall-clock cap (R10.4).
- `per_iteration_task_creation_budget` / `per_run_task_creation_budget` — bound how many new tasks a single iteration or run can admit (R10.6, R10.7). Surplus tasks spill to `pending_tasks.json` and are re-admitted on the next run.

## Git integration

When `git_integration_enabled` is `true` and the working tree is a git repo, each iteration ends with a commit whose message follows:

```
ralph-loop iter <iteration> task=<task-id> persona=<persona-name> outcome=<pass|fail>
```

`ralph rollback N` resets the working tree to the commit for iteration `N` (R13.5). Commits outside this format (for example, manual commits between runs) are left untouched.

## Schemas

JSON Schemas for the core Pydantic models are exported to `schemas/` for editor integration:

- `schemas/task.schema.json`
- `schemas/tasks.schema.json` (top-level array schema for `tasks.json`)
- `schemas/persona.schema.json`
- `schemas/task-spec.schema.json`
- `schemas/config.schema.json`

Regenerate them from the current models with `python scripts/export_schemas.py`.

See `.kiro/specs/ralph-loop/` for the full requirements, design, and task breakdown.
