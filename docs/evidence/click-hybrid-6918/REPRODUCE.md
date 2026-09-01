# click 8.5.1 — first external-repo hybrid reduction

- baseline: pristine clone at `36baa15` ("Start 8.5.1"), editable-installed
  into the host venv; `qwen2.5-coder:7b` via ollama on a RTX 3070 (8 GB)
- result: **6973 -> 6918 canonical code-LOC = 0.79%** (static 0.62% + LLM
  0.17%), `tests_ok: true` (1991 passed / 24 skipped on the reduced tree),
  `api_ok: true`, **all 93 core.py docstrings preserved verbatim**
- budget: 40 LLM calls, `--attempts 2 --num-ctx 16384`, ~35 min wall
- outcomes: 5 accepted, 20 tests-failed, 11 not-smaller, 2 docs-lost,
  2 bad-dedup-reply (+25 budget-exhausted skips over remaining files)
- trust scaling: `_compat.py`, `_winconsole.py`, `_textwrap.py`,
  `__init__.py` excluded from the LLM layer via `--skip-files` — the
  mutation audit (score 0.626, 25 mutants/file) shows their behavior is
  invisible to the suite on linux (platform-dead + re-export code)

## Recipe

```bash
git clone https://github.com/pallets/click && cd click
git checkout 36baa15
uv pip install -e .   # into the less-code host venv
PATH=<less-code>/.venv/bin:$PATH lc reduce . \
    --model qwen2.5-coder:7b \
    --attempts 2 --max-llm-calls 40 --num-ctx 16384 \
    --skip-files _compat.py,_winconsole.py,_textwrap.py,__init__.py \
    --out reduce.json
```

LLM rounds are non-deterministic; the preserved tree in this directory is
the evidence. Run `python -m pytest tests` from here (with the package
editable-installed) to re-verify both gates.

## What this run changed in less-code itself

The first (unguarded) attempt looked better — 0.93% — but the diff showed
the 7B "reduces" a mature library mostly by deleting its published
documentation: `ParameterSource` lost its whole class docstring and every
enum-member docstring, `#:` Sphinx comments vanished. Doc loss is invisible
to the suite and free in code-LOC, so every gate was green while the diff
ate the docs. Two fixes landed from this:

1. **docs-preservation pre-gate** (`docs_lost` in `llm_reduce.py`): python
   docstrings (AST multiset) and js/ts `/** */` / rust `///` comments must
   survive every L2 candidate — whole-file, per-symbol, salvage and dedup
   paths. Cheaper than the parse gate; before any disk write; rejection
   feeds an explicit keep-the-docstrings instruction back into the retry.
2. **prompt rule 4** in all three system prompts: docstrings are published
   documentation, not code to minimize.

## The honest read

- static-only on a mature, lint-clean repo: ~0.6% (dead code, rules,
  outline, ruff all bottom out — click already runs `ruff fix` in CI)
- 7B per-symbol on the same repo: +0.17% for 40 calls (~9 LoC accepted
  per 100 spent on tests-failed)
- the gate stack (tests + API + docs + budget + trust-skip) held: zero
  behavior regressions, zero doc losses, public API byte-identical

External-repo yield at 7B is real but small; the levers that move it are
characterization tests (raise the 0.626 trust score), best-of-N search over
candidates (B5/B6: click's suite runs in 4 s — parallel verification is
cheap), and a stronger proposer model via the openai-compat backend.
