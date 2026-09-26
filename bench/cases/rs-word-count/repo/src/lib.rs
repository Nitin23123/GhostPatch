//! Small text statistics for the editor's status bar.

/// The number of words in `text`. Words are separated by any whitespace.
pub fn word_count(text: &str) -> usize {
    if text.is_empty() {
        return 0;
    }
    text.split(' ').count()
}

/// The average word length in characters, rounded down, or 0 when there are no words.
pub fn average_word_length(text: &str) -> usize {
    let words = word_count(text);
    if words == 0 {
        return 0;
    }
    text.chars().filter(|c| !c.is_whitespace()).count() / words
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_text_has_no_words() {
        assert_eq!(word_count(""), 0);
    }

    #[test]
    fn simple_sentence() {
        assert_eq!(word_count("the quick fox"), 3);
    }
}
