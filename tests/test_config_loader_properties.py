"""Property-based tests for ``ConfigLoader`` (design Properties 24 and 25).

# Feature: ralph-loop, Property 24 & 25: Config merge and required-file fail-fast

Property 24 exercises the merge precedence rule from R15.2 together with
the documented defaults from R15.4-R15.8: for any partial file config and
partial CLI overrides, the merged ``Config`` value for each field equals
the CLI override when set, otherwise the file value when set, otherwise
the model default.

Property 25 exercises the startup fail-fast rule from R15.9: for any
project directory where one of the three required filesystem entries
(``tasks.json``, ``SUMMARY.md``, ``personas/``) is absent, ``load_config``
must raise :class:`ConfigLoadError`.

Both tests build a fresh project root inside
``tempfile.TemporaryDirectory`` per example because pytest's ``tmp_path``
fixture is allocated once per test function and would otherwise leak
state across Hypothesis examples.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ralph_loop.config import ConfigLoadError, load_config


# ---------------------------------------------------------------------------
# Shared helpers and per-field strategy registry
# ---------------------------------------------------------------------------


# Documented defaults for the fields under test (R15.4-R15.8 and the
# ``Config`` Pydantic model). Anchored here so test expectations stay in
# lockstep with the model and the requirements.
_DEFAULTS: dict[str, Any] = {
    "max_iterations": 50,
    "max_retries_per_task": 5,
    "escalation_threshold": 3,
    "automatic_planner": False,
    "git_integration_enabled": True,
    "pending_tasks_path": "pending_tasks.json",
}


# Per-field value strategies. Values stay inside the ``Config`` model's
# validator constraints (``max_iterations >= 1``, ``max_retries_per_task
# >= 1``, ``escalation_threshold >= 0``) and draw from a small sampled set
# for path / persona fields so shrunk counterexamples stay readable.
_FIELD_VALUE_STRATEGIES: dict[str, st.SearchStrategy[Any]] = {
    "fallback_persona": st.sampled_from(["Writer", "Editor", "Reviewer"]),
    "max_iterations": st.integers(min_value=1, max_value=200),
    "max_retries_per_task": st.integers(min_value=1, max_value=20),
    "escalation_threshold": st.integers(min_value=0, max_value=10),
    "automatic_planner": st.booleans(),
    "git_integration_enabled": st.booleans(),
    "pending_tasks_path": st.sampled_from(
        ["pending_tasks.json", "queue.json", "state/pending.json"]
    ),
}


_TESTED_FIELDS = (
    "fallback_persona",
    "max_iterations",
    "max_retries_per_task",
    "escalation_threshold",
    "automatic_planner",
    "git_integration_enabled",
    "pending_tasks_path",
)

_SCALAR_FIELDS = (
    "max_iterations",
    "max_retries_per_task",
    "escalation_threshold",
    "automatic_planner",
    "git_integration_enabled",
)


def _scaffold(project_root: Path) -> None:
    """Write the three required scaffold entries under ``project_root``."""
    (project_root / "tasks.json").write_text("[]", encoding="utf-8")
    (project_root / "SUMMARY.md").write_text("# Brief\n", encoding="utf-8")
    (project_root / "personas").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Property 24: Merge precedence and defaults (R15.2, R15.4-R15.8)
# ---------------------------------------------------------------------------


@st.composite
def config_merge_inputs_strategy(draw):
    """Generate ``(file_data, cli_overrides, expected)`` triples.

    For each tested field we draw a slot in
    ``{"absent", "file", "cli", "both"}``:

    - ``absent``: field appears in neither the file nor the CLI
      overrides. The expected merged value is the documented default
      from :data:`_DEFAULTS`.
    - ``file``: the field is written only into ``ralph.config.json``.
      The expected value is the file value.
    - ``cli``: the field is passed only via ``cli_overrides``. The
      expected value is the CLI value.
    - ``both``: the field is set on both sides with distinct values.
      The expected value is the CLI value, demonstrating R15.2
      precedence.

    ``fallback_persona`` is required on the ``Config`` model, so it
    never lands in the ``absent`` slot; it's always present via at
    least one source.
    """

    file_data: dict[str, Any] = {}
    cli_overrides: dict[str, Any] = {}
    expected: dict[str, Any] = {}

    for field in _TESTED_FIELDS:
        # ``fallback_persona`` has no default on the Config model, so it
        # must always come from at least one source. Every other field
        # can be absent (default wins).
        if field == "fallback_persona":
            slot = draw(st.sampled_from(["file", "cli", "both"]))
        else:
            slot = draw(st.sampled_from(["absent", "file", "cli", "both"]))

        value_strategy = _FIELD_VALUE_STRATEGIES[field]

        if slot == "absent":
            expected[field] = _DEFAULTS[field]
        elif slot == "file":
            value = draw(value_strategy)
            file_data[field] = value
            expected[field] = value
        elif slot == "cli":
            value = draw(value_strategy)
            cli_overrides[field] = value
            expected[field] = value
        else:  # "both"
            file_value = draw(value_strategy)
            # Draw a distinct CLI value so precedence is observable. The
            # filter is safe for every strategy above: booleans have two
            # values (one remains after filtering), the sampled sets
            # have three, and the integer ranges are far larger than one
            # element.
            cli_value = draw(
                value_strategy.filter(lambda v, fv=file_value: v != fv)
            )
            file_data[field] = file_value
            cli_overrides[field] = cli_value
            expected[field] = cli_value

    return file_data, cli_overrides, expected


@given(inputs=config_merge_inputs_strategy())
def test_config_merge_precedence_and_defaults(inputs) -> None:
    """Validates Requirements 15.2, 15.4, 15.5, 15.6, 15.7, 15.8.

    For any partial ``(file_data, cli_overrides)`` drawn by the
    composite strategy, the merged ``Config`` carries, for every tested
    field: the CLI override when present, otherwise the file value when
    present, otherwise the documented model default. Path fields are
    resolved to absolute paths relative to ``project_root`` per the
    loader's documented behavior, so the ``pending_tasks_path``
    assertion applies the same resolution to the expected value.
    """
    file_data, cli_overrides, expected = inputs

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _scaffold(project_root)
        (project_root / "ralph.config.json").write_text(
            json.dumps(file_data), encoding="utf-8"
        )

        cfg = load_config(
            project_root=project_root,
            cli_overrides=cli_overrides,
        )

        # Scalar fields (non-path) compare directly.
        assert cfg.fallback_persona == expected["fallback_persona"]
        for field in _SCALAR_FIELDS:
            assert getattr(cfg, field) == expected[field], (
                f"{field} precedence mismatch: "
                f"got {getattr(cfg, field)!r}, "
                f"expected {expected[field]!r}"
            )

        # ``pending_tasks_path`` is resolved to an absolute path relative
        # to ``project_root`` (R15.5). Apply the same resolution to the
        # expected value so the comparison matches what the loader
        # produces on every platform.
        expected_path = (project_root / expected["pending_tasks_path"]).resolve()
        assert Path(cfg.pending_tasks_path).resolve() == expected_path


# ---------------------------------------------------------------------------
# Property 25: Required-file fail-fast (R15.9)
# ---------------------------------------------------------------------------


@given(missing=st.sampled_from(["tasks", "summary", "personas"]))
def test_missing_required_path_raises(missing: str) -> None:
    """Validates Requirements 15.9.

    For any project root where one of the three required filesystem
    entries (``tasks.json``, ``SUMMARY.md``, or ``personas/``) is absent
    at startup, ``load_config`` must raise :class:`ConfigLoadError`.

    The ``missing`` discriminant is drawn via
    ``st.sampled_from(["tasks", "summary", "personas"])`` so Hypothesis
    shrinks straight to the smallest failing variant when a regression
    affects only one of the three paths.

    Each Hypothesis example allocates a fresh
    ``tempfile.TemporaryDirectory`` so examples don't leak filesystem
    state into each other.
    """
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _scaffold(project_root)

        if missing == "tasks":
            (project_root / "tasks.json").unlink()
        elif missing == "summary":
            (project_root / "SUMMARY.md").unlink()
        else:
            (project_root / "personas").rmdir()

        with pytest.raises(ConfigLoadError):
            load_config(
                project_root=project_root,
                cli_overrides={"fallback_persona": "Writer"},
            )
