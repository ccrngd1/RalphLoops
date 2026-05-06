"""Unit tests for ``ralph_loop.context`` (Tasks 11.3 and 11.5).

Covers :func:`inline_context_files` (R18.5, R18.6) and
:func:`compose_context` (R5.3, R6.1-R6.7, R14.5). The matching property
tests for content inclusion (P9) and truncation (P10) land in
:mod:`tests.test_context_composer_properties`.

Requirements exercised: 5.3, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 14.5, 18.5, 18.6.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ralph_loop.context import (
    LOOP_FRAMING,
    RESUMED_NOTICE,
    compose_context,
    inline_context_files,
)
from ralph_loop.models import (
    Persona,
    ShellCheckConfig,
    Task,
    TaskSpec,
    TaskSpecBody,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "t-001",
    title: str = "Draft chapter",
    status: str = "pending",
    priority: int = 10,
) -> Task:
    return Task(
        id=id,
        title=title,
        priority=priority,
        status=status,  # type: ignore[arg-type]
        spec_path=f"specs/{id}.md",
    )


def _make_spec(
    *,
    id: str = "t-001",
    title: str = "Draft chapter",
    objective: str = "OBJECTIVE",
    context_references: str = "CTX_REF",
    instructions: str = "INSTR",
    notes: str | None = None,
    context_files: list[str] | None = None,
) -> TaskSpec:
    return TaskSpec(
        id=id,
        title=title,
        validation=[ShellCheckConfig(type="shell", commands=["true"])],
        context_files=context_files,
        body=TaskSpecBody(
            objective=objective,
            context_references=context_references,
            instructions=instructions,
            notes=notes,
        ),
    )


def _make_persona(
    *,
    name: str = "Writer",
    prompt_template: str = "PROMPT[{{persona_name}}]",
    instructions: str | None = "PERSONA_INSTR",
) -> Persona:
    return Persona(
        name=name,
        description=f"{name} description",
        prompt_template=prompt_template,
        instructions=instructions,
    )


# ---------------------------------------------------------------------------
# inline_context_files (R18.5, R18.6)
# ---------------------------------------------------------------------------


class TestInlineContextFiles:
    def test_empty_paths_returns_empty_text_and_empty_missing(
        self, tmp_path: Path
    ) -> None:
        text, missing = inline_context_files([], base_dir=tmp_path)

        assert text == ""
        assert missing == []

    def test_all_existing_files_are_inlined(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("CONTENT_A", encoding="utf-8")
        (tmp_path / "b.md").write_text("CONTENT_B", encoding="utf-8")

        text, missing = inline_context_files(
            ["a.md", "b.md"], base_dir=tmp_path
        )

        assert missing == []
        assert "CONTENT_A" in text
        assert "CONTENT_B" in text
        # Each file must be labeled so downstream consumers can
        # distinguish chunks.
        assert "### File: a.md" in text
        assert "### File: b.md" in text

    def test_missing_file_is_recorded_and_logged_without_raising(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "present.md").write_text("PRESENT", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="ralph_loop.context"):
            text, missing = inline_context_files(
                ["present.md", "missing.md"],
                base_dir=tmp_path,
            )

        assert missing == ["missing.md"]
        assert "PRESENT" in text
        # A warning was emitted identifying the missing path.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("missing.md" in r.getMessage() for r in warnings)

    def test_directory_is_treated_as_missing(self, tmp_path: Path) -> None:
        (tmp_path / "subdir").mkdir()

        text, missing = inline_context_files(
            ["subdir"], base_dir=tmp_path
        )

        assert missing == ["subdir"]
        assert text == ""

    def test_no_base_dir_uses_cwd_semantics(self, tmp_path: Path) -> None:
        # A path that clearly doesn't exist on the system. Without
        # ``base_dir`` the function should still log + record missing
        # without raising.
        text, missing = inline_context_files(
            [str(tmp_path / "definitely-not-here.md")]
        )

        assert len(missing) == 1
        assert text == ""


# ---------------------------------------------------------------------------
# compose_context (R5.3, R6.1-R6.7, R14.5)
# ---------------------------------------------------------------------------


class TestComposeContext:
    def test_untruncated_contains_every_section_in_order(
        self, tmp_path: Path
    ) -> None:
        task = _make_task()
        spec = _make_spec()
        persona = _make_persona()
        brief = "PROJECT_BRIEF_CONTENT"

        window = compose_context(
            task=task,
            spec=spec,
            persona=persona,
            brief=brief,
            base_dir=tmp_path,
        )

        # Each required section is present and truncated=False.
        assert window.truncated is False
        assert LOOP_FRAMING.strip() in window.text  # R6.6
        assert "PROJECT_BRIEF_CONTENT" in window.text  # R6.2
        assert "OBJECTIVE" in window.text  # R6.3 (spec body)
        assert "INSTR" in window.text  # R6.3 (spec body)
        assert "PROMPT[Writer]" in window.text  # R6.4 (rendered prompt)
        assert "PERSONA_INSTR" in window.text  # R6.5 (persona instructions)
        # Section ordering: framing < brief < spec < persona.
        framing_pos = window.text.find("Ralph Loop Iteration")
        brief_pos = window.text.find("PROJECT_BRIEF_CONTENT")
        spec_pos = window.text.find("OBJECTIVE")
        persona_pos = window.text.find("PROMPT[Writer]")
        assert framing_pos < brief_pos < spec_pos < persona_pos

    def test_resumed_notice_included_when_requested(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            resumed_notice=True,
            base_dir=tmp_path,
        )

        assert RESUMED_NOTICE.strip() in window.text
        # Notice appears after framing but before brief (R14.5 ordering).
        framing_pos = window.text.find("Ralph Loop Iteration")
        notice_pos = window.text.find("Resumed from interruption")
        brief_pos = window.text.find("BRIEF")
        assert framing_pos < notice_pos < brief_pos

    def test_resumed_notice_omitted_by_default(self, tmp_path: Path) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        assert "Resumed from interruption" not in window.text

    def test_escalation_context_appended_when_present(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            escalation_context="ESCALATION_DETAILS",
            base_dir=tmp_path,
        )

        assert "Escalation Context" in window.text
        assert "ESCALATION_DETAILS" in window.text
        # Escalation context appears last.
        persona_pos = window.text.find("PROMPT[Writer]")
        escalation_pos = window.text.find("ESCALATION_DETAILS")
        assert persona_pos < escalation_pos

    def test_persona_without_instructions_omits_section(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(instructions=None),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        assert "Persona Instructions" not in window.text

    def test_context_files_are_inlined_into_spec_section(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "ctx.md").write_text("CTX_CONTENT", encoding="utf-8")
        spec = _make_spec(context_files=["ctx.md"])

        window = compose_context(
            task=_make_task(),
            spec=spec,
            persona=_make_persona(),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        assert "CTX_CONTENT" in window.text
        assert "### File: ctx.md" in window.text

    def test_missing_context_file_does_not_abort(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        spec = _make_spec(context_files=["absent.md"])

        with caplog.at_level(logging.WARNING, logger="ralph_loop.context"):
            window = compose_context(
                task=_make_task(),
                spec=spec,
                persona=_make_persona(),
                brief="BRIEF",
                base_dir=tmp_path,
            )

        # Composition succeeded; warning logged.
        assert "BRIEF" in window.text
        assert any(
            "absent.md" in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    def test_truncation_preserves_spec_persona_and_flags(
        self, tmp_path: Path
    ) -> None:
        # A huge brief guarantees overflow against a small token cap.
        big_brief = "X" * 10_000
        task = _make_task()
        spec = _make_spec(
            objective="OBJ_MARKER",
            instructions="INSTR_MARKER",
        )
        persona = _make_persona(
            prompt_template="PROMPT_MARKER {{persona_name}}",
            instructions="PERSONA_INSTR_MARKER",
        )

        window = compose_context(
            task=task,
            spec=spec,
            persona=persona,
            brief=big_brief,
            max_tokens=100,
            base_dir=tmp_path,
        )

        assert window.truncated is True
        # R6.7: spec body + persona prompt + persona instructions survive.
        assert "OBJ_MARKER" in window.text
        assert "INSTR_MARKER" in window.text
        assert "PROMPT_MARKER Writer" in window.text
        assert "PERSONA_INSTR_MARKER" in window.text
        # Brief has been shrunk; truncation notice must be present.
        assert "[... project brief truncated ...]" in window.text
        # The full 10_000-char brief is no longer inlined verbatim.
        assert big_brief not in window.text

    def test_approx_tokens_matches_text_length_heuristic(
        self, tmp_path: Path
    ) -> None:
        window = compose_context(
            task=_make_task(),
            spec=_make_spec(),
            persona=_make_persona(),
            brief="BRIEF",
            base_dir=tmp_path,
        )

        # Heuristic is len(text) // 4.
        assert window.approx_tokens == len(window.text) // 4
