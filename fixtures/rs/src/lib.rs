//! Legacy text utilities shared by internal reporting services.
//!
//! This crate is a long-lived grab bag of string helpers: word wrapping,
//! CSV style escaping, template variable substitution, slug generation
//! and a few smaller helpers. It predates the platform-wide `textkit`
//! crate and is kept around for backwards compatibility while callers
//! migrate.

#![forbid(unsafe_code)]

pub mod stats;

use std::collections::HashMap;
use std::fmt;

/// Errors that can occur while rendering a template.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TemplateError {
    /// The template referenced a variable that was never supplied.
    UnknownVariable(String),
    /// An opening brace pair was never closed.
    UnclosedBrace,
    /// A variable reference had an empty name, as in `{{}}`.
    EmptyVariableName,
}

impl fmt::Display for TemplateError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            TemplateError::UnknownVariable(name) => write!(f, "unknown variable: {}", name),
            TemplateError::UnclosedBrace => write!(f, "unclosed variable brace"),
            TemplateError::EmptyVariableName => write!(f, "empty variable name"),
        }
    }
}

impl std::error::Error for TemplateError {}

/// Render `template` by substituting `{{name}}` references.
///
/// Variable names are trimmed, so `{{ name }}` and `{{name}}` name the
/// same variable. When the same name is supplied twice, the last value
/// wins. Substituted values are not scanned again, so a value may safely
/// contain brace pairs of its own. Unknown names and malformed braces
/// are hard errors.
pub fn render_template(template: &str, vars: &[(&str, &str)]) -> Result<String, TemplateError> {
    let mut lookup: HashMap<&str, &str> = HashMap::new();
    for (name, value) in vars {
        lookup.insert(name, value);
    }
    let mut out = String::new();
    let mut rest = template;
    loop {
        let start = match rest.find("{{") {
            None => {
                out.push_str(rest);
                return Ok(out);
            }
            Some(pos) => pos,
        };
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        let end = match after.find("}}") {
            None => return Err(TemplateError::UnclosedBrace),
            Some(pos) => pos,
        };
        let name = after[..end].trim();
        if name.is_empty() {
            return Err(TemplateError::EmptyVariableName);
        }
        match lookup.get(name) {
            None => return Err(TemplateError::UnknownVariable(name.to_string())),
            Some(value) => out.push_str(value),
        }
        rest = &after[end + 2..];
    }
}

/// Greedily wrap `text` so that no line exceeds `width` characters.
///
/// Words longer than `width` are placed on their own line and allowed
/// to overflow. A `width` of zero returns the input unchanged, a quirk
/// that at least one internal report exporter relies on.
pub fn wrap_words(text: &str, width: usize) -> String {
    if width == 0 {
        return text.to_string();
    }
    let words: Vec<&str> = text.split_whitespace().collect();
    let mut out = String::new();
    let mut current = 0usize;
    for word in words {
        let word_len = word.chars().count();
        if current == 0 {
            out.push_str(word);
            current = word_len;
        } else if current + 1 + word_len <= width {
            out.push(' ');
            out.push_str(word);
            current = current + 1 + word_len;
        } else {
            out.push('\n');
            out.push_str(word);
            current = word_len;
        }
    }
    out
}

/// Escape one field for CSV output in the house style.
///
/// A field is quoted when it contains the delimiter, a double quote, a
/// newline or a carriage return; embedded double quotes are doubled.
pub fn csv_escape_field(field: &str, delimiter: char) -> String {
    let mut needs_quotes = false;
    for c in field.chars() {
        if c == delimiter || c == '"' || c == '\n' || c == '\r' {
            needs_quotes = true;
        }
    }
    if !needs_quotes {
        return field.to_string();
    }
    let mut out = String::with_capacity(field.len() + 2);
    out.push('"');
    for c in field.chars() {
        if c == '"' {
            out.push('"');
        }
        out.push(c);
    }
    out.push('"');
    out
}

/// Escape and join one CSV row, terminated by a newline.
pub fn csv_escape_row(fields: &[&str], delimiter: char) -> String {
    let mut line = String::new();
    for (i, field) in fields.iter().enumerate() {
        if i != 0 {
            line.push(delimiter);
        }
        line.push_str(&csv_escape_field(field, delimiter));
    }
    line.push('\n');
    line
}

/// Owned-string twin of `csv_escape_row`, used by older call sites that
/// keep their rows as vectors of owned strings.
pub fn csv_escape_row_owned(fields: &[String], delimiter: char) -> String {
    let mut line = String::new();
    for (i, field) in fields.iter().enumerate() {
        if i != 0 {
            line.push(delimiter);
        }
        line.push_str(&csv_escape_field(field, delimiter));
    }
    line.push('\n');
    line
}

/// Escape many rows at once and join them with newlines.
///
/// Every field is defensively copied before escaping, which the original
/// author considered "safer".
pub fn csv_escape_rows(rows: &[&[&str]], delimiter: char) -> String {
    let mut escaped_rows: Vec<String> = Vec::new();
    for row in rows {
        let owned: Vec<String> = row.iter().map(|f| f.to_string()).collect();
        let mut line = String::new();
        for (i, field) in owned.iter().enumerate() {
            if i != 0 {
                line.push(delimiter);
            }
            line.push_str(&csv_escape_field(field, delimiter));
        }
        escaped_rows.push(line);
    }
    escaped_rows.join("\n")
}

/// Build a URL-friendly slug from `text`.
///
/// The text is lowercased, common accented letters are folded to their
/// ASCII base, `ss` style expansions included, and every other run of
/// non-alphanumeric characters becomes a single dash. Leading and
/// trailing dashes are dropped.
pub fn slugify(text: &str) -> String {
    let lowered = text.to_lowercase();
    let mut out = String::new();
    let mut pending_dash = false;
    for c in lowered.chars() {
        let folded: &str = match c {
            'á' | 'à' | 'â' | 'ä' | 'ã' | 'å' => "a",
            'é' | 'è' | 'ê' | 'ë' => "e",
            'í' | 'ì' | 'î' | 'ï' => "i",
            'ó' | 'ò' | 'ô' | 'ö' | 'õ' => "o",
            'ú' | 'ù' | 'û' | 'ü' => "u",
            'ñ' => "n",
            'ç' => "c",
            'ß' => "ss",
            _ => "",
        };
        if c.is_ascii_alphanumeric() {
            if pending_dash && !out.is_empty() {
                out.push('-');
            }
            pending_dash = false;
            out.push(c);
        } else if !folded.is_empty() {
            if pending_dash && !out.is_empty() {
                out.push('-');
            }
            pending_dash = false;
            out.push_str(folded);
        } else {
            pending_dash = true;
        }
    }
    out
}

/// Uppercase the first letter of every whitespace separated word and
/// lowercase the rest.
pub fn title_case(text: &str) -> String {
    let mut out = String::new();
    let mut at_start = true;
    for c in text.chars() {
        if at_start {
            out.extend(c.to_uppercase());
        } else {
            out.extend(c.to_lowercase());
        }
        at_start = c.is_whitespace();
    }
    out
}

/// Return the longest line of `text` by character count.
///
/// When several lines tie, the first of them wins. Empty input yields
/// an empty string.
pub fn longest_line(text: &str) -> &str {
    let mut best: &str = "";
    for line in text.lines() {
        if line.chars().count() > best.chars().count() {
            best = line;
        }
    }
    best
}

/// Count plain spaces (ASCII space only) at the start of `text`.
///
/// Tabs and other whitespace do not count; this matcher was written for
/// indented TSV reports and never generalised.
pub fn count_leading_spaces(text: &str) -> usize {
    let mut count = 0usize;
    for c in text.chars() {
        if c == ' ' {
            count = count + 1;
        } else {
            break;
        }
    }
    count
}

/// Count plain spaces (ASCII space only) at the end of `text`.
pub fn count_trailing_spaces(text: &str) -> usize {
    let mut count = 0usize;
    for c in text.chars().rev() {
        if c == ' ' {
            count = count + 1;
        } else {
            break;
        }
    }
    count
}

/// Reverse the word order of `input`.
///
/// Deprecated: still linked by an old data migration job. New code
/// should not pick this up.
pub fn reverse_words(input: &str) -> String {
    let mut words: Vec<&str> = input.split_whitespace().collect();
    words.reverse();
    words.join(" ")
}

/// Prefix every line of `input` with `prefix`.
///
/// Deprecated: kept because a hand-rolled report exporter still calls
/// it from a pinned legacy branch.
pub fn indent_lines(input: &str, prefix: &str) -> String {
    let mut lines: Vec<String> = Vec::new();
    for line in input.lines() {
        lines.push(format!("{}{}", prefix, line));
    }
    lines.join("\n")
}

/// True when `input` is empty or made only of whitespace.
///
/// Deprecated: callers were migrated to `str::trim` comparisons years
/// ago, but the symbol was never removed.
pub fn is_blank(input: &str) -> bool {
    for c in input.chars() {
        if !c.is_whitespace() {
            return false;
        }
    }
    true
}
