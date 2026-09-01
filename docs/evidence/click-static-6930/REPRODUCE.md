# click 8.5.1 — static-only reduction (first external-repo row)

- baseline: `git clone` at `36baa15` ("Start 8.5.1"), editable-installed into
  the host venv (shadowed-imports preflight green)
- result: **6973 -> 6930 canonical code-LOC = 0.62%**, `tests_ok: true`
  (1991 passed, 24 skipped on the reduced tree), `api_ok: true`,
  0 LLM calls, ~90 s wall
- tree in this directory is the exact reduced state (plus `static.json`,
  the report); run the suite with `python -m pytest tests` from here with
  the package editable-installed.

## Recipe

```bash
git clone https://github.com/pallets/click && cd click
git checkout 36baa15
uv pip install -e .   # into the less-code host venv
PATH=<less-code>/.venv/bin:$PATH lc reduce . --static-only --out static.json
```

## Why the yield is near zero — and why that is the finding

The py fixture's 33.5% static-only came from dead code, guard outlining and
the rule library. click has none of that headroom:

- **no dead code**: every top-level symbol is referenced (mature library)
- **lint-clean**: click runs `ruff` with `fix = true` in CI, so the safe and
  most unsafe fixes are already applied upstream
- **no repeated guards**: validation exists, but messages differ per site,
  so no >= 3-site group survives alpha-renaming + constant abstraction
  (only `shell_completion.py` had a 3-site group worth 1 line)

The two defects this run surfaced (both fixed in `less_code/outline.py`):

1. cross-module outlining emitted absolute imports (`from _textwrap import
   _check`) — `ModuleNotFoundError` under any src-layout package
2. outlining hoisted a *new* module-level import edge into `core.py`, which
   broke `test_light_imports` (click pins `import click` to a stdlib
   allowlist; `_textwrap` is deliberately lazy). Cross-module outlining now
   only travels along import edges that already exist.

**Conclusion**: on mature, lint-clean, well-tested repos the deterministic
layers bottom out near zero; the LLM semantic layer (verify-gated) is where
external-repo yield must come from. click's suite mutation score is 0.626
(216/345, `--max-mutants 25`), so the trust oracle matters before letting
it be aggressive.
