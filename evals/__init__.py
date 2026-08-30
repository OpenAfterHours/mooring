"""The model-capability eval for mooring's copilot — "can the model I'm allowed
to use actually do this job?"

Not a test suite. Nothing in here is collected by pytest (see ``README.md``): a
run needs a real model, which needs network and an API key, and the repo's suite
is offline by contract. The runner is ``scripts/eval.py``.

What makes this eval possible at all is the copilot's value-blindness. The model
is sent only schema (column names + dtypes) and authored code, so a case needs no
real data — a fixture notebook plus a synthetic header row is the whole world —
and it produces only a PROPOSAL, which mooring can compose into a candidate
notebook and check **statically**. So the scoring function is
:func:`mooring.marimo_rt.validate_notebook_source` plus a handful of structural
predicates: no live kernel, no data, and no LLM judge anywhere.

The modules:

* :mod:`evals.fixtures` — the synthetic workspaces (notebooks + CSV headers).
* :mod:`evals.checks`   — the static scoring vocabulary.
* :mod:`evals.cases`    — the case registry, six buckets.
* :mod:`evals.harness`  — driving one case and scoring it into a capability card.
* :mod:`evals.fake`     — a scripted provider that replays recorded model output,
  so the harness's own logic is CI-tested offline (``tests/test_eval_harness.py``).
* :mod:`evals.providers` — the real-model and scripted session openers.
"""
