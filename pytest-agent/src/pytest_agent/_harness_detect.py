from __future__ import annotations

import os

# Environment variables that AI coding-agent harnesses (or the tools they
# shell out through) are documented to set on their own subprocesses. Sourced
# from each project's own docs/issues, and cross-checked against vercel's
# maintained cross-vendor list (github.com/vercel/detect-agent/blob/main/agents.json)
# and the agents.md standardization proposal
# (github.com/agentsmd/agents.md/issues/136). Presence is treated as a
# boolean signal regardless of value, matching how each harness documents it.
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
    """Return the name of the first AI-agent-harness environment variable
    found set, or None if none of them are present.
    """
    for name in HARNESS_ENV_VARS:
        if os.environ.get(name):
            return name
    return None
