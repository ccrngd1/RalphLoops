"""Prompt-template placeholder rendering (R3.7, R6.4).

Personas declare a ``prompt_template`` containing any subset of the five
supported placeholders documented in R3.7. The :func:`render_prompt`
function substitutes those placeholders with caller-supplied values when
the Context Composer assembles the Kiro CLI prompt (R6.4).

The supported placeholders are:

- ``{{project_brief}}`` — replaced by the contents of ``SUMMARY.md``.
- ``{{task_spec}}`` — replaced by the rendered Task Spec for the
  currently selected task.
- ``{{task_id}}`` — replaced by the task's ``id`` field from
  ``tasks.json``.
- ``{{task_title}}`` — replaced by the task's ``title`` field from
  ``tasks.json``.
- ``{{persona_name}}`` — replaced by the selected persona's ``name``.

Substitution is sequential and non-recursive: placeholders inside the
replacement values are NOT re-expanded. Callers that need the raw value
to survive unchanged should pass values that do not contain a
``{{...}}`` marker (the Ralph Loop's own call sites do, since ``task_id``
/ ``task_title`` / ``persona_name`` come from validated schema fields
and the brief / task-spec bodies are not expected to collide with the
supported placeholder alphabet). Unrecognized markers such as
``{{unknown}}`` are left untouched.
"""

from __future__ import annotations


def render_prompt(
    template: str,
    *,
    project_brief: str,
    task_spec: str,
    task_id: str,
    task_title: str,
    persona_name: str,
) -> str:
    """Substitute the five supported placeholders in ``template``.

    The substitution is a plain sequential ``str.replace`` over the
    supported placeholder alphabet (R3.7). The order of the replacements
    is fixed and documented so callers can reason about the behavior
    when a replacement value happens to contain another supported
    placeholder marker: the later placeholder's replacement would then
    operate on text already injected by an earlier replacement. In
    practice the Ralph Loop's callers pass validated scalar fields (id,
    title, name) and prose bodies (brief, spec) that do not contain
    ``{{...}}`` markers, so ordering is not observable.

    Unsupported placeholders (for example ``{{unknown}}``) are left
    verbatim in the output. This preserves author intent when a persona
    author wants literal braces for downstream tooling.
    """
    return (
        template
        .replace("{{project_brief}}", project_brief)
        .replace("{{task_spec}}", task_spec)
        .replace("{{task_id}}", task_id)
        .replace("{{task_title}}", task_title)
        .replace("{{persona_name}}", persona_name)
    )
