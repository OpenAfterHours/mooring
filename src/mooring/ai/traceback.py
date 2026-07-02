"""Value-safe rewriting of pasted Python tracebacks — the traceback guard's core.

When an analyst pastes a traceback into the copilot chat, the paste can embed
data values — ``KeyError: 'ACME Ltd'``, ``could not convert string to float:
'£1,234'``, a repr of the offending row inside a library frame. This module
structurally rewrites a detected traceback block, FAIL-CLOSED, before any
egress:

* the **exception type** is kept (a code identifier, not data);
* **frames that resolve into the workspace** keep their (workspace-relative)
  path, line number, and function, and their source line is RE-READ FROM DISK —
  the pasted "source" is never trusted, and the re-read is restricted to paths
  that resolve UNDER the workspace AND end in ``.py`` (so a crafted frame can
  never make the sanitiser read a data file — see :func:`_frame_target`);
* **frames outside the workspace** keep only a code-shaped file basename, the
  line number, and the function name; their source lines are dropped;
* the **exception message** becomes a shape-preserving placeholder
  (``<redacted: N chars>``) unless it is provably value-free: it matches a
  fixed allowlist of known interpreter messages, or every quoted token in it is
  already in ``known_tokens`` (text the model has been shown this session);
* **anything inside a detected block that matches no known shape** becomes
  ``<redacted line>`` — parser gaps fail closed, never pass through.

Findings are value-free ``(line, kind)`` pairs (:class:`mooring.ai.pii.Finding`),
so the rewrite report can be logged and streamed as safely as a PII finding.

Like :mod:`mooring.ai.pii` and :mod:`mooring.ai.secrets` this is **defence in
depth, not a structural guarantee** — an analyst can still retype a redacted
value in prose. The docs say so plainly (docs/admins/ai-privacy.md).

Pure stdlib (``re`` + ``pathlib``); the only I/O is the workspace-``.py``
source-line re-read above. Everything else in mooring reaches this module
through :func:`mooring.ai.egress.sanitize_traceback` — the one gateway,
enforced by ``tests/test_egress.py``. (The module name shadows nothing at
runtime: mooring uses absolute imports throughout, the same precedent as
``ai/secrets.py`` beside stdlib ``secrets``.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mooring.ai.pii import Finding

# Value-free finding kinds (what was withheld, never the withheld text).
MESSAGE = "exception message redacted"
LINE = "unrecognised line redacted"
SOURCE = "pasted source line dropped"
FILENAME = "frame filename redacted"
FUNCTION = "frame function redacted"

REDACTED_LINE = "<redacted line>"

# ExceptionGroup renders every nested line behind a margin of "|"/"+" rails;
# plain tracebacks just indent. The margin carries no content (spaces, tabs,
# pipes, plus signs only), so it is stripped for classification and re-emitted.
_MARGIN_RE = re.compile(r"^[ \t]*(?:[|+][ \t]*)*")

_HEADER_RE = re.compile(r"^(?:Exception Group )?Traceback \(most recent call last\):$")
_FRAME_RE = re.compile(r'^File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.*))?$')
_REPEAT_RE = re.compile(r"^\[Previous line repeated \d+ more times\]$")
# Position markers under a source line (SyntaxError carets, 3.11+ anchors):
# only ^ / ~ / whitespace — value-free by construction, but useless once the
# source line is re-read/dropped, so they are removed rather than kept.
_CARET_RE = re.compile(r"^[ \t~^]*\^[ \t~^]*$")
# ExceptionGroup divider rails like "+---------------- 1 ----------------":
# punctuation plus at most a small counter — value-free, kept verbatim.
_RULE_RE = re.compile(r"^[ \t|+\-]+(?:\d{1,4}[ \t|+\-]+)?$")

# The chained-exception separators are fixed interpreter strings — kept verbatim.
_SEPARATORS = frozenset(
    {
        "During handling of the above exception, another exception occurred:",
        "The above exception was the direct cause of the following exception:",
    }
)

# An exception line is "Name: message" or a bare "Name" — but ONLY when the name
# looks like an exception class; otherwise a bare token inside a block ("pass",
# or a stray value) would be emitted verbatim as a fake exception type.
_EXC_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?P<colon>:\s?(?P<msg>.*))?$"
)
_EXC_BARE = frozenset(
    {
        "ArithmeticError",
        "AssertionError",
        "BaseException",
        "BaseExceptionGroup",
        "BufferError",
        "EOFError",
        "Exception",
        "ExceptionGroup",
        "GeneratorExit",
        "KeyboardInterrupt",
        "MemoryError",
        "StopAsyncIteration",
        "StopIteration",
        "SystemExit",
    }
)

# Interpreter messages that are fixed strings — provably value-free, kept as-is.
_SAFE_MESSAGES = frozenset(
    {
        "division by zero",
        "float division by zero",
        "integer division or modulo by zero",
        "list index out of range",
        "string index out of range",
        "tuple index out of range",
        "range object index out of range",
        "pop from empty list",
        "pop from an empty deque",
        "pop from an empty set",
        "maximum recursion depth exceeded",
        "invalid syntax",
        "unexpected indent",
        "unexpected EOF while parsing",
        "unindent does not match any outer indentation level",
        "expected an indented block",
        "invalid decimal literal",
        "cannot convert float NaN to integer",
        "cannot convert float infinity to integer",
        "I/O operation on closed file.",
        "list.remove(x): x not in list",
    }
)

_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_WORD_RE = re.compile(r"\w+")
# A code-shaped filename / function: identifier characters only (no spaces, no
# quotes), so a value pasted into a crafted frame cannot ride out on either.
_SAFE_BASENAME_RE = re.compile(r"[\w.\-]+\.pyw?")
_ANGLE_BASENAME_RE = re.compile(r"<[\w. \-]+>")
_SAFE_FUNC_RE = re.compile(r"[\w.<>]+")


@dataclass(frozen=True)
class Sanitized:
    """The result of :func:`sanitize` — the rewritten text, the value-free
    redaction findings, and whether a traceback block was detected at all."""

    text: str
    findings: list[Finding]
    detected: bool


def known_tokens_from(*texts: str) -> frozenset[str]:
    """The rescue allowlist for exception messages: every word token and every
    quoted string in ``texts`` (the live schema, the system context, the notebook
    source — text the model has ALREADY seen, so re-stating a token from it in an
    exception message reveals nothing new)."""
    tokens: set[str] = set()
    for text in texts:
        if not text:
            continue
        tokens.update(_WORD_RE.findall(text))
        for single, double in _QUOTED_RE.findall(text):
            quoted = single or double
            if quoted:
                tokens.add(quoted)
    return frozenset(tokens)


def detect(text: str) -> list[tuple[int, int]]:
    """The 1-based (start, end) line spans of detected traceback blocks."""
    return [(s + 1, e + 1) for s, e in _spans(text.splitlines())]


def sanitize(
    text: str, *, workspace: Path | None, known_tokens: frozenset[str] = frozenset()
) -> Sanitized:
    """Rewrite every detected traceback block in ``text`` value-safe, fail-closed.

    Text outside a detected block is untouched (the caller runs the normal PII
    scan over the whole result). ``workspace`` bounds the source-line re-read;
    ``known_tokens`` rescues messages whose quoted tokens are already in-channel.
    """
    lines = text.splitlines()
    spans = _spans(lines)
    if not spans:
        return Sanitized(text=text, findings=[], detected=False)
    out: list[str] = []
    findings: list[Finding] = []
    cursor = 0
    for start, end in spans:
        out.extend(lines[cursor:start])
        block_lines, block_findings = _rewrite_block(lines, start, end, workspace, known_tokens)
        out.extend(block_lines)
        findings.extend(block_findings)
        cursor = end + 1
    out.extend(lines[cursor:])
    rewritten = "\n".join(out)
    if text.endswith("\n"):
        rewritten += "\n"
    return Sanitized(text=rewritten, findings=findings, detected=True)


# -- detection ------------------------------------------------------------------


def _split_margin(line: str) -> tuple[str, str]:
    match = _MARGIN_RE.match(line)
    margin = match.group() if match else ""
    return margin, line[len(margin) :]


def _spans(lines: list[str]) -> list[tuple[int, int]]:
    """0-based inclusive (start, end) spans of traceback blocks.

    A block anchors on the ``Traceback (most recent call last):`` header or a
    ``File "…", line N`` frame line, and consumes every following non-blank line
    (fail-closed: whatever sits inside gets classified — or redacted — by the
    rewrite). A blank line ends the block unless the next non-blank line is a
    chained-exception separator or another header, which CPython prints with
    blank lines around it.
    """
    spans: list[tuple[int, int]] = []
    n = len(lines)
    i = 0
    while i < n:
        _, core = _split_margin(lines[i])
        if not (_HEADER_RE.fullmatch(core) or _FRAME_RE.fullmatch(core)):
            i += 1
            continue
        start = i
        last = i
        i += 1
        while i < n:
            if lines[i].strip() == "":
                peek = i
                while peek < n and lines[peek].strip() == "":
                    peek += 1
                if peek >= n:
                    break
                _, peek_core = _split_margin(lines[peek])
                if peek_core in _SEPARATORS or _HEADER_RE.fullmatch(peek_core):
                    last = peek  # the blank gap + separator stay inside the block
                    i = peek + 1
                    continue
                break  # blank line, then prose — the block ended before the blank
            last = i
            i += 1
        spans.append((start, last))
        i = last + 1
    return spans


# -- the rewrite ------------------------------------------------------------------


def _rewrite_block(
    lines: list[str],
    start: int,
    end: int,
    workspace: Path | None,
    known_tokens: frozenset[str],
) -> tuple[list[str], list[Finding]]:
    out: list[str] = []
    findings: list[Finding] = []
    # After a frame line, the next unclassified line is the pasted "source" slot:
    # "consume" = replaced by the disk re-read already emitted (workspace frame);
    # "drop" = removed with a finding (non-workspace frame). Never passed through.
    source_slot: str | None = None
    for i in range(start, end + 1):
        raw = lines[i]
        lineno = i + 1
        if raw.strip() == "":
            out.append("")
            continue
        margin, core = _split_margin(raw)
        if _HEADER_RE.fullmatch(core) or core in _SEPARATORS:
            source_slot = None
            out.append(margin + core)  # fixed interpreter strings
            continue
        frame = _FRAME_RE.fullmatch(core)
        if frame:
            frame_lines, frame_findings, source_slot = _rewrite_frame(
                frame, margin, lineno, workspace
            )
            out.extend(frame_lines)
            findings.extend(frame_findings)
            continue
        if _CARET_RE.fullmatch(core):
            continue  # position markers — meaningless once the source is rewritten
        if _RULE_RE.fullmatch(raw):
            source_slot = None
            out.append(raw)  # ExceptionGroup rails: punctuation + a small counter
            continue
        if _REPEAT_RE.fullmatch(core):
            source_slot = None
            out.append(margin + core)  # fixed string + a count
            continue
        exc = _EXC_RE.fullmatch(core)
        if exc and _exceptionish(exc.group("name")):
            source_slot = None
            out.append(margin + _rewrite_exception(exc, lineno, known_tokens, findings))
            continue
        if source_slot is not None:
            slot, source_slot = source_slot, None
            if slot == "drop":
                findings.append(Finding(lineno, SOURCE))
            continue  # "consume": the disk re-read already replaced this line
        out.append(margin + REDACTED_LINE)  # fail closed: unknown shapes never pass
        findings.append(Finding(lineno, LINE))
    return out, findings


def _rewrite_frame(
    frame: re.Match, margin: str, lineno: int, workspace: Path | None
) -> tuple[list[str], list[Finding], str]:
    """Rewrite one ``File "…", line N[, in f]`` line.

    Returns ``(lines, findings, source_slot)`` — the frame line (plus, for a
    workspace frame, the source line re-read from disk), any redaction findings,
    and how to treat the pasted source line that may follow ("consume"/"drop").
    """
    findings: list[Finding] = []
    path_text = frame.group("path")
    frame_line = int(frame.group("line"))
    func = frame.group("func")
    func_out = ""
    if func is not None:
        if _SAFE_FUNC_RE.fullmatch(func):
            func_out = f", in {func}"
        else:
            func_out = ", in <redacted>"
            findings.append(Finding(lineno, FUNCTION))
    target = _frame_target(path_text, workspace)
    if target is not None:
        assert workspace is not None  # _frame_target returned a workspace-bound path
        rel = target.relative_to(workspace.resolve()).as_posix()
        out = [f'{margin}File "{rel}", line {frame_line}{func_out}']
        source = _read_source_line(target, frame_line)
        if source:
            out.append(f"{margin}  {source}")
        return out, findings, "consume"
    basename = re.split(r"[\\/]", path_text)[-1]
    if not (_SAFE_BASENAME_RE.fullmatch(basename) or _ANGLE_BASENAME_RE.fullmatch(basename)):
        basename = "<redacted>"
        findings.append(Finding(lineno, FILENAME))
    return [f'{margin}File "{basename}", line {frame_line}{func_out}'], findings, "drop"


def _frame_target(path_text: str, workspace: Path | None) -> Path | None:
    """Resolve a pasted frame path to a real workspace ``.py`` file, or ``None``.

    THE security boundary of the module: the sanitiser re-reads a source line
    from disk ONLY through this gate, so a crafted frame naming a workspace CSV
    (or traversing out with ``..``/symlinks) can never turn the sanitiser itself
    into a value channel. The path must RESOLVE under the workspace, carry a
    ``.py`` suffix, and exist as a file — anything else keeps basename-only.
    """
    if workspace is None:
        return None
    try:
        ws = workspace.resolve()
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = workspace / path_text
        resolved = candidate.resolve()
        if resolved.suffix.lower() != ".py":
            return None
        if not resolved.is_relative_to(ws):
            return None
        if not resolved.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _read_source_line(target: Path, lineno: int) -> str:
    """Line ``lineno`` of ``target``, stripped — the disk truth that replaces the
    pasted source. ``target`` has already passed :func:`_frame_target`."""
    if lineno < 1:
        return ""
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                if number == lineno:
                    return line.strip()
    except OSError:
        return ""
    return ""


def _exceptionish(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return last in _EXC_BARE or last.endswith(("Error", "Exception", "Warning", "Group"))


def _rewrite_exception(
    exc: re.Match, lineno: int, known_tokens: frozenset[str], findings: list[Finding]
) -> str:
    name = exc.group("name")
    if exc.group("colon") is None:
        return name  # a bare exception type, e.g. KeyboardInterrupt
    message = exc.group("msg") or ""
    if not message.strip():
        return f"{name}:"
    if _message_is_safe(message, known_tokens):
        return f"{name}: {message}"
    findings.append(Finding(lineno, MESSAGE))
    return f"{name}: <redacted: {len(message)} chars>"


def _message_is_safe(message: str, known_tokens: frozenset[str]) -> bool:
    """Provably value-free: a fixed interpreter message, or a message whose every
    quoted token is already in ``known_tokens`` and whose unquoted remainder is a
    library template (no long digit runs that could be an identifier/value)."""
    if message in _SAFE_MESSAGES:
        return True
    quoted = [single or double for single, double in _QUOTED_RE.findall(message)]
    if not quoted:
        return False
    if not all(token == "" or token in known_tokens for token in quoted):
        return False
    residue = _QUOTED_RE.sub("", message)
    return re.search(r"\d{4,}", residue) is None
