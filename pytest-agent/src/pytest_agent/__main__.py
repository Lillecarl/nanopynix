"""``python -m pytest_agent`` -- the console script, without needing one.

Identical to the installed ``pytest-agent`` entry point. Useful when
pytest-agent is merely on ``PYTHONPATH`` with no distribution metadata (its
own test suite runs it exactly this way, against a throwaway project
directory), and as a fallback anywhere the script directory isn't on ``PATH``.
"""

from __future__ import annotations

from pytest_agent.cli import main

main()
