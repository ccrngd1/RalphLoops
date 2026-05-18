# Implementation Plan: Ralph Loop

## Overview

This plan turns the Ralph Loop design into an incremental build order. It starts from a Python package skeleton and pure data models, moves through pure-logic components (which carry the bulk of property-based tests), then layers on filesystem I/O, subprocess-backed components, the validator, the orchestrator, task-creation processing, observability, the CLI surface, and finally end-to-end integration tests. Property-based tests are interleaved with the components they validate so that each property (P1-P30) lands right next to its target module.

Language and tooling follow the design:

- Python 3.11+ with Pydantic v2 for all models
- `pyproject.toml` + `hatchling` build backend, dependencies managed with `uv`
- `click` for the CLI, installed as a console script `ralph = "ralph_loop.cli:main"`
- `pytest` with `pytest-asyncio`, `pytest-mock`, `pytest-cov`
- `hypothesis` for property-based tests (min 100 examples per property)
- Every property test is tagged `# Feature: ralph-loop, Property <N>: <title>`

Conventions for this task list:

- Top-level items are epics; sub-tasks carry the actual build steps.
- Sub-tasks postfixed with `*` are optional (tests or polish) and will not be auto-implemented.
- `_Requirements:_` lines reference the granular acceptance-criteria ids from `requirements.md` (e.g. `2.7`).
- `_Properties:_` lines reference the properties from `design.md` (e.g. `P1`).
- Checkpoints appear between major phases to surface failures early.

## Tasks

- [ ] 1. Scaffold the project and packaging
  - [x] 1.1 Create the repository layout and `pyproject.toml`
    - Add `pyproject.toml` with `hatchling` backend, project metadata, Python `>=3.11` constraint, runtime deps (`pydantic>=2`, `click`, `pyyaml`, `structlog`), and dev deps (`pytest`, `pytest-asyncio`, `pytest-mock`, `pytest-cov`, `hypothesis`, `mypy` or `pyright`)
    - Declare the console script `ralph = "ralph_loop.cli:main"`
    - Create the `ralph_loop/` package directory with an empty `__init__.py` and placeholder modules: `cli.py`, `config.py`, `persona_registry.py`, `task_selector.py`, `orchestrator.py`, `context.py`, `kiro.py`, `validator.py`, `task_creation.py`, `git_manager.py`, `logger.py`, `tokens.py`, `planner.py`, `resumer.py`, `pending_queue.py`, `budget.py`, `models.py`, `escalation.py`, `atomic_io.py`
    - Create the `tests/` directory with `__init__.py`, `conftest.py`, and an empty `strategies.py`
    - _Requirements: 15.1, 15.8_

  - [x] 1.2 Add `uv.lock`-compatible workflow and editor config
    - Add `.gitignore` for `__pycache__/`, `.venv/`, `dist/`, `.hypothesis/`, `logs/`
    - Add `README.md` with a one-paragraph description and a quickstart pointing to `ralph init`, `ralph init-tasks`, `ralph run`
    - Add `pytest.ini` or `[tool.pytest.ini_options]` in `pyproject.toml` enabling `asyncio_mode = "auto"` and registering a `hypothesis` CI profile
    - _Requirements: 15.1, 15.8_

  - [x] 1.3 Verify the empty package imports cleanly
    - Install with `uv sync` (or `pip install -e .[dev]`)
    - Run `python -c "import ralph_loop"` and `ralph --help` (help should render with no subcommands wired yet)
    - _Requirements: 15.1_

- [ ] 2. Core Pydantic data models
  - [x] 2.1 Implement `Task`, `TaskStatus`, and provenance fields in `models.py`
    - Define `TaskStatus = Literal["pending", "in_progress", "passing", "failing", "stuck"]`
    - Define `Task` with required fields `id`, `title`, `priority`, `status`, `spec_path`, `retry_count`; optional fields `target_persona`, `depends_on`, `tags`, `created_at_iteration`, `created_by_persona`, `creation_chain`, `spilled_run_id`, `admitted_run_id`, `resumed_from_interruption`
    - Enforce `retry_count >= 0` via `field_validator`
    - Provide a `TypeAdapter(list[Task])` helper for `tasks.json` load/dump
    - _Requirements: 2.1, 2.2, 8.5, 8.12, 8.13, 9.7, 14.5_

  - [x] 2.2 Implement Task Spec models
    - Define `ShellCheckConfig`, `PersonaReviewCheckConfig`, `FileExistsCheckConfig` with `Literal["shell" | "persona_review" | "file_exists"]` type discriminators
    - Define `ValidationCheckConfig` as an annotated discriminated union
    - Define `TaskSpecBody` and `TaskSpec` (with required `id`, `title`, `validation`; optional `target_persona`, `tags`, `depends_on`, `persona_fields`, `context_files`)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.6, 18.1, 18.2, 18.3, 18.4_

  - [x] 2.3 Implement Persona and Config models
    - Define `ToolRestrictions`, `Persona`, and `PersonaDescription` (the Orchestrator-facing subset)
    - Define `ModelPrice` and `Config` with all fields from the design (paths, persona wiring, budgets, timeouts, pricing), using `Field(default=...)` for the documented defaults
    - _Requirements: 3.2, 3.3, 3.7, 5.5, 10.1, 10.2, 10.4, 10.6, 10.7, 10.8, 15.3, 15.5, 15.6, 15.7_

  - [x] 2.4 Implement observability and runtime models
    - Define `TokenUsage`, `CallKind`, `LlmCallRecord`, `KindTotals`, `RunTokenTotals`
    - Define `IterationOutcome`, `SelectionPath`, `ContextSummary`, `KiroInvocationLog`, `ValidationLog`, `TaskCreationEventLog`, `GitCommitLog`, `IterationLogEntry`, `RunSummary`
    - Define `CheckResult` and `ValidationResult`
    - Define `PersonaSelection`, `ContextWindow`, `CommitResult`, `PendingQueueResult`, `DiscardedEntry`, `RejectedEntry`, `RevertedEntry`, `TaskCreationResult`, `ResumeResult`, `SpillReason`
    - _Requirements: 4.6, 4.10, 5.7, 11.3, 11.4, 12.1, 12.2, 12.3, 12.4, 12.5, 13.2, 14.5_

  - [x] 2.5 Unit tests for model validation edges
    - Round-trip `Task`/`TaskSpec`/`Persona`/`Config` via `model_dump_json` and `model_validate_json`
    - Assert `ValidationError` on missing required fields, bad enum values, negative `retry_count`, wrong check-type discriminators
    - _Requirements: 2.2, 3.6, 7.1, 18.1, 18.7_

- [ ] 3. Task Selector (pure logic, primary PBT target)
  - [x] 3.1 Implement `TaskSelector.next`
    - Filter eligibility: `status in {"pending", "failing"}`, `retry_count < config.max_retries_per_task`, every `depends_on` id present in the list with status `"passing"`
    - Sort eligible tasks by ascending `priority` and return the head, or `None` when no task is eligible
    - _Requirements: 1.2, 2.7, 2.8, 10.3_

  - [x] 3.2 Property test for Task Selector eligibility and priority
    - **Property 1: Task Selector picks a minimum-priority eligible task**
    - **Validates: Requirements 1.2, 2.7, 2.8, 10.3**
    - Use `task_list_dag_strategy` × `config_strategy`

  - [x] 3.3 Implement completion and blocked-termination decision function
    - Pure function over `list[Task]` returning `("success", exit=0)`, `("blocked", exit != 0, blocked_ids, blocking_dep_ids)`, or `("continue", None)`
    - _Requirements: 1.6, 1.8_

  - [x] 3.4 Property test for completion and blocked-termination decisions
    - **Property 23: Completion and blocked-termination decisions**
    - **Validates: Requirements 1.6, 1.8**

- [ ] 4. Dependency analysis and resume transitions
  - [x] 4.1 Implement dependency analyzer (missing deps + cycle detection)
    - Detect tasks whose `depends_on` references an unknown id and mark them `stuck`
    - Detect cycles via DFS with recursion stack; mark every cycle participant `stuck`; return the cycle path for logging
    - _Requirements: 2.9, 2.10, 2.11_

  - [x] 4.2 Property test for dependency health marking
    - **Property 3: Dependency-health marking**
    - **Validates: Requirements 2.9, 2.10, 2.11**
    - Use `task_list_dag_strategy` and `task_list_with_cycle_strategy` composite strategies

  - [x] 4.3 Implement `Resumer.resume`
    - Reset `in_progress` tasks to `failing` without incrementing `retry_count`; set `resumed_from_interruption = True`
    - Run the dependency analyzer and return the cycle path plus stuck ids
    - _Requirements: 2.9, 2.10, 2.11, 14.3, 14.4, 14.5, 14.6_

  - [x] 4.4 Property test for Resumer transition
    - **Property 4: Resume transition preserves retry count and flags interrupted tasks**
    - **Validates: Requirements 14.3, 14.4, 14.5, 14.6**

  - [x] 4.5 Implement status-update function
    - Given a `Task` and a `list[CheckResult]`, return the next `(status, retry_count)` where overall pass sets `passing` and leaves retry unchanged; any fail sets `failing` and increments retry
    - _Requirements: 2.5, 2.6_

  - [x] 4.6 Property test for status-update determinism
    - **Property 2: Task status update is determined by validation results**
    - **Validates: Requirements 2.5, 2.6**

- [ ] 5. Budget Tracker and Token Accountant (pure logic)
  - [ ] 5.1 Implement `BudgetTracker`
    - Track iteration count, per-iteration created count, per-run created count, wall-clock start
    - Expose `can_create_this_iteration`, `can_create_this_run`, `record_created(n)`, `check_wall_clock`, `record_iteration`
    - Ensure admitted pending-queue tasks are NOT recorded against the run budget
    - _Requirements: 9.8, 10.1, 10.4, 10.5, 10.6, 10.7, 1.7_

  - [x] 5.2 Property test for wall-clock and iteration-cap termination
    - **Property 22: Wall-clock and iteration-cap termination**
    - **Validates: Requirements 10.5, 1.7**

  - [x] 5.3 Implement `TokenAccountant`
    - Append `LlmCallRecord`s; compute totals by kind; apply `Model_Pricing` to derive `estimated_cost` when available; emit warnings for calls without token data
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

  - [x] 5.4 Property test for token record generation
    - **Property 27: Token record generation for every LLM call**
    - **Validates: Requirements 12.1, 12.2, 12.6**

  - [x] 5.5 Property test for cost computation
    - **Property 28: Cost computation**
    - **Validates: Requirements 12.3, 12.4, 12.5**

- [ ] 6. Diff function for `tasks.json` snapshots (pure logic)
  - [x] 6.1 Implement snapshot diff
    - Compare two `list[Task]` / raw dict snapshots keyed by `id`, returning `created`, `modified`, `deleted` sets
    - _Requirements: 8.2, 8.3_

  - [x] 6.2 Property test for diff-based task-creation detection
    - **Property 16: Diff-based task-creation detection**
    - **Validates: Requirements 8.2, 8.3**
    - Use `pre_and_post_snapshot_strategy` composite strategy

- [ ] 7. Configuration loading and merge
  - [x] 7.1 Implement `ConfigLoader`
    - Merge precedence: defaults < `ralph.config.json` < CLI args via `Config.model_copy(update=...)`
    - Resolve paths to absolute paths relative to the project root
    - Fail-fast when required files/directories are missing (`tasks_path`, `summary_path`, `personas_dir`)
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8, 15.9_

  - [x] 7.2 Property test for config merge precedence and defaults
    - **Property 24: Config merge precedence and defaults**
    - **Validates: Requirements 15.2, 15.4, 15.5, 15.6, 15.7, 15.8**

  - [x] 7.3 Property test for required-file fail-fast
    - **Property 25: Config required-file fail-fast**
    - **Validates: Requirements 15.9**
    - Uses pytest `tmp_path` plus injected missing-path variants; shrinks via `st.sampled_from(["tasks", "summary", "personas"])`

- [ ] 8. Persona Registry
  - [x] 8.1 Implement `PersonaRegistry.load` and accessors
    - Read every file in the personas directory; detect YAML vs Markdown+frontmatter; parse with `yaml.safe_load` and validate through the `Persona` Pydantic model
    - Fail fast on duplicate names and missing required fields with a descriptive error (file + field)
    - Expose `get(name)`, `all()`, and `describe_all_for_orchestrator()`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8_

  - [x] 8.2 Property test for persona-registry loader determinism and fail-fast behavior
    - **Property 5: Persona registry loader is a deterministic mapping with fail-fast duplicates and missing fields**
    - **Validates: Requirements 3.2, 3.4, 3.5, 3.6, 3.8**

  - [x] 8.3 Implement prompt-template placeholder renderer
    - Substitute `{{project_brief}}`, `{{task_spec}}`, `{{task_id}}`, `{{task_title}}`, `{{persona_name}}`
    - _Requirements: 3.7, 6.4_

  - [x] 8.4 Property test for prompt-template placeholder substitution
    - **Property 6: Prompt-template placeholder substitution**
    - **Validates: Requirements 3.7**

- [ ] 9. Atomic `tasks.json` / pending-queue writer
  - [x] 9.1 Implement atomic write helper in `atomic_io.py`
    - Pattern: write to `<path>.tmp`, `os.fsync`, close, `os.replace(<tmp>, <path>)`
    - On Windows `PermissionError`, retry with exponential backoff (50ms, 100ms, 200ms, 400ms, 800ms, max 5 attempts), cleanup tmp on final failure
    - _Requirements: 14.2_

  - [x] 9.2 Unit tests for atomic write helper
    - Verify old or new file visible at all times; verify Windows retry path via mock `os.replace` raising `PermissionError` on first N calls
    - _Requirements: 14.2_

- [ ] 10. Pending Queue Manager
  - [x] 10.1 Implement `PendingQueueManager`
    - `process_on_startup`: load `pending_tasks.json`, validate each entry against `Task` and persona existence, admit valid entries (stamp `admitted_run_id`), discard invalid with reason, truncate the file, log counts
    - `spill(task, reason, run_id)`: append to the queue preserving creation metadata and stamping `spilled_run_id`, using the atomic writer
    - Fail-fast on unparseable JSON (`JSONDecodeError`) with a descriptive error
    - _Requirements: 8.10, 8.11, 8.13, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11_

  - [x] 10.2 Property test for pending-queue round trip
    - **Property 20: Pending-queue round trip**
    - **Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.9, 9.10**

  - [x] 10.3 Property test for pending-queue invalid-JSON fatal exit
    - **Property 21: Pending-queue invalid JSON produces a fatal exit**
    - **Validates: Requirements 9.11**

- [ ] 11. Task Spec parser and Context Composer
  - [x] 11.1 Implement Task Spec parser
    - Split `---` frontmatter, parse YAML, validate through `TaskSpec`
    - On `ValidationError`, convert into a "mark task stuck" result with the invalid field identified
    - _Requirements: 7.1, 18.1, 18.2, 18.3, 18.4, 18.7_

  - [x] 11.2 Property test for Task Spec parse round trip and invalid rejection
    - **Property 11: Task spec parse round trip and invalid-rejection**
    - **Validates: Requirements 7.1, 18.1, 18.2, 18.3, 18.4, 18.7**

  - [x] 11.3 Implement context-file inliner
    - For each `context_files` path, include its contents in the context window; warn and continue on missing paths; never abort composition
    - _Requirements: 18.5, 18.6_

  - [x] 11.4 Property test for missing context-file warnings
    - **Property 12: Missing context-file references produce warnings and do not abort composition**
    - **Validates: Requirements 18.5, 18.6**

  - [x] 11.5 Implement `ContextComposer.compose`
    - Assemble sections in order: loop framing, resumed notice (when applicable), project brief, task spec with referenced files inlined, persona prompt template (rendered), persona instructions, escalation context (when present)
    - Estimate tokens; on overflow truncate the project brief to its summary while keeping the full spec and persona prompt/instructions; return `ContextWindow(text, approx_tokens, truncated)`
    - _Requirements: 5.3, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 14.5_

  - [x] 11.6 Property test for context window content inclusion
    - **Property 9: Context window content inclusion**
    - **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 14.5, 5.3**

  - [x] 11.7 Property test for context window truncation
    - **Property 10: Context window truncation preserves spec, persona, and instructions**
    - **Validates: Requirements 6.7**

- [ ] 12. Checkpoint - pure logic and I/O primitives
  - [x] 12.1 Checkpoint
    - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Kiro CLI Invoker
  - [x] 13.1 Implement `KiroInvoker.invoke` in `kiro.py`
    - Spawn `kiro-cli chat --no-interactive` via `asyncio.create_subprocess_exec`
    - Pipe the composed context on stdin; stream stdout/stderr line-by-line concurrently to both the per-iteration log file and process stdout (tee)
    - Parse token usage from Kiro CLI's structured envelope via a dedicated Pydantic model; warn and return `token_usage=None` when absent
    - Enforce optional `timeout_ms` via `asyncio.wait_for`; return `KiroInvocationResult(exit_code, stdout, stderr, token_usage, duration_ms)`
    - Determine `call_kind` from the caller's selection path (escalation path -> `"escalation"`, else `"persona_execution"` for persona iterations)
    - _Requirements: 6.1, 11.2, 11.5, 12.1, 12.6_

  - [x] 13.2 Integration test for Kiro CLI invocation
    - Stub `kiro-cli` via a tiny Python script written to `tmp_path` that echoes a configurable structured response (with or without token usage)
    - Assert the loop passes the composed context on stdin, captures stdout/stderr, parses tokens, and tees to both log file and stdout
    - _Requirements: 11.2, 11.5, 12.1, 12.6_

- [ ] 14. Orchestrator (LLM-based persona selection)
  - [x] 14.1 Implement orchestrator prompt builder
    - Build the LLM prompt containing task id, title, current status, tags, retry counter, task-spec summary, task creation metadata, every persona name and description, and a strict JSON-output instruction
    - _Requirements: 4.2, 4.3, 4.4, 3.8_

  - [x] 14.2 Property test for orchestrator prompt content
    - **Property 8: Orchestrator prompt content**
    - **Validates: Requirements 4.3, 4.4**

  - [x] 14.3 Implement `Orchestrator.select_persona`
    - Handle explicit target (present -> use it; missing -> mark task stuck, no LLM call)
    - Invoke a single structured LLM call through `KiroInvoker` for the `orchestrator_selection` kind; parse the response into an `OrchestratorDecision` Pydantic model
    - Handle hallucinated name (mark task stuck), network/parse/timeout errors (fallback persona with logged warning; no retry)
    - Record token usage and return `PersonaSelection` with `path`, `rationale`, and `llm_decision_raw`
    - _Requirements: 2.3, 2.4, 4.1, 4.2, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 12.1_

  - [x] 14.4 Property test for persona-selection routing
    - **Property 7: Persona selection routes to the correct path for every task-and-registry combination**
    - **Validates: Requirements 2.3, 4.1, 4.2, 4.5, 4.7, 4.8, 4.9, 5.1, 5.2, 5.4**
    - Use `llm_outcome_strategy` sampling over `"valid-known"`, `"valid-hallucinated"`, `"parse-error"`, `"network-error"`, `"timeout"`

- [ ] 15. Escalation Handler
  - [x] 15.1 Implement `EscalationHandler`
    - `should_escalate(task, config)` returns True iff `task.retry_count >= config.escalation_threshold`
    - `route`: when an escalation persona is configured, return it with `path="escalation"`; otherwise delegate to normal orchestrator selection
    - `build_escalation_context`: compose retry history + prior failing validation outputs + prior iteration logs for the task
    - Log the escalation event (task id, retry count, selected persona or "none configured")
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 10.3_

  - [x] 15.2 Unit tests for Escalation Handler edge cases
    - No escalation persona configured path; escalation-with-persona path; retry-limit-exhausted still marks stuck
    - _Requirements: 5.4, 5.6_

- [ ] 16. Validator
  - [x] 16.1 Implement shell and file_exists checks
    - `shell`: run each command via `asyncio.create_subprocess_exec`, pass iff every command exits 0
    - `file_exists`: pass iff every `pathlib.Path(p).exists()`
    - Enforce `validation_timeout_ms` per check via `asyncio.wait_for`; on timeout, terminate, mark check failing with `timed_out=True`, log timeout entry
    - _Requirements: 7.2, 7.4, 7.5, 7.11, 7.13_

  - [x] 16.2 Implement `persona_review` check
    - Resolve pass condition: spec override -> persona default -> stuck + error
    - Invoke the reviewing persona in a separate Kiro CLI session with task artifacts + resolved pass condition; the session has no loop framing, no write access to `tasks.json`, and cannot itself trigger reviews
    - Reject self-review (reviewing persona == executing persona) with a logged error
    - Parse a structured `{"verdict": "pass"|"fail", "rationale": "..."}` via Pydantic; record `reviewing_persona`, `resolved_pass_condition`, `verdict`, `rationale` on the `CheckResult`
    - _Requirements: 7.3, 7.6, 7.7, 7.8, 7.9, 7.10_

  - [x] 16.3 Implement `Validator.run` aggregation
    - Run all checks, aggregate into `ValidationResult(overall, checks, timed_out_checks)` with `overall == "pass"` iff every check passes; capture per-check output in the iteration log
    - _Requirements: 7.10, 7.11, 7.12, 2.6_

  - [x] 16.4 Property test for validation-check aggregation
    - **Property 13: Validation-check aggregation matches per-type pass rules**
    - **Validates: Requirements 7.5, 7.10, 7.11, 7.12**

  - [x] 16.5 Property test for persona_review pass-condition resolution
    - **Property 14: persona_review pass-condition resolution**
    - **Validates: Requirements 7.6, 7.7, 7.8**

  - [x] 16.6 Property test for validation timeout handling
    - **Property 15: Validation timeout produces a task failure with timeout log**
    - **Validates: Requirements 7.13**
    - Simulate slow checks via mocked `asyncio.sleep` so shrinking converges quickly

- [ ] 17. Task Creation Processor
  - [x] 17.1 Implement new-entry validation pipeline
    - Validate each candidate entry against `Task` Pydantic model; verify `target_persona` (when set) exists in the registry; check `creation_chain` depth vs `max_creation_chain_depth`
    - Reject entries failing any check; do NOT spill rejected entries; log the reason
    - _Requirements: 8.4, 8.7, 8.12, 17.6_

  - [x] 17.2 Property test for new-entry validation pipeline
    - **Property 17: New-entry validation pipeline rejects invalid entries without spilling**
    - **Validates: Requirements 8.4, 8.7, 8.12, 17.6**

  - [x] 17.3 Implement revert of unauthorized modifications/deletions
    - For every modified or deleted entry in the post snapshot whose `id` != executing task id, restore the pre-snapshot state and log a warning with iteration + task id + acting persona
    - _Requirements: 8.8_

  - [x] 17.4 Property test for revert of non-executing task edits
    - **Property 18: Modifications or deletions of non-executing tasks are reverted**
    - **Validates: Requirements 8.8**

  - [x] 17.5 Implement budget + spill logic
    - Admit up to per-iteration budget; admit up to remaining per-run budget; spill surplus to `pending_tasks.json` with `spilled_run_id` and preserved creation metadata
    - Stamp accepted entries with `created_at_iteration` and `created_by_persona`
    - _Requirements: 8.5, 8.10, 8.11, 8.13, 9.8_

  - [x] 17.6 Stateful property test for budget + spill invariant
    - **Property 19: Budget + spill invariant preserves creation metadata**
    - **Validates: Requirements 8.10, 8.11, 8.13, 9.8**
    - Implement with `hypothesis.stateful.RuleBasedStateMachine` over a sequence of created-task batches

  - [x] 17.7 Wire `TaskCreationProcessor.process`
    - Orchestrate: diff -> revert unauthorized edits -> validate -> budget-admit/spill -> re-run cycle detection on merged tasks -> return `TaskCreationResult(accepted, rejected, reverted, spilled, cycle_stuck)`
    - Write merged state back via the atomic writer
    - _Requirements: 2.10, 2.11, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.10, 8.11, 8.12, 8.13_

- [ ] 18. Planner Bootstrap
  - [x] 18.1 Implement `Planner.bootstrap`
    - Invoke the configured planner persona in a Kiro CLI session with the project brief, call kind `"planner"`, with pre-snapshot `[]`
    - Delegate to `TaskCreationProcessor.process` against the post-snapshot so planner output goes through the same validation/budget pipeline as in-iteration creation
    - Emit info/error logs for missing planner persona or empty task list without auto-planner
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8, 12.1_

  - [x] 18.2 Property test for auto-planner branching
    - **Property 26: Auto-planner branching**
    - **Validates: Requirements 17.3, 17.4, 17.7**

- [ ] 19. Git Manager
  - [x] 19.1 Implement `GitManager` using `git` subprocess
    - `is_enabled` / `is_git_repo` via `git rev-parse --is-inside-work-tree`
    - `iteration_commit`: `git add -A` + `git commit -m "ralph: iter=<N> task=<id> persona=<name> outcome=<outcome>"`; warn and continue on non-repo, log-and-continue on commit failure, no-op when disabled
    - `rollback(iteration)`: `git log --grep` for the iteration tag; exit non-zero if no match; else `git checkout <sha> -- .`
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_

  - [x] 19.2 Property test for iteration commit message format
    - **Property 29: Iteration commit message format**
    - **Validates: Requirements 13.2**

  - [x] 19.3 Property test for rollback to unknown iteration
    - **Property 30: Rollback to an unknown iteration exits non-zero**
    - **Validates: Requirements 13.6**

  - [x] 19.4 Integration test for Git Manager
    - Create a temp repo via `git init` in `tmp_path`; run a short loop scenario with the real git binary; verify commits and rollbacks; cover non-repo and disabled configurations; simulate a failing git command
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_

- [ ] 20. Logger and observability wiring
  - [x] 20.1 Implement `Logger` and `IterationLogWriter`
    - Back with `structlog` configured for JSON rendering
    - Provide a tee handler writing identical lines to the per-iteration log file and process stdout
    - `record`, `append_token_usage`, `finalize(outcome)` for per-iteration entries; `run_summary` for the end-of-run object
    - Ensure every acceptance-criterion-mandated field lands in the per-iteration log (task id, persona name, selection path, orchestrator rationale when applicable, validation outcomes with persona_review verdicts+rationales, task-creation-event summary, iteration outcome, token records per call)
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 12.2, 12.5_

  - [x] 20.2 Integration test for dual-sink logging
    - Run a short loop with a stub Kiro CLI; assert identical lines on stdout and in the log file
    - _Requirements: 11.5_

- [ ] 21. Checkpoint - observability and external adapters
  - [x] 21.1 Checkpoint
    - Ensure all tests pass, ask the user if questions arise.

- [ ] 22. CLI wiring
  - [x] 22.1 Implement the `click` entry point and subcommand skeleton
    - `ralph` group with subcommands `run`, `init`, `init-tasks`, `rollback <iteration-number>`
    - Wire `--config` and per-field overrides for `run`; pass results to `ConfigLoader.load`
    - _Requirements: 15.1, 15.2, 15.3, 16.8, 13.5_

  - [x] 22.2 Implement `ralph init`
    - Create `SUMMARY.md` template, `tasks.json` with `[]`, `pending_tasks.json` with `[]`, `ralph.config.json` with defaults (placeholder fallback persona, placeholder planner persona, auto-planner disabled, git enabled, default escalation threshold), `specs/`, `personas/` seeded with a minimal default persona (name, description, prompt template)
    - When existing files are detected, prompt the user for confirmation before overwriting each file
    - `--template <name>` seeds domain personas + planner persona
    - `--force` bypasses prompts; in non-interactive contexts without `--force`, exit non-zero with a message directing to use `--force`
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 16.9_

  - [x] 22.3 Implement `ralph init-tasks`
    - Load config and persona registry; invoke `Planner.bootstrap(reason="init-tasks")`
    - _Requirements: 16.8, 17.2, 17.5, 17.6, 17.7, 17.8_

  - [x] 22.4 Implement `ralph rollback <iteration-number>`
    - Delegate to `GitManager.rollback`
    - _Requirements: 13.5, 13.6_

  - [x] 22.5 Implement `ralph run` main loop
    - Startup: load config -> load persona registry -> run `Resumer.resume` -> `PendingQueueManager.process_on_startup` -> auto-planner branching -> iteration loop
    - Iteration: `TaskSelector.next` -> if retry limit reached and escalated, still bounded by retry limit; else Escalation check -> Orchestrator/Escalation persona selection -> `ContextComposer.compose` -> flip task to `in_progress` and persist -> pre-snapshot -> `KiroInvoker.invoke` -> post-snapshot -> `Validator.run` -> `TaskCreationProcessor.process` -> status update -> `GitManager.iteration_commit` -> write iteration log, accumulate tokens
    - Termination: completion (all passing -> exit 0), blocked (exit non-zero), budget caps (exit non-zero), wall-clock (exit non-zero); persist task list on every exit; write the `RunSummary`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 5.1, 5.2, 5.3, 5.6, 10.1, 10.2, 10.3, 10.4, 10.5, 11.4, 14.1, 14.2_

  - [x] 22.6 Unit tests for CLI argument parsing
    - Use `click.testing.CliRunner` for each subcommand happy path and `--help`
    - _Requirements: 15.2, 16.8, 13.5_

- [ ] 23. End-to-end integration tests
  - [x] 23.1 Stub Kiro CLI harness
    - Write a reusable `tests/support/fake_kiro.py` that emits configurable structured responses (with/without token envelope) and can optionally mutate `tasks.json`
    - _Requirements: 6.1, 12.1, 12.6_

  - [x] 23.2 Filesystem scaffolding integration test
    - Run `ralph init` against an empty `tmp_path`; assert every scaffolded file and dir; run again and verify overwrite prompt; run `ralph init --template book` and verify persona seeding
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 16.9_

  - [x] 23.3 Resumption end-to-end
    - Start a run with the stub Kiro CLI; `SIGTERM` mid-iteration; restart; assert the `in_progress` task was reset to `failing`, retry unchanged, and the next iteration's context window contains the interrupted-task notice
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6_

  - [x] 23.4 Pending-queue cross-run integration test
    - Run 1 spills N surplus tasks; Run 2 admits them; verify admitted tasks are not counted against Run 2's creation budget and that original creation metadata is preserved
    - _Requirements: 8.10, 8.11, 8.13, 9.5, 9.7, 9.8, 9.9, 9.10_

  - [x] 23.5 Concurrent non-executing-task edit revert
    - Stub Kiro CLI modifies a non-executing task entry during the iteration; assert the loop reverts the edit on merge and logs a warning
    - _Requirements: 8.8_

  - [x] 23.6 Planner bootstrap integration test
    - Empty `tasks.json` with auto-planner enabled; stub Kiro CLI writes three tasks; assert planner invocation, validation outcomes, accepted/rejected counts in logs, and planner token record
    - _Requirements: 17.2, 17.3, 17.5, 17.6, 17.8, 12.1_

  - [x] 23.7 Book-writing smoke test
    - Minimal end-to-end run with Writer/Reviewer/Editor/Fact-Checker/Outline/Planner personas and 1-2 chapters; assert correct persona selections, validation outcomes, and iteration commit messages
    - _Requirements: 1.6, 4.1, 4.2, 7.10, 13.2_

- [ ] 24. Final checkpoint - Ensure all tests pass
  - [x] 24.1 Checkpoint
    - Ensure all tests pass, ask the user if questions arise.

- [ ] 25. Documentation and example personas (optional polish)
  - [x] 25.1 Write the user-facing README
    - Quickstart for `ralph init`, `ralph init-tasks`, `ralph run`, `ralph rollback`; config reference table; directory layout diagram
    - _Requirements: 15.1, 15.3, 16.1, 17.2_

  - [x] 25.2 Ship example book-authoring persona set
    - Add `templates/book/personas/*.yaml` for Writer, Reviewer, Editor, Fact-Checker, Outline, Planner with filled-in descriptions, prompt templates, instructions, and default `persona_review` pass conditions
    - _Requirements: 16.7, 17.1_

  - [x] 25.3 Add JSON Schemas exported from Pydantic
    - Use `TypeAdapter(Task).json_schema()` etc. to export `schemas/task.schema.json`, `tasks.schema.json`, `persona.schema.json`, `task-spec.schema.json`, `config.schema.json` for editor integration
    - _Requirements: 2.1, 2.2, 3.2, 15.3, 18.1_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; they are primarily tests and polish.
- Each task references specific requirement ids for traceability. Property tests additionally cite the property number and the requirements they validate.
- Property tests are interleaved with their target component (not batched at the end) so that pure-logic modules (task selector, resumer, budget tracker, token accountant, diff function, pending queue, config merge, context composer) ship with their invariants already pinned before any subprocess-heavy component is layered on top.
- External interactions (Kiro CLI, git, filesystem scaffolding, resumption, pending-queue cross-run, concurrent edits, planner bootstrap, book-writing smoke test) are covered in the end-to-end integration section (task 23) because they cross process or filesystem boundaries and are not a fit for property-based testing.
- All 30 correctness properties from the design are mapped one-to-one to test tasks (P1-P30); the Property Index below makes that coverage explicit.
- Checkpoints at tasks 12, 21, and 24 surface regressions early, between the pure-logic layer, the subprocess/observability layer, and the final end-to-end layer.

## Property Index (P1-P30 coverage map)

| Property | Task | Component under test |
|---|---|---|
| P1 | 3.2 | Task Selector eligibility + priority |
| P2 | 4.6 | Status update determinism |
| P3 | 4.2 | Dependency health marking |
| P4 | 4.4 | Resumer transition |
| P5 | 8.2 | Persona registry loader |
| P6 | 8.4 | Prompt-template placeholder substitution |
| P7 | 14.4 | Orchestrator persona selection routing |
| P8 | 14.2 | Orchestrator prompt content |
| P9 | 11.6 | Context window content inclusion |
| P10 | 11.7 | Context window truncation |
| P11 | 11.2 | Task Spec parse round trip |
| P12 | 11.4 | Missing context-file warnings |
| P13 | 16.4 | Validation aggregation |
| P14 | 16.5 | persona_review pass-condition resolution |
| P15 | 16.6 | Validation timeout handling |
| P16 | 6.2 | Diff-based task-creation detection |
| P17 | 17.2 | New-entry validation pipeline |
| P18 | 17.4 | Revert unauthorized edits |
| P19 | 17.6 | Budget + spill invariant (stateful) |
| P20 | 10.2 | Pending-queue round trip |
| P21 | 10.3 | Pending-queue invalid JSON fatal exit |
| P22 | 5.2 | Wall-clock + iteration cap |
| P23 | 3.4 | Completion / blocked-termination decisions |
| P24 | 7.2 | Config merge precedence + defaults |
| P25 | 7.3 | Config required-file fail-fast |
| P26 | 18.2 | Auto-planner branching |
| P27 | 5.4 | Token record generation |
| P28 | 5.5 | Cost computation |
| P29 | 19.2 | Iteration commit message format |
| P30 | 19.3 | Rollback to unknown iteration |
