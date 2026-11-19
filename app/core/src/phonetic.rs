//! Double Metaphone keys for place matching (PRD §7.5 "phonetic key (Double Metaphone)").
//!
//! The encoder is `rphonetic` 4.0.0 (Apache-2.0), a port of Apache commons-codec's
//! `DoubleMetaphone` (Lawrence Philips, 2000). Codes are not truncated, so long names keep their
//! distinguishing tail.

use rphonetic::DoubleMetaphone;
use std::sync::OnceLock;

/// The primary and alternate Double Metaphone codes of one word.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Codes {
    /// Primary code ("RNR" for "rainier").
    pub primary: String,
    /// Alternate code; equal to the primary for most English words.
    pub alternate: String,
}

/// Keys shorter than this are shared by too many words to identify a name on their own.
pub const MIN_DISTINCT_KEY: usize = 3;

impl Codes {
    fn keys(&self) -> [&str; 2] {
        [self.primary.as_str(), self.alternate.as_str()]
    }

    fn has_long_key(&self) -> bool {
        self.keys().iter().any(|k| k.len() >= MIN_DISTINCT_KEY)
    }

    /// True if the two words share a key (PRD §7.5 "phonetic key").
    ///
    /// A key of fewer than [`MIN_DISTINCT_KEY`] letters counts only when neither word has a
    /// longer one. "reno", "ryan", "ron" and "rhine" are all RN, which is also the primary key of
    /// "rainier" (its final "-ier" is read as French); "rainier" also has the alternate RNR, so it
    /// matches "rainer" and "rayneer" (RNR) but not "reno". Words with only short keys, such as
    /// "reyes" and "rays" (RS), still match each other.
    pub fn matches(&self, other: &Codes) -> bool {
        let (mine, theirs) = (self.keys(), other.keys());
        let long = self.has_long_key() || other.has_long_key();
        mine.iter()
            .any(|a| !a.is_empty() && (!long || a.len() >= MIN_DISTINCT_KEY) && theirs.contains(a))
    }
}

fn encoder() -> &'static DoubleMetaphone {
    static ENCODER: OnceLock<DoubleMetaphone> = OnceLock::new();
    ENCODER.get_or_init(|| DoubleMetaphone::new(None))
}

/// Fold common Latin accents to ASCII ("é" → "e"); other non-ASCII letters are dropped.
fn ascii_letter(c: char) -> Option<char> {
    if c.is_ascii_alphabetic() {
        return Some(c);
    }
    Some(match c.to_lowercase().next().unwrap_or(c) {
        'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' | 'ā' => 'a',
        'ç' | 'ć' | 'č' => 'c',
        'è' | 'é' | 'ê' | 'ë' | 'ē' => 'e',
        'ì' | 'í' | 'î' | 'ï' | 'ī' => 'i',
        'ñ' | 'ń' => 'n',
        'ò' | 'ó' | 'ô' | 'õ' | 'ö' | 'ø' | 'ō' => 'o',
        'ù' | 'ú' | 'û' | 'ü' | 'ū' => 'u',
        'ý' | 'ÿ' => 'y',
        'š' | 'ś' => 's',
        'ž' | 'ź' | 'ż' => 'z',
        _ => return None,
    })
}

/// Double Metaphone codes for one word (PRD §7.5).
///
/// Only ASCII letters reach the encoder: accents are folded and anything else is dropped.
/// `rphonetic` 4.0.0 indexes strings by byte and panics on some words with multi-byte letters,
/// such as "café", "señor" and "cé" ("Ó" alone happens to return an empty key); this was found by
/// the `parser_never_panics_on_arbitrary_text` property. Double Metaphone is defined for Latin
/// letters anyway.
pub fn codes(word: &str) -> Codes {
    let letters: String = word.chars().filter_map(ascii_letter).collect();
    if letters.is_empty() {
        return Codes {
            primary: String::new(),
            alternate: String::new(),
        };
    }
    let r = encoder().double_metaphone(&letters);
    Codes {
        primary: r.primary(),
        alternate: r.alternate(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn same(a: &str, b: &str) -> bool {
        codes(a).matches(&codes(b))
    }

    #[test]
    fn known_codes() {
        // Philips' published example: Smith → SM0 / XMT.
        // "-ier" at the end of a word is read as French (as in "Rogier"): the primary code drops
        // the final R and the alternate keeps it, so "rainier" is RN / RNR.
        assert_eq!(codes("rainier").primary, "RN");
        assert_eq!(codes("rainier").alternate, "RNR");
        assert_eq!(codes("rainer").primary, "RNR");
        assert_eq!(codes("smith").primary, "SM0");
        assert_eq!(codes("smith").alternate, "XMT");
        assert_eq!(codes("thumb").primary, "0M");
    }

    #[test]
    fn mishearings_share_a_key() {
        assert!(same("rainier", "rainer"));
        assert!(same("rainier", "rayneer"));
        assert!(same("reyes", "rays"));
        assert!(same("reyes", "raise"));
        assert!(same("yosemite", "yosemity"));
        assert!(same("canyon", "kanyon"));
        assert!(same("acadia", "akadia"));
        assert!(same("teton", "tetan"));
    }

    #[test]
    fn different_names_do_not() {
        assert!(!same("rainier", "reyes"));
        assert!(!same("canyon", "teton"));
        assert!(!same("golden", "grand"));
        assert!(!same("gate", "great"));
    }

    #[test]
    fn short_keys_count_only_between_short_keyed_words() {
        for w in ["reno", "ryan", "ron", "rhine", "rain", "run"] {
            assert_eq!(codes(w).primary, "RN", "{w}");
            assert!(!same("rainier", w), "{w} must not match rainier");
        }
        assert!(same("rainier", "ranier"), "both have RNR");
        assert!(
            same("reyes", "rays") && same("reyes", "reys"),
            "RS on both sides"
        );
    }

    #[test]
    fn non_ascii_never_reaches_the_encoder() {
        assert_eq!(codes("reyés"), codes("reyes"));
        // These panic inside rphonetic 4.0.0 when passed through unfolded.
        assert_eq!(codes("café"), codes("cafe"));
        assert_eq!(codes("señor"), codes("senor"));
        assert_eq!(codes("Ó").primary, "A");
        assert_eq!(codes("ⶀⶁ").primary, "");
        assert_eq!(codes("ⶀrainierⶁ"), codes("rainier"));
    }

    #[test]
    fn empty_and_symbols() {
        assert_eq!(codes("").primary, "");
        assert_eq!(codes("123").primary, "");
        assert!(!codes("").matches(&codes("")));
    }
}
