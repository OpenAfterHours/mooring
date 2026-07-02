"""The value-safe traceback sanitiser, exercised through its ONE gateway.

Everything here calls ``egress.sanitize_traceback`` (the sole entry point the
rest of the app is allowed to use — pinned in ``test_egress.py``); the module is
imported directly only for its constants and ``detect``. The two invariants that
matter most:

* ``SECRET_VALUE_DO_NOT_LEAK`` planted in an exception message, a pasted
  "source" line, a frame path, or a WORKSPACE DATA FILE named by a crafted frame
  never survives — the disk re-read is restricted to real ``.py`` files that
  resolve UNDER the workspace (the CSV re-read hole);
* fail-closed — junk inside a detected block never passes through verbatim.
"""

from __future__ import annotations

import argparse

from mooring import config
from mooring.ai import egress
from mooring.ai import traceback as tb

SECRET = "SECRET_VALUE_DO_NOT_LEAK"


def _san(text, workspace=None, known=""):
    return egress.sanitize_traceback(text, workspace=workspace, known_text=known)


def _plain_tb(message=f"'{SECRET}'"):
    return (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\site-packages\\polars\\dataframe.py", line 88, in __getitem__\n'
        "    return self._df.get(key)\n"
        f"KeyError: {message}"
    )


# -- detection -------------------------------------------------------------------


def test_prose_is_not_a_traceback():
    text = "why does my filter return zero rows?\nthe column is called revenue"
    result = _san(text)
    assert result.detected is False
    assert result.text == text and result.findings == []
    assert tb.detect(text) == []


def test_detect_reports_one_based_spans():
    text = "prose first\n\n" + _plain_tb() + "\n\nprose after"
    assert tb.detect(text) == [(3, 6)]


def test_a_lone_exception_line_in_prose_does_not_anchor():
    # "Note: this happens" / "KeyError: 'x'" quoted in prose must not trip the
    # rewrite — a block needs the header or a File-frame anchor.
    text = "I keep getting KeyError: 'revenue' from that cell"
    assert _san(text).detected is False


# -- the basic rewrite ------------------------------------------------------------


def test_plain_traceback_keeps_type_redacts_message():
    result = _san("Explain this:\n\n" + _plain_tb() + "\n\nthanks!")
    assert result.detected is True
    assert SECRET not in result.text
    assert "Traceback (most recent call last):" in result.text
    # Exception type survives; the message becomes a shape-preserving placeholder.
    assert f"KeyError: <redacted: {len(SECRET) + 2} chars>" in result.text
    # Non-workspace frame: basename + line + function only, source line dropped.
    assert 'File "dataframe.py", line 88, in __getitem__' in result.text
    assert "site-packages" not in result.text
    assert "self._df.get(key)" not in result.text
    kinds = {f.kind for f in result.findings}
    assert tb.MESSAGE in kinds and tb.SOURCE in kinds


def test_prose_around_the_block_is_untouched():
    result = _san("Explain this:\n\n" + _plain_tb() + "\n\nthanks!")
    assert result.text.startswith("Explain this:\n\n")
    assert result.text.endswith("\n\nthanks!")


def test_trailing_newline_preserved():
    assert _san(_plain_tb() + "\n").text.endswith("\n")
    assert not _san(_plain_tb()).text.endswith("\n")


def test_findings_are_value_free():
    result = _san(_plain_tb())
    assert SECRET not in repr(result.findings)
    assert all(isinstance(f.line, int) for f in result.findings)


def test_bare_exception_type_survives():
    text = 'Traceback (most recent call last):\n  File "x.py", line 1, in f\nKeyboardInterrupt'
    assert "KeyboardInterrupt" in _san(text).text


# -- the four SECRET placements ----------------------------------------------------


def test_secret_in_exception_message_never_survives():
    assert SECRET not in _san(_plain_tb()).text


def test_secret_in_pasted_source_line_never_survives(tmp_path):
    # The pasted "source" under a workspace frame is NEVER trusted — the line is
    # re-read from disk, so a doctored paste cannot smuggle a value through.
    (tmp_path / "nb.py").write_text("import marimo\nx = load()\n", "utf-8")
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "nb.py"}", line 2, in _\n'
        f"    x = load({SECRET!r})\n"
        "ValueError: boom"
    )
    result = _san(text, workspace=tmp_path)
    assert SECRET not in result.text
    assert "x = load()" in result.text  # the disk truth replaced the paste


def test_secret_in_non_workspace_frame_path_never_survives():
    text = (
        "Traceback (most recent call last):\n"
        f'  File "C:\\exports\\{SECRET}\\loader.py", line 3, in load\n'
        "ValueError: boom"
    )
    result = _san(text)
    assert SECRET not in result.text
    assert 'File "loader.py", line 3, in load' in result.text


def test_value_shaped_frame_basename_is_redacted():
    # A basename that does not look like a code file (spaces, not .py) is withheld.
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\exports\\report for ACME Ltd.py", line 1, in run\n'
        "ValueError: boom"
    )
    result = _san(text)
    assert "ACME" not in result.text
    assert 'File "<redacted>", line 1, in run' in result.text
    assert any(f.kind == tb.FILENAME for f in result.findings)


def test_the_csv_reread_hole_is_closed(tmp_path):
    # THE pinned hole: a crafted frame naming a workspace DATA file must never
    # make the sanitiser read (and egress) that file's bytes. The re-read is
    # restricted to resolved-under-workspace paths that END IN .py.
    data = tmp_path / "data" / "customers.csv"
    data.parent.mkdir(parents=True)
    data.write_text(f"name,pan\n{SECRET},4111\n", "utf-8")
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{data}", line 1, in load\n'
        "UnicodeDecodeError: boom"
    )
    result = _san(text, workspace=tmp_path)
    assert SECRET not in result.text
    # Not even the data file's name survives (a .csv basename is not code-shaped).
    assert "customers.csv" not in result.text


def test_path_traversal_out_of_the_workspace_is_not_read(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(f"token = {SECRET!r}\n", "utf-8")
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{ws}\\..\\outside.py", line 1, in f\n'
        "ValueError: boom"
    )
    result = _san(text, workspace=ws)
    assert SECRET not in result.text  # resolved out of the workspace -> no re-read


# -- workspace frames --------------------------------------------------------------


def test_workspace_frame_keeps_rel_path_and_rereads_source(tmp_path):
    (tmp_path / "notebooks").mkdir()
    (tmp_path / "notebooks" / "nb.py").write_text(
        "import marimo\nrevenue = df.sum()\n", "utf-8"
    )
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "notebooks" / "nb.py"}", line 2, in _\n'
        "    revenue = df.sum()\n"
        "AttributeError: boom"
    )
    result = _san(text, workspace=tmp_path)
    assert 'File "notebooks/nb.py", line 2, in _' in result.text
    assert "revenue = df.sum()" in result.text  # re-read from disk
    # The consumed pasted source produced no "dropped" finding — nothing was lost.
    assert all(f.kind != tb.SOURCE for f in result.findings)


def test_no_source_line_is_inserted_when_the_paste_showed_none(tmp_path):
    # THE insert-channel pin: a crafted frame naming ANY workspace .py must not
    # make the sanitiser ADD that file's line to the outbound text. The paste
    # below contains no source line, so the rewrite may not contain one either —
    # otherwise a fabricated traceback becomes a one-line-per-frame read
    # primitive over settings/credentials modules the tools can never reach.
    (tmp_path / "local_settings.py").write_text(
        f'DB_PASSWORD = "{SECRET}"\n', "utf-8"
    )
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "local_settings.py"}", line 1, in <module>\n'
        "ValueError: boom"
    )
    result = _san(text, workspace=tmp_path)
    assert SECRET not in result.text
    assert "DB_PASSWORD" not in result.text
    assert 'File "local_settings.py", line 1, in <module>' in result.text


def test_reread_needs_the_frame_line_to_exist(tmp_path):
    # A pasted source line under a frame whose line number is PAST the file's end
    # is implausible — it is dropped (visibly), never replaced by guesswork.
    (tmp_path / "nb.py").write_text("import marimo\n", "utf-8")
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "nb.py"}", line 99, in _\n'
        f"    x = load({SECRET!r})\n"
        "ValueError: boom"
    )
    result = _san(text, workspace=tmp_path)
    assert SECRET not in result.text
    assert any(f.kind == tb.SOURCE for f in result.findings)  # dropped, not silent


def test_non_code_shaped_disk_lines_are_not_emitted(tmp_path):
    # Even a real workspace .py can hold data-shaped lines (a row inside a
    # triple-quoted literal). A re-read line that doesn't look like a Python
    # statement stays out — the pasted line is dropped with a finding instead.
    (tmp_path / "nb.py").write_text(
        'import marimo\nrows = """\n4111111111111111,Jane Doe\n"""\n', "utf-8"
    )
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "nb.py"}", line 3, in _\n'
        "    some pasted source\n"
        "ValueError: boom"
    )
    result = _san(text, workspace=tmp_path)
    assert "4111111111111111" not in result.text
    assert "Jane Doe" not in result.text
    assert any(f.kind == tb.SOURCE for f in result.findings)


def test_missing_workspace_file_keeps_basename_only(tmp_path):
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "gone.py"}", line 2, in _\n'
        "    x = 1\n"
        "ValueError: boom"
    )
    result = _san(text, workspace=tmp_path)
    assert 'File "gone.py", line 2, in _' in result.text  # basename path, no re-read
    assert "x = 1" not in result.text  # pasted source still dropped


# -- fail-closed ------------------------------------------------------------------


def test_junk_lines_inside_a_block_never_pass_verbatim():
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "    row = df[0]\n"
        f"    {{'name': '{SECRET}', 'pan': 4111111111111111}}\n"
        "some stray repr output here\n"
        "KeyError: 'x'"
    )
    result = _san(text)
    assert SECRET not in result.text
    assert "4111111111111111" not in result.text
    assert "stray repr" not in result.text
    assert tb.REDACTED_LINE in result.text
    assert any(f.kind == tb.LINE for f in result.findings)


def test_bare_identifier_line_is_not_mistaken_for_an_exception():
    # A lone word inside a block must NOT ride out as a fake exception type.
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "    x = 1\n"
        f"{SECRET}\n"
        "ValueError: boom"
    )
    result = _san(text)
    assert SECRET not in result.text


def test_message_continuation_lines_after_the_exception_fail_closed():
    # Multi-line exception messages (pydantic and friends) keep no free text.
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "ValidationError: 1 validation error for Order\n"
        f"  value {SECRET} is not a valid integer"
    )
    result = _san(text)
    assert SECRET not in result.text


def test_weird_function_name_is_redacted():
    text = (
        "Traceback (most recent call last):\n"
        f'  File "C:\\other\\lib.py", line 2, in {SECRET} handler\n'
        "ValueError: boom"
    )
    result = _san(text)
    assert SECRET not in result.text
    assert ", in <redacted>" in result.text
    assert any(f.kind == tb.FUNCTION for f in result.findings)


# -- messages: allowlist + known-token rescue ---------------------------------------


def test_allowlisted_interpreter_message_survives():
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "ZeroDivisionError: division by zero"
    )
    result = _san(text)
    assert "ZeroDivisionError: division by zero" in result.text
    assert all(f.kind != tb.MESSAGE for f in result.findings)


def test_known_token_rescues_a_schema_column():
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "KeyError: 'revenue'"
    )
    rescued = _san(text, known="- revenue: Int64\n- cost: Int64")
    assert "KeyError: 'revenue'" in rescued.text
    redacted = _san(text, known="- amount: Int64")
    assert "revenue" not in redacted.text
    assert "KeyError: <redacted: 9 chars>" in redacted.text


def test_known_token_rescue_covers_quoted_multiword_tokens():
    # A column name with a space only exists as a QUOTED token in the notebook
    # source — the tokenizer must pick it up whole.
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "KeyError: 'net revenue'"
    )
    result = _san(text, known='df = df.select("net revenue")')
    assert "KeyError: 'net revenue'" in result.text


def test_partially_known_quoted_tokens_do_not_rescue():
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        f"SomeError: 'revenue' and '{SECRET}'"
    )
    result = _san(text, known="revenue")
    assert SECRET not in result.text


def test_known_tokens_do_not_rescue_long_digit_runs_in_the_residue():
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "SomeError: account 40128888 rejected for 'revenue'"
    )
    result = _san(text, known="revenue")
    assert "40128888" not in result.text


def test_alphabetic_residue_values_are_not_rescued_by_one_known_quoted_token():
    # 'balance' is a schema column the model already knows — but the unquoted
    # remainder carries a customer NAME from an f-string message. One known
    # quoted word must not rescue the whole message (the "value-safe" label
    # would forward "Jane Doe" verbatim).
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "ValueError: customer Jane Doe exceeds limit in field 'balance'"
    )
    result = _san(text, known="- balance: Int64\n- cost: Int64")
    assert "Jane" not in result.text
    assert "ValueError: <redacted:" in result.text


def test_grouped_digit_residue_is_not_rescued():
    # Thousands separators keep every digit run under 4 chars, so the long-run
    # check alone misses 1,234,567 — the grouped-digit check must catch it even
    # when every word in the residue is already known.
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "SomeError: balance 1,234,567 exceeds limit for 'balance'"
    )
    result = _san(text, known="the balance exceeds this limit for that field")
    assert "1,234,567" not in result.text


def test_known_residue_words_still_rescue_a_template_message():
    # The rescue is not dead: when the quoted token AND every longish residue
    # word are already in-channel, the message survives verbatim.
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        "KeyError: column 'revenue' not found in the frame"
    )
    result = _san(text, known="the column revenue was not found in a frame")
    assert "KeyError: column 'revenue' not found in the frame" in result.text
    assert all(f.kind != tb.MESSAGE for f in result.findings)


def test_non_ascii_message_is_redacted_with_char_count():
    message = "could not convert string to float: '£1,234'"
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\lib.py", line 2, in f\n'
        f"ValueError: {message}"
    )
    result = _san(text)
    assert "£1,234" not in result.text
    assert f"ValueError: <redacted: {len(message)} chars>" in result.text


# -- real-world shapes --------------------------------------------------------------


def test_chained_traceback_keeps_the_fixed_separators():
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\a.py", line 1, in <module>\n'
        f"ValueError: {SECRET}\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\a.py", line 3, in <module>\n'
        "TypeError: bang bang"
    )
    result = _san(text)
    assert SECRET not in result.text
    assert "During handling of the above exception, another exception occurred:" in result.text
    assert "ValueError: <redacted:" in result.text and "TypeError: <redacted:" in result.text
    assert tb.detect(text) == [(1, 9)]  # one block, separators inside


def test_syntaxerror_caret_lines_are_removed_not_redacted(tmp_path):
    (tmp_path / "nb.py").write_text("def f(:\n", "utf-8")
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "nb.py"}", line 1\n'
        "    def f(:\n"
        "          ^\n"
        "SyntaxError: invalid syntax"
    )
    result = _san(text, workspace=tmp_path)
    assert "SyntaxError: invalid syntax" in result.text
    assert "^" not in result.text  # dropped, not turned into a <redacted line>
    assert tb.REDACTED_LINE not in result.text


def test_exception_group_rails_and_margins_survive():
    text = (
        "  + Exception Group Traceback (most recent call last):\n"
        '  |   File "C:\\other\\eg.py", line 3, in <module>\n'
        f'  |     raise ExceptionGroup("eg", [ValueError("{SECRET}")])\n'
        "  | ExceptionGroup: eg (2 sub-exceptions)\n"
        "  +-+---------------- 1 ----------------\n"
        "    | Traceback (most recent call last):\n"
        '    |   File "C:\\other\\eg.py", line 1, in <module>\n'
        f"    | ValueError: {SECRET}\n"
        "    +------------------------------------"
    )
    result = _san(text)
    assert SECRET not in result.text
    assert "Exception Group Traceback (most recent call last):" in result.text
    assert "+-+---------------- 1 ----------------" in result.text
    assert "    | ValueError: <redacted:" in result.text  # margin kept, message gone


def test_repeated_frame_marker_survives():
    text = (
        "Traceback (most recent call last):\n"
        '  File "C:\\other\\r.py", line 5, in loop\n'
        "  [Previous line repeated 996 more times]\n"
        "RecursionError: maximum recursion depth exceeded"
    )
    result = _san(text)
    assert "[Previous line repeated 996 more times]" in result.text
    assert "RecursionError: maximum recursion depth exceeded" in result.text


def test_marimo_cell_frame_is_a_workspace_frame(tmp_path):
    # marimo runs the notebook file itself; a cell frame points at the .py in the
    # workspace with the cell function named "_". The paste showed NO source line
    # under the frame, so none is inserted (the sanitiser never ADDS text the
    # paste didn't contain — that would be a disk-read channel; see below).
    (tmp_path / "nb.py").write_text("import marimo\ndf = load()\n", "utf-8")
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "nb.py"}", line 2, in _\n'
        "ArrowInvalid: boom boom"
    )
    result = _san(text, workspace=tmp_path)
    assert 'File "nb.py", line 2, in _' in result.text
    assert "df = load()" not in result.text


def test_posix_paths_reduce_to_basename():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.12/json/decoder.py", line 355, in raw_decode\n'
        "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
    )
    result = _san(text)
    assert 'File "decoder.py", line 355, in raw_decode' in result.text
    assert "/usr/lib" not in result.text


def test_angle_bracket_pseudo_files_survive():
    text = (
        "Traceback (most recent call last):\n"
        '  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load\n'
        "ModuleNotFoundError: No module named 'polars'"
    )
    result = _san(text)
    assert '"<frozen importlib._bootstrap>"' in result.text


# -- the offline CLI (`mooring ai traceback check`) ---------------------------------


def test_cli_check_prints_the_rewrite(cfg, tmp_path, capsys):
    paste = tmp_path / "paste.txt"
    paste.write_text(_plain_tb(), "utf-8")
    args = argparse.Namespace(file=str(paste))
    from mooring import cli

    code = cli.cmd_ai_traceback_check(config.AppConfig(), cfg, args)
    out = capsys.readouterr().out
    assert code == 0
    assert SECRET not in out
    assert "KeyError: <redacted:" in out
    assert "redactions:" in out


def test_cli_check_reports_no_traceback(cfg, tmp_path, capsys):
    paste = tmp_path / "paste.txt"
    paste.write_text("just a question about polars\n", "utf-8")
    args = argparse.Namespace(file=str(paste))
    from mooring import cli

    code = cli.cmd_ai_traceback_check(config.AppConfig(), cfg, args)
    assert code == 0
    assert "No traceback detected" in capsys.readouterr().out


def test_cli_check_reads_powershell_utf16_files(cfg, tmp_path, capsys):
    # PowerShell 5.1's `python x.py 2> tb.txt` / Out-File write UTF-16 LE — the
    # very files users point this command at. It must sanitise them, not die
    # with a raw UnicodeDecodeError (the no-raw-tracebacks command's own crash).
    paste = tmp_path / "tb16.txt"
    paste.write_bytes(_plain_tb().encode("utf-16"))  # BOM'd UTF-16, PS-style
    args = argparse.Namespace(file=str(paste))
    from mooring import cli

    code = cli.cmd_ai_traceback_check(config.AppConfig(), cfg, args)
    out = capsys.readouterr().out
    assert code == 0
    assert SECRET not in out
    assert "KeyError: <redacted:" in out


def test_cli_check_degrades_kindly_on_binary_files(cfg, tmp_path, capsys):
    blob = tmp_path / "not-text.bin"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")  # 11 bytes: bad UTF-8, odd for UTF-16
    args = argparse.Namespace(file=str(blob))
    from mooring import cli

    code = cli.cmd_ai_traceback_check(config.AppConfig(), cfg, args)
    out = capsys.readouterr().out
    assert code == 1
    assert "not UTF-8 or UTF-16 text" in out
