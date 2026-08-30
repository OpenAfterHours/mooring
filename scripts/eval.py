#!/usr/bin/env python3
"""Run mooring's model-capability eval and print a capability card.

Answers the question an analyst actually asks — "can the model I'm allowed to use
do this job?" — by putting a model through ~30 cases in six buckets, each isolating
one known way a weaker model breaks a marimo notebook, and scoring every answer
STATICALLY with mooring's own notebook validator. No data, no live kernel, no LLM
judge. See evals/README.md.

    uv run python scripts/eval.py --model gpt-5.1
    uv run python scripts/eval.py --model gpt-4o-mini --repeat 3
    uv run python scripts/eval.py --model gpt-5.1 --bucket dag,repair
    uv run python scripts/eval.py --model gpt-5.1 --case no-cycle --json
    uv run python scripts/eval.py --list

Needs a signed-in provider: `--provider openai` reads MOORING_OPENAI_API_KEY (or
the keyring, like the app), `--provider copilot` needs `mooring ai login`. This is
NOT part of CI — it costs money and needs network. The harness's own logic is
tested offline in tests/test_eval_harness.py.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import cases as case_registry  # noqa: E402
from evals.harness import Card, DEFAULT_TURN_TIMEOUT, render_card, run_case  # noqa: E402
from evals.providers import preflight, real_opener  # noqa: E402


def _color_enabled() -> bool:
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            kernel32.SetConsoleMode(ctypes.c_void_p(handle), 7)
        except Exception:
            return False
    return True


_COLOR = _color_enabled()


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def fail(message: str) -> NoReturn:
    print(_paint(f"ERROR: {message}", "31"), file=sys.stderr)
    raise SystemExit(1)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part for part in (value or "").replace(" ", ",").split(",") if part)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the mooring copilot capability eval against one model.",
    )
    parser.add_argument("--model", default="", help="Model id under test (e.g. gpt-5.1).")
    parser.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "copilot"],
        help="Which mooring AI provider to open the session through (default: openai).",
    )
    parser.add_argument(
        "--effort", default="", help="Reasoning effort to request, when the model takes one."
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="An OpenAI-compatible gateway to test through (the org-gateway case).",
    )
    parser.add_argument(
        "--api-version", default="", help="Azure OpenAI api-version, for a classic deployment."
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Sweep without checking the provider is signed in first.",
    )
    parser.add_argument(
        "--bucket",
        default="",
        help=f"Only these buckets, comma-separated. One of: {', '.join(case_registry.BUCKETS)}.",
    )
    parser.add_argument(
        "--case",
        default="",
        help="Only these cases, comma-separated. Matches a full id or a substring.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Runs per case (default 1). Models are stochastic; one run is not a measurement.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TURN_TIMEOUT,
        help="Seconds one turn may take before the run is abandoned "
        f"(default {DEFAULT_TURN_TIMEOUT:.0f}).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the structured result instead of the card."
    )
    parser.add_argument(
        "--json-out", metavar="PATH", default="", help="Also write the structured result here."
    )
    parser.add_argument("--list", action="store_true", help="List the cases and exit.")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return args


def _list_cases() -> int:
    for bucket in case_registry.BUCKETS:
        picked = case_registry.select(buckets=(bucket,))
        print(f"\n{bucket}  ({len(picked)} cases)")
        for case in picked:
            print(f"  {case.id:<36} {case.turns[0][:60]}")
    print(f"\n{len(case_registry.CASES)} cases total.")
    return 0


def main() -> int:
    args = _parse_args()
    if args.list:
        return _list_cases()

    picked = case_registry.select(case_ids=_csv(args.case), buckets=_csv(args.bucket))
    if not picked:
        fail("No cases matched the --bucket / --case filters (try --list).")

    # One check, before the sweep. Without it an uninstalled extra or an unset key
    # reads as every case failing — a capability card saying the model can do
    # nothing, when the truth is that it was never asked.
    if not args.skip_preflight:
        blocked = preflight(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_version=args.api_version,
        )
        if blocked:
            fail(f"Cannot run the sweep: {blocked}")

    opener = real_opener(
        provider=args.provider,
        model=args.model,
        reasoning_effort=args.effort,
        base_url=args.base_url,
        api_version=args.api_version,
    )
    total = len(picked) * args.repeat
    if not args.json:
        print(
            f"Running {total} run(s): {len(picked)} case(s) x {args.repeat} "
            f"against {args.model or '(provider default)'} via {args.provider}."
        )

    results = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mooring-eval-") as root:
        for attempt in range(args.repeat):
            for case in picked:
                result = run_case(case, opener, root=Path(root), turn_timeout=args.timeout)
                results.append(result)
                if not args.json:
                    mark = _paint("PASS", "32") if result.passed else _paint("FAIL", "31")
                    suffix = "" if result.passed else f"  {result.failures[0].reason[:70]}"
                    print(
                        f"  [{len(results):>3}/{total}] {mark} {case.id:<36} "
                        f"{result.seconds:5.1f}s{suffix}"
                    )
                    if attempt == 0 and result.error:
                        print(f"        session: {result.error[:100]}")

    card = Card(
        model=args.model, provider=args.provider, repeat=args.repeat, results=tuple(results)
    )
    if args.json_out:
        Path(args.json_out).write_text(card.as_json(), "utf-8", newline="\n")
    if args.json:
        print(card.as_json())
    else:
        print(render_card(card))
        print(f"  Swept in {time.monotonic() - started:.0f}s.")
        if args.json_out:
            print(f"  JSON written to {args.json_out}")
    # A capability card is a measurement, not a gate: a weak model is a finding, not
    # a broken run. Only a harness/session failure is a non-zero exit.
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
