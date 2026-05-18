# RalphLoops

Multiple implementations of the Ralph Loop concept - a domain-agnostic automated iteration wrapper for AI CLI tools.

## What is Ralph Loop?

Ralph Loop drives an AI agent through a persistent loop until explicit completion criteria are met. Each iteration is a fresh, short-lived CLI session that works on exactly one task under exactly one persona. The filesystem acts as durable state between iterations, while in-memory agent context is discarded.

## Implementations

### [kiro-ralph-loop/](kiro-ralph-loop/)
Implementation using Kiro CLI for agent invocation.

### [ClaudeRalphLoop/](ClaudeRalphLoop/)
Implementation using Claude Code CLI for agent invocation.

## Shared Resources

### [templates/](templates/)
Domain-specific persona sets shared across all implementations:

- **book/** - Book-authoring persona set (Writer, Reviewer, Editor, FactChecker, Outline, Planner)

To use these personas with either implementation:

**Option 1: Copy to your project**
```bash
cp -r templates/book/personas/* ./personas/
```

**Option 2: Reference directly with --personas-dir flag**
```bash
ralph init-tasks --personas-dir ../templates/book/personas
ralph run --personas-dir ../templates/book/personas
```

## Quick Start

1. Choose an implementation (kiro-ralph-loop or ClaudeRalphLoop)
2. Install it: `uv sync` or `pip install -e ".[dev]"`
3. Initialize a project: `ralph init`
4. Copy or reference personas from `templates/`
5. Edit `SUMMARY.md` with your project brief
6. Bootstrap tasks: `ralph init-tasks`
7. Run the loop: `ralph run`

See the README in each implementation directory for detailed documentation.
