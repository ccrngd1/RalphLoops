"""Property-based tests for the Planner (Task 18.2).

One property from ``design.md`` lands here:

- Property 26 (auto-planner branching): for any startup state defined
  by (number of tasks ``n``, ``automatic_planner`` flag ``a``,
  ``planner_persona`` configured or not):
    * ``should_auto_planner`` returns ``True`` iff ``n == 0 and a is
      True``.
    * ``should_exit_empty_no_auto`` returns ``True`` iff ``n == 0 and
      a is False``.
    * Both helpers return ``False`` in every other combination.

The property does not directly exercise the missing-planner-persona
branch of R17.7 because that decision is made inside
:meth:`Planner.bootstrap` (tested in ``test_planner.py``). The two
helpers encode R17.3 / R17.4 which is what the Property 26 spec text
actually calls out.

Requirements validated: 17.3, 17.4, 17.7.
"""

# Feature: ralph-loop, Property 26: Auto-planner branching

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ralph_loop.models import Config, Task
from ralph_loop.planner import should_auto_planner, should_exit_empty_no_auto

from tests.strategies import task_list_dag_strategy


# Config field pool: a small pool of planner persona names plus ``None``
# so the generator exercises both the configured and unconfigured
# branches of R17.7 without spending the shrinker's budget on arbitrary
# strings.
_PLANNER_NAME_POOL = (None, "Planner", "Outline", "Strategist")


@st.composite
def _auto_planner_config_strategy(draw) -> Config:
    """Generate a ``Config`` randomising only the planner-related knobs.

    The helpers under test read exactly two fields:
    ``automatic_planner`` and (implicitly, via the surrounding loop
    logic) ``planner_persona``. Every other field keeps its documented
    default so counterexamples stay compact.
    """

    automatic_planner = draw(st.booleans())
    planner_persona = draw(st.sampled_from(_PLANNER_NAME_POOL))
    return Config(
        fallback_persona="Writer",
        automatic_planner=automatic_planner,
        planner_persona=planner_persona,
    )


@given(
    tasks=task_list_dag_strategy(min_size=0, max_size=5),
    config=_auto_planner_config_strategy(),
)
def test_auto_planner_branching_matches_r17_3_r17_4(
    tasks: list[Task], config: Config,
) -> None:
    """Property 26: the two helpers encode the R17.3 / R17.4 decision table.

    Rule (independently replicated here so the test catches drift
    between the spec and the implementation):

    - ``should_auto_planner(tasks, config) == (len(tasks) == 0 and
      config.automatic_planner)``.
    - ``should_exit_empty_no_auto(tasks, config) == (len(tasks) == 0
      and not config.automatic_planner)``.
    - The two helpers are mutually exclusive.
    - When ``len(tasks) > 0``, both helpers return ``False`` (R17.3 /
      R17.4 only apply to the empty-task-list startup state).
    """

    empty = len(tasks) == 0
    auto = bool(config.automatic_planner)

    expected_auto = empty and auto
    expected_exit = empty and not auto

    assert should_auto_planner(tasks, config) is expected_auto
    assert should_exit_empty_no_auto(tasks, config) is expected_exit

    # Mutual exclusion and non-empty invariants.
    assert not (
        should_auto_planner(tasks, config)
        and should_exit_empty_no_auto(tasks, config)
    )
    if not empty:
        assert should_auto_planner(tasks, config) is False
        assert should_exit_empty_no_auto(tasks, config) is False
