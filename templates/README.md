# Ralph Loop Templates

This directory contains domain-specific persona sets that can be used with any Ralph Loop implementation (kiro-ralph-loop, ClaudeRalphLoop, or future implementations).

## Available Templates

### book/

A complete persona set for book-authoring projects. Includes:

- **Writer** - Drafts new prose from approved chapter outlines
- **Reviewer** - Reviews drafts for clarity, coherence, and adherence to the outline
- **Editor** - Performs line editing for style, grammar, and consistency
- **FactChecker** - Verifies factual claims and citations
- **Outline** - Creates structured chapter outlines from project briefs
- **Planner** - Decomposes high-level goals into task breakdowns

## Usage

### Option 1: Copy to Your Project

Copy the personas into your project's `personas/` directory:

```bash
cd your-project/
cp -r /path/to/RalphLoops/templates/book/personas/* ./personas/
```

### Option 2: Reference Directly (Recommended)

Use the `--personas-dir` flag to reference the shared personas without copying:

```bash
# From your project directory
ralph init-tasks --personas-dir ../templates/book/personas
ralph run --personas-dir ../templates/book/personas
```

Adjust the path (`../templates/book/personas`) based on where your project is relative to the RalphLoops repository.

## Creating New Templates

To create a new domain-specific template:

1. Create a directory: `templates/<domain-name>/personas/`
2. Add persona YAML files following the schema in `schemas/persona.schema.json`
3. Include at least:
   - A fallback persona (e.g., `worker.yaml`)
   - A planner persona (e.g., `planner.yaml`)
4. Document the personas in a README

## Persona File Format

Each persona is a YAML file with these fields:

```yaml
name: PersonaName
description: >-
  Brief description of the persona's role
prompt_template: |
  Template using {{placeholders}} for:
  - {{persona_name}}
  - {{task_id}}
  - {{task_title}}
  - {{project_brief}}
  - {{task_spec}}
default_persona_review_pass_condition: "PASS"  # Optional
instructions: |  # Optional
  Additional instructions for this persona
```

See existing personas in `book/personas/` for examples.
