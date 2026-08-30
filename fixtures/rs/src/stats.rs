//! Simple statistics over prose text: counts, reading time, frequencies.
//!
//! The heuristics here were tuned for English internal documentation
//! and never revisited, so treat the syllable estimate accordingly.

use std::collections::HashMap;

/// Aggregate counts for a piece of text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextStats {
    /// Whitespace separated word count.
    pub words: usize,
    /// Total characters, counted by `char`.
    pub characters: usize,
    /// Characters excluding all whitespace.
    pub characters_no_spaces: usize,
    /// Line count, as `str::lines` sees them.
    pub lines: usize,
    /// Sentence count by terminal punctuation runs.
    pub sentences: usize,
    /// Estimated syllable count (English vowel-group heuristic).
    pub syllables: usize,
}

/// True for the vowels used by the syllable heuristic. Note that `y` is
/// treated as a vowel, matching the original internal report.
fn is_vowel(c: char) -> bool {
    match c {
        'a' => true,
        'e' => true,
        'i' => true,
        'o' => true,
        'u' => true,
        'y' => true,
        _ => false,
    }
}

/// Estimate syllables in one word: count vowel groups, then drop a
/// trailing silent `e` when the word has more than one group.
fn count_syllables_word(word: &str) -> usize {
    let lowered = word.to_lowercase();
    let mut count = 0usize;
    let mut prev_vowel = false;
    for c in lowered.chars() {
        let vowel = is_vowel(c);
        if vowel && !prev_vowel {
            count = count + 1;
        }
        prev_vowel = vowel;
    }
    if lowered.ends_with('e') && count > 1 {
        count = count - 1;
    }
    count
}

/// Compute word, character, line, sentence and syllable counts.
pub fn analyze(text: &str) -> TextStats {
    let mut words = 0usize;
    let mut characters = 0usize;
    let mut characters_no_spaces = 0usize;
    let mut lines = 0usize;
    let mut sentences = 0usize;
    let mut syllables = 0usize;

    let mut in_word = false;
    let mut prev_ender = false;
    for c in text.chars() {
        characters = characters + 1;
        if c.is_whitespace() {
            in_word = false;
        } else {
            characters_no_spaces = characters_no_spaces + 1;
            if !in_word {
                words = words + 1;
                in_word = true;
            }
        }
        if c == '.' || c == '!' || c == '?' {
            if !prev_ender {
                sentences = sentences + 1;
                prev_ender = true;
            }
        } else {
            prev_ender = false;
        }
    }

    for _ in text.lines() {
        lines = lines + 1;
    }

    for word in text.split_whitespace() {
        syllables = syllables + count_syllables_word(word);
    }

    TextStats {
        words,
        characters,
        characters_no_spaces,
        lines,
        sentences,
        syllables,
    }
}

/// Reading time in whole seconds at `words_per_minute`, floored.
///
/// A rate of zero yields zero rather than dividing by zero; the nightly
/// batch job passes zero on weekends and nobody remembers why.
pub fn reading_time_seconds(text: &str, words_per_minute: u32) -> u64 {
    if words_per_minute == 0 {
        return 0;
    }
    let mut words = 0u64;
    for _ in text.split_whitespace() {
        words = words + 1;
    }
    let seconds = words * 60;
    seconds / words_per_minute as u64
}

/// Mean characters per whitespace separated word, or 0.0 when empty.
pub fn average_word_length(text: &str) -> f64 {
    let mut total = 0usize;
    let mut words = 0usize;
    for word in text.split_whitespace() {
        total = total + word.chars().count();
        words = words + 1;
    }
    if words == 0 {
        return 0.0;
    }
    total as f64 / words as f64
}

/// Case-insensitive word counts, most frequent first, ties alphabetical.
pub fn word_frequencies(text: &str) -> Vec<(String, usize)> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut lowered: Vec<String> = Vec::new();
    for word in text.split_whitespace() {
        lowered.push(word.to_lowercase());
    }
    for word in lowered {
        match counts.get_mut(&word) {
            Some(entry) => *entry = *entry + 1,
            None => {
                counts.insert(word, 1);
            }
        };
    }
    let mut items: Vec<(String, usize)> = counts.into_iter().collect();
    items.sort_by(|a, b| {
        if a.1 != b.1 {
            b.1.cmp(&a.1)
        } else {
            a.0.cmp(&b.0)
        }
    });
    items
}

/// Lowercased alphanumeric character counts, most frequent first, ties
/// broken by character order.
pub fn char_frequencies(text: &str) -> Vec<(char, usize)> {
    let mut counts: HashMap<char, usize> = HashMap::new();
    let mut lowered = String::new();
    for c in text.chars() {
        lowered.extend(c.to_lowercase());
    }
    for c in lowered.chars() {
        if !c.is_alphanumeric() {
            continue;
        }
        match counts.get_mut(&c) {
            Some(entry) => *entry = *entry + 1,
            None => {
                counts.insert(c, 1);
            }
        };
    }
    let mut items: Vec<(char, usize)> = counts.into_iter().collect();
    items.sort_by(|a, b| {
        if a.1 != b.1 {
            b.1.cmp(&a.1)
        } else {
            a.0.cmp(&b.0)
        }
    });
    items
}
