# Bugfix Requirements Document

## Introduction

The `persona_review` validation check in `ralph_loop/validator.py` frequently fails
to parse a reviewing persona's verdict even when that persona returned a valid
`{"verdict": "pass"|"fail", "rationale": "..."}` JSON object. When the parser
can't extract the verdict, `_run_persona_review_check` logs:

```
persona_review check '<name>': could not parse verdict from reviewing persona
'<PersonaName>'; marking fail
```

and records the check as `fail`. This is happening on output that is actually
passing review, so tasks like `ch02-r01-python` (observed in a real HCLS book
project run with reviewers `TechCodeReviewer`, `TechExpertReviewer`,
`TechEditor`) burn their full `max_retries_per_task` budget and end up `stuck`
or failing unnecessarily.

Two causes are suspected (to be confirmed during design):

1. `_extract_first_json_object` (`ralph_loop/validator.py`, ~line 145) performs
   a naive brace-matching scan that does not track string/escape state, so any
   `{` or `}` character appearing inside a rationale string breaks depth
   counting and returns a truncated or unbalanced substring that subsequently
   fails `json.loads`.
2. Kiro CLI stdout often carries more than just the verdict object. It may wrap
   the JSON in a markdown fence (```` ```json ... ``` ````) or prepend a
   tool-use envelope (e.g. `{"tool": ...}`) before the real verdict. The
   current extractor returns the *first* balanced `{...}` it finds, which may
   be the tool-use envelope or something other than the verdict, causing the
   Pydantic `PersonaReviewVerdict` validation to fail and the check to be
   marked `fail`.

An identical `_extract_first_json_object` helper exists in
`ralph_loop/orchestrator.py` and is used by `Orchestrator._parse_decision` to
parse `{"persona": "...", "rationale": "..."}`. It is vulnerable to the same
string-handling and envelope-wrapping patterns; whether to fix both in one
place (shared helper) or fix each independently is a design decision tracked in
design.md.

Impact: valid persona reviews are silently dropped, retry counters are wasted
on false failures, `max_retries_per_task` is exhausted on tasks that should
have passed, and escalations/stuck transitions are triggered on review-only
failures rather than real content defects.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the reviewing persona's stdout contains a valid verdict JSON object
wrapped in a markdown code fence (e.g. ```` ```json\n{"verdict": "pass",
"rationale": "..."}\n``` ````) THEN the system fails to parse the verdict,
logs the "could not parse verdict" warning, and records the check as `fail`.

1.2 WHEN the reviewing persona's stdout contains a leading tool-use or
metadata JSON object followed by the verdict JSON object (e.g.
`{"tool": "read_file", ...}\n{"verdict": "pass", "rationale": "..."}`) THEN
the system extracts the first JSON object (the tool-use envelope), Pydantic
validation of `PersonaReviewVerdict` rejects it, and the check is recorded as
`fail`.

1.3 WHEN the reviewing persona's stdout contains a verdict JSON whose
`rationale` string contains a literal `{` or `}` character (e.g.
`{"verdict": "pass", "rationale": "missing }  in expression"}`) THEN the
brace-matching scan miscounts depth (because it does not track string state),
returns a substring that is not the full verdict object, `json.loads` raises,
and the check is recorded as `fail`.

1.4 WHEN the reviewing persona's stdout contains a verdict JSON whose
`rationale` string contains an escaped quote (`\"`) with a brace later in the
same string (e.g. `{"verdict": "fail", "rationale": "saw \"{\" unexpected"}`)
THEN the naive scanner can either misinterpret a quoted `{`/`}` as structural
or terminate early at the escaped-close case, producing a substring that fails
`json.loads` and the check is recorded as `fail`.

1.5 WHEN the reviewing persona's stdout contains multiple JSON objects and
only one of them is a valid verdict shape (the others being tool-use envelopes,
progress messages, or other Kiro CLI metadata) THEN the system parses only the
first balanced `{...}` substring; if that first object is not the verdict,
Pydantic validation fails and the check is recorded as `fail`.

### Expected Behavior (Correct)

2.1 WHEN the reviewing persona's stdout contains a valid verdict JSON object
wrapped in a markdown code fence THEN the system SHALL strip fence markers
(```` ``` ```` and any language tag such as `json`) before parsing and SHALL
parse the enclosed verdict, yielding the correct `pass`/`fail` and rationale.

2.2 WHEN the reviewing persona's stdout contains a leading tool-use or
metadata JSON object followed by the verdict JSON object THEN the system SHALL
find the verdict object specifically (e.g. by scanning all balanced JSON
objects in stdout and selecting the first one that successfully validates as
`PersonaReviewVerdict`), yielding the correct `pass`/`fail` and rationale.

2.3 WHEN the reviewing persona's stdout contains a verdict JSON whose
`rationale` string contains literal `{` or `}` characters THEN the system
SHALL correctly track string state during brace matching (ignoring `{`/`}`
inside `"..."` strings) and SHALL return the full verdict object to Pydantic
for a successful parse.

2.4 WHEN the reviewing persona's stdout contains a verdict JSON whose
`rationale` string contains escaped quotes (`\"`) or escaped backslashes (`\\`)
around braces THEN the system SHALL correctly track JSON string-escape state
during brace matching, SHALL treat quoted/escaped braces as non-structural,
and SHALL return the full verdict object for a successful parse.

2.5 WHEN the reviewing persona's stdout contains multiple JSON objects and
exactly one of them validates as `PersonaReviewVerdict` THEN the system SHALL
return that object's verdict and rationale rather than failing on a
non-verdict object encountered earlier in the stream.

2.6 WHEN verdict extraction fails despite all of the above (genuinely
malformed or missing verdict in stdout) THEN the system SHALL CONTINUE TO
record the check as `fail` with the existing "could not parse verdict" warning
and capture the stdout in `CheckResult.output`, preserving the current
malformed-output diagnostic behavior.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the reviewing persona's stdout is exactly a single valid verdict JSON
object with no surrounding prose (the current happy path covered by
`test_valid_verdict_captures_rationale_and_condition`) THEN the system SHALL
CONTINUE TO parse it and yield the correct `verdict` and `rationale`.

3.2 WHEN the reviewing persona's stdout contains a valid verdict JSON object
surrounded by prose but with no code fences and no other JSON objects (the
current extraction case covered by `test_verdict_wrapped_in_prose_is_extracted`)
THEN the system SHALL CONTINUE TO extract and parse it correctly.

3.3 WHEN the reviewing persona's stdout cannot be parsed into any valid
`PersonaReviewVerdict` THEN the system SHALL CONTINUE TO record a failing
`CheckResult` with `verdict="fail"`, `reviewing_persona` set,
`resolved_pass_condition` set, `timed_out=False`, and `output` containing the
raw stdout (the current behavior covered by
`test_unparseable_verdict_marks_fail`).

3.4 WHEN the reviewing persona's Kiro CLI invocation times out THEN the
system SHALL CONTINUE TO return a `CheckResult` with `verdict="fail"` and
`timed_out=True`, independent of any verdict-parsing changes.

3.5 WHEN the `Orchestrator._parse_decision` path receives a simple
`{"persona": "...", "rationale": "..."}` JSON object (with or without
surrounding prose) THEN the system SHALL CONTINUE TO parse it to an
`OrchestratorDecision` as it does today, regardless of whether the parser
helper is kept per-module or extracted to a shared location.

3.6 WHEN all other validation checks run (`shell`, `file_exists`, aggregation
in `aggregate_checks`, pass-condition resolution in `resolve_pass_condition`,
self-review rejection, context-file inlining, missing-reviewer stuck
behavior) THEN the system SHALL CONTINUE TO behave exactly as today, since
the fix is scoped to verdict-string extraction and parsing only.
