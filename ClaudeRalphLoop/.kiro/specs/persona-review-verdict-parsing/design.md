# Persona Review Verdict Parsing Bugfix Design

## Overview

The `persona_review` validation check in `ralph_loop/validator.py` loses valid
reviewing-persona verdicts whenever the reviewer wraps its JSON in a markdown
fence, prepends a tool-use envelope, or emits a rationale string that contains
`{`, `}`, `\"`, or `\\`. The root cause is a single helper —
`_extract_first_json_object` — that is duplicated in both `validator.py`
(~line 145) and `orchestrator.py` (~line 166) and does not track JSON string
state, escape state, or markdown fences.

The fix replaces both duplicated helpers with a shared module
`ralph_loop/json_extract.py` exposing:

- `strip_markdown_fences(text: str) -> str` — one-pass removal of
  ```` ```<lang>\n ... \n``` ```` wrappers when they bracket otherwise-JSON
  content; a no-op when no fences are present.
- `iter_balanced_json_objects(text: str) -> Iterator[str]` — yields every
  top-level balanced `{...}` substring in order of appearance, correctly
  honoring JSON string state (`"..."`) and escape state (`\"`, `\\`). O(n) in
  `len(text)`; never raises; silently skips unbalanced spans.
- `extract_validating_object(text: str, model: type[T]) -> T | None` — the
  single entry point both `validator.py::_run_persona_review_check` and
  `orchestrator.py::Orchestrator._parse_decision` will call. Strips fences,
  iterates candidates, returns the first one that successfully validates as
  `model`, else `None`.

Both existing module-private `_extract_first_json_object` helpers are deleted
(not kept as thin adapters); the two call sites migrate to
`extract_validating_object` directly. The helper uses no regex, no new
dependencies, and lives behind a 3-function API consumed by exactly two
callers.

## Glossary

- **Bug_Condition (C)**: An input `stdout: str` for which the reviewing
  persona did emit a valid `PersonaReviewVerdict` JSON object somewhere in
  the stream, yet the current
  `_extract_first_json_object + json.loads + PersonaReviewVerdict.model_validate`
  pipeline returns no verdict (either `_extract_first_json_object` returned
  `None` / a truncated substring, or the returned substring parsed as a
  non-verdict object).
- **Property (P)**: When `C(stdout)` holds, the fixed pipeline SHALL return a
  `PersonaReviewVerdict` whose `verdict` and `rationale` equal the payload
  that was embedded into `stdout`.
- **Preservation**: For any `stdout` where `C(stdout)` does NOT hold, the
  fixed pipeline SHALL produce exactly the same outcome as the original
  pipeline (same returned `PersonaReviewVerdict`, same `None`). In
  particular, the "single flat JSON object with no prose/fences/envelopes"
  happy path used by both `PersonaReviewVerdict` and `OrchestratorDecision`
  must be byte-for-byte unchanged.
- **`_extract_first_json_object`**: The naive brace-matching helper in
  `ralph_loop/validator.py` and its copy in `ralph_loop/orchestrator.py`; the
  site of the bug.
- **`PersonaReviewVerdict`**: The Pydantic v2 model in `ralph_loop/validator.py`
  with fields `verdict: Literal["pass","fail"]` and `rationale: str`.
- **`OrchestratorDecision`**: The Pydantic v2 model in
  `ralph_loop/orchestrator.py` with fields `persona: str` and `rationale: str`.
- **Markdown fence**: A line matching ``` ```<lang> ``` ``` (optional
  language tag) opening a block, closed by a line consisting solely of
  ``` ``` ```.
- **Balanced object**: The substring from an opening `{` at position `i`
  through the matching `}` at position `j` such that `text[i:j+1]`
  brace-balances under JSON-aware scanning (`{`/`}` inside `"..."` and
  behind `\` do not count as structural).

## Bug Details

### Bug Condition

The bug manifests when the reviewing persona's stdout contains a valid
`PersonaReviewVerdict` JSON object, but the first balanced `{...}`
substring produced by the naive scanner is either (a) wrapped so that the
scanner never finds it, (b) preceded by a non-verdict object that the
scanner returns first, or (c) corrupted by a literal brace or escape
sequence inside a string value.

**Formal Specification:**

```
FUNCTION isBugCondition(stdout)
  INPUT:  stdout of type str
  OUTPUT: boolean

  # There exists a valid verdict payload embedded somewhere in stdout...
  EXISTS verdict_payload SUCH THAT
      PersonaReviewVerdict.model_validate(verdict_payload) succeeds
      AND json.dumps(verdict_payload) appears as a substring of stdout
          (possibly surrounded by prose, fences, or other JSON objects)

  # ...but the current pipeline fails to recover it.
  AND LET
      extracted := _extract_first_json_object(stdout)   # naive helper
  IN
      extracted IS None
      OR NOT try(PersonaReviewVerdict.model_validate(json.loads(extracted)))

  RETURN True when both clauses hold, else False
END FUNCTION
```

The same predicate applies, mutatis mutandis, when the target model is
`OrchestratorDecision`. The helper module parameterizes over the Pydantic
model so a single implementation covers both call sites.

### Examples

- **1.1 Markdown fence**:
  `stdout = "```json\n{\"verdict\": \"pass\", \"rationale\": \"ok\"}\n```"`.
  The naive scanner returns `"{\"verdict\"... \"ok\"}"` correctly, but this
  example is included because a common variant wraps the fence around
  the entire stdout without a leading `{` on line 0; the fenced-content
  extractor makes the intent explicit. Expected: verdict=`pass`,
  rationale=`"ok"`.

- **1.2 Leading tool-use envelope**:
  `stdout = '{"tool":"read_file","args":{"path":"x"}}\n{"verdict":"pass","rationale":"ok"}'`.
  The naive scanner returns the tool-use envelope, which fails
  `PersonaReviewVerdict.model_validate`, and the check is marked fail.
  Expected: verdict=`pass`, rationale=`"ok"`.

- **1.3 Literal `}` inside rationale**:
  `stdout = '{"verdict":"fail","rationale":"missing } in expression"}'`.
  The naive scanner increments depth to 1 on the opening `{`, decrements
  to 0 on the `}` inside the rationale string, and returns
  `'{"verdict":"fail","rationale":"missing }'`. `json.loads` raises;
  check marked fail. Expected: verdict=`fail`, rationale=`"missing } in
  expression"`.

- **1.4 Escaped quotes around a brace**:
  `stdout = '{"verdict":"fail","rationale":"saw \\"{\\" unexpected"}'`.
  The naive scanner sees a `"` that it treats as unstructured, walks
  into what it thinks is body, and either terminates early or over-runs.
  Expected: verdict=`fail`, rationale=`'saw "{" unexpected'`.

- **1.5 Multiple objects, only one is the verdict** (edge case):
  `stdout = '{"progress":1}\n{"tool":"read_file","args":{}}\n{"verdict":"pass","rationale":"ok"}'`.
  The naive scanner returns the first object, which is not a verdict.
  Expected: verdict=`pass`, rationale=`"ok"`.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- Stdout consisting of exactly a single flat valid verdict JSON object with
  no surrounding prose, fences, or other objects MUST parse to the same
  `PersonaReviewVerdict` it produces today (existing test
  `test_valid_verdict_captures_rationale_and_condition`, R3.1).
- Stdout consisting of a single flat valid verdict JSON object surrounded by
  prose (no fences, no other objects) MUST parse to the same
  `PersonaReviewVerdict` it produces today (existing test
  `test_verdict_wrapped_in_prose_is_extracted`, R3.2).
- Stdout that cannot be parsed into any `PersonaReviewVerdict` MUST continue
  to yield a `CheckResult(verdict="fail", timed_out=False, ...)` with the
  existing `"could not parse verdict"` log line and raw stdout captured in
  `CheckResult.output` (existing test `test_unparseable_verdict_marks_fail`,
  R3.3).
- Reviewer timeouts MUST continue to produce
  `CheckResult(verdict="fail", timed_out=True, ...)` independent of
  verdict-parsing changes (existing test `test_timeout_marks_timed_out`,
  R3.4).
- `Orchestrator._parse_decision` MUST continue to parse simple
  `{"persona":"...","rationale":"..."}` stdout to an `OrchestratorDecision`
  exactly as today, whether or not the stdout is surrounded by prose
  (existing orchestrator tests, R3.5).
- Every other validator surface (shell checks, file_exists checks,
  aggregation in `aggregate_checks`, pass-condition resolution in
  `resolve_pass_condition`, self-review rejection, context-file inlining,
  missing-reviewer stuck behavior) MUST behave exactly as today; the fix is
  scoped to verdict-string extraction and parsing only (R3.6).

**Scope:**

All inputs that do NOT match the bug condition must be completely
unaffected by this fix. This includes:

- Non-`persona_review` validation checks (`shell`, `file_exists`).
- Non-verdict stdout streams (unparseable and timeout cases).
- Every orchestrator code path other than `_parse_decision`.
- Every Pydantic model definition (`PersonaReviewVerdict` and
  `OrchestratorDecision` schemas are untouched).
- The reviewing-persona prompt text (the instruction to emit JSON is
  unchanged; the fix only makes the receiver more forgiving).
- Timeout handling, self-review guard, context-file inlining.

## Hypothesized Root Cause

Based on the bug description and direct inspection of
`ralph_loop/validator.py` and `ralph_loop/orchestrator.py`, the most likely
issues are:

1. **No JSON string-state tracking** (confirmed by code inspection):
   `_extract_first_json_object` is a plain depth counter that treats every
   `{` and `}` as structural. Any literal `{`/`}` inside a JSON string
   value (e.g. inside `rationale`) miscounts depth, so the returned
   substring is truncated or unbalanced. This directly explains example
   **1.3**.

2. **No JSON escape-state tracking** (confirmed by code inspection):
   the helper does not honor `\"` or `\\`. Even if it added naive string
   tracking (toggle on `"`), an escaped quote would flip the scanner out
   of string state mid-value. This directly explains example **1.4**.

3. **No markdown-fence handling** (confirmed by code inspection and
   reviewer-prompt wording): the prompt instructs the reviewer not to use
   fences, but Kiro CLI wrappers sometimes add them anyway. The scanner
   does see the `{` inside the fence so the plain-fence case works by
   accident; however, variants where the fence content starts on a line
   other than the one containing `{` — or where the fence wraps multiple
   objects — fall back to the naive scanner. Fence stripping is a
   cheap preprocessing pass that makes the behavior deterministic.
   This directly explains example **1.1**.

4. **Returns first `{...}` rather than first-that-validates**: by design
   the helper returns after the first balanced object. Any leading
   tool-use envelope or progress object preempts the real verdict.
   This directly explains examples **1.2** and **1.5**.

5. **Duplicated implementation**: identical helpers in two modules mean a
   fix applied to one leaves the other broken. `orchestrator.py` is less
   exposed (its LLM prompt tightly constrains the shape) but has the same
   failure mode. Extracting a shared helper eliminates the drift risk.

Exploratory testing (per the Testing Strategy below) will confirm the
theoretical root causes by generating counterexamples that fail on the
UNFIXED code exactly as predicted above. If any counterexample fails for a
different reason we will re-hypothesize before implementing the fix.

## Correctness Properties

Property 1: Bug Condition - Reviewer Verdict Is Recovered From Any Legitimate Envelope

_For any_ `stdout: str` where `isBugCondition(stdout)` holds — that is,
where a valid `PersonaReviewVerdict` payload is embedded into stdout inside
a markdown fence, after a leading tool-use / progress JSON envelope, and/or
contains structural-looking characters (`{`, `}`, `\"`, `\\`) inside a
rationale string — the fixed pipeline (`extract_validating_object(stdout,
PersonaReviewVerdict)`) SHALL return a `PersonaReviewVerdict` whose
`verdict` and `rationale` equal the payload that was embedded.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Non-Bug Stdout Behaves Identically

_For any_ `stdout: str` where `isBugCondition(stdout)` does NOT hold — in
particular the "single flat JSON object, no fences, no prose with other
objects" happy path and the "unparseable" path — the fixed pipeline SHALL
produce exactly the same outcome as the original pipeline (same returned
`PersonaReviewVerdict`, same `None`), preserving R3.1, R3.2, R3.3, R3.5,
and R3.6.

**Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis above is correct, the fix is a small,
surgical replacement of both `_extract_first_json_object` helpers with a
single shared module.

**New File**: `ralph_loop/json_extract.py`

Public surface:

```
def strip_markdown_fences(text: str) -> str: ...
def iter_balanced_json_objects(text: str) -> Iterator[str]: ...
def extract_validating_object(
    text: str,
    model: type[T],
) -> T | None: ...   # where T: BaseModel
```

Module contract and algorithm:

```
FUNCTION strip_markdown_fences(text)
  INPUT:  text: str
  OUTPUT: str

  # Single pass, no regex. Look for a leading line "```<optional lang>"
  # and a matching trailing line "```". If both are found, return the
  # content between them; otherwise return text unchanged.
  #
  # Implementation: split on "\n", drop a leading fence line if present
  # (startswith "```"), drop a trailing fence line if present (stripped
  # == "```"), re-join on "\n".
END FUNCTION

FUNCTION iter_balanced_json_objects(text)
  INPUT:  text: str
  OUTPUT: Iterator[str]

  i := 0
  n := len(text)
  WHILE i < n:
    IF text[i] != "{":
      i := i + 1
      CONTINUE

    # Walk from the opening brace forward, tracking string/escape state.
    depth         := 0
    in_string     := False
    escape_next   := False
    start         := i
    j             := i
    balanced      := False
    WHILE j < n:
      c := text[j]
      IF escape_next:
        escape_next := False
      ELIF in_string:
        IF c == "\\":
          escape_next := True
        ELIF c == "\"":
          in_string := False
        # else: consume any other char as string content
      ELSE:
        IF c == "\"":
          in_string := True
        ELIF c == "{":
          depth := depth + 1
        ELIF c == "}":
          depth := depth - 1
          IF depth == 0:
            balanced := True
            BREAK
        # else: structural whitespace / punctuation; ignore
      j := j + 1

    IF balanced:
      YIELD text[start : j + 1]
      i := j + 1
    ELSE:
      # Unbalanced: skip past this opening brace; never raise.
      i := start + 1
END FUNCTION

FUNCTION extract_validating_object(text, model)
  INPUT:  text: str, model: type[BaseModel]
  OUTPUT: Optional[BaseModel]

  stripped := strip_markdown_fences(text)
  FOR candidate IN iter_balanced_json_objects(stripped):
    TRY:
      payload := json.loads(candidate)
      RETURN model.model_validate(payload)
    EXCEPT (json.JSONDecodeError, pydantic.ValidationError):
      CONTINUE
  RETURN None
END FUNCTION
```

Complexity: `strip_markdown_fences` is O(n); `iter_balanced_json_objects`
walks each character at most once per top-level object (O(n) amortized
across the whole stream since the outer pointer advances past each yielded
object). `extract_validating_object` runs `json.loads` + `model_validate`
at most once per candidate; in the common case (happy path) that's a
single call. No regex, no backtracking.

**File**: `ralph_loop/validator.py`

1. Delete `_extract_first_json_object` (the block around line 145).
2. Add `from ralph_loop.json_extract import extract_validating_object`.
3. In `_run_persona_review_check`, replace the current block:

   ```
   json_text = _extract_first_json_object(invocation.stdout)
   parsed_verdict: Optional[PersonaReviewVerdict] = None
   if json_text is not None:
       try:
           payload = json.loads(json_text)
           parsed_verdict = PersonaReviewVerdict.model_validate(payload)
       except (json.JSONDecodeError, ValidationError):
           parsed_verdict = None
   ```

   with:

   ```
   parsed_verdict = extract_validating_object(
       invocation.stdout, PersonaReviewVerdict
   )
   ```

4. Everything downstream of `parsed_verdict` (fail-marking, log line,
   `CheckResult` construction) is unchanged.

**File**: `ralph_loop/orchestrator.py`

1. Delete `_extract_first_json_object` (the block around line 166).
2. Add `from ralph_loop.json_extract import extract_validating_object`.
3. In `Orchestrator._parse_decision`, replace the candidate-list +
   try-each loop with a single call:

   ```
   def _parse_decision(self, raw: str) -> Optional[OrchestratorDecision]:
       if not raw:
           return None
       return extract_validating_object(raw, OrchestratorDecision)
   ```

   Note on behavior delta: for a happy-path flat
   `{"persona":"X","rationale":"Y"}` stdout, `extract_validating_object`
   yields exactly one candidate (the whole stripped string), which passes
   validation on the first attempt. The returned `OrchestratorDecision`
   is byte-for-byte identical to what the current code returns. For the
   "prose-wrapped" case the current code tries `json.loads(raw)` first,
   fails, then falls back to extraction; the new code goes straight to
   the iterator but produces the same result. The preservation property
   below proves this formally.

**Impact analysis**:

- `_extract_first_json_object` callers (search result above): exactly two
  — `validator.py::_run_persona_review_check` and
  `orchestrator.py::Orchestrator._parse_decision`. Both migrate to
  `extract_validating_object`. No adapter layer is kept; the old helpers
  are deleted.
- The `json` import in `validator.py` is still needed elsewhere (no
  change); in `orchestrator.py` the local `json` import becomes unused
  and is removed.
- The `ValidationError` import in `validator.py` is still used by other
  Pydantic-backed code paths (no change).
- Token counting, tracing, log lines, `CheckResult` shape, and
  `PersonaSelection` shape are untouched.
- No prompt text, persona YAML, schema, or public API changes.

## Testing Strategy

### Validation Approach

Two phases. Phase 1 (exploratory) runs new tests against UNFIXED code to
surface counterexamples that demonstrate the bug and confirm the
root-cause hypotheses. Phase 2 (fix + verification) implements the shared
module, switches both call sites over, and runs the full 463-test suite
plus the new property-based tests to confirm fix and preservation.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE
implementing the fix. Confirm (or refute) the root-cause analysis. If any
counterexample fails for a reason other than the predicted root cause we
re-hypothesize before touching implementation code.

**Test Plan**: Write five deterministic unit tests inside
`TestPersonaReviewCheck` in `tests/test_validator.py` that stub the Kiro
invoker with a stdout known to contain a valid verdict, and assert the
resulting `CheckResult.verdict == "pass"` with the matching `rationale`.
Run the tests against the UNFIXED code; every one of them MUST fail in
a predictable way (either `_extract_first_json_object` returned the wrong
substring, or Pydantic rejected the returned substring). Capture the
failure mode in the task's failing-example field when recording the PBT
status for Property 1.

**Test Cases** (all will fail on unfixed code):

1. **Markdown-fenced verdict**: stdout =
   ` ```json\n{"verdict":"pass","rationale":"ok"}\n``` `. Expected:
   `verdict="pass"`, `rationale="ok"`.
2. **Leading tool-use envelope**: stdout =
   `'{"tool":"read_file","args":{"path":"x"}}\n{"verdict":"pass","rationale":"ok"}'`.
   Expected: `verdict="pass"`, `rationale="ok"`.
3. **Literal `}` inside rationale**: stdout =
   `'{"verdict":"fail","rationale":"missing } in expression"}'`. Expected:
   `verdict="fail"`, `rationale="missing } in expression"`.
4. **Escaped quotes around a brace**: stdout =
   `'{"verdict":"fail","rationale":"saw \"{\" unexpected"}'`
   (JSON-escaped). Expected: `verdict="fail"`,
   `rationale='saw "{" unexpected'`.
5. **Multiple objects, verdict is not first**: stdout =
   `'{"progress":1}\n{"tool":"read_file","args":{}}\n{"verdict":"pass","rationale":"ok"}'`.
   Expected: `verdict="pass"`, `rationale="ok"`.

**Orchestrator mirror** (in `tests/test_orchestrator.py`): the same five
scenarios adapted to `OrchestratorDecision` shape
(`{"persona":"X","rationale":"Y"}`). Each MUST fail on unfixed code and
pass on fixed code for the same reason.

**Expected Counterexamples**:

- For 1.1: `_extract_first_json_object` may return a substring whose
  first character is a backtick (not `{`) when the fence opens the
  stream; `json.loads` raises.
- For 1.2: first balanced object is the tool-use envelope; validates
  against neither `PersonaReviewVerdict` nor `OrchestratorDecision`.
- For 1.3: returned substring is truncated at the rationale's inner `}`;
  `json.loads` raises "Expecting value" or "unterminated string".
- For 1.4: returned substring terminates mid-string at the escaped
  quote; `json.loads` raises.
- For 1.5: returned substring is the first balanced object (not the
  verdict); validates to neither model.

If any counterexample instead passes the unfixed code, the predicted
root cause is incomplete and we re-investigate.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the
fixed pipeline produces a `PersonaReviewVerdict` matching the embedded
payload.

**Pseudocode:**

```
FOR ALL stdout WHERE isBugCondition(stdout) DO
  verdict := extract_validating_object(stdout, PersonaReviewVerdict)
  ASSERT verdict IS NOT None
  ASSERT verdict.verdict   == embedded_payload.verdict
  ASSERT verdict.rationale == embedded_payload.rationale
END FOR
```

**Hypothesis property** (lives in `tests/test_validator_properties.py`
next to Property 13 / Property 14; call it Property 16):

```
@given(
    verdict   = st.sampled_from(["pass", "fail"]),
    rationale = st.text(
        alphabet=st.characters(
            # exclude only the control chars JSON can't carry raw
            blacklist_categories=("Cs",),
            blacklist_characters="\x00",
        ),
        max_size=200,
    ),
    use_fence            = st.booleans(),
    lang_tag             = st.sampled_from(["", "json", "JSON"]),
    prepend_tool_use     = st.booleans(),
    leading_prose        = st.text(max_size=50),
    trailing_prose       = st.text(max_size=50),
)
def test_fix_checking_persona_review_verdict_is_recovered(...):
    payload = {"verdict": verdict, "rationale": rationale}
    body = json.dumps(payload)  # handles escapes for us

    segments = []
    if leading_prose:
        segments.append(leading_prose)
    if prepend_tool_use:
        segments.append('{"tool":"read_file","args":{"path":"x"}}')
    if use_fence:
        segments.append(f"```{lang_tag}\n{body}\n```")
    else:
        segments.append(body)
    if trailing_prose:
        segments.append(trailing_prose)
    stdout = "\n".join(segments)

    recovered = extract_validating_object(stdout, PersonaReviewVerdict)
    assert recovered is not None
    assert recovered.verdict   == verdict
    assert recovered.rationale == rationale
```

The same property is also asserted for `OrchestratorDecision` (Property
17) with `persona`/`rationale` fields.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT
hold — in particular the single-flat-object happy path — the fixed
pipeline returns exactly the same result as the naive pipeline.

**Pseudocode:**

```
FOR ALL stdout WHERE NOT isBugCondition(stdout) DO
  ASSERT naive_pipeline(stdout) == fixed_pipeline(stdout)
END FOR
```

In particular, for the "simple happy path" subset (a single flat
`{...}` JSON object, no fences, no prose with embedded other objects):

```
FOR ALL stdout = json.dumps(valid_payload) DO
  naive := try_parse(_extract_first_json_object(stdout))
  fixed := extract_validating_object(stdout, Model)
  ASSERT naive == fixed
END FOR
```

**Testing Approach**: Property-based testing is the right tool here
because:

- It generates many inputs across the non-bug-condition domain
  automatically.
- It catches edge cases (empty string, whitespace-only, single brace,
  nested objects with no outer envelope) that manual tests would miss.
- It provides strong evidence that the "happy path for both models"
  behavior is byte-for-byte unchanged.

**Hypothesis property** (Property 18, `tests/test_validator_properties.py`):

```
@given(
    verdict   = st.sampled_from(["pass", "fail"]),
    rationale = st.text(max_size=100),
)
def test_preservation_simple_happy_path_matches_naive(verdict, rationale):
    payload = {"verdict": verdict, "rationale": rationale}
    stdout  = json.dumps(payload)

    # Naive pipeline (as of the unfixed code).
    naive_sub   = _extract_first_json_object_naive(stdout)
    naive_model = None
    if naive_sub is not None:
        try:
            naive_model = PersonaReviewVerdict.model_validate(
                json.loads(naive_sub)
            )
        except (json.JSONDecodeError, ValidationError):
            naive_model = None

    # Fixed pipeline.
    fixed_model = extract_validating_object(stdout, PersonaReviewVerdict)

    assert naive_model == fixed_model
```

A copy of the naive helper is pinned inside the test module
(`_extract_first_json_object_naive`) so the preservation property keeps
comparing against the original behavior even after the production helper
is deleted. The same property is instantiated for `OrchestratorDecision`
(Property 19) to cover R3.5.

**Test Plan**: Observe the naive behavior on UNFIXED code for each happy
path, pin those observations into the naive helper copied into the test
module, then assert the fixed pipeline matches for every input drawn by
Hypothesis.

**Test Cases**:

1. **Single flat PersonaReviewVerdict**: verify the fixed extractor
   returns the same `PersonaReviewVerdict` as the naive pipeline for
   any flat JSON dump of a valid payload.
2. **Single flat OrchestratorDecision**: same, for orchestrator.
3. **Unparseable stdout**: both pipelines return `None`.
4. **Empty stdout / whitespace-only stdout**: both pipelines return
   `None`.

### Unit Tests

- `tests/test_validator.py::TestPersonaReviewCheck` gains five new
  tests, one per exploratory scenario (1.1 through 1.5). Each stubs
  the Kiro invoker with the bug-inducing stdout and asserts the
  correct `CheckResult.verdict` and `rationale`.
- `tests/test_orchestrator.py` gains five mirror tests covering the
  same five scenarios for `OrchestratorDecision`.
- New direct tests in `tests/test_json_extract.py` for the helper
  module: markdown fence stripping (with and without language tag,
  no-op when fence absent), balanced-object iteration (string-state
  tracking, escape tracking, multiple objects, unbalanced skip), and
  `extract_validating_object` against both Pydantic models.

### Property-Based Tests

- **Property 16** (fix-checking, `PersonaReviewVerdict`): generates
  arbitrary valid verdict payloads, optionally wraps in fence, optionally
  prepends a tool-use envelope, optionally surrounds with prose; asserts
  the fixed extractor recovers the original payload.
- **Property 17** (fix-checking, `OrchestratorDecision`): same strategy
  with `persona`/`rationale` fields.
- **Property 18** (preservation, single-flat happy path,
  `PersonaReviewVerdict`): asserts the fixed extractor agrees with the
  naive pipeline on `json.dumps(valid_payload)` inputs.
- **Property 19** (preservation, single-flat happy path,
  `OrchestratorDecision`): same.

All four properties live in `tests/test_validator_properties.py`
alongside Property 13 / Property 14 so the existing PBT harness
(Hypothesis profiles, conftest fixtures) is reused unchanged. Each
property's test class is annotated with
`# Validates: Requirements 2.x` / `# Validates: Requirements 3.x` per
the Correctness Properties section so the hover-status feature surfaces
the right description.

### Integration Tests

No new integration tests are required — the fix is scoped to a single
helper and two call sites, both covered by the unit and property-based
tests above. The full existing 463-test suite acts as the regression
gate for integration-level behavior (`test_integration.py`,
`test_cli.py`, end-to-end validator orchestration). Every one of those
tests MUST continue to pass without modification; any test whose
expected output captured `_extract_first_json_object`'s return value
directly would be a signal that the change surface leaked. A quick audit
(`grep _extract_first_json_object tests/`) confirms no test imports the
helper by name today, so no test-level migration is required.
