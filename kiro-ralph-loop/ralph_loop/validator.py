"""Validator: runs shell, file_exists, and persona_review checks (R7.2-R7.13).

The Validator executes every :class:`ValidationCheckConfig` declared on a
task's spec and aggregates the outcomes into a :class:`ValidationResult`
(R7.10, R7.12, R2.6). Each check type has its own runner:

* ``shell`` (:func:`_run_shell_check`): spawns each configured command
  via :func:`asyncio.create_subprocess_exec`; the check passes iff every
  command exits with code ``0`` (R7.2, R7.5).
* ``file_exists`` (:func:`_run_file_exists_check`): passes iff every
  configured :class:`pathlib.Path` exists (R7.4, R7.11).
* ``persona_review`` (:func:`_run_persona_review_check`): resolves the
  pass condition (spec override -> persona default -> stuck/error per
  R7.6-R7.8), invokes the reviewing persona in a separate Kiro CLI
  session with no loop framing and no ``tasks.json`` write access
  (R7.9), rejects self-review (reviewing persona == executing persona),
  and parses a strict ``{"verdict": "pass"|"fail", "rationale": "..."}``
  response via Pydantic (R7.9, R7.10).

Per-check timeouts (R7.13) are enforced via :func:`asyncio.wait_for`;
on timeout the check is terminated, recorded with ``timed_out=True``
and ``verdict="fail"``, and the :class:`ValidationResult` records the
check name in ``timed_out_checks`` so the iteration log captures the
event.

Two pure helpers are exposed for property-based testing:

* :func:`aggregate_checks` — the pure aggregation rule used by
  :meth:`Validator.run` and Property 13.
* :func:`resolve_pass_condition` — the pure pass-condition resolution
  rule used by the persona_review runner and Property 14.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from ralph_loop.json_extract import extract_validating_object
from ralph_loop.kiro import KiroInvocationTimeout, KiroInvoker
from ralph_loop.models import (
    CheckResult,
    FileExistsCheckConfig,
    Persona,
    PersonaReviewCheckConfig,
    ShellCheckConfig,
    Task,
    TaskSpec,
    ValidationCheckConfig,
    ValidationResult,
    Verdict,
)
from ralph_loop.persona_registry import PersonaRegistry

logger = logging.getLogger(__name__)


class PersonaReviewVerdict(BaseModel):
    """Parsed ``{"verdict": "pass"|"fail", "rationale": "..."}`` decision (R7.9).

    The reviewing persona's Kiro CLI session is instructed to emit this
    structured verdict as a JSON object in its stdout. Keeping the
    parser model separate from :class:`CheckResult` lets the reviewing
    persona's output envelope change without touching the iteration-log
    schema.
    """

    verdict: Verdict
    rationale: str


class ValidatorStuckError(Exception):
    """Raised when a persona_review check cannot resolve a pass condition.

    R7.8 mandates that an executing task be marked stuck when a
    ``persona_review`` check declares no pass condition and the
    reviewing persona's definition declares no default either. The outer
    loop catches this exception, marks the task stuck, and logs the
    identifying task id and reviewing persona name.
    """

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"Task {task_id!r} marked stuck: {reason}")
        self.task_id = task_id
        self.reason = reason



def aggregate_checks(check_results: list[CheckResult]) -> ValidationResult:
    """Aggregate a list of per-check outcomes into a :class:`ValidationResult`.

    The aggregation rule (R7.12, R2.6, Property 13):

    * ``overall == "pass"`` iff every check has ``verdict == "pass"``.
      An empty list aggregates to ``"pass"`` (vacuous truth), which
      matches the design's rule that a task with zero validation checks
      is treated as passing — though the spec parser in R18.1 requires
      at least one check, so this branch is only ever exercised in
      tests.
    * ``timed_out_checks`` lists the ``name`` of every check whose
      ``timed_out`` flag is set (R7.13).

    Kept as a pure function so Property 13 can exercise the aggregation
    rule without any subprocess or filesystem work.
    """
    overall: Verdict = (
        "pass" if all(r.verdict == "pass" for r in check_results) else "fail"
    )
    timed_out = [r.name for r in check_results if r.timed_out]
    return ValidationResult(
        overall=overall, checks=list(check_results), timed_out_checks=timed_out
    )


def resolve_pass_condition(
    check: PersonaReviewCheckConfig,
    reviewing_persona: Persona,
) -> Optional[str]:
    """Return the resolved pass condition for a ``persona_review`` check.

    Resolution order (R7.6, R7.7, R7.8, Property 14):

    1. Spec-level override on the check (``check.pass_condition``) wins
       when present.
    2. Otherwise, the reviewing persona's
       ``default_persona_review_pass_condition`` is used (R7.7).
    3. If neither is set, ``None`` is returned; the caller is
       responsible for raising :class:`ValidatorStuckError` (R7.8).

    Kept as a pure function so Property 14 can exercise the resolution
    rule without constructing a full Validator.
    """
    if check.pass_condition is not None:
        return check.pass_condition
    return reviewing_persona.default_persona_review_pass_condition



async def _run_shell_check(
    check: ShellCheckConfig,
    *,
    default_timeout_ms: int,
    cwd: Optional[Path] = None,
) -> CheckResult:
    """Run a ``shell`` check; pass iff every command exits 0 (R7.2, R7.5, R7.13).

    Commands run sequentially in the order they were declared. The
    first non-zero exit short-circuits the run and marks the check
    ``fail``; subsequent commands are not executed, matching the
    "every command exits 0" rule in R7.5 and avoiding masking of the
    first failure.

    ``timeout_ms`` resolution: the check's per-check ``timeout_ms`` if
    set, otherwise ``default_timeout_ms`` (typically
    ``Config.validation_timeout_ms``, R7.13). On timeout the subprocess
    chain is cancelled, the check is marked ``fail`` with
    ``timed_out=True``, and the timeout message is captured in
    ``output`` so the iteration log records the event.
    """
    start = time.monotonic()
    timeout_ms = (
        check.timeout_ms if check.timeout_ms is not None else default_timeout_ms
    )
    name = check.name or "shell"

    # Hold the currently-running subprocess so the timeout handler can
    # kill and reap it after ``asyncio.wait_for`` cancels the inner
    # coroutine. We deliberately do NOT clear this in a finally inside
    # ``_run_all``: on cancellation, that finally runs before we reach
    # the except block and would leave the timeout handler with no
    # handle to the still-alive process (causing ResourceWarning on
    # Windows and a leaked PID on POSIX).
    current_proc: list[Optional[asyncio.subprocess.Process]] = [None]

    async def _run_all() -> tuple[Verdict, str]:
        outputs: list[str] = []
        for cmd in check.commands:
            argv = shlex.split(cmd)
            if not argv:
                # Skip empty/whitespace-only commands rather than raising:
                # validators should be lenient about leading/trailing
                # whitespace in user-authored YAML.
                continue
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd) if cwd is not None else None,
            )
            current_proc[0] = proc
            stdout_b, _ = await proc.communicate()
            stdout_t = (
                stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            )
            outputs.append(f"$ {cmd}\n{stdout_t}\nexit={proc.returncode}")
            if proc.returncode != 0:
                return "fail", "\n".join(outputs)
        return "pass", "\n".join(outputs)

    try:
        verdict, output = await asyncio.wait_for(
            _run_all(), timeout=timeout_ms / 1000.0
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return CheckResult(
            type="shell",
            name=name,
            verdict=verdict,
            output=output,
            duration_ms=duration_ms,
            timed_out=False,
        )
    except asyncio.TimeoutError:
        # Terminate any still-running child and reap it so the subprocess
        # resources are closed before we return. Skipping this leaks
        # file descriptors / PipeHandles on Windows and raises
        # ResourceWarning at interpreter shutdown.
        proc = current_proc[0]
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001 - cleanup must not raise
                pass
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "shell check %r exceeded timeout_ms=%d", name, timeout_ms
        )
        return CheckResult(
            type="shell",
            name=name,
            verdict="fail",
            output=f"Shell check {name!r} exceeded timeout_ms={timeout_ms}",
            duration_ms=duration_ms,
            timed_out=True,
        )


async def _run_file_exists_check(
    check: FileExistsCheckConfig,
    *,
    cwd: Optional[Path] = None,
) -> CheckResult:
    """Run a ``file_exists`` check; pass iff every configured path exists (R7.4, R7.11).

    Paths are resolved relative to ``cwd`` when it is provided, so the
    validator can be driven from an arbitrary working directory without
    relying on ``os.getcwd()``. Missing paths are captured in ``output``
    so the iteration log identifies exactly which paths failed the
    check.
    """
    start = time.monotonic()
    name = check.name or "file_exists"
    missing: list[str] = []
    for p in check.paths:
        path = Path(p) if cwd is None else (cwd / p)
        if not path.exists():
            missing.append(p)
    verdict: Verdict = "pass" if not missing else "fail"
    if missing:
        output = f"missing paths: {missing}"
    else:
        output = "all paths exist"
    duration_ms = int((time.monotonic() - start) * 1000)
    return CheckResult(
        type="file_exists",
        name=name,
        verdict=verdict,
        output=output,
        duration_ms=duration_ms,
        timed_out=False,
    )



def _build_review_prompt(
    *,
    reviewing_persona: Persona,
    task: Task,
    spec: TaskSpec,
    pass_condition: str,
    inlined_context: str = "",
    cwd_note: str = "",
) -> str:
    """Build the prompt for a ``persona_review`` invocation (R7.9).

    The prompt:

    * Identifies the reviewing persona by name.
    * States the resolved pass condition.
    * Includes the task id and title plus the spec body's objective
      and instructions as review artifacts.
    * Inlines the contents of every ``context_files`` path declared
      on the spec, so the reviewer can judge content quality even if
      the reviewer's filesystem tools resolve against the wrong cwd.
    * Tells the reviewer where the project root is, so any file tool
      calls use absolute paths rather than ralph-loop's own source tree.
    * Asks for a strict JSON verdict of the form
      ``{"verdict": "pass"|"fail", "rationale": "..."}``.

    Critically, it omits the loop-framing instructions that drive a
    normal persona iteration and does not suggest writing to
    ``tasks.json``. Combined with the separate Kiro CLI session
    (one invocation per review), this means the reviewing persona
    cannot itself trigger additional reviews or create tasks, which
    eliminates the risk of recursive review chains (R7.9 note on
    recursion safety in design.md).
    """
    parts = [
        f"You are the reviewing persona {reviewing_persona.name!r}. Review the "
        "following task artifacts and return a strict JSON decision of the form "
        '{"verdict": "pass"|"fail", "rationale": "<brief text>"}. '
        "Do not include any additional text or code fences outside the JSON object.\n",
    ]
    if cwd_note:
        parts.append(f"\n{cwd_note}\n")
    parts.append(f"\nPass condition: {pass_condition}\n")
    parts.append("\n## Task\n")
    parts.append(f"id: {task.id}\n")
    parts.append(f"title: {task.title}\n")
    parts.append("\n## Task Spec Objective\n")
    parts.append(f"{spec.body.objective}\n")
    parts.append("\n## Task Spec Instructions\n")
    parts.append(f"{spec.body.instructions}\n")
    if inlined_context:
        parts.append("\n## Context Files (inlined for review)\n\n")
        parts.append(inlined_context)
        parts.append("\n")
    return "".join(parts)


async def _run_persona_review_check(
    check: PersonaReviewCheckConfig,
    *,
    task: Task,
    spec: TaskSpec,
    executing_persona_name: str,
    registry: PersonaRegistry,
    invoker: KiroInvoker,
    log_path: Path,
    default_timeout_ms: int,
    model_id: Optional[str] = None,
    cwd: Optional[Path] = None,
    fallback_reviewer: Optional[str] = None,
) -> CheckResult:
    """Run a ``persona_review`` check (R7.3, R7.6-R7.10, R7.13).

    Steps (in order):

    1. Resolve the reviewing persona by name; a missing persona is a
       stuck condition (R7.8-style configuration error).
    2. Reject self-review: a ``persona_review`` check whose reviewing
       persona matches the executing persona is a configuration error
       (recursion safety guard in design.md §Validator). The check is
       recorded as ``fail`` with a logged error and an explanatory
       ``output``; the reviewing persona name is still captured on the
       :class:`CheckResult` so the iteration log surfaces the
       self-review attempt.
    3. Resolve the pass condition via :func:`resolve_pass_condition`;
       a ``None`` return raises :class:`ValidatorStuckError` (R7.8).
    4. Build the review prompt and invoke Kiro CLI with
       ``call_kind="persona_review"`` and a per-check timeout. Timeouts
       produce a failing :class:`CheckResult` with ``timed_out=True``
       (R7.13).
    5. Parse the reviewing persona's stdout into a
       :class:`PersonaReviewVerdict`. Parse failures (unbalanced JSON,
       schema mismatch) produce a failing :class:`CheckResult` rather
       than raising, because an LLM-generated malformed verdict is a
       runtime condition that should fail the iteration cleanly (R7.10:
       verdict != "pass" -> fail).
    """
    start = time.monotonic()
    name = check.name or "persona_review"
    reviewing_persona = registry.get(check.persona)
    if reviewing_persona is None:
        raise ValidatorStuckError(
            task.id,
            f"reviewing persona {check.persona!r} not found in persona "
            "registry for persona_review check",
        )

    # --- Self-review handling (design.md §Validator recursion safety) --
    # R7.9 recursion safety: a persona cannot review its own iteration.
    # The canonical case where this trips is escalation: the escalation
    # persona runs the iteration AND is the declared reviewer. Rather
    # than fail the check and force the task into a retry loop that can
    # never pass, swap in ``fallback_reviewer`` (the configured
    # ``fallback_persona``) as the substitute reviewer and log a
    # warning. If no substitute is available, we still have to fail so
    # no persona ends up reviewing its own output.
    if check.persona == executing_persona_name:
        substitute_name: Optional[str] = None
        if (
            fallback_reviewer
            and fallback_reviewer != executing_persona_name
            and registry.get(fallback_reviewer) is not None
        ):
            substitute_name = fallback_reviewer

        if substitute_name is None:
            duration_ms = int((time.monotonic() - start) * 1000)
            msg = (
                f"Self-review rejected: reviewing persona {check.persona!r} "
                "matches the executing persona, and no distinct "
                "fallback_persona is available to substitute"
            )
            logger.error(msg)
            return CheckResult(
                type="persona_review",
                name=name,
                verdict="fail",
                output=msg,
                reviewing_persona=check.persona,
                duration_ms=duration_ms,
                timed_out=False,
            )

        logger.warning(
            "persona_review check %r: reviewing persona %r equals "
            "executing persona; substituting fallback reviewer %r",
            name, check.persona, substitute_name,
        )
        reviewing_persona = registry.get(substitute_name)
        # ``registry.get`` is truthy here because we gated on it above,
        # but mypy wants the narrowing.
        assert reviewing_persona is not None

    # --- Pass-condition resolution (R7.6, R7.7, R7.8) -----------------
    pass_condition = resolve_pass_condition(check, reviewing_persona)
    if pass_condition is None:
        raise ValidatorStuckError(
            task.id,
            f"persona_review check {name!r} declares no pass condition "
            f"and reviewing persona {check.persona!r} declares no default "
            "persona_review pass condition",
        )

    # --- Invoke reviewing persona (R7.9) ------------------------------
    # Inline declared context_files so the reviewer sees content even if
    # its own filesystem tools resolve against an unexpected cwd. Missing
    # files are logged and skipped; a read failure never aborts the check.
    inlined_context = ""
    if spec.context_files:
        parts: list[str] = []
        for rel_path in spec.context_files:
            abs_path = (
                (cwd / rel_path) if cwd is not None else Path(rel_path)
            )
            try:
                if abs_path.is_file():
                    body = abs_path.read_text(encoding="utf-8")
                    parts.append(f"### File: {rel_path}\n\n{body}\n")
                else:
                    parts.append(
                        f"### File: {rel_path}\n\n"
                        f"[MISSING: {abs_path} does not exist]\n"
                    )
            except Exception as exc:  # noqa: BLE001
                parts.append(
                    f"### File: {rel_path}\n\n"
                    f"[READ ERROR: {exc}]\n"
                )
        inlined_context = "\n".join(parts)

    cwd_note = (
        f"The project root is {cwd}. Resolve any file references below "
        "relative to that path. The content of every declared "
        "``context_files`` entry is inlined below; prefer that inline "
        "content over filesystem lookups."
        if cwd is not None
        else ""
    )

    prompt = _build_review_prompt(
        reviewing_persona=reviewing_persona,
        task=task,
        spec=spec,
        pass_condition=pass_condition,
        inlined_context=inlined_context,
        cwd_note=cwd_note,
    )
    timeout_ms = (
        check.timeout_ms if check.timeout_ms is not None else default_timeout_ms
    )
    try:
        invocation = await invoker.invoke(
            context=prompt,
            log_path=log_path,
            call_kind="persona_review",
            timeout_ms=timeout_ms,
            model_id=model_id,
            cwd=cwd,
        )
    except KiroInvocationTimeout:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "persona_review check %r exceeded timeout_ms=%d", name, timeout_ms
        )
        return CheckResult(
            type="persona_review",
            name=name,
            verdict="fail",
            output=(
                f"persona_review check {name!r} exceeded timeout_ms={timeout_ms}"
            ),
            reviewing_persona=check.persona,
            resolved_pass_condition=pass_condition,
            duration_ms=duration_ms,
            timed_out=True,
        )

    # --- Parse the reviewing persona's verdict (R7.9, R7.10) ----------
    # Delegates to the shared helper so fences, leading tool-use JSON
    # envelopes, braces inside rationale strings, and escaped quotes
    # are all handled uniformly (see
    # ``.kiro/specs/persona-review-verdict-parsing/`` and
    # :mod:`ralph_loop.json_extract`).
    parsed_verdict = extract_validating_object(
        invocation.stdout, PersonaReviewVerdict
    )

    duration_ms = int((time.monotonic() - start) * 1000)
    if parsed_verdict is None:
        # --- Debug dump on parse failure ------------------------------
        # Capture the EXACT bytes the parser saw, independent of the
        # shared per-iteration log file (which is append-mode and
        # commingles multiple subprocess invocations). One file per
        # failed parse, named by task id / iteration / check name.
        # Safe on every platform: filename components are sanitized.
        try:
            dump_dir = log_path.parent / "parse_failures"
            dump_dir.mkdir(parents=True, exist_ok=True)
            iter_hint = log_path.stem  # e.g. "iter-0001"
            safe_task = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in task.id
            )
            safe_check = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in name
            )
            dump_path = (
                dump_dir
                / f"{iter_hint}-{safe_task}-{safe_check}-{check.persona}.txt"
            )
            with dump_path.open("w", encoding="utf-8") as f:
                # Read the bound commit_sha from structlog context so
                # each dump identifies the ralph_loop build that
                # produced it. Falls back gracefully when structlog is
                # not available or no SHA was bound.
                commit_sha = "unknown"
                try:
                    import structlog
                    ctx = structlog.contextvars.get_contextvars()
                    commit_sha = ctx.get("commit_sha", "unknown")
                except Exception:  # noqa: BLE001
                    pass
                f.write(f"commit_sha: {commit_sha}\n")
                f.write(f"task_id: {task.id}\n")
                f.write(f"check_name: {name}\n")
                f.write(f"reviewing_persona: {check.persona}\n")
                f.write(f"stdout_len: {len(invocation.stdout)}\n")
                f.write(f"exit_code: {invocation.exit_code}\n")
                f.write("--- STDOUT (repr) ---\n")
                f.write(repr(invocation.stdout))
                f.write("\n--- STDOUT (verbatim) ---\n")
                f.write(invocation.stdout)
                f.write("\n--- STDERR (repr) ---\n")
                f.write(repr(invocation.stderr))
                f.write("\n")
            logger.warning(
                "persona_review check %r: verdict parse failed; "
                "wrote debug dump to %s",
                name, dump_path,
            )
        except Exception:  # noqa: BLE001
            # Debug dumping must never crash the loop.
            logger.exception(
                "persona_review check %r: failed to write parse-failure "
                "debug dump",
                name,
            )

        logger.warning(
            "persona_review check %r: could not parse verdict from "
            "reviewing persona %r; marking fail",
            name,
            check.persona,
        )
        return CheckResult(
            type="persona_review",
            name=name,
            verdict="fail",
            output=(
                f"could not parse structured verdict from reviewing persona "
                f"{check.persona!r}: {invocation.stdout}"
            ),
            reviewing_persona=check.persona,
            resolved_pass_condition=pass_condition,
            duration_ms=duration_ms,
            timed_out=False,
        )

    return CheckResult(
        type="persona_review",
        name=name,
        verdict=parsed_verdict.verdict,
        output=invocation.stdout,
        rationale=parsed_verdict.rationale,
        resolved_pass_condition=pass_condition,
        reviewing_persona=check.persona,
        duration_ms=duration_ms,
        timed_out=False,
    )



class Validator:
    """Runs every validation check on a task and aggregates results.

    The validator is stateless; one instance can be reused across
    iterations. It holds references to the Kiro invoker (used by
    ``persona_review`` checks) and the persona registry (used to
    resolve reviewing personas and their default pass conditions).

    :meth:`run` executes every check configured on the task's spec and
    aggregates the outcomes into a :class:`ValidationResult` (R7.10,
    R7.12, R2.6).
    """

    def __init__(
        self,
        *,
        invoker: KiroInvoker,
        registry: PersonaRegistry,
        model_id: Optional[str] = None,
        fallback_reviewer: Optional[str] = None,
    ) -> None:
        self._invoker = invoker
        self._registry = registry
        self._model_id = model_id
        self._fallback_reviewer = fallback_reviewer

    async def run(
        self,
        *,
        task: Task,
        spec: TaskSpec,
        executing_persona_name: str,
        log_path: Path,
        default_timeout_ms: int = 5 * 60 * 1000,
        cwd: Optional[Path] = None,
    ) -> ValidationResult:
        """Run every check declared on ``spec`` and aggregate (R7.10-R7.12, R2.6).

        Each check dispatches to its type-specific runner:

        * :class:`ShellCheckConfig` -> :func:`_run_shell_check`
        * :class:`FileExistsCheckConfig` -> :func:`_run_file_exists_check`
        * :class:`PersonaReviewCheckConfig` ->
          :func:`_run_persona_review_check`

        Checks run sequentially, not in parallel, so a ``shell`` check
        that modifies the filesystem is observed by a subsequent
        ``file_exists`` check.

        Args:
            task: The executing task whose spec is being validated.
            spec: The parsed task spec; ``spec.validation`` is the list
                of checks to execute.
            executing_persona_name: The name of the persona that just
                ran the iteration, used to reject self-review
                ``persona_review`` checks.
            log_path: Log file receiving ``persona_review`` Kiro CLI
                output; shell/file_exists checks do not write to it.
            default_timeout_ms: Default per-check timeout in ms
                (typically ``Config.validation_timeout_ms``, R7.13). A
                check's own ``timeout_ms`` overrides this when set.
            cwd: Optional working directory for shell and file_exists
                checks. When ``None``, commands run and paths resolve
                relative to the current process cwd.

        Returns:
            :class:`ValidationResult` with ``overall`` computed by
            :func:`aggregate_checks`, ``checks`` preserving the order of
            ``spec.validation``, and ``timed_out_checks`` listing every
            check whose runner set ``timed_out=True`` (R7.13).
        """
        check_results: list[CheckResult] = []
        for check in spec.validation:
            # The discriminated union in TaskSpec.validation guarantees
            # ``check`` is one of the three known types; the final
            # ``else`` is defensive so a future check type cannot
            # silently pass through unvalidated.
            if isinstance(check, ShellCheckConfig):
                result = await _run_shell_check(
                    check, default_timeout_ms=default_timeout_ms, cwd=cwd
                )
            elif isinstance(check, FileExistsCheckConfig):
                result = await _run_file_exists_check(check, cwd=cwd)
            elif isinstance(check, PersonaReviewCheckConfig):
                result = await _run_persona_review_check(
                    check,
                    task=task,
                    spec=spec,
                    executing_persona_name=executing_persona_name,
                    registry=self._registry,
                    invoker=self._invoker,
                    log_path=log_path,
                    default_timeout_ms=default_timeout_ms,
                    model_id=self._model_id,
                    cwd=cwd,
                    fallback_reviewer=self._fallback_reviewer,
                )
            else:  # pragma: no cover - defensive: union is closed
                raise AssertionError(f"unknown check type: {type(check)!r}")
            check_results.append(result)

        return aggregate_checks(check_results)
