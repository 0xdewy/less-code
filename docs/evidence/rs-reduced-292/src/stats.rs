use std::collections::HashMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TextStats {
    pub words: usize,
    pub characters: usize,
    pub characters_no_spaces: usize,
    pub lines: usize,
    pub sentences: usize,
    pub syllables: usize,
}

fn is_vowel(c: char) -> bool {
    match c {
        'a' | 'e' | 'i' | 'o' | 'u' | 'y' => true,
        _ => false,
    }
}
fn count_syllables_word(word: &str) -> usize {
    let mut count = 0;
    let mut prev_vowel = false;
    for c in word.to_lowercase().chars() {
        if "aeiouy".contains(c) && !prev_vowel {
            count += 1;
        }
        prev_vowel = "aeiouy".contains(c);
    }
    if word.ends_with('e') && count > 1 {
        count -= 1;
    }
    count
}

pub fn analyze(text: &str) -> TextStats {
    let mut words = 0;
    let mut characters = 0;
    let mut characters_no_spaces = 0;
    let mut lines = 0;
    let mut sentences = 0;
    let mut syllables = 0;

    let mut in_word = false;
    let mut prev_ender = false;
    for c in text.chars() {
        characters += 1;
        if c.is_whitespace() {
            in_word = false;
        } else {
            characters_no_spaces += 1;
            if !in_word {
                words += 1;
                in_word = true;
            }
        }
        if c == '.' || c == '!' || c == '?' {
            if !prev_ender {
                sentences += 1;
                prev_ender = true;
            }
        } else {
            prev_ender = false;
        }
    }

    for _ in text.lines() {
        lines += 1;
    }

    for word in text.split_whitespace() {
        syllables += count_syllables_word(word);
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

pub fn reading_time_seconds(text: &str, words_per_minute: u32) -> u64 {
    if words_per_minute == 0 {
        return 0;
    }
    text.split_whitespace().count() as u64 * 60 / words_per_minute as u64
}

pub fn average_word_length(text: &str) -> f64 {
    let (total, words) = text.split_whitespace().fold((0, 0), |(acc, count), word| {
        (acc + word.chars().count(), count + 1)
    });
    if words == 0 {
        return 0.0;
    }
    total as f64 / words as f64
}

pub fn word_frequencies(text: &str) -> Vec<(String, usize)> {
    let mut counts = HashMap::new();
    for word in text.split_whitespace() {
        *counts.entry(word.to_lowercase()).or_insert(0) += 1;
    }
    let mut items: Vec<_> = counts.into_iter().collect();
    items.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    items
}

pub fn char_frequencies(text: &str) -> Vec<(char, usize)> {
    let mut counts = HashMap::new();
    for c in text.to_lowercase().chars() {
        if c.is_alphanumeric() {
            *counts.entry(c).or_insert(0) += 1;
        }
    }
    let mut items: Vec<_> = counts.into_iter().collect();
    items.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    items
}
