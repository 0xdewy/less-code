# Fixture: `legacy_textutils` (Rust)

Legacy-style internal utility crate (text/template grab-bag) used as a
demo target for the code-reduction tool.

## Measured

| Metric | Value | Command |
|---|---|---|
| Language | rust | `uv run lc analyze fixtures/rs` |
| Source code-LOC | 387 (src/lib.rs + src/stats.rs) | `uv run lc analyze fixtures/rs` |
| Tests | 39 fns, 96 assertions, all green | `cargo test --quiet` |
| Historical mutation audit (retired command) | **1.0** (50/50 killed) | previous measurement |

Deterministic, std-only, zero dependencies (no dev-dependencies).

## Embedded reduction opportunities (genuine, line refs)

### Dead public functions (never called anywhere, incl. tests)
- `reverse_words` — src/lib.rs:286
- `indent_lines` — src/lib.rs:296
- `is_blank` — src/lib.rs:308

### Copy-pasted near-duplicate blocks (mergeable into one generic helper)
- `csv_escape_row` (lib.rs:136) vs `csv_escape_row_owned` (lib.rs:150) —
  identical bodies modulo `&str` vs `&String`; one `AsRef<str>` generic
  covers both. `csv_escape_rows` (lib.rs:166) pastes the same join loop
  a third time.
- `count_leading_spaces` (lib.rs:257) vs `count_trailing_spaces`
  (lib.rs:270) — same loop, forward vs `.rev()` iterator.
- `word_frequencies` (stats.rs:139) vs `char_frequencies`
  (stats.rs:166) — identical count-then-sort scaffolding.
- `slugify` (lib.rs:200-217) — the dash-flush `if` block is pasted in
  both arms.

### Verbose manual loops (replaceable by iterator chains)
- `analyze` (stats.rs:59) — characters/words/sentences/lines/syllables
  counters vs `chars().filter`/`split_whitespace().count()`/
  `lines().count()`.
- `reading_time_seconds` (stats.rs:112), `average_word_length`
  (stats.rs:125) — manual counting loops.
- `csv_escape_field` (lib.rs:114-119) — needs-quotes scan is
  `field.contains(...)` / `.any()`.
- `longest_line` (lib.rs:243) — manual max vs `.max_by_key`.
- `wrap_words` (lib.rs:88) — needless intermediate `Vec<&str>` collect.

### Long match arms (lookup table / `matches!`)
- `is_vowel` (stats.rs:27) — six-arm boolean match, `matches!` one-liner.
- `slugify` accent folding (lib.rs:194-203) — table-driven match.

### Over-defensive clones / needless intermediates
- `csv_escape_rows` (lib.rs:169) — copies every field into an owned
  `Vec<String>` before escaping.
- `word_frequencies` (stats.rs:141-145) — builds a full `Vec<String>`
  of lowered words before counting.
- `count_syllables_word` (stats.rs:42) — allocates a lowered copy of
  each word.
- `csv_escape_field` (lib.rs:121) — `field.to_string()` copy on the
  no-quote fast path.

## Notes

- Dead functions are documented as deprecated-but-linked, the usual
  reason legacy utilities survive; they are excluded from tests on
  purpose so static dead-code detection flags them.
- Deliberate legacy quirks pinned by tests: `wrap_words(_, 0)` returns
  input unchanged; U+0020-only space counting; the vowel-group syllable
  heuristic (with silent-`e` rule and `y` as vowel).
