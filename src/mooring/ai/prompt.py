"""Prompt construction for the schema-only AI helper.

The prompt carries the dataset *schema* (column names + dtypes) and the user's
goal — and nothing else. It never contains data values: that is stated to the
model here and enforced upstream by :mod:`mooring.schema`, which emits no
values. The system instruction also forbids the assistant from trying to read
the file itself (a belt-and-braces complement to running it with no tools).
"""

from __future__ import annotations

SYSTEM_PROMPT = (
    "You are a careful data-analysis coding assistant embedded in a financial "
    "institution's notebook tool. You help an analyst write code for the NEXT "
    "cell of a marimo (Python) notebook.\n\n"
    "STRICT RULES:\n"
    "- You are given ONLY the dataset's schema (column names and types). For "
    "privacy and regulatory reasons you can NEVER see the actual data values, "
    "and you must not ask for them or attempt to read any file.\n"
    "- Generate {target} code only. Prefer the Polars API (the notebook has "
    "polars imported as `pl`). Do not use pandas.\n"
    "- Assume the data is already loaded into a Polars DataFrame named `df` "
    "with the schema given below, unless the user says otherwise.\n"
    "- Return a single fenced ```python code block with the code, plus at most "
    "a one-line comment explaining intent. No prose before or after.\n"
    "- If the request is ambiguous given only the schema, make a reasonable "
    "assumption and note it in a brief inline comment."
)


def build_messages(
    *, schema_context: str, instruction: str, target: str = "polars"
) -> tuple[str, str]:
    """Return ``(system, user)`` prompt strings for the provider to send.

    Only ``schema_context`` (names + dtypes) and ``instruction`` cross the wire.
    """
    system = SYSTEM_PROMPT.format(target=target)
    user = (
        f"{schema_context.strip()}\n\n"
        f"Task: {instruction.strip()}\n\n"
        f"Write the {target} code for the next cell."
    )
    return system, user
