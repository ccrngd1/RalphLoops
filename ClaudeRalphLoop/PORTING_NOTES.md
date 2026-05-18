# ClaudeRalphLoop Porting Notes

## Overview

Successfully ported kiro-ralph-loop to use Claude Code CLI instead of Kiro CLI. This port maintains the same architecture while replacing only the invocation layer.

## Key Changes

### 1. New ClaudeCodeInvoker (`ralph_loop/claude_code.py`)

Replaces `KiroInvoker` with `ClaudeCodeInvoker` that:
- Uses `claude -p` (print/non-interactive mode) instead of `kiro-cli chat --no-interactive`
- Adds `--output-format json` for structured output
- Supports `--dangerously-skip-permissions` flag for tool use
- Supports `--model <id>` for model selection
- Supports `--max-turns N` for turn limits
- Supports `--allowedTools tool1,tool2` for tool restrictions
- Supports `--system-prompt "text"` for custom system prompts
- Parses token usage from Claude's JSON output format (has `input_tokens`, `output_tokens`)
- Maintains same async streaming/tee pattern for real-time output
- Handles timeouts identically to KiroInvoker

### 2. Models (`ralph_loop/models.py`)

- Added `ClaudeCodeInvocationResult` class (mirrors KiroInvocationResult structure)
- Changed `kiro_cli_command` → `claude_cli_command` in Config model
- Updated default value from `"kiro-cli"` → `"claude"`
- Updated all docstrings referencing Kiro CLI to Claude Code CLI

### 3. Updated Imports and References

Updated all modules to use ClaudeCodeInvoker:
- `ralph_loop/orchestrator.py`
- `ralph_loop/planner.py`
- `ralph_loop/validator.py`
- `ralph_loop/cli.py`

All instances of:
- `KiroInvoker` → `ClaudeCodeInvoker`
- `KiroInvocationTimeout` → `ClaudeCodeInvocationTimeout`
- `kiro_cli_command` → `claude_cli_command`

### 4. Test Infrastructure

- Created `tests/support/fake_claude.py` (mirrors fake_kiro.py behavior)
- Ported `tests/test_kiro_invoker.py` → `tests/test_claude_code_invoker.py`
- Updated all test references to use Claude Code CLI conventions

### 5. Documentation

- Updated `README.md` with Claude Code CLI specifics
- Changed title to "Ralph Loop (Claude Code Edition)"
- Added Claude Code CLI Integration section documenting supported flags
- Updated configuration table to show `claude_cli_command` instead of `kiro_cli_command`

### 6. Package Configuration

- Updated `pyproject.toml`:
  - Package name: `ralph-loop` → `ralph-loop-claude`
  - Description updated to reference Claude Code CLI
  - Keywords: `["kiro", ...]` → `["claude", "claude-code", ...]`

## Architecture Preserved

The following components remain unchanged (same architecture):
- Orchestrator (persona selection logic)
- Planner (task generation)
- Task Selector (dependency resolution)
- Validator (shell/file/persona_review checks)
- Budget tracking
- Git manager
- Pending queue
- Context composer
- Task creation processor
- Token accountant
- All models and data structures (except invocation-related)

## Testing

All imports verified to work correctly:
```python
from ralph_loop.claude_code import ClaudeCodeInvoker, ClaudeCodeInvocationTimeout
from ralph_loop.models import ClaudeCodeInvocationResult
# ✓ All imports successful
```

Full test suite requires dependencies installation:
```bash
cd ClaudeRalphLoop
uv sync  # or: pip install -e ".[dev]"
pytest tests/
```

## Usage

The CLI remains identical:
```bash
ralph init
ralph init-tasks
ralph run
ralph rollback N
```

Configuration in `ralph.config.json` now uses:
```json
{
  "claude_cli_command": "claude",
  "default_model_id": "claude-opus-4.7",
  ...
}
```

## Claude Code CLI Command Format

The invoker constructs commands like:
```bash
claude -p --output-format json --dangerously-skip-permissions [--model <id>] [--max-turns N] [--allowedTools tool1,tool2] [--system-prompt "text"]
```

Input is piped via stdin, output is streamed and teed to log files.

## Token Usage Parsing

The invoker supports two formats:
1. Legacy format (for test compatibility): `RALPH_TOKEN_USAGE: {"input_tokens": N, "output_tokens": N}`
2. Claude Code JSON format: parses `input_tokens` and `output_tokens` from JSON output

## Completion Status

✅ All architectural components ported
✅ All imports updated and verified
✅ Test infrastructure ported
✅ Documentation updated
✅ Package configuration updated
✅ Imports verified to work correctly

The port is complete and ready for testing with Claude Code CLI.
