"""think_tool — ODR's forced-reflection pattern (plan §4.2, §4.4). No external call: the
value is in making the model articulate a reasoning step before its next decision, not
in any side effect."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def think_tool(reflection: str) -> str:
    """Record a structured reflection before deciding what to do next. Use this to assess
    what you've learned, what's still missing, and whether you should continue, refine,
    or stop. This does not fetch new information — it only records your reasoning.

    Args:
        reflection: Your analysis of progress so far and what should happen next.
    """
    return f"Reflection recorded: {reflection}"
