"""Reusable Hypothesis strategies for Ralph Loop property-based tests.

These composite strategies build the generators shared across the
property tests in ``tests/test_*``. Each strategy is small and
composable, tuned to produce inputs that exercise the full universe of
eligibility, status, and dependency combinations declared in
``design.md`` (primarily properties 1-4).

The strategies defined here are:

- ``task_id_strategy`` / ``priority_strategy`` / ``task_status_strategy``
  / ``title_strategy`` / ``retry_count_strategy`` - primitive scalar
  strategies over the Task field alphabets.
- ``task_strategy`` - a ``@composite`` strategy for a single ``Task``
  with optional overrides for ``id``, ``depends_on``, and ``status``.
- ``task_list_dag_strategy`` - a ``@composite`` strategy for a
  ``list[Task]`` whose ``depends_on`` edges form a DAG by construction
  (each task's dependencies reference only strictly earlier ids, and all
  ids are unique).
- ``task_list_with_cycle_strategy`` - a ``@composite`` strategy for a
  ``list[Task]`` guaranteed to contain at least one ``depends_on``
  cycle; used by Property 3 to exercise the cycle-detection branch of
  ``analyze_dependencies``.
- ``task_list_with_missing_dep_strategy`` - a ``@composite`` strategy
  for a ``list[Task]`` in which at least one task's ``depends_on``
  references an id that does not exist in the list; used by Property 3
  to exercise the missing-dependency branch of
  ``analyze_dependencies``.
- ``config_strategy`` - a ``@composite`` strategy for a ``Config`` with
  sensible bounds on the retry / iteration / escalation knobs.
"""

from __future__ import annotations

import string
from typing import Any, Optional

from hypothesis import strategies as st

from ralph_loop.models import CheckResult, Config, Task, TaskStatus


# Every TaskStatus literal value; ``Literal`` values aren't iterable at
# runtime so we mirror them here explicitly. Property 1 relies on the
# full five-value range to exercise both "eligible" statuses
# (``pending``/``failing``) and the three "ineligible" statuses.
_TASK_STATUSES: tuple[TaskStatus, ...] = (
    "pending",
    "in_progress",
    "passing",
    "failing",
    "stuck",
)

# Identifier alphabet kept short and URL-safe so ids print cleanly in
# shrunk counterexamples without the shrinker spending time on exotic
# unicode corner cases.
_ID_ALPHABET = string.ascii_letters + string.digits + "_-"


task_id_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=8)
"""Short URL-safe identifier string, ``min_length >= 1`` (matches
``Task.id`` / ``Task.spec_path`` Pydantic constraints)."""

priority_strategy = st.integers(min_value=-10, max_value=100)
"""Priority bounds chosen to include negative values (valid per the
Task schema) while keeping shrunk counterexamples readable."""

task_status_strategy = st.sampled_from(_TASK_STATUSES)
"""Uniform draw over the five TaskStatus literal values."""

title_strategy = st.text(min_size=1, max_size=20)
"""Non-empty title; the Task model enforces ``min_length=1``."""

retry_count_strategy = st.integers(min_value=0, max_value=10)
"""Non-negative retry count with a small cap so the generator spends
its budget on the interesting boundary around ``max_retries_per_task``
rather than on arbitrarily large integers."""


@st.composite
def task_strategy(
    draw,
    *,
    id_: Optional[str] = None,
    depends_on: Optional[list[str]] = None,
    status: Optional[TaskStatus] = None,
) -> Task:
    """Generate a single ``Task`` with optional overrides.

    The override hooks let list-level composite strategies pin ids and
    DAG-correct ``depends_on`` edges while the remaining fields are
    still drawn randomly.

    ``spec_path`` is derived from the id so counterexamples stay
    readable and so the ``min_length=1`` constraint on ``spec_path`` is
    satisfied even when the id is a single character.
    """

    tid = id_ if id_ is not None else draw(task_id_strategy)
    title = draw(title_strategy)
    priority = draw(priority_strategy)
    resolved_status = status if status is not None else draw(task_status_strategy)
    retry_count = draw(retry_count_strategy)
    spec_path = f"specs/{tid}.md"
    return Task(
        id=tid,
        title=title,
        priority=priority,
        status=resolved_status,
        spec_path=spec_path,
        retry_count=retry_count,
        depends_on=depends_on,
    )


@st.composite
def task_list_dag_strategy(
    draw,
    *,
    min_size: int = 0,
    max_size: int = 8,
) -> list[Task]:
    """Generate a ``list[Task]`` whose ``depends_on`` edges form a DAG.

    Construction:

    1. Draw ``n`` in ``[min_size, max_size]``.
    2. Draw ``n`` unique task ids.
    3. For each task at index ``i``, draw ``depends_on`` as either
       ``None`` or a subset (up to 3 entries) of the ids at indices
       ``< i``.

    Building dependencies only backward guarantees the resulting graph
    is acyclic, and ``unique=True`` on the id list guarantees
    ``{t.id for t in tasks}`` has no duplicates.
    """

    n = draw(st.integers(min_value=min_size, max_value=max_size))
    if n == 0:
        return []

    ids = draw(
        st.lists(task_id_strategy, min_size=n, max_size=n, unique=True)
    )

    tasks: list[Task] = []
    for i, tid in enumerate(ids):
        earlier = ids[:i]
        if earlier:
            deps = draw(
                st.one_of(
                    st.none(),
                    st.lists(
                        st.sampled_from(earlier),
                        min_size=0,
                        max_size=min(3, len(earlier)),
                        unique=True,
                    ),
                )
            )
        else:
            deps = None
        tasks.append(draw(task_strategy(id_=tid, depends_on=deps)))
    return tasks


@st.composite
def config_strategy(draw) -> Config:
    """Generate a ``Config`` focused on the loop / retry / escalation knobs.

    Only the fields that interact with Task Selector eligibility and
    escalation are randomized. Every other field keeps its model
    default. ``fallback_persona`` is drawn from a short set of
    human-readable names so counterexamples stay legible.

    Bounds chosen:

    - ``max_retries_per_task`` in ``[1, 10]`` (model enforces ``ge=1``).
    - ``max_iterations`` in ``[1, 100]`` (model enforces ``ge=1``).
    - ``escalation_threshold`` in ``[0, 10]`` (model enforces ``ge=0``).
    """

    fallback_persona = draw(
        st.sampled_from(["Writer", "Editor", "Reviewer", "Coder", "Tester"])
    )
    max_retries_per_task = draw(st.integers(min_value=1, max_value=10))
    max_iterations = draw(st.integers(min_value=1, max_value=100))
    escalation_threshold = draw(st.integers(min_value=0, max_value=10))
    return Config(
        fallback_persona=fallback_persona,
        max_retries_per_task=max_retries_per_task,
        max_iterations=max_iterations,
        escalation_threshold=escalation_threshold,
    )


@st.composite
def budget_config_strategy(draw) -> Config:
    """Generate a ``Config`` focused on the budget / wall-clock knobs.

    Only the fields Property 22 reads are randomized; everything else
    keeps its documented default. Bounds:

    - ``max_iterations`` in ``[1, 20]`` (model enforces ``ge=1``). The
      small upper bound keeps shrunk counterexamples legible while
      still exercising both the under-cap and at-or-past-cap branches.
    - ``wall_clock_timeout_ms`` in ``[0, 60_000]``. The lower bound of
      ``0`` is the documented "disabled" value (``check_wall_clock``
      returns ``False`` regardless of elapsed time); the upper bound
      is one minute, which is plenty of headroom vs. the 120_000 ms
      elapsed-time draw in the test.
    """

    max_iterations = draw(st.integers(min_value=1, max_value=20))
    wall_clock_timeout_ms = draw(st.integers(min_value=0, max_value=60_000))
    return Config(
        fallback_persona="fallback",
        max_iterations=max_iterations,
        wall_clock_timeout_ms=wall_clock_timeout_ms,
    )


@st.composite
def task_list_with_cycle_strategy(
    draw,
    *,
    min_cycle_size: int = 2,
    max_cycle_size: int = 5,
    extra_min: int = 0,
    extra_max: int = 4,
) -> list[Task]:
    """Generate a ``list[Task]`` guaranteed to contain at least one cycle.

    Construction:

    1. Draw a DAG of ``extra`` tasks in ``[extra_min, extra_max]`` (their
       ``depends_on`` edges only point backward within the DAG, so they
       contribute no cycle on their own).
    2. Draw ``k`` unique cycle ids in ``[min_cycle_size, max_cycle_size]``,
       disjoint from the DAG ids.
    3. Build the cycle by stitching ``cycle_ids[i] -> cycle_ids[i+1]`` for
       every ``i`` with the last pointing back at the first, guaranteeing a
       single k-node cycle.
    4. Interleave the cycle tasks into the DAG at a random position so the
       analyzer cannot rely on positional ordering to find the cycle.

    The returned list has unique ids and at least one ``depends_on``
    cycle.
    """

    k = draw(st.integers(min_value=min_cycle_size, max_value=max_cycle_size))
    extra = draw(st.integers(min_value=extra_min, max_value=extra_max))

    # Draw all ids at once with uniqueness so the cycle ids never collide
    # with the surrounding DAG ids.
    all_ids = draw(
        st.lists(
            task_id_strategy,
            min_size=k + extra,
            max_size=k + extra,
            unique=True,
        )
    )
    cycle_ids = all_ids[:k]
    dag_ids = all_ids[k:]

    # Build the cycle tasks: id[i] depends on id[(i+1) % k]. For k == 1
    # this naturally produces a self-loop (``A -> A``).
    cycle_tasks: list[Task] = []
    for i, tid in enumerate(cycle_ids):
        next_id = cycle_ids[(i + 1) % k]
        cycle_tasks.append(draw(task_strategy(id_=tid, depends_on=[next_id])))

    # Build the DAG tasks: each task's deps only reference strictly
    # earlier DAG ids, guaranteeing these contribute no cycle.
    dag_tasks: list[Task] = []
    for i, tid in enumerate(dag_ids):
        earlier = dag_ids[:i]
        if earlier:
            deps = draw(
                st.one_of(
                    st.none(),
                    st.lists(
                        st.sampled_from(earlier),
                        min_size=0,
                        max_size=min(3, len(earlier)),
                        unique=True,
                    ),
                )
            )
        else:
            deps = None
        dag_tasks.append(draw(task_strategy(id_=tid, depends_on=deps)))

    # Interleave the cycle tasks into the DAG at a random insertion point
    # so the analyzer sees the cycle in arbitrary list positions.
    insert_at = draw(st.integers(min_value=0, max_value=len(dag_tasks)))
    return dag_tasks[:insert_at] + cycle_tasks + dag_tasks[insert_at:]


@st.composite
def task_list_with_missing_dep_strategy(
    draw,
    *,
    min_size: int = 0,
    max_size: int = 6,
) -> list[Task]:
    """Generate a ``list[Task]`` where at least one task references a missing id.

    Construction:

    1. Draw a DAG-structured base list of ``[min_size, max_size]`` tasks.
    2. Draw a fresh id that is not in the base list (via ``filter``).
    3. Draw a referring task whose ``depends_on`` contains that fresh id
       (optionally alongside ids from the base list), and append it.

    The result is guaranteed to have at least one task with a missing
    ``depends_on`` target, which is exactly the condition for R2.9.
    """

    base = draw(task_list_dag_strategy(min_size=min_size, max_size=max_size))
    existing_ids = {t.id for t in base}

    # Draw a fresh id not in the base list. The filter keeps the
    # strategy robust even against small alphabets.
    missing_id = draw(
        task_id_strategy.filter(lambda tid: tid not in existing_ids)
    )

    # Build the new referring task. Its own id must also be unique.
    referrer_id = draw(
        task_id_strategy.filter(
            lambda tid: tid not in existing_ids and tid != missing_id
        )
    )

    # Optionally draw additional (valid) deps from the base list so the
    # referring task exercises the "some valid, some missing" case too.
    if existing_ids:
        extra_deps = draw(
            st.lists(
                st.sampled_from(sorted(existing_ids)),
                min_size=0,
                max_size=min(3, len(existing_ids)),
                unique=True,
            )
        )
    else:
        extra_deps = []
    deps = [missing_id] + extra_deps

    referrer = draw(task_strategy(id_=referrer_id, depends_on=deps))
    return base + [referrer]


# -- CheckResult strategies (Property 2, R2.5, R2.6) --------------------------
#
# The status-update rule in ``ralph_loop.status_update.status_after_validation``
# consumes a ``list[CheckResult]``. Property 2 needs a generator that covers
# the full ``(pass, fail)`` verdict space while keeping the other CheckResult
# fields well-formed. ``rationale``, ``resolved_pass_condition``, and
# ``reviewing_persona`` are populated only for ``persona_review`` checks at
# runtime (R7.9, R7.10); for Property 2 they do not influence the aggregation
# rule, so we leave them as ``None`` to keep counterexamples small.


_CHECK_TYPES: tuple[str, ...] = ("shell", "persona_review", "file_exists")
_VERDICTS: tuple[str, ...] = ("pass", "fail")

check_type_strategy = st.sampled_from(_CHECK_TYPES)
"""Uniform draw over the three CheckType literal values."""

verdict_strategy = st.sampled_from(_VERDICTS)
"""Uniform draw over the two Verdict literal values."""

check_name_strategy = st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=8)
"""Short URL-safe name for a CheckResult; keeps shrunk examples readable."""

check_output_strategy = st.text(max_size=20)
"""Short output string. Content is irrelevant to Property 2."""

duration_ms_strategy = st.integers(min_value=0, max_value=10_000)
"""Non-negative duration, bounded so the shrinker doesn't chase huge ints."""


@st.composite
def check_result_strategy(draw) -> CheckResult:
    """Generate a single well-formed ``CheckResult``.

    Fields:

    - ``type`` drawn from ``{"shell", "persona_review", "file_exists"}``.
    - ``name`` short URL-safe identifier (``min_length >= 1``).
    - ``verdict`` drawn from ``{"pass", "fail"}``.
    - ``output`` short text (``max_size=20``).
    - ``duration_ms`` non-negative integer, capped at 10_000.
    - ``timed_out`` defaults to False; Property 2 cares only about the
      aggregate verdict, so we do not randomize timeout semantics here.
    - ``rationale`` / ``resolved_pass_condition`` / ``reviewing_persona``
      left as ``None`` (only populated for persona_review at runtime;
      R7.9/R7.10).
    """

    return CheckResult(
        type=draw(check_type_strategy),  # type: ignore[arg-type]
        name=draw(check_name_strategy),
        verdict=draw(verdict_strategy),  # type: ignore[arg-type]
        output=draw(check_output_strategy),
        duration_ms=draw(duration_ms_strategy),
        timed_out=False,
    )


@st.composite
def check_result_list_strategy(
    draw,
    *,
    min_size: int = 1,
    max_size: int = 6,
) -> list[CheckResult]:
    """Generate a ``list[CheckResult]`` of length in ``[min_size, max_size]``.

    Property 2 is stated over a non-empty check list, so ``min_size``
    defaults to ``1``. The upper bound is small to keep the shrinker
    focused on interesting boundaries (all-pass vs single-fail) rather
    than chasing long lists.
    """

    return draw(
        st.lists(
            check_result_strategy(),
            min_size=min_size,
            max_size=max_size,
        )
    )


@st.composite
def task_with_retry_count_strategy(draw) -> Task:
    """Generate a ``Task`` with a bounded ``retry_count`` in ``[0, 10]``.

    Property 2 reads ``task.retry_count`` and expects the next value
    to be either ``r`` (all-pass) or ``r + 1`` (any-fail). The
    underlying ``task_strategy`` already draws ``retry_count`` from
    ``retry_count_strategy`` (``[0, 10]``); this wrapper is the named
    alias documented by the Property 2 design so call sites are
    self-explanatory.
    """

    return draw(task_strategy())


# -- Token Accountant strategies (Properties 27, 28; R12.1-R12.6) --------------
#
# Properties 27 and 28 exercise ``TokenAccountant`` with arbitrary sequences
# of ``LlmCallRecord`` plus a ``Model_Pricing`` map. The strategies below
# generate well-formed records and configs while keeping the search space
# small enough that counterexamples are readable and numerically stable.
#
# Key choices:
# - ``model_id_strategy`` draws from a small fixed pool so model ids on
#   records collide with pricing-map keys often enough that the
#   cost-derivation branch is exercised routinely rather than almost never
#   (which would happen with an independent alphanumeric alphabet).
# - ``token_count_strategy`` keeps counts to a modest upper bound so
#   accumulated floats stay well within double precision. Combined with the
#   bounded prices in ``model_price_strategy`` this keeps the worst-case
#   total cost small enough that ``pytest.approx`` comparisons remain
#   stable.


from ralph_loop.models import CallKind, LlmCallRecord, ModelPrice


# Every ``CallKind`` literal value. We mirror the literal here because
# ``typing.Literal`` values aren't iterable at runtime. Property 27 cares
# that the accountant preserves ``kind`` across every call site, so the
# generator covers the full five-value alphabet.
_CALL_KINDS: tuple[CallKind, ...] = (
    "persona_execution",
    "orchestrator_selection",
    "persona_review",
    "planner",
    "escalation",
)


# A small, fixed pool of model identifiers. Drawing record models and
# pricing-map keys from the same small pool makes collisions common, so
# the cost-derivation branch of ``TokenAccountant.record`` is exercised
# on most generated scenarios rather than trivially bypassed.
_MODEL_IDS: tuple[str, ...] = ("m1", "m2", "m3", "m4", "m5")


call_kind_strategy = st.sampled_from(_CALL_KINDS)
"""Uniform draw over the five ``CallKind`` literal values."""

model_id_strategy = st.sampled_from(_MODEL_IDS)
"""Short model identifier drawn from a small fixed pool so pricing-map
overlap with record ``model`` fields is common."""

token_count_strategy = st.integers(min_value=0, max_value=100_000)
"""Non-negative token count. The ``100_000`` cap keeps accumulated
floats numerically stable even across the 30-call test scenarios used
by Properties 27 and 28."""


@st.composite
def llm_call_record_strategy(draw) -> LlmCallRecord:
    """Generate a single ``LlmCallRecord`` with optional token / model fields.

    Property 27 needs the full optional-field universe: records with
    neither token set (warning branch), only one set (partial data),
    both set (aggregated into totals), plus the optional ``model``
    identifier (required for cost derivation in Property 28). Drawing
    each field via ``st.one_of(st.none(), ...)`` covers all eight
    combinations with comparable probability.

    ``estimated_cost`` is intentionally left unset. The accountant's
    cost derivation is what the property tests aim to exercise;
    pre-setting the cost on incoming records would short-circuit that
    code path. The unit tests in ``test_token_accountant.py`` cover the
    pre-set-cost branch explicitly.
    """

    kind = draw(call_kind_strategy)
    model = draw(st.one_of(st.none(), model_id_strategy))
    input_tokens = draw(st.one_of(st.none(), token_count_strategy))
    output_tokens = draw(st.one_of(st.none(), token_count_strategy))
    return LlmCallRecord(
        kind=kind,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


@st.composite
def llm_call_record_strategy_with_tokens(draw) -> LlmCallRecord:
    """Generate an ``LlmCallRecord`` biased toward having all fields set.

    Property 28 tests the cost-computation rule, so the cost-derivation
    branch should be exercised on most generated calls. This strategy
    always sets ``input_tokens``, ``output_tokens``, and ``model``, which
    is exactly the precondition for cost derivation in
    ``TokenAccountant.record``. The accountant will then either derive a
    cost (when pricing is configured for the model) or leave
    ``estimated_cost`` absent (when it isn't), exercising R12.3 / R12.4
    in the same draw.
    """

    kind = draw(call_kind_strategy)
    model = draw(model_id_strategy)
    input_tokens = draw(token_count_strategy)
    output_tokens = draw(token_count_strategy)
    return LlmCallRecord(
        kind=kind,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


@st.composite
def model_price_strategy(draw) -> ModelPrice:
    """Generate a ``ModelPrice`` with modest per-token prices.

    Prices are bounded to ``[0.0, 0.01]`` so the worst-case total cost
    across a 30-call run of 100_000-token records is ~30_000, well
    within the range where float accumulation order has negligible
    effect on ``pytest.approx`` comparisons. The ``0.0`` lower bound
    exercises the free-tier / zero-cost path documented in R12.3.
    """

    input_price = draw(
        st.floats(
            min_value=0.0,
            max_value=0.01,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    output_price = draw(
        st.floats(
            min_value=0.0,
            max_value=0.01,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    return ModelPrice(
        input_price_per_token=input_price,
        output_price_per_token=output_price,
    )


@st.composite
def pricing_map_strategy(draw) -> dict[str, ModelPrice]:
    """Generate a ``dict[str, ModelPrice]`` suitable for ``Config.model_pricing``.

    Keys are drawn from the same ``_MODEL_IDS`` pool used by
    ``model_id_strategy`` so overlap with record ``model`` identifiers
    is common. Sizes range from zero (empty map: R12.4 path) up to the
    full pool (every model priced).
    """

    return draw(
        st.dictionaries(
            keys=model_id_strategy,
            values=model_price_strategy(),
            min_size=0,
            max_size=len(_MODEL_IDS),
        )
    )


@st.composite
def priced_config_strategy(draw) -> Config:
    """Generate a ``Config`` whose ``model_pricing`` map is randomized.

    Only ``model_pricing`` is randomized; every other field keeps its
    documented default. Property 28 reads only the pricing map from the
    config, so varying unrelated fields would waste the generator's
    budget without widening coverage.
    """

    pricing = draw(pricing_map_strategy())
    return Config(fallback_persona="fallback", model_pricing=pricing)


# -- Snapshot diff strategy (Property 16; R8.2, R8.3) --------------------------
#
# Property 16 is stated over arbitrary (pre, post) snapshot pairs. The
# strategy below builds such pairs by first generating a valid pre list
# (DAG-structured so every task is well-formed) and then deriving a
# ``post`` list from ``pre`` by applying a random mix of edits:
#
# 1. keep: pass the pre entry through unchanged
# 2. modify: change a mutable field on the pre entry
# 3. delete: drop the pre entry from post
# 4. add: append a brand-new entry with a fresh id
#
# Deriving ``post`` from ``pre`` (rather than drawing the two lists
# independently) gives every diff bucket a roughly equal share of the
# generator's budget, so the shrinker converges on small and
# diff-bucket-mixed counterexamples quickly.


@st.composite
def pre_and_post_snapshot_strategy(
    draw,
    *,
    min_pre_size: int = 0,
    max_pre_size: int = 6,
    max_new_size: int = 4,
) -> tuple[list[Task], list[dict[str, Any]]]:
    """Generate a ``(pre, post)`` pair for Property 16.

    ``pre`` is a DAG-structured ``list[Task]`` drawn via
    :func:`task_list_dag_strategy` so every entry is well-formed and
    carries a unique id (the diff function assumes unique ids on the
    pre side -- that invariant is guaranteed by the atomic writer in
    production).

    ``post`` is derived from ``pre`` by drawing a per-entry action:

    - ``"keep"``: pass the task's JSON dump through unchanged.
    - ``"modify"``: dump the task and mutate one or more mutable
      fields (``priority``, ``status``, ``retry_count``, ``title``) so
      the resulting dict differs from the pre dump.
    - ``"delete"``: drop the entry entirely.

    After processing every pre entry the strategy appends up to
    ``max_new_size`` brand-new tasks whose ids are disjoint from the
    pre ids so they land in the ``created`` bucket.

    The returned post list always contains only dicts with valid
    string ``id`` fields so Property 16's invariants hold without
    needing special-casing for malformed entries (those are covered
    by the unit tests in ``test_snapshot_diff.py``).
    """

    pre = draw(task_list_dag_strategy(min_size=min_pre_size, max_size=max_pre_size))
    pre_ids = {t.id for t in pre}

    # Action distribution: keep slightly more common than modify / delete
    # so the ``modified`` and ``deleted`` buckets each see action, while
    # the unchanged common-ids case also shows up regularly.
    action_strategy = st.sampled_from(("keep", "modify", "delete"))

    post: list[dict[str, Any]] = []
    for task in pre:
        action = draw(action_strategy)
        if action == "keep":
            post.append(task.model_dump(mode="json"))
        elif action == "modify":
            # Mutate a non-id field. Draw each mutator independently so
            # the shrinker can isolate which field matters for a failing
            # case. The priority bump guarantees at least one field
            # actually changes, so the modified dict always differs
            # from the pre dump.
            dump = task.model_dump(mode="json")
            dump["priority"] = task.priority + draw(
                st.integers(min_value=1, max_value=5)
            )
            if draw(st.booleans()):
                dump["status"] = draw(task_status_strategy)
            if draw(st.booleans()):
                dump["retry_count"] = task.retry_count + draw(
                    st.integers(min_value=1, max_value=3)
                )
            if draw(st.booleans()):
                dump["title"] = draw(title_strategy)
            post.append(dump)
        else:  # delete
            continue

    # Append a handful of brand-new entries with ids disjoint from any
    # pre id. ``task_id_strategy`` can produce colliding ids, so filter
    # against both ``pre_ids`` and the ids already emitted into post.
    emitted_ids: set[str] = {
        e["id"] for e in post if isinstance(e.get("id"), str) and e["id"]
    }
    num_new = draw(st.integers(min_value=0, max_value=max_new_size))
    for _ in range(num_new):
        fresh_id = draw(
            task_id_strategy.filter(
                lambda tid, seen=emitted_ids, pre=pre_ids: (
                    tid not in pre and tid not in seen
                )
            )
        )
        new_task = draw(task_strategy(id_=fresh_id))
        post.append(new_task.model_dump(mode="json"))
        emitted_ids.add(fresh_id)

    return pre, post


# -- Persona Registry strategies (Property 5; R3.2, R3.4, R3.5, R3.6, R3.8) ---
#
# Property 5 exercises ``PersonaRegistry.load`` across two branches:
#
# - Happy path: every file has the required fields and no two files share a
#   ``name`` -> the loader returns a dict-like registry keyed by ``name``.
# - Fail-fast path: at least one file is missing a required field OR two
#   files declare the same ``name`` -> the loader raises
#   ``PersonaRegistryError`` with the offending file / field / name in the
#   message.
#
# The strategies below build the *data* for those scenarios. The property
# tests are responsible for materializing the dicts into YAML files on a
# ``tmp_path`` before calling ``PersonaRegistry.load``.


# A small pool of human-readable persona names. Drawing from a fixed pool
# (rather than arbitrary text) makes the duplicate-name branch cheap to
# generate: ``st.lists(..., unique=False)`` plus a post-filter that checks
# for duplicates converges quickly when the pool is small.
_PERSONA_NAME_POOL: tuple[str, ...] = (
    "Writer",
    "Editor",
    "Reviewer",
    "Coder",
    "Tester",
    "Planner",
    "Researcher",
    "Analyst",
)

# Names not in the pool would still work but keep the generator ASCII-only
# so YAML serialization stays trivial and counterexamples stay readable.
persona_name_strategy = st.sampled_from(_PERSONA_NAME_POOL)


# Short prose for description / prompt_template / instructions fields. The
# alphabet excludes YAML special characters (``:``, ``#``, ``{``, ``}``,
# ``[``, ``]``, ``&``, ``*``, ``!``, ``|``, ``>``, ``'``, ``"``, ``\n``) so
# the dict-to-YAML step in the test can use a single-quoted scalar without
# needing to escape. The goal here is to exercise the loader, not YAML's
# escaping rules -- those are covered by the unit tests.
_PERSONA_TEXT_ALPHABET = string.ascii_letters + string.digits + " -_.,"

persona_text_strategy = st.text(
    alphabet=_PERSONA_TEXT_ALPHABET,
    min_size=1,
    max_size=40,
)


@st.composite
def persona_dict_strategy(
    draw,
    *,
    name: Optional[str] = None,
    include_instructions: Optional[bool] = None,
    include_default_pass: Optional[bool] = None,
) -> dict[str, Any]:
    """Generate a valid persona-definition dict.

    The dict carries the three required fields (``name``, ``description``,
    ``prompt_template``) and a random mix of the two optional string fields
    (``instructions``, ``default_persona_review_pass_condition``). The
    ``tool_restrictions`` field is intentionally omitted; the loader unit
    tests already cover it, and its shape (nested mapping) would complicate
    YAML materialization without widening coverage for Property 5.

    The ``name`` override hook lets list-level composite strategies pin a
    specific name (used by the duplicate-name scenario to guarantee at
    least two files collide).
    """
    resolved_name = name if name is not None else draw(persona_name_strategy)
    description = draw(persona_text_strategy)
    prompt_template = draw(persona_text_strategy)

    data: dict[str, Any] = {
        "name": resolved_name,
        "description": description,
        "prompt_template": prompt_template,
    }

    # Decide whether to include each optional field.
    if include_instructions is None:
        include_instructions = draw(st.booleans())
    if include_default_pass is None:
        include_default_pass = draw(st.booleans())

    if include_instructions:
        data["instructions"] = draw(persona_text_strategy)
    if include_default_pass:
        data["default_persona_review_pass_condition"] = draw(persona_text_strategy)

    return data


@st.composite
def persona_dict_list_unique_strategy(
    draw,
    *,
    min_size: int = 1,
    max_size: int = 5,
) -> list[dict[str, Any]]:
    """Generate a list of persona dicts with unique ``name`` fields.

    Used by the happy-path branch of Property 5. Uniqueness is enforced by
    first drawing a set of distinct names and then building a persona dict
    per name.
    """
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    names = draw(
        st.lists(
            persona_name_strategy,
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    return [draw(persona_dict_strategy(name=name)) for name in names]


@st.composite
def persona_dict_list_with_duplicate_strategy(
    draw,
    *,
    min_extras: int = 0,
    max_extras: int = 3,
) -> list[dict[str, Any]]:
    """Generate a list of persona dicts guaranteed to contain a duplicate name.

    Used by the fail-fast branch of Property 5 for duplicate detection
    (R3.5). The list always contains at least two dicts that share the
    same ``name``; other dicts with distinct names may be interleaved
    around them.
    """
    # Pick the name that will be duplicated.
    duplicated_name = draw(persona_name_strategy)

    # Always include two dicts that share the duplicated name.
    duplicated_dicts = [
        draw(persona_dict_strategy(name=duplicated_name)),
        draw(persona_dict_strategy(name=duplicated_name)),
    ]

    # Optionally draw a few extra dicts with distinct names (not equal to
    # the duplicated name) so the duplicate doesn't always land in the
    # first two entries. Some extras may match the duplicated name by
    # chance -- that's fine, R3.5 still holds.
    n_extras = draw(st.integers(min_value=min_extras, max_value=max_extras))
    extras: list[dict[str, Any]] = []
    for _ in range(n_extras):
        extras.append(draw(persona_dict_strategy()))

    # Interleave: insert the two duplicated dicts at arbitrary positions
    # in the extras list so the duplicate pair can appear anywhere.
    result = list(extras)
    pos1 = draw(st.integers(min_value=0, max_value=len(result)))
    result.insert(pos1, duplicated_dicts[0])
    pos2 = draw(st.integers(min_value=0, max_value=len(result)))
    result.insert(pos2, duplicated_dicts[1])
    return result


# -- Prompt-template strategies (Property 6; R3.7) -----------------------------
#
# Property 6 exercises ``ralph_loop.prompt_template.render_prompt`` over the
# full five-placeholder alphabet. The strategies below build (a) a template
# string composed of interleaved text fragments and placeholder markers and
# (b) plain-text replacement values that cannot collide with the supported
# placeholder substrings (so sequential ``str.replace`` can't rewrite a
# value back into a marker).


# The supported placeholder alphabet (R3.7). Kept as a module-level constant
# so both the strategies and the property tests can import the same set
# without risk of drift.
SUPPORTED_PLACEHOLDERS: tuple[str, ...] = (
    "{{project_brief}}",
    "{{task_spec}}",
    "{{task_id}}",
    "{{task_title}}",
    "{{persona_name}}",
)

# Alphabet for placeholder *values* and for template text fragments that
# are not placeholders. ``{`` and ``}`` are excluded so generated text
# can't accidentally form a ``{{...}}`` marker that the renderer would
# then rewrite. ``\n`` and other controls are also excluded to keep
# counterexamples single-line and readable.
_PLAIN_TEXT_ALPHABET = string.ascii_letters + string.digits + " -_.,"

placeholder_value_strategy = st.text(
    alphabet=_PLAIN_TEXT_ALPHABET,
    min_size=0,
    max_size=20,
)
"""Generate a replacement value that cannot collide with any supported
placeholder marker.

The alphabet excludes ``{`` and ``}``, so the value can never contain
``{{...}}``. This guarantees the sequential ``str.replace`` calls in
``render_prompt`` can't rewrite a value back into a marker, which keeps
Property 6's two assertions (value-present and marker-absent) independent
of replacement order."""


_TEMPLATE_FRAGMENT_STRATEGY = st.text(
    alphabet=_PLAIN_TEXT_ALPHABET,
    min_size=0,
    max_size=10,
)


@st.composite
def placeholder_template_strategy(
    draw,
    *,
    min_parts: int = 0,
    max_parts: int = 8,
) -> tuple[str, set[str]]:
    """Generate a ``(template, placeholders_used)`` pair for Property 6.

    The template is a string assembled by interleaving plain-text
    fragments with randomly-drawn placeholder markers from
    ``SUPPORTED_PLACEHOLDERS``. Each ``part`` is either a fragment or a
    marker, drawn independently so the template can contain zero,
    repeated, or all-five placeholder occurrences.

    Returns the generated template string plus the ``set`` of distinct
    placeholder markers actually used in the template. The property
    test uses this set to decide which markers MUST disappear from the
    rendered output and which values MUST appear.
    """
    n_parts = draw(st.integers(min_value=min_parts, max_value=max_parts))

    parts: list[str] = []
    placeholders_used: set[str] = set()
    for _ in range(n_parts):
        # 50/50 split between text fragment and placeholder marker. An
        # equal split keeps the generator from biasing toward
        # placeholder-heavy or placeholder-free templates, so both
        # directions of Property 6 are exercised regularly.
        if draw(st.booleans()):
            parts.append(draw(_TEMPLATE_FRAGMENT_STRATEGY))
        else:
            marker = draw(st.sampled_from(SUPPORTED_PLACEHOLDERS))
            parts.append(marker)
            placeholders_used.add(marker)

    return "".join(parts), placeholders_used
