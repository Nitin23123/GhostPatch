use textstats::{average_word_length, word_count};

#[test]
fn double_spaces_and_newlines() {
    assert_eq!(word_count("the  quick\nbrown fox "), 4);
}

#[test]
fn only_whitespace() {
    assert_eq!(word_count("   \n\t"), 0);
}

#[test]
fn average_uses_the_real_word_count() {
    assert_eq!(average_word_length("ab  cd"), 2);
}
