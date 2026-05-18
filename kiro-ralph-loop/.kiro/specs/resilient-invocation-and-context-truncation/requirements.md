# Requirements Document

## Introduction

This spec covers two related resiliency capabilities for the Ralph_Loop that together prevent a single large-context Kiro_CLI failure from killing an entire run.

**Motivating real-world failure.** During an HCLS book project run, an `expert-review` task declared a 138 KB recipe Markdown file as a context reference. The Context_Composer inlined the file verbatim, which caused Kiro_CLI to error with `Separator is not found, and chunk exceed the limit`. The resulting exception propagated out of `KiroInvoker.invoke`, past the iteration loop in `ralph_loop/cli.py::_run_loop` (which currently catches only `KiroInvocationTimeout` inside the loop, and catches any other `Exception` with a `break` followed by process exit), and terminated the run with exit code `EXIT_INVOCATION_ERROR`. One bad task killed all remaining tasks. The operator was forced to manually mark 128 `expert-review` tasks as `stuck` and restart the loop twice.

The failure has two independent contributing causes and this spec addresses each as a separately-numbered capability so design and tasks can reference them independently:

- **Capability A — Graceful invocation-error handling.** Any Kiro_CLI subprocess failure not already classified (anything other than `KiroInvocationTimeout`) must be caught inside the per-iteration body, recorded as an iteration failure, counted against the Task's `retry_count` via the existing `status_after_validation` rule (R2.5, R2.6), logged with the captured Kiro_CLI error output, and then the loop must proceed to the next iteration rather than exit. The existing retry-cap and termination rules (Requirement 2 and Requirement 10 of the Ralph_Loop spec) continue to govern when a Task transitions to `stuck` or when the overall run terminates. Automatic retry of the *same* Kiro_CLI call within a single iteration is explicitly out of scope; retries happen on the next iteration via normal Task selection.

- **Capability B — Context-file truncation in the Context_Composer.** `ralph_loop/context.py::compose_context` currently inlines every declared `context_files` entry verbatim into the Context_Window. A new per-file byte cap, configured via a new `Config.max_context_file_bytes` field, bounds the size of any single inlined file before it reaches Kiro_CLI. Files under the cap are included unchanged; files over the cap are truncated head-only with an explicit marker recording the original and truncated sizes.

The two capabilities are complementary: Capability B removes the most common *cause* of invocation failures at the source, and Capability A ensures that any remaining invocation failure — from oversized context, transient subprocess errors, or any other exception raised from `KiroInvoker.invoke` — degrades to a single failed iteration rather than a run-ending crash.

### Non-Goals

- Changing the declaration format for `context_files` in Task_Spec files.
- Adding new `call_kind` values or altering the Validator pipeline.
- Automatic same-iteration retry of a failed Kiro_CLI invocation.
- Modifying the Kiro_CLI subprocess protocol or its stdin/stdout contract.
- Smarter truncation strategies (head+tail, summarization, token-aware slicing). The design document may propose these as follow-on work; this spec ships head-only truncation.

## Glossary

- **Invocation_Error**: Any exception raised from `KiroInvoker.invoke` during an Iteration's `persona_execution` or `escalation` call_kind, including `subprocess.CalledProcessError`, non-zero-exit subprocess failures not surfaced through another classified exception, Kiro_CLI chunk-limit errors (identifiable by the substring `chunk exceed the limit` or equivalent), and any other `Exception` subclass raised from the subprocess wrapper. `KiroInvocationTimeout` is explicitly *included* in this definition for Capability A so the handling path is uniform, even though the existing loop already catches it separately at the Validator and Orchestrator layers.
- **Iteration_Failure**: The outcome recorded for an Iteration whose `persona_execution` or `escalation` Kiro_CLI invocation raised an Invocation_Error. An Iteration_Failure is treated equivalently to a failed `persona_review` Validation_Check for the purposes of Task status and retry counting: the Task's next status is `failing` and its `retry_count` increments by one per R2.6 and the existing `status_after_validation` function.
- **Context_File**: A path listed in a Task_Spec's `context_files` field that the Context_Composer reads and inlines into the Task_Spec section of the Context_Window (R18.5).
- **Truncation_Marker**: The deterministic footer string appended by the Context_Composer after the truncated prefix of a Context_File whose byte size exceeds `Config.max_context_file_bytes`. The marker records both the original byte size and the retained byte size so the text is self-describing in logs and in the composed prompt.
- **Context_File_Byte_Cap**: The configured value of `Config.max_context_file_bytes`. Measured in bytes of the file's UTF-8 encoded contents, matching how Kiro_CLI's chunk limit is measured.
- **Truncation_Event**: A single application of head-only truncation to one Context_File during a single `compose_context` call. Emitted as a structured log record containing the path, original byte size, and retained byte size so operators can correlate truncations with downstream iteration behavior.

## Requirements

### Requirement 1: Graceful handling of Kiro CLI invocation errors

**User Story:** As a project owner, I want a single failing Kiro_CLI invocation to mark one iteration as failed and continue the run, so that one oversized context or one transient subprocess error does not kill a run that contains dozens or hundreds of unrelated tasks.

#### Acceptance Criteria

1. WHEN `KiroInvoker.invoke` raises any `Exception` subclass during the `persona_execution` or `escalation` call_kind invocation inside the iteration loop, THE Ralph_Loop SHALL treat the current Iteration as an Iteration_Failure, apply the existing `status_after_validation` rule to increment the executing Task's `retry_count` by one, and set the executing Task's status to `failing`.
2. WHEN an Iteration_Failure occurs, THE Ralph_Loop SHALL log a structured record containing the Task identifier, the Persona name, the exception type name, the exception message, and any captured stdout and stderr attached to the exception.
3. WHEN an Iteration_Failure occurs, THE Ralph_Loop SHALL proceed to the next iteration of the loop rather than terminate the process.
4. WHEN an Iteration_Failure occurs, THE Ralph_Loop SHALL persist the updated Task status and `retry_count` to `tasks.json` atomically before starting the next iteration.
5. WHEN the executing Task's `retry_count` reaches `Config.max_retries_per_task` as a result of an Iteration_Failure, THE Ralph_Loop SHALL transition the Task to `stuck` via the existing retry-cap rule in Requirement 2 of the Ralph_Loop spec, not via a process-level crash.
6. WHEN the iteration loop terminates after one or more Iteration_Failures, THE Ralph_Loop SHALL determine the process exit code from the overall termination verdict produced by `termination_decision` and the Budget_Limit checks (success, blocked, budget exceeded) rather than from the most recent Iteration_Failure.
7. IF `KiroInvoker.invoke` raises an exception whose captured output matches the substring `chunk exceed the limit`, THEN THE Ralph_Loop SHALL classify the Iteration_Failure with a distinct log marker identifying it as a chunk-limit failure so operators can correlate it with the Context_File sizes emitted by Capability B.
8. WHEN an Iteration_Failure occurs, THE Ralph_Loop SHALL NOT retry the same Kiro_CLI invocation within the same iteration.
9. WHEN an Iteration_Failure occurs before `Validator.run` has been called for the iteration, THE Ralph_Loop SHALL skip the Validator, the Task_Creation_Processor, and the Git_Integration commit for that iteration, while still persisting the Task status update required by acceptance criterion 1.

### Requirement 2: Per-file byte cap for inlined Context_Files

**User Story:** As a project owner, I want oversized reference files inlined into the Context_Window to be truncated to a configured cap, so that a single large file cannot push a prompt past Kiro_CLI's chunk limit.

#### Acceptance Criteria

1. THE Config SHALL expose a new optional field named `max_context_file_bytes`, typed as a positive integer number of bytes, with a documented default of 65536 bytes (64 KiB).
2. WHEN `max_context_file_bytes` is not set in `ralph.config.json`, THE Config SHALL apply the documented default of 65536 bytes.
3. WHEN the Context_Composer inlines a Context_File whose UTF-8 encoded byte size is less than or equal to `Config.max_context_file_bytes`, THE Context_Composer SHALL include the file's contents verbatim with no Truncation_Marker appended.
4. WHEN the Context_Composer inlines a Context_File whose UTF-8 encoded byte size exceeds `Config.max_context_file_bytes`, THE Context_Composer SHALL include only the first `Config.max_context_file_bytes` bytes of the UTF-8 encoded contents followed by a Truncation_Marker.
5. THE Truncation_Marker SHALL be a deterministic string containing the literal text `[truncated: <original_bytes> bytes, showing first <cap> bytes]`, with `<original_bytes>` replaced by the file's original byte size and `<cap>` replaced by the configured `max_context_file_bytes` value.
6. WHEN a Truncation_Event occurs, THE Context_Composer SHALL emit a structured log record at WARNING level containing the Context_File path, its original byte size, and the retained byte size.
7. WHEN the head-only truncation rule in acceptance criterion 4 is applied, THE Context_Composer SHALL ensure the emitted Markdown block for the file contains the truncated contents followed by the Truncation_Marker on a separate line so the marker is visibly distinct in the composed prompt.
8. WHERE the truncation cut point falls inside a multi-byte UTF-8 code point, THE Context_Composer SHALL preserve UTF-8 well-formedness in the emitted prefix by trimming back to the nearest complete code point boundary before appending the Truncation_Marker.
9. WHEN the Context_Composer applies per-file truncation to one or more Context_Files during an Iteration, THE Ralph_Loop SHALL include those Truncation_Events in the iteration log so downstream analysis can correlate large-file truncations with subsequent Validation or invocation outcomes.
10. THE per-file truncation rule in this Requirement SHALL apply to every Context_File independently and SHALL NOT be conflated with the existing whole-Context_Window truncation rule (R6.7) that governs the Project_Brief summary fallback.

### Requirement 3: Interaction between the two capabilities and existing loop rules

**User Story:** As a project owner, I want the new error handling and truncation behavior to compose cleanly with the existing retry, escalation, and termination rules, so that diagnosing a stuck or blocked Task does not require me to understand new exit codes or new Task states.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL NOT introduce new Task status values, new call_kind values, new exit codes, or new terminal states as a result of Capability A or Capability B.
2. WHEN Capability A records an Iteration_Failure, THE Ralph_Loop SHALL apply the same retry-count and status-transition rules used for failed `persona_review` Validation_Checks as defined in Requirement 2 and the existing `status_after_validation` function.
3. WHEN a Task escalated under Requirement 5 of the Ralph_Loop spec raises an Invocation_Error, THE Ralph_Loop SHALL record the Iteration_Failure, increment the `retry_count`, and continue the loop, preserving the existing Escalation routing rule for the next eligible Iteration that selects the Task.
4. WHEN the Config field `max_context_file_bytes` is present in an existing `ralph.config.json`, THE Config loader SHALL parse it without breaking previously valid configurations, and WHEN it is absent, THE Config loader SHALL apply the documented default without requiring any config edit.
5. THE Ralph_Loop SHALL preserve every currently-passing test in the existing test suite when Capability A and Capability B are implemented together.
