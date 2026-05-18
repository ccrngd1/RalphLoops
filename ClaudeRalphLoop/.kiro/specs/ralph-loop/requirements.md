# Requirements Document

## Introduction

The Ralph Loop is a domain-agnostic automated iteration wrapper for Kiro CLI that drives an agent through a persistent loop until explicit completion criteria are met. Rather than relying on a single long-running agent session, the Ralph Loop repeatedly invokes Kiro CLI in non-interactive mode, picks the next eligible task, invokes a named Persona (a role with its own prompt and instructions) to work on that task, runs validation checks, and decides whether to loop again or stop. The filesystem (project brief, task list, per-task spec files, persona definitions, validation artifacts) serves as durable state, while in-memory agent context is discarded between iterations to prevent drift.

The loop is designed to work across any iterative workflow where a project can be decomposed into discrete tasks and a measurable notion of "done." Example use cases include:

- Writing a non-fiction technical book, with Writer, Reviewer, Editor, Fact-Checker, and Outline personas collaborating on chapters.
- Implementing and refactoring a software codebase, with Coder, Tester, and Reviewer personas.
- Producing a research report with Researcher, Analyst, and Editor personas.
- Running a data-analysis pipeline with Ingest, Transform, and Validator personas.

The same loop mechanics apply in every case: tasks are selected, a persona executes, validation checks run, status updates are persisted, and optionally new tasks are created for subsequent iterations.

## Glossary

- **Ralph_Loop**: The outer orchestration program that repeatedly invokes Kiro CLI sessions, one Persona per Iteration, until all tasks pass or a Budget_Limit is reached.
- **Kiro_CLI**: The Kiro command-line interface invoked in non-interactive mode via `kiro-cli chat --no-interactive`. Serves as the underlying agent runner for each Iteration and may also serve as the Orchestrator's LLM selection engine.
- **Task**: A discrete unit of work tracked by the Ralph_Loop. A Task is not assumed to be code-related; it may represent drafting a chapter, editing prose, running an analysis, refactoring a module, or any other work item.
- **Task_List**: A `tasks.json` file that stores all Tasks with their status, priority, target Persona, spec-file reference, dependency list, and metadata.
- **Task_Spec**: A per-task specification file containing objective, context references, instructions, persona assignment, and Validation_Checks for a single Task.
- **Task_Dependency**: A relationship declared on a Task via its `depends_on` field that lists one or more Task identifiers whose status must be `passing` before the dependent Task is eligible for selection by the Orchestrator.
- **Project_Brief**: A `SUMMARY.md` file capturing overall project goals, scope, constraints, and shared context for every Iteration. Equivalent in role to a PRD for code projects or an outline/prospectus for writing projects.
- **Persona**: A named role with its own prompt template, instructions, Persona_Description, and optional tool or resource restrictions. A Persona defines *how* an Iteration executes a Task. Examples: Writer, Reviewer, Editor, Fact_Checker, Outline, Coder, Tester.
- **Persona_Description**: A human-readable description declared in each Persona definition that specifies the Persona's responsibilities, capabilities, and the situations in which the Persona should be used. Persona_Descriptions are the primary input the Orchestrator uses to select a Persona for a Task.
- **Persona_Registry**: The configured collection of available Personas for a project, stored as files in a configurable directory, where each Persona has a definition containing at minimum a name, a Persona_Description, a prompt template, and optional metadata.
- **Orchestrator**: The LLM-backed component of the Ralph_Loop responsible for selecting the next Task and, for Tasks without an explicit target Persona, invoking a single LLM call that evaluates every Persona's Persona_Description against the Task and returns the single best-matching Persona. Kiro_CLI or another configured LLM endpoint acts as the Orchestrator's selection engine.
- **Escalation**: The first-class handling path for a Task whose retry counter has reached the Escalation_Threshold. Escalation causes the Ralph_Loop to route the next Iteration for that Task to the Escalation_Persona with additional failure context, rather than through normal Orchestrator Persona selection.
- **Escalation_Persona**: A designated Persona responsible for handling escalated Tasks. Receives the Task's retry history, prior Validation_Check failure outputs, and prior Iteration logs as additional context.
- **Escalation_Threshold**: The configurable per-Task retry count at which the Ralph_Loop treats a Task as escalated and invokes the Escalation_Persona in the next Iteration.
- **Planner_Persona**: A configurable Persona invoked during project bootstrap, or on demand via the `init-tasks` subcommand, that reads the Project_Brief and generates the initial set of Tasks written to the Task_List.
- **Iteration**: A single cycle consisting of Task selection, Persona selection (or Escalation routing), Context_Window composition, Kiro_CLI invocation, Validation_Checks execution, status update, and optional Task_Creation_Event processing.
- **Context_Window**: The prompt content provided to Kiro_CLI for a single Iteration, composed of the Project_Brief, the selected Task_Spec, the selected Persona's prompt and instructions, and loop-framing instructions.
- **Validation_Check**: An objective check that determines whether a Task is passing or failing. A Validation_Check may be a shell command, a review verdict emitted by another Persona, a file-presence check, or any other programmatic check declared by the Task_Spec.
- **Completion_Marker**: A signal emitted by the Ralph_Loop (log line and exit code) indicating that all Tasks have a passing status and the loop has terminated successfully.
- **Budget_Limit**: A configurable maximum applied to Iteration count, per-Task retry count, and wall-clock time, beyond which the Ralph_Loop terminates regardless of Task status.
- **Task_Creation_Event**: The act of a running Persona appending one or more new Tasks to the Task_List during an Iteration by writing new entries to `tasks.json` using Kiro_CLI's file tools.
- **Task_Creation_Budget**: Configurable limits on how many new Tasks may be created per Iteration and in total over a run, used to prevent runaway task generation.
- **Pending_Task_Queue**: The persisted file (`pending_tasks.json` by default) that stores Tasks which exceeded a Task_Creation_Budget during a prior run so those Tasks can be re-admitted to the Task_List on subsequent Ralph_Loop runs, subject to schema validation and persona-existence validation.
- **Git_Integration**: The Ralph_Loop subsystem that creates an Iteration_Commit after each completed Iteration, supports rolling back the working tree to a prior Iteration_Commit, and can be enabled or disabled via configuration.
- **Iteration_Commit**: A git commit created by Git_Integration after an Iteration completes, capturing all working-tree changes from that Iteration with a commit message that records the Iteration number, Task identifier, Persona name, and Validation outcome.
- **Token_Usage**: The input token count, output token count, and optional estimated cost reported by Kiro_CLI or the Orchestrator's LLM for each LLM call performed during an Iteration, persona_review, planner invocation, or Escalation invocation.
- **Model_Pricing**: A configurable mapping from model identifier to per-input-token and per-output-token price used by the Ralph_Loop to estimate cost from Token_Usage.

## Requirements

### Requirement 1: Loop Orchestration

**User Story:** As a project owner, I want an automated loop that repeatedly selects a Task, invokes a Persona, and validates the result until all Tasks pass, so that I do not have to manually re-run the agent after each step.

#### Acceptance Criteria

1. WHEN the Ralph_Loop is started, THE Ralph_Loop SHALL read the Task_List from `tasks.json` and identify all Tasks with a non-passing status.
2. WHEN at least one Task has a non-passing status and that Task is eligible per Requirement 2, THE Orchestrator SHALL select the highest-priority eligible Task for the current Iteration.
3. WHEN a Task is selected, THE Orchestrator SHALL select a Persona for that Task according to Requirement 4 or Requirement 5 and compose a Context_Window according to Requirement 6.
4. WHEN the Context_Window is composed, THE Ralph_Loop SHALL invoke Kiro_CLI in non-interactive mode with that Context_Window.
5. WHEN Kiro_CLI completes an Iteration, THE Ralph_Loop SHALL execute the Validation_Checks associated with the current Task according to Requirement 7.
6. WHEN all Tasks in the Task_List have a passing status, THE Ralph_Loop SHALL emit the Completion_Marker and terminate with exit code 0.
7. WHEN a Budget_Limit is reached before all Tasks pass, THE Ralph_Loop SHALL terminate with a non-zero exit code and log which Tasks remain non-passing.
8. WHEN every non-passing Task is either `stuck` or blocked by at least one non-passing dependency, THE Ralph_Loop SHALL terminate with a non-zero exit code and log the blocked Tasks and their blocking dependency identifiers.

### Requirement 2: Task List Management

**User Story:** As a project owner, I want a structured Task_List that tracks status, priority, persona assignment, and dependencies for each Task, so that the Orchestrator always knows what to work on next and only runs Tasks whose prerequisites have been met.

#### Acceptance Criteria

1. THE Task_List SHALL be stored as a JSON file (`tasks.json`) containing an array of Task objects.
2. THE Task_List SHALL include, for each Task: a unique identifier, a human-readable title, a priority number, a status field whose values are one of `pending`, `in_progress`, `passing`, `failing`, or `stuck`, a path to the Task_Spec file, an optional target Persona name, and an optional `depends_on` field whose value is an array of Task identifiers.
3. WHERE a Task declares a target Persona, THE Orchestrator SHALL use that Persona for Iterations that execute the Task.
4. WHERE a Task does not declare a target Persona, THE Orchestrator SHALL resolve one using the LLM-based Persona selection defined in Requirement 4.
5. WHEN Validation_Checks for a Task all pass, THE Ralph_Loop SHALL update that Task's status to `passing` in the Task_List.
6. WHEN any Validation_Check for a Task fails, THE Ralph_Loop SHALL update that Task's status to `failing` in the Task_List and increment a per-Task retry counter.
7. WHEN the Orchestrator selects the next Task, THE Orchestrator SHALL choose the non-passing Task with the lowest priority number whose per-Task retry counter is below the configured retry limit AND whose every declared `depends_on` identifier references a Task in the Task_List with status `passing`.
8. WHILE any `depends_on` identifier on a Task references a Task whose status is not `passing`, THE Orchestrator SHALL treat that Task as ineligible for selection.
9. IF a Task's `depends_on` field references an identifier that does not exist in the Task_List, THEN THE Ralph_Loop SHALL mark that Task as `stuck` and log an error identifying the Task identifier and the missing dependency identifier.
10. WHEN the Ralph_Loop starts, and again after every Task_Creation_Event, THE Ralph_Loop SHALL analyze the `depends_on` relationships across the Task_List for cycles.
11. IF a cycle is detected across `depends_on` relationships, THEN THE Ralph_Loop SHALL mark every Task participating in the cycle as `stuck` and log the cycle path in the order it was detected.

### Requirement 3: Persona Management

**User Story:** As a project owner, I want to define the Personas available to the Ralph_Loop with clear descriptions of what each one does, so that the Orchestrator can reason about which Persona fits each Task without hard-coded rules.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL load Personas from a configurable Persona_Registry directory.
2. THE Persona_Registry SHALL contain one Persona definition per file, in a format that includes at minimum a unique Persona name, a Persona_Description, and a prompt template, plus optional fields for instructions, tool or resource restrictions, and default Validation_Check configuration including a default `persona_review` pass condition.
3. THE Persona_Description SHALL describe the Persona's responsibilities, capabilities, and the types of Tasks for which the Persona should be selected.
4. WHEN the Ralph_Loop starts, THE Ralph_Loop SHALL parse every Persona file in the Persona_Registry and build an in-memory index keyed by Persona name that includes each Persona's Persona_Description.
5. IF two Persona files declare the same Persona name, THEN THE Ralph_Loop SHALL exit with a descriptive error and a non-zero exit code.
6. IF a Persona definition is missing a required field, THEN THE Ralph_Loop SHALL exit with a descriptive error and a non-zero exit code identifying the invalid file and missing field.
7. THE Ralph_Loop SHALL support at least the following prompt-template placeholders: project brief content, task spec content, task identifier, task title, and persona name.
8. THE Ralph_Loop SHALL provide every Persona's Persona_Description to the Orchestrator for use in LLM-based Persona selection.

### Requirement 4: Persona Selection and Orchestration

**User Story:** As a project owner, I want the Orchestrator to pick the best Persona for each Iteration by reasoning over Persona descriptions, so that Tasks are routed to the right role without me writing and maintaining rule files.

#### Acceptance Criteria

1. WHEN a Task declares a target Persona that exists in the Persona_Registry, THE Orchestrator SHALL use that declared Persona for the current Iteration.
2. WHEN a Task does not declare a target Persona and the Task is not escalated, THE Orchestrator SHALL invoke a single LLM call that receives the Task context and the complete set of Personas in the Persona_Registry paired with their Persona_Descriptions.
3. THE Task context passed to the Orchestrator's LLM call SHALL include the Task identifier, title, current status, tags, retry counter, Task_Spec summary, and Task creation metadata.
4. THE Orchestrator SHALL instruct the LLM to evaluate all provided Personas and return a structured decision containing the name of the single best-matching Persona and a brief rationale.
5. WHEN the LLM returns a valid decision naming a Persona that exists in the Persona_Registry, THE Orchestrator SHALL select that Persona for the current Iteration.
6. THE Orchestrator SHALL log the structured decision, including the chosen Persona name and the rationale, for every LLM-based Persona selection.
7. IF the Persona name returned by the Orchestrator's LLM call does not exist in the Persona_Registry, THEN THE Ralph_Loop SHALL mark the current Task as `stuck` and log an error identifying the hallucinated Persona name and the Task identifier.
8. IF the Orchestrator's LLM call fails due to a network error, an invalid or unparseable response, or a timeout, THEN THE Orchestrator SHALL select the configured fallback Persona for the current Iteration and log a warning identifying the failure cause.
9. IF a Task declares a target Persona that does not exist in the Persona_Registry, THEN THE Ralph_Loop SHALL mark that Task as `stuck` and log an error identifying the missing Persona.
10. THE Ralph_Loop SHALL log the selected Persona and the selection path (explicit target, LLM decision, fallback, or Escalation_Persona) for every Iteration.

### Requirement 5: Escalation

**User Story:** As a project owner, I want the Ralph_Loop to escalate Tasks that keep failing, so that a specialized Persona can take a different approach before the Task is abandoned as stuck.

#### Acceptance Criteria

1. WHEN a Task's per-Task retry counter reaches the configured Escalation_Threshold, THE Ralph_Loop SHALL treat the next Iteration for that Task as escalated.
2. WHEN a Task is escalated and an Escalation_Persona is configured, THE Orchestrator SHALL select the Escalation_Persona for the current Iteration instead of performing the LLM-based Persona selection defined in Requirement 4.
3. WHEN the Escalation_Persona is selected, THE Ralph_Loop SHALL include in the Context_Window the Task's retry history, the captured output of every prior failing Validation_Check for that Task, and the Iteration log entries for every prior attempt on that Task.
4. WHERE no Escalation_Persona is configured, THE Ralph_Loop SHALL continue using the Persona selection defined in Requirement 4 for the remainder of the Task's retries.
5. THE Ralph_Loop SHALL accept a configurable Escalation_Threshold with a default value of 3.
6. WHEN an escalated Task's retry counter reaches the per-Task retry limit without achieving passing Validation_Checks, THE Ralph_Loop SHALL mark the Task as `stuck` and exclude it from further selection.
7. WHEN a Task becomes escalated, THE Ralph_Loop SHALL log an escalation event recording the Task identifier, the retry count at escalation, and either the selected Escalation_Persona name or an indication that no Escalation_Persona is configured.

### Requirement 6: Context Composition

**User Story:** As a project owner, I want each Iteration to receive a focused Context_Window that combines project-level context with Persona-specific instructions, so that the agent operates on minimal, relevant input.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL start a fresh Kiro_CLI session for each Iteration, discarding any prior in-memory agent context.
2. THE Context_Window SHALL include the contents of the Project_Brief file.
3. THE Context_Window SHALL include the contents of the Task_Spec file for the currently selected Task.
4. THE Context_Window SHALL include the selected Persona's prompt template, rendered with Task and project placeholders substituted.
5. THE Context_Window SHALL include the selected Persona's instructions when those instructions are defined in the Persona definition.
6. THE Context_Window SHALL include loop-framing instructions telling the agent to execute the Task under the current Persona's role, report results, and avoid exceeding the Persona's declared tool or resource restrictions.
7. IF the combined Context_Window exceeds a configurable maximum token estimate, THEN THE Ralph_Loop SHALL truncate the Project_Brief to a summary section while preserving the full Task_Spec and the full Persona prompt and instructions.

### Requirement 7: Validation Checks

**User Story:** As a project owner, I want the loop to run objective checks after each Iteration, so that Task status is determined by evidence rather than agent self-report.

#### Acceptance Criteria

1. THE Task_Spec SHALL declare one or more Validation_Checks, each with a type and type-specific configuration.
2. THE Ralph_Loop SHALL support Validation_Checks of type `shell` configured with one or more shell commands.
3. THE Ralph_Loop SHALL support Validation_Checks of type `persona_review` configured with a reviewing Persona name and an optional natural-language pass condition.
4. THE Ralph_Loop SHALL support Validation_Checks of type `file_exists` configured with one or more filesystem paths.
5. WHEN a `shell` Validation_Check runs, THE Ralph_Loop SHALL mark that check as passing when every configured command exits with code 0 and failing otherwise.
6. EACH `persona_review` Validation_Check SHALL declare a pass condition expressed in natural language, for example "no critical issues or gaps" for an editorial review or "all acceptance criteria met" for a code review.
7. WHERE a `persona_review` Validation_Check in a Task_Spec does not declare its own pass condition, THE Ralph_Loop SHALL use the default `persona_review` pass condition declared in the reviewing Persona's definition.
8. IF a `persona_review` Validation_Check does not declare a pass condition and the reviewing Persona's definition does not declare a default pass condition, THEN THE Ralph_Loop SHALL mark the current Task as `stuck` and log an error identifying the Task identifier and the reviewing Persona name.
9. WHEN a `persona_review` Validation_Check runs, THE Ralph_Loop SHALL invoke the reviewing Persona in a separate Kiro_CLI session with the Task artifacts and the resolved pass condition as input, and the reviewing Persona SHALL return a structured verdict of `pass` or `fail` together with a rationale.
10. THE Ralph_Loop SHALL mark a `persona_review` Validation_Check as passing when the reviewing Persona returns a `pass` verdict and failing otherwise, and SHALL log the reviewing Persona name, the resolved pass condition, the verdict, and the rationale.
11. WHEN a `file_exists` Validation_Check runs, THE Ralph_Loop SHALL mark that check as passing when every configured path exists on the filesystem and failing otherwise.
12. WHEN any Validation_Check for a Task fails, THE Ralph_Loop SHALL mark the Task as failing and capture each failing check's output in the Iteration log.
13. IF a Validation_Check exceeds a configurable timeout, THEN THE Ralph_Loop SHALL terminate that check, mark the Task as failing, and log a timeout error identifying the check.

### Requirement 8: Dynamic Task Creation

**User Story:** As a project owner, I want a running Persona to be able to create new Tasks when it discovers additional work, so that the loop can expand its own scope (for example, a Reviewer creating follow-up Tasks for a Writer).

#### Acceptance Criteria

1. WHEN a Persona creates new Tasks during an Iteration, THE Persona SHALL do so by writing new Task entries directly to the `tasks.json` file using Kiro_CLI's file tools.
2. WHEN an Iteration completes, THE Ralph_Loop SHALL detect newly added Task entries by comparing the state of `tasks.json` captured immediately before the Iteration with the state captured immediately after the Iteration.
3. WHEN newly added Task entries are detected after an Iteration, THE Ralph_Loop SHALL treat those entries as a Task_Creation_Event.
4. WHEN processing a Task_Creation_Event, THE Ralph_Loop SHALL validate each new Task against the Task schema defined in Requirement 2 and reject entries that fail validation without appending them to the Pending_Task_Queue.
5. WHEN a new Task is accepted, THE Ralph_Loop SHALL record the Iteration number and the creating Persona on that Task as creation metadata.
6. THE Ralph_Loop SHALL make accepted new Tasks eligible for selection in subsequent Iterations according to Requirement 1 and Requirement 2.
7. IF a new Task references a target Persona that does not exist in the Persona_Registry, THEN THE Ralph_Loop SHALL reject that Task without appending the Task to the Pending_Task_Queue and log an error identifying the missing Persona.
8. IF an Iteration modifies or deletes an existing Task entry in `tasks.json` other than the Task currently being executed, THEN THE Ralph_Loop SHALL revert that modification or deletion, restore the pre-Iteration state for that Task entry, and log a warning identifying the Iteration number, the affected Task identifier, and the acting Persona.
9. THE Ralph_Loop SHALL enforce a Task_Creation_Budget consisting of a per-Iteration maximum and a per-run maximum number of new Tasks.
10. WHEN the per-Iteration Task_Creation_Budget is exceeded, THE Ralph_Loop SHALL append the surplus Tasks to the Pending_Task_Queue and log a warning identifying the Iteration, the creating Persona, and the number of Tasks appended to the Pending_Task_Queue.
11. WHEN the per-run Task_Creation_Budget is exceeded, THE Ralph_Loop SHALL append all further newly created Tasks to the Pending_Task_Queue for the remainder of the run and log a warning identifying the run, the creating Persona, and the number of Tasks appended to the Pending_Task_Queue.
12. WHEN the creation-metadata chain for a new Task exceeds a configurable maximum depth, THE Ralph_Loop SHALL reject the Task without appending the Task to the Pending_Task_Queue and log a circular-creation warning identifying the ancestor chain.
13. WHEN appending a Task to the Pending_Task_Queue under acceptance criterion 10 or 11, THE Ralph_Loop SHALL preserve the Task's creation metadata (Iteration number and creating Persona) and record the run identifier in which the Task was spilled to the Pending_Task_Queue.

### Requirement 9: Pending Task Queue Processing

**User Story:** As a project owner, I want Tasks that were pushed out of prior runs by Task_Creation_Budget limits to be re-admitted on the next run, so that surplus work is not silently lost between runs.

#### Acceptance Criteria

1. WHEN the Ralph_Loop starts, after loading the Task_List and before selecting the first Task for the first Iteration, THE Ralph_Loop SHALL check for the existence of the Pending_Task_Queue file at the configured path.
2. WHERE the Pending_Task_Queue file does not exist or is empty, THE Ralph_Loop SHALL proceed with normal Task selection without modification.
3. WHEN the Pending_Task_Queue file exists and contains one or more Task entries, THE Ralph_Loop SHALL load each entry and validate the entry against the Task schema defined in Requirement 2.
4. WHEN a pending Task entry passes schema validation and references a target Persona, THE Ralph_Loop SHALL verify that the referenced Persona exists in the Persona_Registry per Requirement 3.
5. WHEN a pending Task entry passes both schema validation and persona-existence validation, THE Ralph_Loop SHALL admit the Task into the Task_List and make the Task eligible for selection in subsequent Iterations.
6. IF a pending Task entry fails schema validation or references a target Persona that does not exist in the Persona_Registry, THEN THE Ralph_Loop SHALL discard that pending Task entry and log a warning identifying the pending Task's identifier and the reason for discard.
7. WHEN a pending Task entry is admitted to the Task_List, THE Ralph_Loop SHALL preserve the Task's original creation metadata (the Iteration number and creating Persona from the run that originally created the Task) and SHALL add metadata recording the run identifier in which the Task was spilled to the Pending_Task_Queue and the run identifier in which the Task was re-admitted.
8. THE Ralph_Loop SHALL NOT count Tasks admitted from the Pending_Task_Queue against the per-run Task_Creation_Budget for the current run.
9. WHEN Pending_Task_Queue processing completes, THE Ralph_Loop SHALL truncate the Pending_Task_Queue file to an empty state.
10. THE Ralph_Loop SHALL log, after Pending_Task_Queue processing completes, the number of pending Tasks loaded, the number admitted, and the number discarded.
11. IF the Pending_Task_Queue file cannot be parsed as valid JSON, THEN THE Ralph_Loop SHALL exit with a descriptive error and a non-zero exit code identifying the Pending_Task_Queue file path and the parse error.

### Requirement 10: Budget and Safety Controls

**User Story:** As a project owner, I want configurable limits on iterations, retries, time, and Task creation, so that the loop cannot run indefinitely or consume unbounded resources.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL accept a configurable maximum number of total Iterations with a default value of 50.
2. THE Ralph_Loop SHALL accept a configurable maximum number of retries per individual Task with a default value of 5.
3. WHEN a Task's retry counter reaches the per-Task retry limit, THE Ralph_Loop SHALL mark that Task as `stuck` and exclude it from further selection.
4. THE Ralph_Loop SHALL accept a configurable maximum wall-clock time limit with a default value of 60 minutes.
5. WHEN the wall-clock time limit is reached, THE Ralph_Loop SHALL stop scheduling new Iterations, persist the current Task_List state, and terminate with a non-zero exit code.
6. THE Ralph_Loop SHALL accept a configurable per-Iteration Task_Creation_Budget with a default value of 10 new Tasks per Iteration.
7. THE Ralph_Loop SHALL accept a configurable per-run Task_Creation_Budget with a default value of 100 new Tasks per run.
8. THE Ralph_Loop SHALL accept a configurable maximum creation-chain depth for Task_Creation_Events with a default value of 5.

### Requirement 11: Logging and Observability

**User Story:** As a project owner, I want detailed logs of each Iteration, so that I can understand what each Persona did and debug failures.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL create a log directory containing one log file per Iteration.
2. THE Ralph_Loop SHALL capture Kiro_CLI stdout and stderr output in the per-Iteration log file.
3. THE Ralph_Loop SHALL log, for each Iteration, the start time, end time, selected Task identifier, selected Persona name, Persona selection path, Orchestrator LLM decision rationale when applicable, Validation_Check outcomes (including `persona_review` verdicts and rationales per Requirement 7), Task_Creation_Event summary, and Iteration outcome chosen from `pass`, `fail`, `stuck`, `escalated`, or `timeout`.
4. WHEN the Ralph_Loop terminates, THE Ralph_Loop SHALL write a summary log containing total Iterations run, count of Tasks in each status, total new Tasks created, count of escalation events, total elapsed time, and the Token_Usage totals defined in Requirement 12.
5. THE Ralph_Loop SHALL write all log output to both log files and stdout concurrently.

### Requirement 12: Token and Cost Observability

**User Story:** As a project owner, I want the Ralph_Loop to record token usage and estimated cost for every LLM call, so that I can monitor spend and compare cost across Personas and Iterations.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL capture the Token_Usage reported by Kiro_CLI or the Orchestrator's LLM for each LLM call, including Persona execution Iterations, Orchestrator Persona selection calls, `persona_review` Validation_Check invocations, Planner_Persona invocations, and Escalation_Persona invocations.
2. THE per-Iteration log file SHALL include, for each LLM call performed during the Iteration, the call kind (chosen from `persona_execution`, `orchestrator_selection`, `persona_review`, `planner`, or `escalation`), the model identifier when known, the input token count, the output token count, and the estimated cost when Model_Pricing is configured for the model identifier.
3. WHERE Model_Pricing is configured for a model identifier, THE Ralph_Loop SHALL compute the estimated cost for each LLM call using that model identifier as (input_tokens × input_price) + (output_tokens × output_price).
4. WHERE Model_Pricing is not configured for a model identifier, THE Ralph_Loop SHALL record only the input and output token counts and omit the estimated cost field.
5. WHEN the Ralph_Loop terminates, THE Ralph_Loop SHALL include in the summary log the total input tokens, total output tokens, total combined tokens, and total estimated cost summed across all LLM calls in the run.
6. IF Kiro_CLI or the Orchestrator LLM does not report Token_Usage for a given call, THEN THE Ralph_Loop SHALL log a warning identifying the call kind and Iteration and SHALL exclude that call from the token and cost totals.

### Requirement 13: Git Integration

**User Story:** As a project owner, I want the Ralph_Loop to commit the working tree after each Iteration and support rollback, so that I can review per-Iteration changes and revert to earlier states when needed.

#### Acceptance Criteria

1. WHEN an Iteration completes (including Iterations whose Task failed Validation_Checks or was marked `stuck`) and Git_Integration is enabled, THE Ralph_Loop SHALL create an Iteration_Commit capturing every working-tree change made during that Iteration.
2. THE commit message for each Iteration_Commit SHALL include the Iteration number, the Task identifier, the Persona name, and the Validation outcome for the Iteration.
3. IF the project directory is not a git repository, THEN THE Ralph_Loop SHALL log a warning, skip all git operations for the run, and continue executing without treating the absence of a git repository as a fatal error.
4. IF Git_Integration is disabled by configuration, THEN THE Ralph_Loop SHALL skip all git operations and log that Git_Integration is disabled at startup.
5. THE Ralph_Loop SHALL support a `rollback` subcommand (or equivalent CLI flag) that accepts an Iteration number and reverts the working tree to the state captured by the corresponding Iteration_Commit.
6. IF the `rollback` subcommand is invoked with an Iteration number for which no Iteration_Commit exists, THEN THE Ralph_Loop SHALL exit with a descriptive error and a non-zero exit code.
7. WHEN Git_Integration is enabled and a git operation fails (for example, a commit or checkout error), THE Ralph_Loop SHALL log the failure with the underlying error message and continue the run without terminating.

### Requirement 14: Resumability

**User Story:** As a project owner, I want to interrupt the Ralph_Loop at any time and restart it without losing progress, so that I can pause long runs and recover from crashes without manual cleanup.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL treat the filesystem state, comprising `tasks.json`, the Pending_Task_Queue file, the Task_Spec files, the Project_Brief, and the Persona_Registry, as the single source of truth for resuming a run.
2. THE Ralph_Loop SHALL be safely interruptible; the user may terminate the Ralph_Loop process at any time, and restarting the Ralph_Loop SHALL resume from the current filesystem state without corruption.
3. WHEN the Ralph_Loop starts, THE Ralph_Loop SHALL scan the Task_List for Tasks with status `in_progress` and reset each such Task's status to `failing`.
4. WHEN the Ralph_Loop resets an `in_progress` Task's status to `failing` per acceptance criterion 3, THE Ralph_Loop SHALL NOT increment that Task's per-Task retry counter, on the basis that the prior attempt was interrupted before completion and not caused by the Persona.
5. WHEN an Iteration is selected for a Task whose status was reset from `in_progress` to `failing` by acceptance criterion 3, THE Ralph_Loop SHALL include in the Context_Window a notice stating that the Task was previously interrupted and instructing the Persona to inspect the current state of Task artifacts before proceeding.
6. WHEN the Ralph_Loop starts, THE Ralph_Loop SHALL log the number of Tasks whose status was reset from `in_progress` to `failing` and the identifiers of those Tasks.

### Requirement 15: Configuration

**User Story:** As a project owner, I want to configure loop behavior via a config file or CLI arguments, so that I can adapt it to different projects and domains.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL accept configuration via a `ralph.config.json` file in the project root.
2. THE Ralph_Loop SHALL accept CLI arguments that override values from the config file.
3. THE Ralph_Loop SHALL support configuring at least the following: path to `tasks.json`, path to `SUMMARY.md`, path to the Persona_Registry directory, path to the Pending_Task_Queue file, fallback Persona name, Escalation_Persona name, Escalation_Threshold, Planner_Persona name, automatic-planner flag, Orchestrator LLM command path or model identifier, maximum Iterations, maximum retries per Task, wall-clock timeout, log directory path, Kiro_CLI command path, Validation_Check timeout, per-Iteration Task_Creation_Budget, per-run Task_Creation_Budget, maximum creation-chain depth, Git_Integration enabled flag, and Model_Pricing map from model identifier to per-input-token and per-output-token price.
4. WHERE an Orchestrator LLM command path or model identifier is not configured, THE Ralph_Loop SHALL use the configured Kiro_CLI command for Orchestrator LLM selection calls.
5. WHERE the Pending_Task_Queue file path is not configured, THE Ralph_Loop SHALL use `pending_tasks.json` in the project root as the default path.
6. WHERE the Git_Integration enabled flag is not configured, THE Ralph_Loop SHALL default Git_Integration to enabled.
7. WHERE the automatic-planner flag is not configured, THE Ralph_Loop SHALL default the automatic-planner flag to disabled.
8. IF no configuration file is found and no CLI arguments are provided, THEN THE Ralph_Loop SHALL use default values for all configurable options.
9. IF a required file identified in configuration is missing at startup, THEN THE Ralph_Loop SHALL exit with a descriptive error message identifying the missing file and a non-zero exit code.

### Requirement 16: Project Initialization

**User Story:** As a project owner, I want a command to scaffold the Ralph_Loop project structure, so that I can quickly set up a new project for any supported domain.

#### Acceptance Criteria

1. WHEN the user runs the Ralph_Loop with an `init` subcommand, THE Ralph_Loop SHALL create a `SUMMARY.md` template file in the project root.
2. WHEN the user runs the Ralph_Loop with an `init` subcommand, THE Ralph_Loop SHALL create a `tasks.json` file containing an empty Task array.
3. WHEN the user runs the Ralph_Loop with an `init` subcommand, THE Ralph_Loop SHALL create an empty `pending_tasks.json` file at the configured Pending_Task_Queue path, containing an empty Task array.
4. WHEN the user runs the Ralph_Loop with an `init` subcommand, THE Ralph_Loop SHALL create a `ralph.config.json` file populated with default configuration values, including a default Escalation_Threshold, a placeholder fallback Persona name, a placeholder Planner_Persona name, a default automatic-planner flag value of disabled, and a default Git_Integration enabled flag value of enabled.
5. WHEN the user runs the Ralph_Loop with an `init` subcommand, THE Ralph_Loop SHALL create a `specs/` directory for Task_Spec files.
6. WHEN the user runs the Ralph_Loop with an `init` subcommand, THE Ralph_Loop SHALL create a `personas/` directory seeded with a minimal default Persona definition that includes a Persona name, a Persona_Description, and a prompt template.
7. WHEN the user runs the Ralph_Loop with an `init` subcommand and provides a domain template flag, THE Ralph_Loop SHALL seed the `personas/` directory with the Persona definitions associated with that template, each including a Persona_Description, and SHALL seed the Planner_Persona definition associated with that template.
8. THE Ralph_Loop SHALL support an `init-tasks` subcommand that invokes the configured Planner_Persona to generate the initial Task_List per Requirement 17.
9. IF the target project directory already contains Ralph_Loop files, THEN THE Ralph_Loop SHALL prompt the user before overwriting any existing file.

### Requirement 17: Planner Persona and Project Bootstrap

**User Story:** As a project owner, I want a Planner_Persona that reads my Project_Brief and generates an initial Task_List, so that I do not have to hand-author Tasks before the Ralph_Loop can begin useful work.

#### Acceptance Criteria

1. THE Ralph_Loop SHALL support a configurable Planner_Persona, selected by the same Persona name mechanism used for the Escalation_Persona and fallback Persona.
2. WHEN the user runs the `init-tasks` subcommand, THE Ralph_Loop SHALL invoke the Planner_Persona via a Kiro_CLI session whose Context_Window includes the Project_Brief and instructions to produce the initial Task_List.
3. WHEN the Ralph_Loop starts a run, the automatic-planner flag is enabled, and the Task_List is empty, THE Ralph_Loop SHALL invoke the Planner_Persona before selecting the first Task for the first Iteration.
4. WHEN the Ralph_Loop starts a run, the automatic-planner flag is disabled, and the Task_List is empty, THE Ralph_Loop SHALL log an informational message identifying the `init-tasks` subcommand and terminate with a non-zero exit code without selecting any Task.
5. WHEN the Planner_Persona is invoked, THE Planner_Persona SHALL write new Task entries directly to `tasks.json` using Kiro_CLI's file tools.
6. WHEN the Planner_Persona completes, THE Ralph_Loop SHALL validate each newly added Task entry against the Task schema defined in Requirement 2 and against Persona existence in the Persona_Registry, rejecting entries that fail validation with a logged error identifying the rejected entry.
7. IF no Planner_Persona is configured when the `init-tasks` subcommand is invoked, or when the automatic-planner flag is enabled and the Task_List is empty, THEN THE Ralph_Loop SHALL exit with a descriptive error and a non-zero exit code.
8. THE Ralph_Loop SHALL log the invocation of the Planner_Persona, its Token_Usage per Requirement 12, and the number of Tasks accepted and rejected from its output.

### Requirement 18: Task Spec File Format

**User Story:** As a project owner, I want a clear spec file format for each Task, so that each Persona receives well-structured instructions and knows how to verify its work.

#### Acceptance Criteria

1. THE Task_Spec SHALL be a Markdown file with a YAML frontmatter block containing at minimum the fields `id`, `title`, and `validation`.
2. THE Task_Spec frontmatter SHALL support an optional `target_persona` field, an optional `tags` field used as input to the Orchestrator's LLM-based Persona selection, an optional `persona_fields` map containing Persona-specific configuration passed to the selected Persona's prompt template, and an optional `depends_on` field whose value is an array of Task identifiers mirroring the `depends_on` field on the Task entry in the Task_List.
3. THE Task_Spec body SHALL contain sections for objective, context references, instructions, and notes.
4. WHEN composing the Context_Window, THE Ralph_Loop SHALL parse the Task_Spec frontmatter to extract `target_persona`, `validation`, `tags`, `persona_fields`, and `depends_on`.
5. THE Task_Spec SHALL support referencing other files via relative paths in a context section, and THE Ralph_Loop SHALL include the content of each referenced file in the Context_Window.
6. IF a referenced context file does not exist, THEN THE Ralph_Loop SHALL log a warning identifying the missing path and continue without that file's content.
7. IF a Task_Spec frontmatter is missing a required field or contains an invalid value, THEN THE Ralph_Loop SHALL mark the Task as `stuck` and log an error identifying the Task identifier and the invalid field.
