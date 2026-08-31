# The model-capability eval

**"Can the model I'm allowed to use actually do this job?"**

Analysts don't all get to pick their model. Some are on whatever their org's
gateway offers; some are on a small local model. mooring's copilot behaves very
differently across them, and until now the only way to find out was to hand
someone a broken notebook.

This eval answers the question directly: it puts a model through ~30 cases, each
isolating one known way a weaker model breaks a marimo notebook, and prints a
**capability card** — a per-bucket pass rate that says *what* the model is good
and bad at, not just a single number.

It is **not part of CI.** A run needs network, an API key, and money. The
harness's own logic *is* covered offline, by `tests/test_eval_harness.py`.

## Running it

```bash
uv sync --extra openai                                  # or --extra copilot
uv run python scripts/eval.py --list                    # what the cases are
uv run python scripts/eval.py --model gpt-5.1
uv run python scripts/eval.py --model gpt-4o-mini --repeat 3
uv run python scripts/eval.py --model gpt-5.1 --bucket dag,repair
uv run python scripts/eval.py --model gpt-5.1 --case no-cycle --json
```

| flag | what it does |
| --- | --- |
| `--model` | the model id under test. Omit to use the provider's default. |
| `--provider` | `openai` (default) or `copilot`. Opens the session through mooring's own provider seam. |
| `--effort` | reasoning effort to request, when the model takes one. |
| `--base-url` / `--api-version` | an OpenAI-compatible gateway, or an Azure deployment — the case this eval exists for. |
| `--bucket` / `--case` | filters, comma-separated. `--case` matches a full id or a substring. |
| `--repeat` | runs per case. **Use it.** Models are stochastic; one run is not a measurement. 3 is a sensible floor, 5 for a decision. |
| `--timeout` | seconds one turn may take before the run is abandoned (default 240). |
| `--json` / `--json-out` | structured output, for tracking a model across versions. |
| `--skip-preflight` | sweep without checking the provider is signed in first. |

Credentials are resolved exactly as the app resolves them — `MOORING_OPENAI_API_KEY`
or the keyring for `--provider openai`, `mooring ai login` for `--provider copilot`.
The eval never reads your `config.toml`: the model under test is the one you named,
not the one you happen to have configured. A preflight check runs once before the
sweep, because a missing extra or an unset key would otherwise read as every case
failing — a card saying the model can do nothing, when it was never asked.

**A sweep costs money.** 32 cases x `--repeat 3` is ~96 model turns plus their tool
loops. Start with `--case` or `--bucket` on one case to confirm your credentials
work before committing to a full sweep.

## Reading a capability card

```
  Capability card: gpt-4o-mini via openai
  96 runs | 3 per case | 32 cases

  bucket             cases  runs  pass   rate
  -------------------------------------------------------
  format                 5    15    14   93%   #########.
  tool choice            7    21    10   48%   #####.....
  DAG hygiene            6    18     6   33%   ###.......
  schema fidelity        5    15    12   80%   ########..
  sql cells              4    12     9   75%   ########..
  repair                 5    15     5   33%   ###.......
  -------------------------------------------------------
  OVERALL               32    96    56   58%   ######....

  Weakest: DAG hygiene (33%).
  The propose gate refused 24 proposal(s) across the sweep: each one a break the
  analyst never saw.
```

Read it bucket by bucket, not by the overall number:

* **`format` high, everything else low** — the model can write a cell but not
  reason about the notebook. Usable for "add a cell that…", not for edits.
* **`tool choice` low** — either it appends near-duplicates instead of editing, or
  it cannot hold an index: the write tool makes every edit and
  delete carry an `expect` (what the model believes is at that index), and a wrong
  or missing one is refused outright. A low score here with a **high refusal count**
  means the model is guessing indices rather than reading the notebook — nothing
  reaches the analyst, which is the safe failure, but nothing gets done either.
* **`DAG hygiene` low** — it treats marimo as a script. This is the one that
  actually breaks notebooks: a redefined name stops the cell *and everything
  downstream of it*.
* **`repair` low with a high refusal count** — the gate is catching the breakage
  (good) but the model cannot act on the diagnostics it is handed (bad). It will
  loop and give up rather than converge.
* **`schema fidelity` low** — it invents columns. The most expensive failure to
  spot by eye, because the code looks perfectly reasonable.

The refusal line is worth its own look: every refusal is a proposal the propose
gate held back, i.e. a broken notebook the analyst never saw. A high refusal count
with a high `repair` rate is a *healthy* combination.

## The buckets

A bucket is a **cause**, not a topic. "The copilot is bad on this model" is
useless; "it emits valid cells but redefines names other cells own" is actionable.
Each case is written to fail for one reason, and cases that would fail for two
reasons at once were split.

| bucket | the question | the failure it isolates |
| --- | --- | --- |
| `format` | Does a proposal come back at all, in the body-only shape mooring writes? | Answering a code request with a prose ```` ```python ```` fence, or pasting the `@app.cell` wrapper back. |
| `tool` | There is **one** write tool, so: does the change go in the right **field**, and can the model say what it believes is at the index it aims at (`expect`)? | Appending a near-duplicate instead of editing; guessing an index, so `expect` names a cell it never meant. |
| `dag` | marimo is a dataflow graph, not a script. | Redefining a name another cell owns → `MB002 multiple-definitions`. |
| `schema` | Does it use the columns it was shown — right names, right case — and polars rather than pandas? | Invented columns; `df.groupby(...)` on a polars frame. |
| `sql` | Can it author a valid, read-only `mo.sql` cell? | A `DELETE`; a `PIVOT` (which turns data values into column names). |
| `repair` | Handed a diagnostic, does it fix the problem within two turns? | Re-proposing the same broken cell until the budget runs out. |

`repair` is the bucket that measures **mooring** rather than the model: everything
else scores what a model produces, `repair` scores whether telling it what is wrong
actually helps.

## How scoring works

Every check is **static**. The eval composes the notebook a proposal would produce
— through `cellwrite.apply_wire_patch`, the same call the hub's Apply endpoint
makes — and runs `marimo_rt.validate_notebook_source` on it, the same checker the
propose gate uses. So a case's verdict and the copilot's in-loop diagnostics can
never disagree about whether a notebook works.

Nothing runs a cell. Nothing reads a data value. **There is no LLM judge.** That
is possible only because the copilot is value-blind: the model is sent schema and
authored code, so a case needs no real data, and it produces a *proposal*, which
is text that can be checked.

On top of the validator, `evals/checks.py` adds structural predicates: was a
proposal emitted; is each cell body free of `@app.cell` / `def _(` / a trailing
`return`; does each body parse; did it target the right cell index; are all
referenced columns in the schema; is the SQL read-only.

### Silence is not a decline

Four cases — `dag/no-cycle`, `schema/no-invented-column`, `sql/read-only`,
`sql/no-pivot` — are ones where **declining is a correct answer**: the request is
impossible, or asks for something the copilot should refuse. Their structural
checks are wrapped in `if_proposed(...)`, which passes when nothing was proposed.

That combinator alone is not enough, and getting this wrong is the single most
damaging bug this eval can have. Every predicate here is *vacuously true over an
empty proposal* — no invented column, no destructive SQL, no dependency cycle — so
a case built only from `if_proposed` is passed by a model with no tool-calling
ability whatsoever. It scored 12.5% instead of 0% and its card credited it with
four correct declines. **A flattering eval is worse than no eval.**

So each of those four also carries:

* `answered()` — a proposal, or words. Kills silence outright.
* `declined_explaining(*terms)` — when nothing was proposed, the reply must mention
  the vocabulary of the **constraint** (`cycle`/`circular`, `exist`/`available`,
  `read-only`/`delete`, `pivot`/`crosstab`), never of the request. A reply that
  hands back code without noticing the constraint matches none of them.

`declined_explaining` is the one place the eval reads a model's prose, and it is a
keyword heuristic. What makes that acceptable is the gate: it runs **only when
nothing was proposed**, so it can never fail a model that did the work. Its entire
blast radius is the population where the eval otherwise cannot tell a reasoned
decline from a shrug.

Three tests hold the line, and two of them audit the **whole** registry rather than
the four known cases — enumerating them by hand is how the first four were missed:
`test_a_silent_model_passes_nothing`, `test_a_prose_only_model_passes_nothing`, and
`test_no_case_is_built_only_from_vacuous_checks`. A fourth,
`test_a_reasoned_decline_still_passes`, keeps the intended reading alive so the fix
cannot degenerate into "declining always fails".

The card reports unanswered runs on their own line rather than folding them into
the rate — a model that cannot call tools looks merely cautious until you count
them.

Two checks are honest heuristics rather than proofs, and are scoped to one
bucket each:

* **`columns-in-schema`** collects string literals in unambiguous *column
  positions* (`pl.col("x")`, `df["x"]`, `select`/`group_by`/`join(on=)`, …) and
  ignores anything it cannot confidently place. It is tuned to under-report: a
  missed reference is a lenient pass, a caught invention is unambiguous. It does
  not read inside a SQL string.
* **`polars-api`** flags a short closed list of pandas method names that have a
  *different* polars spelling (`groupby`, `iloc`, `astype`, `fillna`, …), so a hit
  is a real `AttributeError` waiting to happen, not a style opinion.

### Two things worth knowing about how a run is scored

**The candidate is the LAST proposal of the run, applied to the base notebook.**
That is what an analyst who waited for the model to settle would apply — so a
model that corrects itself is judged on the correction. Earlier proposals still
count: they are what the `proposals` and `refusals` numbers report.

**A pre-existing fault is never blamed on the model.** The `already_broken`
fixture ships with a real `MB002`, and diagnostics are attributed by *count*, not
membership — mirroring `ai/tools.py`'s `_split_by_blame`, so a third definition of
an already-duplicated name still reads as introduced.

## Why it isn't in CI, and how that's enforced

Three independent things keep `evals/` out of the pytest suite, so no single edit
can drag it in:

1. `testpaths = ["tests"]` — a bare `uv run pytest` never looks outside `tests/`.
2. `addopts = "--ignore=evals"` — an explicit `pytest .` at the repo root skips it.
3. **Naming** — no file here matches pytest's `python_files` (`test_*.py` /
   `*_test.py`) and no function is named `test_*`, so there is nothing to collect
   even if the config were removed. `pytest evals` collects zero items.

`pythonpath = ["."]` in the same block is what lets `tests/test_eval_harness.py`
import this package.

## The offline half: how the harness itself is checked

The scoring is ordinary code and can be ordinary-code wrong. A predicate that
never fires would report a weak model as capable, and it would look exactly like a
true finding — the only way to catch it would be to run a weak model.

So `evals/fake.py` is a **scripted provider** that replays a recorded model output
with no network and no `openai` package. It replaces the HTTP client, not the
session: a scripted turn is fed to a real `OpenAIChatSession` through its
`client_factory` seam, so the real tool loop, the real value-free handlers, the
real propose gate and the real egress minters all run. Nothing is stubbed except
the model. (A fake that emitted proposal *events* directly would score the harness
against a pipeline the product does not have, and would sail past a regression in
the gate.)

`tests/test_eval_harness.py` uses it to pin, offline:

* the seven catalogued weak-model outputs, each scored to its known verdict — both
  end to end through the gate, and through the checks alone with the gate out of
  the way (which is what a regression in `ai/tools.py` would look like);
* a **golden correct answer for every case**, so no case can be unwinnable;
* a weak model, a **silent** model and a **prose-only** model that each score 0/32,
  so the harness cannot pass everything — and cannot credit silence as a decline;
* a **reasoned decline** that still passes, so the above cannot degenerate into
  "declining always fails";
* the `expect` refusals — an absent claim, a stale one, and a rewrite that
  misstates the cell count;
* the pre-existing-fault rule, the tool-choice predicates, and the card renderer;
* that no check can return a non-ASCII failure reason (a static scan, so it also
  covers the rare messages only a genuinely broken model would trigger).

### Scripting `expect`

`fake.propose_cell_edit(index, expect, code)` takes `expect` as a **required
positional**, and `fake.propose_notebook_rewrite(cells, expect_cells)` likewise.
The fake deliberately does *not* look the line up in the fixture: `expect` is a
claim the model has to earn by reading, so handing it over for free would make
"the model got the index wrong" unscriptable — which is the failure the `tool`
bucket now exists to measure.

Golden answers derive their claim with `fixtures.first_line(notebook, index)` /
`fixtures.cell_count(notebook)`, so editing a fixture cell keeps every golden that
targets it correct. A test that wants a model to get `expect` *wrong* passes its
own literal.

## Adding a case

1. Add a fixture to `evals/fixtures.py` if an existing notebook doesn't fit.
   Fixtures are **fabricated** — a case is sent to a real model, so a fixture
   carrying real column names would be exactly the egress the copilot exists to
   prevent. Notebooks are composed from cell bodies through marimo's own codegen,
   never hand-written.
2. Add the `Case` to `evals/cases.py`, in the bucket matching the *cause* it
   isolates. If it can fail for two reasons, split it.
3. If the case can be passed by *declining*, give it `answered()` and
   `declined_explaining(...)` — see [Silence is not a decline](#silence-is-not-a-decline).
   Never build a case out of `if_proposed` alone.
4. Add a golden answer to `GOLDEN` in `tests/test_eval_harness.py` and run
   `uv run pytest tests/test_eval_harness.py -q`. A case with no golden fails
   `test_a_golden_answer_exists_for_every_case`, an unwinnable one fails
   `test_every_case_passes_on_a_correct_answer`, and one a silent model can pass
   fails `test_a_silent_model_passes_nothing`.
