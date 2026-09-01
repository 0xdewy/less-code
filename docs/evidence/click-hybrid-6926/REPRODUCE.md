# click 8.5.1 — first external-repo hybrid reduction (docs fully preserved)

- baseline: pristine clone at `36baa15`, editable-installed into the host
  venv; `qwen2.5-coder:7b` via ollama on a RTX 3070 (8 GB)
- result: **6973 -> 6926 canonical code-LOC = 0.67%** (static 0.62% + LLM
  0.06%), `tests_ok: true` (1991 passed / 24 skipped), `api_ok: true`,
  **zero docstring and zero comment lines lost** (verified per changed file)
- budget: 40 LLM calls, `--attempts 2 --num-ctx 16384`, `--skip-files
  _compat.py,_winconsole.py,_textwrap.py,__init__.py` (per the 0.626
  mutation audit: their behavior is invisible to the suite on linux)
- outcomes: **2 accepted, 16 docs-lost (40% of all proposals!), 13
  not-smaller, 7 tests-failed, 2 bad-dedup-reply** (+25 budget-exhausted)

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

LLM rounds are non-deterministic; the preserved tree here is the evidence
(`python -m pytest tests` with the package editable-installed re-verifies
both gates).

## The three-run doc story (why this tree supersedes click-hybrid-6918)

| run | docs gate | hybrid % | what the model actually did |
|---|---|---|---|
| 1 | none | 0.93 | deleted ParameterSource's docstrings + `#:` Sphinx docs wholesale |
| 2 | docstrings | 0.79 | docstrings kept, but 10 `#:` attribute docs + issue-ref comments still eaten |
| 3 | docstrings + all `#` comments | **0.67** | 40% of proposals *were* doc-eating and are now rejected |

Two conclusions, both load-bearing for the roadmap:

1. **Docs are outside the metric AND touching them rejects.** code-LOC never
   counted docstrings or comments; now any deletion or rewording of them
   (python AST docstrings + every `#` line, js `/** */`, rust `///`) fails
   the cheapest pre-gate before a disk write, on every L2 path.
2. **The 7B's default minimization strategy on mature code is documentation
   destruction.** With that off the table, free-form per-symbol proposals
   are ~5% productive (2/40). The remaining yield must come from
   deterministic rules mined from accepted rewrites (the accepted ones are
   exactly ruff's non-autofixed gap: else-collapse, dict-merge, `or {}`),
   mechanical dedup from the computed token diff, yield-ranked scheduling
   (13/17 files never saw an LLM call), and characterization tests that
   raise the 0.626 trust score. That is docs/ROADMAP.md Phase H.
