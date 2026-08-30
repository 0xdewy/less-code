//! Integration tests for the legacy_textutils public API.

use legacy_textutils::stats::{
    analyze, average_word_length, char_frequencies, reading_time_seconds, word_frequencies,
    TextStats,
};
use legacy_textutils::{
    count_leading_spaces, count_trailing_spaces, csv_escape_field, csv_escape_row,
    csv_escape_row_owned, csv_escape_rows, longest_line, render_template, slugify, title_case,
    wrap_words, TemplateError,
};

#[test]
fn wrap_basic_greedy() {
    assert_eq!(wrap_words("the quick brown fox", 10), "the quick\nbrown fox");
    assert_eq!(wrap_words("aa bb cc", 7), "aa bb\ncc");
    assert_eq!(wrap_words("aa bb cc", 8), "aa bb cc");
}

#[test]
fn wrap_exact_boundary_width() {
    assert_eq!(wrap_words("aa bb", 5), "aa bb");
    assert_eq!(wrap_words("aa bb", 4), "aa\nbb");
    assert_eq!(wrap_words("aa bb cc", 8), "aa bb cc");
}

#[test]
fn wrap_zero_width_returns_input_unchanged() {
    assert_eq!(wrap_words("a b c", 0), "a b c");
    assert_eq!(wrap_words("", 0), "");
}

#[test]
fn wrap_long_word_overflows_on_own_line() {
    assert_eq!(wrap_words("extraordinary tiny", 5), "extraordinary\ntiny");
    assert_eq!(wrap_words("toolong", 3), "toolong");
}

#[test]
fn wrap_empty_and_single_char() {
    assert_eq!(wrap_words("", 8), "");
    assert_eq!(wrap_words("x", 1), "x");
    assert_eq!(wrap_words(" ", 5), "");
}

#[test]
fn wrap_counts_chars_not_bytes() {
    assert_eq!(wrap_words("héllo wörld", 6), "héllo\nwörld");
    assert_eq!(wrap_words("α β", 3), "α β");
}

#[test]
fn csv_field_plain_needs_no_quotes() {
    assert_eq!(csv_escape_field("ab", ','), "ab");
    assert_eq!(csv_escape_field("", ','), "");
    assert_eq!(csv_escape_field("a b", ','), "a b");
    assert_eq!(csv_escape_field("a,b", ';'), "a,b");
}

#[test]
fn csv_field_delimiter_triggers_quotes() {
    assert_eq!(csv_escape_field("a,b", ','), "\"a,b\"");
    assert_eq!(csv_escape_field("a;b", ';'), "\"a;b\"");
}

#[test]
fn csv_field_quote_doubling() {
    assert_eq!(csv_escape_field("say \"hi\"", ','), "\"say \"\"hi\"\"\"");
    assert_eq!(csv_escape_field("\"", ','), "\"\"\"\"");
}

#[test]
fn csv_field_newline_and_cr_trigger_quotes() {
    assert_eq!(csv_escape_field("a\nb", ','), "\"a\nb\"");
    assert_eq!(csv_escape_field("a\rb", ','), "\"a\rb\"");
}

#[test]
fn csv_row_joins_and_terminates() {
    assert_eq!(csv_escape_row(&["a", "b,c", "d"], ','), "a,\"b,c\",d\n");
    assert_eq!(csv_escape_row(&[], ','), "\n");
}

#[test]
fn csv_row_owned_matches_row() {
    let owned = vec!["x".to_string(), "y".to_string()];
    assert_eq!(csv_escape_row_owned(&owned, '\t'), "x\ty\n");
    assert_eq!(
        csv_escape_row_owned(&["a,b".to_string()], ','),
        "\"a,b\"\n"
    );
}

#[test]
fn csv_rows_joins_records() {
    assert_eq!(csv_escape_rows(&[&["a", "b"], &["c"]], ','), "a,b\nc");
    assert_eq!(csv_escape_rows(&[], ','), "");
    assert_eq!(csv_escape_rows(&[&[]], ','), "");
}

#[test]
fn template_simple_substitution() {
    assert_eq!(
        render_template("Hello, {{name}}!", &[("name", "Ada")]),
        Ok("Hello, Ada!".to_string())
    );
}

#[test]
fn template_trims_names_and_supports_several_vars() {
    assert_eq!(
        render_template("{{ x }}={{y}}", &[("x", "1"), ("y", "2")]),
        Ok("1=2".to_string())
    );
}

#[test]
fn template_without_variables_is_identity() {
    assert_eq!(render_template("plain text", &[]), Ok("plain text".to_string()));
    assert_eq!(render_template("", &[]), Ok(String::new()));
    assert_eq!(render_template("a{b}c", &[]), Ok("a{b}c".to_string()));
}

#[test]
fn template_unknown_variable_is_error() {
    let err = render_template("Hi {{city}}", &[("name", "x")]).unwrap_err();
    assert_eq!(err, TemplateError::UnknownVariable("city".to_string()));
    assert_eq!(err.to_string(), "unknown variable: city");
}

#[test]
fn template_unclosed_brace_is_error() {
    assert_eq!(
        render_template("{{open", &[]),
        Err(TemplateError::UnclosedBrace)
    );
    assert_eq!(
        render_template("done {{", &[]),
        Err(TemplateError::UnclosedBrace)
    );
}

#[test]
fn template_empty_name_is_error() {
    assert_eq!(
        render_template("{{}}", &[]),
        Err(TemplateError::EmptyVariableName)
    );
    assert_eq!(
        render_template("{{ }}", &[]),
        Err(TemplateError::EmptyVariableName)
    );
}

#[test]
fn template_last_value_wins() {
    assert_eq!(
        render_template("{{v}}", &[("v", "1"), ("v", "2")]),
        Ok("2".to_string())
    );
}

#[test]
fn template_values_are_not_rescanned() {
    assert_eq!(
        render_template("{{a}}", &[("a", "{{b}}")]),
        Ok("{{b}}".to_string())
    );
}

#[test]
fn template_unicode_variable_names() {
    assert_eq!(
        render_template("{{héllo}}", &[("héllo", "wörld")]),
        Ok("wörld".to_string())
    );
}

#[test]
fn slug_basic_and_punctuation() {
    assert_eq!(slugify("Hello, World!"), "hello-world");
    assert_eq!(slugify("obj 42 go"), "obj-42-go");
    assert_eq!(slugify("under_score"), "under-score");
}

#[test]
fn slug_collapses_and_trims_dashes() {
    assert_eq!(slugify("  spacing   odd  "), "spacing-odd");
    assert_eq!(slugify("日本語 text"), "text");
}

#[test]
fn slug_empty_and_junk_only() {
    assert_eq!(slugify(""), "");
    assert_eq!(slugify("!!!"), "");
    assert_eq!(slugify("   "), "");
}

#[test]
fn slug_folds_accents() {
    assert_eq!(slugify("Grüße von Köln"), "grusse-von-koln");
    assert_eq!(slugify("café"), "cafe");
    assert_eq!(slugify("Ångström"), "angstrom");
}

#[test]
fn title_case_basics() {
    assert_eq!(title_case("hello world"), "Hello World");
    assert_eq!(title_case("don't stop"), "Don't Stop");
    assert_eq!(title_case("a"), "A");
    assert_eq!(title_case(""), "");
    assert_eq!(title_case("élan vital"), "Élan Vital");
}

#[test]
fn longest_line_prefers_first_on_tie() {
    assert_eq!(longest_line("ab\nabcd\nabc"), "abcd");
    assert_eq!(longest_line("abc\ndef\nxyz"), "abc");
    assert_eq!(longest_line(""), "");
    assert_eq!(longest_line("solo"), "solo");
}

#[test]
fn leading_spaces_counts_only_u0020() {
    assert_eq!(count_leading_spaces("   hi"), 3);
    assert_eq!(count_leading_spaces("hi"), 0);
    assert_eq!(count_leading_spaces("     "), 5);
    assert_eq!(count_leading_spaces(""), 0);
    assert_eq!(count_leading_spaces("\t x"), 0);
}

#[test]
fn trailing_spaces_counts_only_u0020() {
    assert_eq!(count_trailing_spaces("hi   "), 3);
    assert_eq!(count_trailing_spaces("hi"), 0);
    assert_eq!(count_trailing_spaces("   "), 3);
    assert_eq!(count_trailing_spaces(""), 0);
    assert_eq!(count_trailing_spaces("x\t"), 0);
}

#[test]
fn stats_basic_sentence() {
    assert_eq!(
        analyze("Hello world."),
        TextStats {
            words: 2,
            characters: 12,
            characters_no_spaces: 11,
            lines: 1,
            sentences: 1,
            syllables: 3,
        }
    );
}

#[test]
fn stats_empty_text_is_all_zeros() {
    assert_eq!(
        analyze(""),
        TextStats {
            words: 0,
            characters: 0,
            characters_no_spaces: 0,
            lines: 0,
            sentences: 0,
            syllables: 0,
        }
    );
}

#[test]
fn stats_blank_lines_count_as_lines() {
    assert_eq!(
        analyze("one\n\ntwo"),
        TextStats {
            words: 2,
            characters: 8,
            characters_no_spaces: 6,
            lines: 3,
            sentences: 0,
            syllables: 2,
        }
    );
}

#[test]
fn stats_punctuation_runs_collapse_into_one_sentence() {
    assert_eq!(
        analyze("Wait... what?! really?"),
        TextStats {
            words: 3,
            characters: 22,
            characters_no_spaces: 20,
            lines: 1,
            sentences: 3,
            syllables: 4,
        }
    );
}

#[test]
fn reading_time_floors_and_handles_zero_rate() {
    assert_eq!(reading_time_seconds("one two three four five", 200), 1);
    assert_eq!(reading_time_seconds("one two", 120), 1);
    assert_eq!(reading_time_seconds("one", 30), 2);
    assert_eq!(reading_time_seconds("", 250), 0);
    assert_eq!(reading_time_seconds("one", 0), 0);
}

#[test]
fn average_word_length_basics() {
    assert_eq!(average_word_length("aa bb cc d"), 1.75);
    assert_eq!(average_word_length(""), 0.0);
    assert_eq!(average_word_length("héllo"), 5.0);
    assert_eq!(average_word_length("   "), 0.0);
}

#[test]
fn word_frequencies_sorted_by_count_then_alpha() {
    assert_eq!(
        word_frequencies("The cat the dog the bird"),
        vec![
            ("the".to_string(), 3),
            ("bird".to_string(), 1),
            ("cat".to_string(), 1),
            ("dog".to_string(), 1),
        ]
    );
    assert_eq!(word_frequencies(""), Vec::<(String, usize)>::new());
}

#[test]
fn char_frequencies_lowercase_alnum_only() {
    assert_eq!(char_frequencies("Aa bb!"), vec![('a', 2), ('b', 2)]);
    assert_eq!(char_frequencies("É é é"), vec![('é', 3)]);
    assert_eq!(char_frequencies("bbba c"), vec![('b', 3), ('a', 1), ('c', 1)]);
    assert_eq!(char_frequencies("!!!"), Vec::<(char, usize)>::new());
}

#[test]
fn stats_single_syllable_word_ending_in_e_is_not_trimmed() {
    assert_eq!(
        analyze("the table"),
        TextStats {
            words: 2,
            characters: 9,
            characters_no_spaces: 8,
            lines: 1,
            sentences: 0,
            syllables: 2,
        }
    );
}
