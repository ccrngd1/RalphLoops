"""Property-based tests for ``ralph_loop.prompt_template.render_prompt`` (design Property 6).

This test exercises Property 6 from ``design.md``:

    For any persona prompt template ``T`` containing any subset of the
    supported placeholders, and any values for those placeholders, the
    rendered result contains each substituted value and contains none
    of the substituted placeholder markers.

The template strategy (:func:`placeholder_template_strategy`) builds
templates by interleaving text fragments with randomly-drawn
placeholder markers from the supported alphabet (R3.7). The value
strategy (:func:`placeholder_value_strategy`) constrains replacement
values to a plain ASCII alphabet that excludes ``{`` and ``}``, so no
value can accidentally form a ``{{...}}`` marker that sequential
``str.replace`` might rewrite. This keeps Property 6's "value present"
and "marker absent" assertions independent of replacement order.

Requirements validated: 3.7.
"""

# Feature: ralph-loop, Property 6: Prompt-template placeholder substitution

from __future__ import annotations

from hypothesis import given

from ralph_loop.prompt_template import render_prompt

from tests.strategies import (
    SUPPORTED_PLACEHOLDERS,
    placeholder_template_strategy,
    placeholder_value_strategy,
)


# Map from the literal placeholder marker (as it appears in the template)
# to the keyword argument name that ``render_prompt`` accepts for its
# replacement value. Declaring the mapping once at module level keeps the
# property test readable and makes the link between R3.7's placeholder
# alphabet and the function signature explicit.
_MARKER_TO_KWARG: dict[str, str] = {
    "{{project_brief}}": "project_brief",
    "{{task_spec}}": "task_spec",
    "{{task_id}}": "task_id",
    "{{task_title}}": "task_title",
    "{{persona_name}}": "persona_name",
}


@given(
    template_and_used=placeholder_template_strategy(),
    project_brief=placeholder_value_strategy,
    task_spec=placeholder_value_strategy,
    task_id=placeholder_value_strategy,
    task_title=placeholder_value_strategy,
    persona_name=placeholder_value_strategy,
)
def test_placeholder_substitution_covers_every_used_marker(
    template_and_used: tuple[str, set[str]],
    project_brief: str,
    task_spec: str,
    task_id: str,
    task_title: str,
    persona_name: str,
) -> None:
    """Validates: Requirements 3.7.

    For any template ``T`` drawn from the placeholder+text alphabet and
    any set of replacement values drawn from the placeholder-safe
    alphabet:

    - every supported placeholder marker that appears in ``T`` is
      substituted with its corresponding value; and
    - none of the supported placeholder markers that appeared in ``T``
      remain in the rendered output.

    The values are drawn from a ``{``/``}``-free alphabet so that a
    value cannot accidentally form a ``{{...}}`` marker. This makes the
    two assertions independent of sequential ``str.replace`` ordering:
    no replacement can rewrite another marker back into the text.

    Unsupported placeholders such as ``{{unknown}}`` are not exercised
    by this test because they are not part of R3.7's alphabet; the
    unit tests in ``test_prompt_template.py`` cover that branch.
    """
    template, placeholders_used = template_and_used

    values = {
        "project_brief": project_brief,
        "task_spec": task_spec,
        "task_id": task_id,
        "task_title": task_title,
        "persona_name": persona_name,
    }

    rendered = render_prompt(template, **values)

    # Assertion 1: every used supported placeholder marker is gone from
    # the rendered output. ``render_prompt`` always attempts all five
    # replacements, so the "used" set is really a *lower bound* -- we
    # assert the stronger invariant that every supported marker from
    # R3.7's alphabet is absent from the output, which implies the
    # per-used-marker check.
    for marker in SUPPORTED_PLACEHOLDERS:
        assert marker not in rendered, (
            f"marker {marker!r} should have been substituted but is still "
            f"present in rendered output {rendered!r}"
        )

    # Assertion 2: every marker that was used in the template has its
    # substituted value appear in the rendered output. Because values
    # come from a ``{``/``}``-free alphabet, ``str.replace`` cannot
    # introduce a value inside a later-replaced marker, so "value in
    # output" is a stable property regardless of replacement order.
    for marker in placeholders_used:
        kwarg = _MARKER_TO_KWARG[marker]
        value = values[kwarg]
        assert value in rendered, (
            f"marker {marker!r} was in the template but its value "
            f"{value!r} is missing from rendered output {rendered!r}"
        )
