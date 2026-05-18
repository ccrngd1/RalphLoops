"""Pydantic data models for Tasks, TaskSpecs, Personas, and Config."""

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter, field_validator


TaskStatus = Literal["pending", "in_progress", "passing", "failing", "stuck"]


class Task(BaseModel):
    """A single entry in ``tasks.json`` (R2.1, R2.2).

    The model captures the required fields (id, title, priority, status,
    spec_path, retry_count) plus optional routing, dependency, and
    provenance metadata used by the task creation and pending-queue
    subsystems (R8.5, R8.12, R8.13, R9.7, R14.5).
    """

    # Required fields (R2.2)
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    priority: int
    status: TaskStatus
    spec_path: str = Field(..., min_length=1)
    retry_count: int = Field(default=0, ge=0)

    # Optional routing and dependency metadata (R2.2, R2.3, R2.4, R18.2, R4.3)
    target_persona: Optional[str] = None
    depends_on: Optional[list[str]] = None
    tags: Optional[list[str]] = None

    # Creation and provenance metadata (R8.5, R8.12)
    created_at_iteration: Optional[int] = Field(default=None, ge=0)
    created_by_persona: Optional[str] = None
    creation_chain: Optional[list[str]] = None

    # Pending-queue provenance (R8.13, R9.7)
    spilled_run_id: Optional[str] = None
    admitted_run_id: Optional[str] = None

    # Resumer flag (R14.5)
    resumed_from_interruption: Optional[bool] = None

    @field_validator("retry_count")
    @classmethod
    def _retry_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("retry_count must be >= 0")
        return v


# TypeAdapter helper for loading and dumping the top-level ``tasks.json``
# array (R2.1). Use ``TASK_LIST_ADAPTER.validate_json(raw_bytes)`` to load
# and ``TASK_LIST_ADAPTER.dump_json(tasks)`` to serialize.
TASK_LIST_ADAPTER: TypeAdapter[list[Task]] = TypeAdapter(list[Task])


# -- Task Spec models (R7.1-R7.6, R18.1-R18.4) ---------------------------------
#
# A Task_Spec is the Markdown+YAML-frontmatter file stored in ``specs/`` that
# describes a single task. The frontmatter carries routing metadata and
# validation configuration; the body carries the prose sections (objective,
# context references, instructions, notes) consumed by the persona.


class ShellCheckConfig(BaseModel):
    """Validation check that runs one or more shell commands (R7.2, R7.5)."""

    type: Literal["shell"]
    name: Optional[str] = None
    commands: list[str] = Field(..., min_length=1)
    timeout_ms: Optional[int] = Field(default=None, ge=0)


class PersonaReviewCheckConfig(BaseModel):
    """Validation check that invokes a reviewing persona (R7.3, R7.6, R7.7).

    ``persona`` names the reviewing persona. ``pass_condition`` is the
    optional spec-level override; when absent, the reviewing persona's
    default pass condition is used (R7.7). If neither is present, the
    executing task is marked stuck (R7.8).
    """

    type: Literal["persona_review"]
    name: Optional[str] = None
    persona: str
    pass_condition: Optional[str] = None
    timeout_ms: Optional[int] = Field(default=None, ge=0)


class FileExistsCheckConfig(BaseModel):
    """Validation check that asserts every path exists (R7.4, R7.11)."""

    type: Literal["file_exists"]
    name: Optional[str] = None
    paths: list[str] = Field(..., min_length=1)


# Annotated discriminated union keyed on the ``type`` field (R7.1). Pydantic
# uses this to dispatch to the right model at parse time and to raise a
# precise ``ValidationError`` when the discriminator is unknown (R18.7).
ValidationCheckConfig = Annotated[
    Union[ShellCheckConfig, PersonaReviewCheckConfig, FileExistsCheckConfig],
    Field(discriminator="type"),
]


class TaskSpecBody(BaseModel):
    """The prose body of a Task_Spec (R18.3).

    This is the structured representation of the sections that follow the
    YAML frontmatter: ``objective``, ``context_references``, ``instructions``,
    and the optional ``notes`` section.
    """

    objective: str
    context_references: str
    instructions: str
    notes: Optional[str] = None


class TaskSpec(BaseModel):
    """A parsed Task_Spec file (R18.1-R18.4, R7.1).

    Required fields: ``id``, ``title``, and at least one ``validation`` entry
    (R18.1). Optional routing and configuration fields follow R18.2:
    ``target_persona`` (explicit routing override), ``tags`` (input to the
    Orchestrator's LLM persona selection), ``depends_on`` (mirrors the same
    field on the Task entry in tasks.json), ``persona_fields`` (persona-
    specific configuration passed to the selected persona's prompt template),
    and ``context_files`` (relative paths inlined into the Context_Window
    per R18.5).

    The ``body`` field holds the structured prose sections (R18.3).
    """

    id: str
    title: str
    target_persona: Optional[str] = None
    tags: Optional[list[str]] = None
    depends_on: Optional[list[str]] = None
    persona_fields: Optional[dict[str, Any]] = None
    validation: list[ValidationCheckConfig] = Field(..., min_length=1)
    context_files: Optional[list[str]] = None
    body: TaskSpecBody


# -- Persona models (R3.2, R3.3, R3.7, R5.5, R10.x) ---------------------------
#
# Personas live under ``personas/`` (one file per persona, YAML or Markdown +
# frontmatter). The Persona model is the parsed representation. The
# Orchestrator only needs ``name`` and ``description`` for LLM-based selection,
# so ``PersonaDescription`` is the reduced projection passed to it (R3.8,
# R4.2).


class ToolRestrictions(BaseModel):
    """Optional allow/disallow lists scoping the tools a persona may use."""

    allow: Optional[list[str]] = None
    disallow: Optional[list[str]] = None


class Persona(BaseModel):
    """A parsed persona definition (R3.2, R3.3).

    ``prompt_template`` supports the placeholder set documented in R3.7
    (``{{project_brief}}``, ``{{task_spec}}``, ``{{task_id}}``,
    ``{{task_title}}``, ``{{persona_name}}``).
    ``default_persona_review_pass_condition`` supplies the fallback pass
    condition when a ``persona_review`` check omits one (R7.7).
    """

    name: str
    description: str
    prompt_template: str
    instructions: Optional[str] = None
    tool_restrictions: Optional[ToolRestrictions] = None
    default_persona_review_pass_condition: Optional[str] = None


class PersonaDescription(BaseModel):
    """Subset passed to the Orchestrator for LLM-based selection (R3.8, R4.2)."""

    name: str
    description: str


# -- Config (R15.3-R15.7, R10.x, R5.5, R6.7, R7.13) ---------------------------
#
# Parsed form of ``ralph.config.json``. Defaults live here via
# ``Field(default=...)`` so CLI overrides applied via
# ``Config.model_copy(update=...)`` compose cleanly (R15.2).


class ModelPrice(BaseModel):
    """Per-token pricing for a single model id (R12.3, R12.4)."""

    input_price_per_token: float = Field(..., ge=0)
    output_price_per_token: float = Field(..., ge=0)


class Config(BaseModel):
    """Runtime configuration for a Ralph Loop run (R15.3).

    ``fallback_persona`` is the only required field; every other knob has a
    documented default drawn from R10.x, R5.5, R6.7, R7.13, R15.5-R15.7.
    """

    # Paths (R15.3)
    tasks_path: str = "tasks.json"
    summary_path: str = "SUMMARY.md"
    personas_dir: str = "personas/"
    specs_dir: str = "specs/"
    pending_tasks_path: str = "pending_tasks.json"  # R15.5
    log_dir: str = "logs/"

    # Personas (R15.3, R5.5)
    fallback_persona: str
    escalation_persona: Optional[str] = None
    escalation_threshold: int = Field(default=3, ge=0)  # R5.5
    planner_persona: Optional[str] = None
    automatic_planner: bool = False  # R15.7

    # Orchestrator (R15.3, R15.4)
    orchestrator_llm_command: Optional[str] = None  # falls back to claude_cli_command
    orchestrator_model_id: Optional[str] = None

    # Loop (R15.3, R10)
    max_iterations: int = Field(default=50, ge=1)  # R10.1
    max_retries_per_task: int = Field(default=5, ge=1)  # R10.2
    wall_clock_timeout_ms: int = Field(default=60 * 60 * 1000, ge=0)  # R10.4
    validation_timeout_ms: int = Field(default=5 * 60 * 1000, ge=0)  # R7.13

    # Claude Code CLI (R15.3). ``default_model_id`` is passed as ``--model <id>``
    # on every Claude Code CLI invocation (persona execution, persona_review,
    # planner, escalation). ``orchestrator_model_id`` above overrides
    # this for orchestrator_selection calls only; when it's ``None`` the
    # orchestrator uses ``default_model_id`` too.
    claude_cli_command: str = "claude"
    default_model_id: str = "claude-opus-4.7"

    # Task Creation Budgets (R10.6, R10.7, R10.8)
    per_iteration_task_creation_budget: int = Field(default=10, ge=0)
    per_run_task_creation_budget: int = Field(default=100, ge=0)
    max_creation_chain_depth: int = Field(default=5, ge=0)

    # Context (R6.7, R2.1-R2.2)
    max_context_tokens: int = Field(default=32_000, ge=1)
    max_context_file_bytes: int = Field(default=65536, gt=0)

    # Git (R15.3, R15.6)
    git_integration_enabled: bool = True

    # Observability (R15.3, R12.3, R12.4)
    model_pricing: dict[str, ModelPrice] = Field(default_factory=dict)


# -- Observability: Token accounting (R12.1-R12.6) ----------------------------
#
# The Token Accountant records one ``LlmCallRecord`` per Claude Code CLI / LLM call
# and aggregates them into ``RunTokenTotals`` for the run summary (R12.5).


CallKind = Literal[
    "persona_execution",
    "orchestrator_selection",
    "persona_review",
    "planner",
    "escalation",
]


class TokenUsage(BaseModel):
    """Raw token counts reported by a single LLM call (R12.1, R12.2)."""

    input_tokens: int
    output_tokens: int
    model: Optional[str] = None


class LlmCallRecord(BaseModel):
    """Persisted record of a single LLM call (R12.1, R12.2, R12.6).

    ``input_tokens``, ``output_tokens``, and ``estimated_cost`` are optional
    because Claude Code CLI may omit the token envelope (R12.6) and because pricing
    may not be configured for the model (R12.4).
    """

    kind: CallKind
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost: Optional[float] = None


class KindTotals(BaseModel):
    """Per-call-kind aggregate of tokens and cost (R12.5)."""

    input: int = 0
    output: int = 0
    cost: Optional[float] = None


class RunTokenTotals(BaseModel):
    """Run-level token and cost totals (R12.5)."""

    total_input: int = 0
    total_output: int = 0
    total_combined: int = 0
    total_estimated_cost: Optional[float] = None
    by_kind: dict[CallKind, KindTotals] = Field(default_factory=dict)


# -- Observability: Iteration log + run summary (R11.3, R11.4) ----------------


IterationOutcome = Literal["pass", "fail", "stuck", "escalated", "timeout"]
SelectionPath = Literal["explicit", "llm", "fallback", "escalation"]


class ContextSummary(BaseModel):
    """Approximate size and provenance of the composed Context_Window.

    ``resumed_from_interruption`` is set when the executing task carried the
    Resumer flag and the resumed notice was inlined (R14.5). ``escalation_enriched``
    is set when supplemental escalation context was included (R5.3).
    """

    approx_tokens: int
    truncated: bool
    resumed_from_interruption: bool
    escalation_enriched: bool


class ClaudeInvocationLog(BaseModel):
    """Summary of the Claude Code CLI subprocess invocation for the iteration (R11.3)."""

    exit_code: int
    duration_ms: int
    stdout_path: str
    stderr_path: str


# -- Validator result models (R7.2-R7.13) -------------------------------------


Verdict = Literal["pass", "fail"]
CheckType = Literal["shell", "persona_review", "file_exists"]


class CheckResult(BaseModel):
    """Outcome of a single validation check (R7.2, R7.4, R7.9, R7.10, R7.13).

    ``rationale``, ``resolved_pass_condition``, and ``reviewing_persona`` are
    only populated for ``persona_review`` checks (R7.9, R7.10). ``timed_out``
    is set when the check exceeded ``validation_timeout_ms`` (R7.13).
    """

    type: CheckType
    name: str
    verdict: Verdict
    output: str
    rationale: Optional[str] = None
    resolved_pass_condition: Optional[str] = None
    reviewing_persona: Optional[str] = None
    duration_ms: int
    timed_out: bool = False


class ValidationResult(BaseModel):
    """Aggregated result returned by the Validator (R7.12).

    ``overall`` is ``"pass"`` iff every check in ``checks`` is ``"pass"``.
    ``timed_out_checks`` lists the ``name`` of each check that hit the
    per-check timeout (R7.13).
    """

    overall: Verdict
    checks: list[CheckResult]
    timed_out_checks: list[str]


# -- Iteration log entry building blocks (R11.3) ------------------------------


class ValidationLog(BaseModel):
    """Validator output as embedded in the iteration log (R11.3)."""

    overall: Verdict
    checks: list[CheckResult]


class TaskCreationEventLog(BaseModel):
    """Per-iteration task-creation summary for the iteration log (R8.9, R11.3)."""

    accepted_count: int
    rejected_count: int
    spilled_count: int
    reverted_ids: list[str]


class GitCommitLog(BaseModel):
    """Git commit outcome embedded in the iteration log (R13.2, R13.3, R13.4)."""

    sha: Optional[str] = None
    skipped: bool
    skip_reason: Optional[str] = None


class IterationLogEntry(BaseModel):
    """One JSON object per iteration, as specified in R11.3.

    ``orchestrator_rationale`` is populated when ``selection_path == "llm"``
    (R4.6). ``task_creation_event`` is ``None`` for iterations that produced
    no task-creation diff. ``git_commit`` is ``None`` when git integration is
    disabled or the working tree is not a git repo (R13.3, R13.4).
    """

    iteration: int
    run_id: str
    start_time: str
    end_time: str

    task_id: str
    persona_name: str
    selection_path: SelectionPath
    orchestrator_rationale: Optional[str] = None

    context_summary: ContextSummary
    claude_invocation: ClaudeInvocationLog
    validation: ValidationLog

    task_creation_event: Optional[TaskCreationEventLog] = None

    llm_calls: list[LlmCallRecord]
    outcome: IterationOutcome

    git_commit: Optional[GitCommitLog] = None


class RunSummary(BaseModel):
    """End-of-run totals written to SUMMARY.md / the run summary log (R11.4, R12.5)."""

    run_id: str
    total_iterations: int
    status_counts: dict[TaskStatus, int]
    total_new_tasks: int
    escalation_events: int
    elapsed_ms: int
    token_totals: RunTokenTotals
    exit_code: int


# -- Orchestrator and Context Composer runtime results (R4.6, R4.10, R6.7) ----


class PersonaSelection(BaseModel):
    """Outcome of ``Orchestrator.select_persona`` (R4.1, R4.6, R4.10).

    ``path`` records how the persona was chosen (R4.10). ``rationale`` and
    ``llm_decision_raw`` are populated when the LLM was consulted (R4.6).
    ``token_usage`` is populated when Claude Code CLI reports tokens (R12.1).
    """

    persona: Persona
    path: SelectionPath
    rationale: Optional[str] = None
    llm_decision_raw: Optional[str] = None
    token_usage: Optional[TokenUsage] = None


class ContextWindow(BaseModel):
    """The composed prompt handed to Claude Code CLI (R6.1, R6.7).

    ``truncated`` is set when the Context Composer had to shrink the project
    brief to fit within ``Config.max_context_tokens`` (R6.7).
    """

    text: str
    approx_tokens: int
    truncated: bool


class ClaudeInvocationResult(BaseModel):
    """Result of a single Claude Code CLI invocation (R6.1, R11.2, R12.1, R12.6).

    ``exit_code`` is the process exit code; ``stdout`` and ``stderr`` are the
    complete captured output streams. ``token_usage`` is ``None`` when the
    Claude Code CLI stdout did not contain a parseable token envelope (R12.6).
    ``duration_ms`` is the wall-clock time from process spawn to wait.
    """

    exit_code: int
    stdout: str
    stderr: str
    token_usage: Optional[TokenUsage] = None
    duration_ms: int


# -- Git Manager result (R13.2, R13.3, R13.4, R13.7) --------------------------


class CommitResult(BaseModel):
    """Outcome of a per-iteration git commit attempt (R13.2, R13.3, R13.4)."""

    sha: Optional[str] = None
    skipped: bool
    skip_reason: Optional[str] = None


# -- Task Creation Processor + Pending Queue result models (R8, R9) -----------


SpillReason = Literal["per_iteration_budget", "per_run_budget"]


class DiscardedEntry(BaseModel):
    """A pending-queue entry that failed validation on admit (R9.2, R9.6)."""

    raw_entry: dict
    reason: str


class RejectedEntry(BaseModel):
    """A post-iteration task-creation entry that failed validation (R8.4, R8.7, R8.12)."""

    entry: dict[str, Any]
    reason: str


class RevertedEntry(BaseModel):
    """A pre-existing task that was modified or deleted mid-iteration (R8.8)."""

    task_id: str
    reason: Literal["modified", "deleted"]


class PendingQueueResult(BaseModel):
    """Outcome of ``PendingQueueManager.process_on_startup`` (R9.1-R9.7)."""

    loaded: int
    admitted: list[Task]
    discarded: list[DiscardedEntry]


class TaskCreationResult(BaseModel):
    """Outcome of ``TaskCreationProcessor.process`` (R8.2-R8.13).

    ``accepted`` are the new tasks admitted to ``tasks.json`` this iteration.
    ``rejected`` are entries that failed schema/persona/chain-depth checks and
    were discarded (R8.4, R8.7, R8.12). ``reverted`` tracks pre-existing tasks
    that were illicitly modified or deleted and restored (R8.8). ``spilled``
    are surplus accepted tasks routed to the pending queue because a budget
    was exhausted (R8.10, R8.11, R8.13). ``cycle_stuck`` lists task ids marked
    stuck by the post-merge cycle-detection pass (R2.10, R2.11).
    """

    accepted: list[Task]
    rejected: list[RejectedEntry]
    reverted: list[RevertedEntry]
    spilled: list[Task]
    cycle_stuck: list[str]


# -- Task Selector termination decision (R1.6, R1.8) --------------------------


TerminationVerdict = Literal["success", "blocked", "continue"]


class TerminationDecision(BaseModel):
    """Outcome of the loop-termination decision function (R1.6, R1.8).

    The loop calls :func:`ralph_loop.task_selector.termination_decision`
    each time it needs to decide whether another iteration should run. The
    three cases are mutually exclusive (design Property 23):

    - ``verdict="success"`` with ``exit_code=0`` when every task has status
      ``"passing"`` (R1.6). An empty task list vacuously satisfies this
      condition, so the loop exits cleanly rather than looping forever.
    - ``verdict="blocked"`` with ``exit_code`` set to a non-zero value when
      every non-passing task is either ``"stuck"`` or has at least one
      ``depends_on`` id that references a task whose status is not
      ``"passing"`` (R1.8). ``blocked_ids`` lists the task ids that are
      blocked; ``blocking_dep_ids`` lists the union of non-passing
      dependency ids across those blocked tasks, so the caller can log
      both the stuck/blocked tasks and the dependencies that are holding
      them up.
    - ``verdict="continue"`` when at least one non-passing task can still
      make progress. ``exit_code`` stays ``None`` because the loop is not
      terminating.
    """

    verdict: TerminationVerdict
    exit_code: Optional[int] = None
    blocked_ids: list[str] = Field(default_factory=list)
    blocking_dep_ids: list[str] = Field(default_factory=list)


# -- Dependency analyzer result (R2.9, R2.10, R2.11) --------------------------


class DependencyAnalysis(BaseModel):
    """Outcome of the pure dependency analyzer (R2.9, R2.10, R2.11).

    ``updated_tasks`` is the input task list with any task newly identified
    as stuck (by a missing ``depends_on`` target or by cycle participation)
    re-labeled via ``task.model_copy(update={"status": "stuck"})``. Tasks
    that were already stuck are passed through unchanged; tasks unaffected
    by either check are returned as-is. The list preserves input order.

    ``stuck_by_missing_dep`` lists every task whose ``depends_on`` contains
    at least one identifier that does not exist in the analyzed list
    (R2.9); the entries are the updated versions from ``updated_tasks``.
    ``stuck_by_cycle`` lists every task participating in at least one
    detected cycle (R2.11), again in the updated form.

    ``detected_cycles`` is the list of cycle paths discovered by the DFS
    sweep (R2.11). Each inner list is an ordered sequence of task ids in
    the order they appear on the DFS recursion stack at the point the
    back-edge was found. The list can be empty when no cycle exists, can
    contain a single self-loop (``[A]`` when ``A.depends_on == ["A"]``),
    or can contain multiple entries when disjoint cycles exist in the
    graph. Callers use this list to log the cycle path (R2.11).
    """

    updated_tasks: list["Task"]
    stuck_by_missing_dep: list["Task"]
    stuck_by_cycle: list["Task"]
    detected_cycles: list[list[str]]


# -- Snapshot diff result (R8.2, R8.3) ----------------------------------------


class SnapshotDiff(BaseModel):
    """Structured diff of two ``tasks.json`` snapshots keyed by ``Task.id``
    (R8.2, R8.3).

    The Task Creation Processor calls
    :func:`ralph_loop.snapshot_diff.diff_snapshots` with the validated
    pre-iteration task list and the raw post-iteration list (which may
    contain entries that fail schema validation). The returned diff is the
    sole input to downstream new-entry validation (R8.4), revert handling
    (R8.8), and budget / spill enforcement (R8.10, R8.11).

    Semantics (Property 16):

    - ``created``: post-snapshot entries whose ``id`` is absent from the
      pre snapshot, plus any post entry missing a usable ``id``. Entries
      are preserved as raw dicts so the downstream validation pipeline
      can reject invalid ones without reinterpreting them (R8.4).
    - ``modified``: ``(pre_dump, post_dump)`` pairs for ids present in
      both snapshots where the pre task's JSON dump differs from the
      post dict. Used by the revert path (R8.8) to detect illicit edits.
    - ``deleted``: pre-snapshot :class:`Task` instances whose ``id`` is
      absent from the post snapshot. Used by the revert path (R8.8) to
      restore illicitly deleted entries.
    """

    created: list[dict[str, Any]]
    modified: list[tuple[dict[str, Any], dict[str, Any]]]
    deleted: list["Task"]


# -- Resumer result (R14.3-R14.6, R2.9, R2.10) --------------------------------


class ResumeResult(BaseModel):
    """Outcome of ``Resumer.resume`` (R14.3-R14.6).

    ``reset_tasks`` lists tasks that had status ``in_progress`` and were
    reset to ``failing`` without incrementing ``retry_count`` (R14.3, R14.4).
    ``stuck_by_missing_dep`` and ``stuck_by_cycle_tasks`` are tasks that the
    dependency analyzer flagged during the startup sweep (R2.9, R2.10, R2.11).
    ``detected_cycle`` holds the ordered task ids forming the first detected
    cycle, for logging.
    """

    reset_tasks: list[Task]
    stuck_by_missing_dep: list[Task]
    stuck_by_cycle_tasks: list[Task]
    detected_cycle: list[str]
