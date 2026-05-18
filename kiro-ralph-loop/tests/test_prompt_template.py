"""Unit tests for ``ralph_loop.prompt_template.render_prompt`` (Task 8.3).

These are plain pytest unit tests for the placeholder renderer's
happy-path and edge-case behavior. The matching property test (P6) for
universal substitution over all subsets of the placeholder alphabet
lands in task 8.4.

Requirements exercised: 3.7, 6.4.
"""

from __future__ import annotations

import pytest

from ralph_loop.prompt_template import render_prompt


# A fixture-like default argument bundle used by every test. Individual
# tests override whichever keyword arguments they care about. Keeping
# the defaults short and distinctive (``BRIEF`` / ``SPEC`` / ``ID`` /
# etc.) makes it easy to eyeball whether every placeholder was
# substituted correctly.
_DEFAULTS = {
    "project_brief": "BRIEF",
    "task_spec": "SPEC",
    "task_id": "TID",
    "task_title": "TITLE",
    "persona_name": "PERSONA",
}


def _render(template: str, **overrides: str) -> str:
    """Invoke ``render_prompt`` with ``_DEFAULTS`` overlaid by overrides."""
    kwargs = {**_DEFAULTS, **overrides}
    return render_prompt(template, **kwargs)


class TestEmptyAndNoPlaceholder:
    def test_empty_template_returns_empty_string(self) -> None:
        assert _render("") == ""

    def test_template_without_placeholders_is_returned_unchanged(self) -> None:
        template = "Hello, world. No markers here."
        assert _render(template) == template


class TestSinglePlaceholderSubstitution:
    """Each supported placeholder substitutes correctly in isolation (R3.7)."""

    @pytest.mark.parametrize(
        "marker,kwarg,value",
        [
            ("{{project_brief}}", "project_brief", "Project brief content."),
            ("{{task_spec}}", "task_spec", "Task spec body."),
            ("{{task_id}}", "task_id", "ch-02-draft"),
            ("{{task_title}}", "task_title", "Draft Chapter 2"),
            ("{{persona_name}}", "persona_name", "Writer"),
        ],
    )
    def test_single_marker_is_replaced_with_its_value(
        self, marker: str, kwarg: str, value: str
    ) -> None:
        rendered = _render(marker, **{kwarg: value})
        assert rendered == value


class TestAllPlaceholdersTogether:
    def test_all_five_placeholders_in_one_template(self) -> None:
        template = (
            "brief={{project_brief}} "
            "spec={{task_spec}} "
            "id={{task_id}} "
            "title={{task_title}} "
            "persona={{persona_name}}"
        )
        rendered = _render(
            template,
            project_brief="B",
            task_spec="S",
            task_id="T1",
            task_title="Hello",
            persona_name="Writer",
        )
        assert rendered == "brief=B spec=S id=T1 title=Hello persona=Writer"

    def test_all_placeholders_concatenated_without_separators(self) -> None:
        # Placeholder markers sit adjacent with no intervening text. The
        # rendered output must concatenate the values in the same order.
        template = "{{task_id}}{{task_title}}{{persona_name}}"
        rendered = _render(
            template, task_id="A", task_title="B", persona_name="C"
        )
        assert rendered == "ABC"


class TestRepeatedPlaceholders:
    def test_repeated_placeholder_is_substituted_every_occurrence(self) -> None:
        template = "{{task_id}}/{{task_id}}/{{task_id}}"
        rendered = _render(template, task_id="42")
        assert rendered == "42/42/42"

    def test_repeated_placeholders_across_different_kinds(self) -> None:
        template = (
            "[{{persona_name}}] {{task_title}} ({{task_id}}) "
            "[{{persona_name}}] again"
        )
        rendered = _render(
            template,
            persona_name="Editor",
            task_title="Chapter 2",
            task_id="ch-02",
        )
        assert rendered == "[Editor] Chapter 2 (ch-02) [Editor] again"


class TestUnsupportedPlaceholders:
    def test_unsupported_placeholder_is_left_verbatim(self) -> None:
        # ``{{unknown}}`` is not in the R3.7 alphabet; it must survive
        # the render untouched.
        template = "pre {{unknown}} post"
        assert _render(template) == "pre {{unknown}} post"

    def test_unsupported_placeholder_alongside_supported_is_left_verbatim(
        self,
    ) -> None:
        template = "id={{task_id}} extra={{custom_field}}"
        rendered = _render(template, task_id="abc")
        assert rendered == "id=abc extra={{custom_field}}"

    def test_single_brace_is_not_a_placeholder(self) -> None:
        # Only the ``{{double}}`` braces form placeholders. Single-brace
        # ``{task_id}`` is incidental prose and must be preserved.
        template = "{task_id} vs {{task_id}}"
        rendered = _render(template, task_id="42")
        assert rendered == "{task_id} vs 42"


class TestEmptyReplacementValues:
    def test_empty_string_replacement_removes_marker(self) -> None:
        template = "[{{persona_name}}]"
        rendered = _render(template, persona_name="")
        assert rendered == "[]"

    def test_all_empty_values_flatten_template_to_literals(self) -> None:
        template = (
            "brief:{{project_brief}}|"
            "spec:{{task_spec}}|"
            "id:{{task_id}}"
        )
        rendered = _render(
            template, project_brief="", task_spec="", task_id=""
        )
        assert rendered == "brief:|spec:|id:"
