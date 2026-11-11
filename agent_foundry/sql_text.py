"""
Shared SQL text handling for brick scripts.

Bricks that deploy ``.sql`` files (``data``, ``sql_function``) need the same
pre-processing: substitute ``${var.*}`` placeholders, drop SQL comments, and
split the file into statements. Keeping one implementation here stops the
copies from drifting -- they had already diverged over backtick handling, so
back-quoted identifiers were only safe in one of the two bricks.

Known limitation: dollar-quoted bodies (``AS $$ ... $$``, used by metric views)
are not treated as their own context, so a ``--`` or ``;`` inside such a body
is interpreted as SQL rather than as body text. No shipped template relies on
that, but it is the next thing to fix if one ever needs to.
"""

from __future__ import annotations

from agent_foundry.variables import substitute


def split_sql_statements(raw: str, variables: dict[str, str]) -> list[str]:
    """Substitute variables, strip SQL comments, and split on top-level ``;``.

    Walks the input once, tracking whether the cursor is inside a single- or
    double-quoted string literal, a backtick-quoted identifier, inside a ``--``
    line comment, or inside a ``/* ... */`` block comment. Semicolons are only
    treated as statement terminators when the cursor is in none of those
    contexts.
    """
    substituted = substitute(raw, variables)
    statements: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    in_backtick = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(substituted)
    while i < n:
        ch = substituted[i]
        nxt = substituted[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                buf.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"' and nxt == '"':
                buf.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue

        if in_backtick:
            buf.append(ch)
            if ch == "`" and nxt == "`":
                buf.append(nxt)
                i += 2
                continue
            if ch == "`":
                in_backtick = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == "`":
            in_backtick = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements
