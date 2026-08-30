# Static LOC-Reduction Survey

Scope: non-LLM, static lines-of-code reduction and code-size analysis for Rust, JavaScript/TypeScript, and Python. All tool claims cite the numbered sources in **Sources**. "Est." marks estimates, not measured claims.

## Per-language tool table

### Rust

| Tool | What it removes/detects | Safety (compile/test-verified?) | LOC impact estimate |
|---|---|---|---|
| `rustc` `dead_code` lint (warn-by-default) | Unused, unexported items [4] | Compile-verified after deletion; documented caveat: removing fields kept only for side effects (e.g. Drop) changes behavior [4] | Small–medium; per-item lines accumulate |
| `rustc` `unused_imports` / `unused_macros` / `unused_variables` (warn-by-default); `redundant_imports` (allow-by-default) [4][5] | Unused imports, macros, variables, redundant `use`s | Compile-verified; `cargo fix` auto-applies compiler-suggested fixes [5] | Small (line/import-level) |
| `rustc` `unreachable_pub`, `unused_crate_dependencies`, `dead_code_pub_in_binary` (allow-by-default) | Unreachable pub items, unused crate deps, unused `pub` in binaries [5] | Compile-verified once enabled | Small–medium in binaries |
| Clippy (`cargo clippy`) | 800+ lints; `correctness` (deny), `suspicious`/`style`/`complexity`/`perf` (warn) [6] | Suggestions applied via `cargo clippy --fix` then recompiled | Small; mostly cosmetic-to-modest shrink |
| cargo-machete | Unused dependencies in `Cargo.toml`; fast but "yet imprecise" (text scan); false positives for build.rs-generated and renamed crates; `--with-metadata` more accurate [2] | Rebuild verifies (removing a truly used dep breaks compile) | ~0 source LOC; removes manifest lines, build time, supply-chain surface |
| cargo-udeps | Unused dependencies via `cargo +nightly udeps`; some false negatives (crates used by std or by your deps) [3] | Same rebuild gate as machete | Same as machete |
| cargo-mutants | Mutation testing (oracle, not a shrinker) — see below [18] | Test-verified | n/a (enables risky shrinks) |

### JavaScript / TypeScript

| Tool | What it removes/detects | Safety | LOC impact estimate |
|---|---|---|---|
| Knip | Unused dependencies, exports, and files; module-graph analysis from 150+ framework/tooling plugins for accurate entry points [1] | No compile gate in JS; deletions verified by `tsc` + test suite; reports "no false positives" are user testimonials, not guarantees [1] | High on first run in mature repos — user reports: 6k LOC in 30 min, 41k in a legacy codebase, ~300k at Vercel [1]; near-zero on clean repos (Est.) |
| ESLint `--fix` | Auto-fixes fixable rule violations; `--fix-dry-run`; `--fix-type problem|suggestion|layout|directive` scopes fixes; "not all problems are fixable" [7] | Fix semantics per rule; `layout` fixes don't change the AST [7] | Small; mostly cosmetic |
| Biome formatter (`biome format --write`) | Opinionated Prettier-style formatting; lineWidth 80, indent 2 defaults [8] | Layout-only by construction | Cosmetic only (line wrapping churn) |
| dprint (`dprint fmt`) | Pluggable Rust+Wasm formatting platform; milliseconds for hundreds of files [9] | Layout-only by construction | Cosmetic only |
| jscpd | Copy/paste detection via Rabin-Karp token matching; 223 formats; v5 Rust engine 24–37x faster (17k files in 3.44s); used by GitHub Super-Linter, MegaLinter, Codacy; AI reporter emits ~79% fewer tokens [13] | Detection only — no fixes | Detects duplicated blocks; realized savings require manual/LLM dedup (Est.: up to ~half of duplicated LOC for N copies) |
| PMD CPD (also runs on JS/TS) | Token-based duplicate detection (`--minimum-tokens`, e.g. 100) [14] | Detection only; exits 4 when duplicates found (CI gate) | Same as jscpd |
| StrykerJS | Mutation testing oracle for JS/TS incl. TS/React/Vue/Svelte; Jest/Vitest/Mocha/etc. runners; incremental mode; parallel workers [16][17] | Test-verified | n/a (enables risky shrinks) |

### Python

| Tool | What it removes/detects | Safety | LOC impact estimate |
|---|---|---|---|
| Vulture | Unused code via AST name collection: functions, classes, imports, variables, unreachable code; confidence 60–100%; `--min-confidence 100` = guaranteed unused within analyzed files; `--sort-by-size` prioritizes big wins [11] | 100%-confidence items are provably unused within scanned files; 60% items are risky because Python is dynamic and implicitly-called code can be flagged [11] | Medium on dirty codebases; bounded by actual dead volume (Est.) |
| pyflakes | Unused imports, unused locals, plus programmatic errors; parses without importing; "never complains about style", aims to never emit false positives; per-file analysis limits what it can check [12] | High (near-zero false positives by design) [12] | Small (import/line-level) |
| Ruff (`ruff check --fix`) | Flake8+isort+pyupgrade+autoflake replacement; removes unused imports (F401), can remove unused `noqa` (RUF100 with `--fix`) [10] | Fix safety is explicit: safe fixes "preserve runtime behavior"; unsafe fixes may change behavior (e.g. RUF015 swaps `IndexError` for `StopIteration`) [10] | Small per fix; deterministic and cheap |
| jscpd / PMD CPD | Both support Python sources for duplicate detection [13][14] | Detection only | Same as JS table |
| mutmut | Mutation testing oracle; see below [15] | Test-verified | n/a |

## Safe vs unsafe transformations

**Provably-safe (compiler/type/lint-verified — apply automatically):**
- Rust `unused_imports`/`redundant_imports`/`dead_code` removals: recompile is the proof, with one documented exception (fields kept only for side effects like `Drop`, which `dead_code` itself warns about) [4][5]. `cargo fix` automates applying machine-applicable suggestions [5].
- Ruff *safe* fixes: documented to preserve runtime behavior (e.g. unused-import removal) [10].
- Formatter runs (Biome, dprint, `eslint --fix --fix-type layout`): cannot change AST/semantics [7][8][9] — but their LOC deltas are cosmetic, not real shrinkage.

**Test-verified (needs the suite as the gate):**
- Knip-driven deletion of unused files/exports/deps: JS has no compiler to catch a wrong deletion; `tsc` + tests are the verification [1].
- Duplicate-block refactoring guided by jscpd/CPD reports: both tools explicitly only *detect*; PMD's own guidance is to refactor duplicates out, which changes call structure and must be behavior-checked [13][14].
- Clippy complexity/style rewrites and Rust edition migrations: compiler-verified to compile, behavior verified by tests [5][6].
- cargo-machete/cargo-udeps dep removal: false positives exist (machete on renamed/build.rs crates [2]; udeps misses some deps [3]) but a rebuild catches them immediately.

**Risky (report-only or behavior-changing):**
- Vulture findings below 100% confidence: dynamic Python means implicitly-called code (getattr, frameworks, decorators) may be reported unused [11].
- Ruff *unsafe* fixes (`--unsafe-fixes`): explicitly may change runtime behavior such as raised exception types [10].
- Aggressive whole-file deletions from any heuristic tool without the compile/test gate.

**Meaningful vs cosmetic:** Real LOC shrink comes from (1) deleting unused files/exports/items, (2) dependency pruning, and (3) deduplicating repeated blocks. Formatters and most style-lint `--fix`es merely re-wrap lines — LOC-neutral churn that should be excluded from "reduction" accounting [7][8][9].

## Mutation testing as a safety oracle

Mechanism: inject small bugs ("mutants" — e.g. `>=` → `>`, integer +1, `break` ↔ `continue` [15][16]) into source, run the tests per mutant; a failing test *kills* the mutant, a passing suite means it *survived*. **Mutation score = % of mutants killed**; higher score = tests that actually check behavior, not just reach code [16]. Coverage only proves code was *reached*, not *checked* [16][18]. Stryker mutates only your source to avoid false positives [16]. For an LOC-reduction pipeline, a high mutation score on the region being shrunk certifies that the test suite would catch a behavior-breaking "simplification."

Recommended tool + command per language:
- Rust: `cargo install --locked cargo-mutants` then `cargo mutants` (scope with `-f src/file.rs`); designed for non-flaky `cargo test`/`nextest` suites; CI recipes support incremental PR testing [18].
- Python: `pip install mutmut` then `mutmut run`; `mutmut browse` to inspect/apply mutants; runs pytest over `tests/` by default [15].
- JS/TS: StrykerJS — supports TypeScript/React/Vue/Svelte/Node with Jest, Vitest, Mocha, Jasmine, Karma, Cucumber, Tap runners; has incremental mode and parallel workers [16][17].

Typical runtime cost (Est., grounded in tool docs): raw cost ≈ (#mutants) × (test-suite runtime), so it is orders of magnitude above a single test run. Mitigations documented by the tools: mutmut remembers prior work, only runs tests it knows exercise a mutated function, and runs in parallel [15]; StrykerJS ships incremental mode and parallel workers [17]; cargo-mutants supports per-file filtering and incremental CI runs [18]. Practical pattern: run full mutation analysis on a schedule/main branch and use cached/incremental results to gate risky static or LLM refactors locally.

## Key findings for a hybrid pipeline

- Dead-code deletion is the highest-yield static win and is cheap: Knip user reports range from 6k LOC in 30 minutes to ~300k LOC removed at Vercel [1]; Rust adds compiler-verified `dead_code`/`unused*` lints on top [4].
- Dependency pruning (cargo-machete/udeps, Knip's unused-dependency report) moves ~0 LOC but cuts build time and attack surface; expect false positives (machete) and false negatives (udeps) and gate with a rebuild [1][2][3].
- Static duplication detectors (jscpd, PMD CPD) stop at the report: neither tool refactors, so realizing the LOC savings is exactly where an LLM layer must operate — with tests as the acceptance gate [13][14].
- Formatting (Biome, dprint) and style fixes are LOC-cosmetic; exclude them from reduction metrics and run them first to normalize the diff before measuring anything [7][8][9].
- Static analysis plateaus at "provably unused" and "token-identical": semantic simplification, API-shape consolidation, and cross-module dedup need reasoning about intent — the gap an LLM layer fills.
- Python's dynamism caps static safety: Vulture's 60% confidence tier and pyflakes' per-file scope mean Python deletions lean harder on tests than Rust deletions lean on the compiler [11][12].
- Use tools' own safety taxonomies as pipeline gates: Ruff's safe/unsafe fix split [10] and rustc's machine-applicable `cargo fix` suggestions [5] give a provably-safe tier; everything else routes to the test gate.
- Mutation score is the right oracle for "is this suite strong enough to trust aggressive shrinking": killed/survived ratio quantifies test strength beyond coverage [16][18], with cargo-mutants, mutmut, and StrykerJS as the per-language choices [15][16][17][18].
- Precision knobs materially reduce review load in a hybrid loop: Knip's 150+ plugin entry-point detection [1], machete `--with-metadata` [2], CPD `--ignore-identifiers`/`--ignore-literals` [14], and jscpd's token-efficient AI reporter built for LLM consumption [13].
- Ordering matters: run the millisecond-scale static pass first (dprint formats hundreds of files in ms [9]; jscpd v5 scans 17k files in ~3.4s [13]; Ruff is an "extremely fast" linter [10]), spend LLM effort only on what remains.

## Sources

1. https://knip.dev/ — Knip homepage (features, plugins, user LOC-deletion reports)
2. https://github.com/bnjbvr/cargo-machete — cargo-machete README (usage, false positives, --with-metadata, JSON output)
3. https://github.com/est31/cargo-udeps — cargo-udeps README (nightly requirement, known bugs, trophy case)
4. https://doc.rust-lang.org/rustc/lints/listing/warn-by-default.html — rustc warn-by-default lints (dead_code, unused_imports, unused_macros, unreachable_code)
5. https://doc.rust-lang.org/rustc/lints/listing/allowed-by-default.html — rustc allowed-by-default lints (unreachable_pub, unused_crate_dependencies, redundant_imports; cargo fix auto-application)
6. https://doc.rust-lang.org/clippy/ — Clippy lint categories and default levels
7. https://eslint.org/docs/latest/use/command-line-interface — ESLint CLI (--fix, --fix-dry-run, --fix-type semantics)
8. https://biomejs.dev/formatter/ — Biome formatter (CLI, options, defaults)
9. https://dprint.dev/ — dprint (pluggable Rust+Wasm formatting platform)
10. https://docs.astral.sh/ruff/linter/ — Ruff linter (--fix, fix safety safe/unsafe, F401, RUF100)
11. https://github.com/jendrikseipp/vulture — Vulture README (confidence values, whitelists, --sort-by-size, similar tools)
12. https://github.com/PyCQA/pyflakes — pyflakes README (design principles, no-import parsing)
13. https://github.com/kucherenko/jscpd — jscpd README (Rabin-Karp, formats, v5 performance, adopters)
14. https://docs.pmd-code.org/latest/pmd_userdocs_cpd.html — PMD CPD userdocs (options, exit codes, refactoring guidance, suppression)
15. https://mutmut.readthedocs.io/en/latest/ — mutmut docs (run/browse workflow, example mutations, caching, test selection)
16. https://stryker-mutator.io/docs/ — Stryker "What is mutation testing?" (killed/survived, score, coverage contrast)
17. https://stryker-mutator.io/docs/stryker-js/introduction/ — StrykerJS intro (supported stacks, runners, incremental, parallel workers)
18. https://github.com/sourcefrog/cargo-mutants — cargo-mutants README (install, quick start, CI integration, prerequisites)
