"""Tell an AI-agent harness apart from a person at a terminal.

A command-line program often has two good output shapes for one question. A
person reads a full-screen interface, scrolls it and narrows the query. An AI
agent reads a list on stdout, and a full-screen interface gives that agent an
unusable screen of control codes. So a program that offers both has to pick
one, and `human_at_terminal` is the question it asks.

**The table below is a copy of the one in `pytest_agent._harness_detect`.**
Neither project can import the other: `pytest-agent` is a pytest plugin that
depends on pytest, and no program takes that at run time to draw a screen;
this project is the command-line layer of a Nix CLI, and a general pytest
plugin must not depend on it. A third distribution for 30 lines of
`os.environ` costs more than the copy does.

The copy is safe to let drift. A missing entry means one harness sees a
full-screen interface it did not want, and the fix is one more string.
"""

from __future__ import annotations

import os
import sys

#: Environment variables that AI coding-agent harnesses (or the tools they
#: shell out through) are documented to set on their own subprocesses. Sourced
#: from each project's own docs and issues, and cross-checked against vercel's
#: cross-vendor list (github.com/vercel/detect-agent/blob/main/agents.json) and
#: the agents.md standardization proposal
#: (github.com/agentsmd/agents.md/issues/136). The presence of a variable is
#: the signal, and its value means nothing, which is how each harness
#: documents it.
HARNESS_ENV_VARS: tuple[str, ...] = (
    "CLAUDECODE",  # Claude Code
    "CLAUDE_CODE",  # Claude Code (older / alternate name)
    "CURSOR_TRACE_ID",  # Cursor (editor-integrated agent)
    "CURSOR_AGENT",  # Cursor CLI
    "GEMINI_CLI",  # Google Gemini CLI
    "CLINE_ACTIVE",  # Cline
    "CODEX_SANDBOX",  # OpenAI Codex CLI
    "CODEX_SANDBOX_NETWORK_DISABLED",  # OpenAI Codex CLI
    "CODEX_CI",  # OpenAI Codex CLI
    "CODEX_THREAD_ID",  # OpenAI Codex CLI
    "ANTIGRAVITY_AGENT",  # Antigravity
    "ANTIGRAVITY_CLI_ALIAS",  # Antigravity
    "AUGMENT_AGENT",  # Augment CLI
    "OPENCODE_CLIENT",  # OpenCode
    "OPENCODE",  # OpenCode
    "GOOSE_PROVIDER",  # Goose
    "JUNIE_DATA",  # JetBrains Junie
    "JUNIE_SHIM_PATH",  # JetBrains Junie
    "REPL_ID",  # Replit Agent
    "OPENCLAW_SHELL",  # OpenClaw
    "TRAE_AI_SHELL_ID",  # Trae AI
    "AI_AGENT",  # emerging cross-vendor convention (vercel/detect-agent)
)


def detect_agent_harness() -> str | None:
    """Return the name of the first harness variable that is set, or `None`.

    The caller gets the name and not a boolean, so that a diagnostic message
    can say which harness it found.
    """
    for name in HARNESS_ENV_VARS:
        if os.environ.get(name):
            return name
    return None


def human_at_terminal() -> bool:
    """Say whether a person can see and drive a full-screen interface.

    Three conditions must hold. stdin must be a terminal, or the interface
    reads no key. stdout must be a terminal, or the interface draws into a
    pipe or a file. And no AI-agent harness variable may be set, because such
    a harness gives its tool a pseudo-terminal and still reads the output as
    text.
    """
    if detect_agent_harness() is not None:
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()
