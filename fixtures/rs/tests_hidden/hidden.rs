//! Hidden behaviour tests for the rs fixture.
//!
//! Not part of `cargo test` (see the `[[test]] test = false` entry in
//! Cargo.toml) and never shown to the reducer; `lc bench` runs this target
//! after a reduction as an independent safety check.

use legacy_textutils::stats::{
    analyze, average_word_length, char_frequencies, reading_time_seconds, word_frequencies,
};
use legacy_textutils::{
    count_leading_spaces, count_trailing_spaces, csv_escape_field, csv_escape_row,
    csv_escape_row_owned, csv_escape_rows, longest_line, render_template, slugify, title_case,
    wrap_words, TemplateError,
};

#[test]
fn template_substitutes_trims_and_takes_last_value() {
    assert_eq!(render_template("hi {{ name }}!", &[("name", "bo")]), Ok("hi bo!".to_string()));
    assert_eq!(render_template("{{a}}{{a}}", &[("a", "x")]), Ok("xx".to_string()));
    assert_eq!(render_template("{{a}}", &[("a", "1"), ("a", "2")]), Ok("2".to_string()));
    assert_eq!(render_template("plain", &[]), Ok("plain".to_string()));
    // substituted values are not re-scanned
    assert_eq!(render_template("{{a}}", &[("a", "{{b}}")]), Ok("{{b}}".to_string()));
}

#[test]
fn template_errors_are_precise() {
    assert_eq!(render_template("{{x}}", &[]), Err(TemplateError::UnknownVariable("x".into())));
    assert_eq!(render_template("{{x", &[]), Err(TemplateError::UnclosedBrace));
    assert_eq!(render_template("{{ }}", &[]), Err(TemplateError::EmptyVariableName));
    assert_eq!(
        format!("{}", TemplateError::UnknownVariable("q".into())),
        "unknown variable: q"
    );
    assert_eq!(format!("{}", TemplateError::UnclosedBrace), "unclosed variable brace");
    assert_eq!(format!("{}", TemplateError::EmptyVariableName), "empty variable name");
}

#[test]
fn wrap_words_greedy_and_quirks() {
    assert_eq!(wrap_words("aaa bbb ccc", 7), "aaa bbb\nccc");
    assert_eq!(wrap_words("aaa bbb ccc", 0), "aaa bbb ccc");
    assert_eq!(wrap_words("", 5), "");
    assert_eq!(wrap_words("  spaced   out  ", 20), "spaced out");
    // an overlong word gets its own line and is allowed to overflow
    assert_eq!(wrap_words("a enormouslylongword b", 5), "a\nenormouslylongword\nb");
}

#[test]
fn csv_escaping_rules() {
    assert_eq!(csv_escape_field("plain", ','), "plain");
    assert_eq!(csv_escape_field("a,b", ','), "\"a,b\"");
    assert_eq!(csv_escape_field("a,b", ';'), "a,b");
    assert_eq!(csv_escape_field("say \"hi\"", ','), "\"say \"\"hi\"\"\"");
    assert_eq!(csv_escape_field("two\nlines", ','), "\"two\nlines\"");
    assert_eq!(csv_escape_field("cr\r", ','), "\"cr\r\"");
    assert_eq!(csv_escape_field("", ','), "");
}

#[test]
fn csv_row_variants_agree() {
    let borrowed = ["a", "b,c"];
    let owned: Vec<String> = borrowed.iter().map(|s| s.to_string()).collect();
    assert_eq!(csv_escape_row(&borrowed, ','), "a,\"b,c\"\n");
    assert_eq!(csv_escape_row_owned(&owned, ','), csv_escape_row(&borrowed, ','));
    let rows: Vec<&[&str]> = vec![&borrowed, &borrowed];
    assert_eq!(csv_escape_rows(&rows, ','), "a,\"b,c\"\na,\"b,c\"");
    assert_eq!(csv_escape_rows(&[], ','), "");
    assert_eq!(csv_escape_row(&[], ','), "\n");
}

#[test]
fn slugify_folds_accents_and_collapses_runs() {
    assert_eq!(slugify("Hello World"), "hello-world");
    assert_eq!(slugify("  --Hello--  World-- "), "hello-world");
    assert_eq!(slugify("Café Ñandú"), "cafe-nandu");
    assert_eq!(slugify("Straße"), "strasse");
    assert_eq!(slugify("a  ...  b"), "a-b");
    assert_eq!(slugify("!!!"), "");
    assert_eq!(slugify(""), "");
}

#[test]
fn title_case_lowercases_the_rest() {
    assert_eq!(title_case("hello world"), "Hello World");
    assert_eq!(title_case("HELLO wORLD"), "Hello World");
    assert_eq!(title_case("  spaced"), "  Spaced");
    assert_eq!(title_case(""), "");
}

#[test]
fn longest_line_first_wins_on_ties() {
    assert_eq!(longest_line("ab\ncd\nefg"), "efg");
    assert_eq!(longest_line("aa\nbb"), "aa");
    assert_eq!(longest_line(""), "");
    assert_eq!(longest_line("ünïcödé\nab"), "ünïcödé");
}

#[test]
fn space_counters_only_count_ascii_spaces() {
    assert_eq!(count_leading_spaces("   x  "), 3);
    assert_eq!(count_trailing_spaces("   x  "), 2);
    assert_eq!(count_leading_spaces("\t x"), 0);
    assert_eq!(count_trailing_spaces("x\t"), 0);
    assert_eq!(count_leading_spaces("   "), 3);
    assert_eq!(count_trailing_spaces(""), 0);
}

#[test]
fn analyze_counts_every_field() {
    let s = analyze("Hello there. How are you?\nSecond line!");
    assert_eq!(s.words, 7);
    assert_eq!(s.characters, 38);
    assert_eq!(s.characters_no_spaces, 32);
    assert_eq!(s.lines, 2);
    assert_eq!(s.sentences, 3);
    let empty = analyze("");
    assert_eq!(empty.words, 0);
    assert_eq!(empty.lines, 0);
    assert_eq!(empty.sentences, 0);
    // runs of terminal punctuation count once
    assert_eq!(analyze("wow!!! really?!").sentences, 2);
}

#[test]
fn reading_time_and_average_word_length() {
    assert_eq!(reading_time_seconds("one two three four", 2), 120);
    assert_eq!(reading_time_seconds("one two three", 0), 0);
    assert_eq!(reading_time_seconds("", 200), 0);
    assert!((average_word_length("ab cde") - 2.5).abs() < 1e-9);
    assert_eq!(average_word_length(""), 0.0);
}

#[test]
fn frequencies_sort_by_count_then_key() {
    let words = word_frequencies("the The cat dog cat");
    assert_eq!(words[0], ("cat".to_string(), 2));
    assert_eq!(words[1], ("the".to_string(), 2));
    assert_eq!(words[2], ("dog".to_string(), 1));
    assert!(word_frequencies("").is_empty());
    let chars = char_frequencies("aA b!");
    assert_eq!(chars[0], ('a', 2));
    assert_eq!(chars[1], ('b', 1));
    assert_eq!(chars.len(), 2);
}
