//! Utterance normalisation: lower-case, punctuation, contractions and filler words (PRD §7.3 step 1).
//!
//! [`tokenize`] turns a transcript into plain lower-case tokens:
//! - `"Is Rainier out tomorrow at 4?"` → `is rainier out tomorrow at 4`;
//! - `"4:30 p.m."` → `4:30 pm`, `"4pm"` → `4 pm`, `"o'clock"` → `oclock`;
//! - `"Rainier's"` → `rainier`, `"what's"` → `what is`, `"twenty-one"` → `twenty one`;
//! - disfluencies (`um`, `uh`, `er`, ...) are dropped.
//!
//! Filler and carrier words are removed later, by the parser, in two passes so that they never
//! break a place name or a time phrase: multi-word carriers ([`MULTIWORD_FILLERS`]) before the
//! time grammar runs, and single filler words ([`FILLER_WORDS`]) after it, never inside an alias.
//! Any other word must be explained by the time grammar or a place name; the parser hands the
//! utterance to the fallback rather than drop it (PRD §7.3).

/// Disfluencies removed during tokenisation.
pub const DISFLUENCIES: &[&str] = &[
    "um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "hmm", "hm", "mm", "mmm",
];

/// Carrier phrases removed before the time grammar runs (longest first).
pub const MULTIWORD_FILLERS: &[&[&str]] = &[
    &["i", "would", "like", "to", "see"],
    &["is", "it", "going", "to", "be"],
    &["can", "you", "show", "me"],
    &["could", "you", "show", "me"],
    &["would", "you", "show", "me"],
    &["is", "it", "gonna", "be"],
    &["i", "want", "to", "see"],
    &["i", "would", "like"],
    &["i", "wanna", "see"],
    &["can", "i", "see"],
    &["could", "i", "see"],
    &["going", "to", "be"],
    &["will", "it", "be"],
    &["let", "me", "see"],
    &["let", "us", "see"],
    &["looking", "like"],
    &["look", "like"],
    &["looks", "like"],
    // The product's name (D-080), said to the app as "hey snow globe, ...". "lookout", its name
    // before the rename, stays a single filler word below.
    &["snow", "globe"],
    &["gonna", "be"],
    &["show", "me"],
    &["give", "me"],
    &["tell", "me"],
    &["what", "about"],
    &["how", "about"],
    &["what", "does"],
    &["how", "does"],
    &["what", "will"],
    &["how", "will"],
    &["what", "is"],
    &["how", "is"],
    &["view", "of"],
    &["i", "am"],
];

/// Single filler and carrier words removed after the time grammar has taken its words: carrier
/// words, and words about what the picture shows (weather, sky, light), which name neither a
/// place nor a time.
///
/// Words inside an exact alias (for example "look" in "look rock") are never removed.
pub const FILLER_WORDS: &[&str] = &[
    "a",
    "about",
    "actually",
    "again",
    "also",
    "an",
    "and",
    "approximately",
    "are",
    "around",
    "at",
    "be",
    "by",
    "can",
    "check",
    "clear",
    "cloud",
    "clouds",
    "cloudy",
    "color",
    "colors",
    "colour",
    "colours",
    "conditions",
    "could",
    "did",
    "do",
    "does",
    "fog",
    "foggy",
    "for",
    "forecast",
    "give",
    "going",
    "gonna",
    "good",
    "haze",
    "hazy",
    "here",
    "hey",
    "how",
    "i",
    "id",
    "image",
    "images",
    "in",
    "is",
    "it",
    "its",
    "just",
    "know",
    "light",
    "lighting",
    "like",
    "look",
    "looking",
    "lookout",
    "looks",
    "maybe",
    "me",
    "mist",
    "misty",
    "my",
    "near",
    "nice",
    "of",
    "ok",
    "okay",
    "on",
    "or",
    "our",
    "out",
    "over",
    "photo",
    "photos",
    "pic",
    "pics",
    "picture",
    "pictures",
    "please",
    "pretty",
    "quick",
    "quickly",
    "rain",
    "raining",
    "really",
    "roughly",
    "see",
    "show",
    "skies",
    "sky",
    "smoggy",
    "smoke",
    "snapshot",
    "snow",
    "snowglobe",
    "snowing",
    "so",
    "stars",
    "storm",
    "storms",
    "stormy",
    "sun",
    "sunlight",
    "sunny",
    "sunshine",
    "tell",
    "temperature",
    "the",
    "then",
    "there",
    "think",
    "to",
    "view",
    "visibility",
    "visible",
    "wanna",
    "want",
    "was",
    "we",
    "weather",
    "were",
    "what",
    "whats",
    "will",
    "wind",
    "windy",
    "wonder",
    "wondering",
    "would",
    "you",
    "your",
];

/// Words that name no place in particular. They are ignored when fuzzy-matching place text, and
/// a place text made only of them counts as "no place said" (PRD §7.4 follow-up rule).
pub const GENERIC_PLACE_WORDS: &[&str] = &[
    "national",
    "park",
    "parks",
    "seashore",
    "area",
    "place",
    "spot",
    "viewpoint",
    "camera",
    "cam",
    "webcam",
    "the",
    "of",
    "at",
    "in",
    "on",
];

/// Words for a kind of place ("beach", "mountain", "science center") and the modifiers that start
/// many names ("big", "grand", "mount", "point", "south"). Each is part of the names of many places
/// Snow Globe does not cover ("Pebble Beach", "Stone Mountain", "Sunset Point", "Big Sur"), so a fuzzy
/// or phonetic match must rest on some other word or on the whole name (PRD §7.5: a miss returns
/// the closest places, never a guess). Unlike [`GENERIC_PLACE_WORDS`] they still count towards the
/// similarity score, so "smokey mountain" matches "smoky mountains".
pub const FEATURE_WORDS: &[&str] = &[
    "bay",
    "beach",
    "beaches",
    "big",
    "bridge",
    "campground",
    "canyon",
    "canyons",
    "center",
    "centre",
    "city",
    "cove",
    "creek",
    "dome",
    "east",
    "falls",
    "forest",
    "grand",
    "great",
    "harbor",
    "harbour",
    "hill",
    "hills",
    "inn",
    "island",
    "islands",
    "junction",
    "lake",
    "lakes",
    "lighthouse",
    "little",
    "lodge",
    "lower",
    "meadow",
    "meadows",
    "mesa",
    "mount",
    "mountain",
    "mountains",
    "mt",
    "museum",
    "north",
    "pass",
    "peak",
    "peaks",
    "point",
    "pt",
    "range",
    "ridge",
    "rim",
    "river",
    "rock",
    "rocks",
    "school",
    "science",
    "south",
    "springs",
    "station",
    "summit",
    "tower",
    "town",
    "trail",
    "upper",
    "valley",
    "village",
    "visitor",
    "volcano",
    "waterfall",
    "west",
];

/// True if `word` is a filler or carrier word.
pub fn is_filler(word: &str) -> bool {
    FILLER_WORDS.contains(&word)
}

/// True if `word` names no place in particular.
pub fn is_generic_place_word(word: &str) -> bool {
    GENERIC_PLACE_WORDS.contains(&word)
}

/// True if `word` names a kind of place rather than a place ([`FEATURE_WORDS`]).
pub fn is_feature_word(word: &str) -> bool {
    FEATURE_WORDS.contains(&word)
}

/// Split a transcript into normalised lower-case tokens (PRD §7.3 step 1).
pub fn tokenize(utterance: &str) -> Vec<String> {
    let mut text = String::with_capacity(utterance.len() + 8);
    for ch in utterance.to_lowercase().chars() {
        match ch {
            '\u{2018}' | '\u{2019}' | '\u{02BC}' | '`' | '\u{00B4}' => text.push('\''),
            '\u{2013}' | '\u{2014}' | '\u{2212}' | '_' => text.push(' '),
            '&' => text.push_str(" and "),
            '+' => text.push_str(" plus "),
            _ => text.push(ch),
        }
    }
    let mut out = Vec::new();
    for piece in text.split(|c: char| {
        c.is_whitespace()
            || matches!(
                c,
                ',' | ';'
                    | '!'
                    | '?'
                    | '('
                    | ')'
                    | '"'
                    | '['
                    | ']'
                    | '{'
                    | '}'
                    | '\u{201C}'
                    | '\u{201D}'
                    | '\u{2026}'
            )
    }) {
        clean_piece(piece, &mut out);
    }
    out.retain(|t| !DISFLUENCIES.contains(&t.as_str()));
    out
}

fn trim_edges(s: &str) -> &str {
    s.trim_matches(|c: char| matches!(c, '.' | '\'' | ':' | '-' | '/' | '*' | '#' | '~'))
}

fn meridiem_word(s: &str) -> Option<&'static str> {
    match s {
        "am" | "a.m" | "a.m." | "a.m.." => Some("am"),
        "pm" | "p.m" | "p.m." | "p.m.." => Some("pm"),
        _ => None,
    }
}

fn is_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// `4`, `4:30`, `16:00`: a clock-shaped numeric token.
fn is_clockish(s: &str) -> bool {
    match s.split_once(':') {
        Some((h, m)) => is_digits(h) && h.len() <= 2 && is_digits(m) && m.len() == 2,
        None => is_digits(s) && s.len() <= 2,
    }
}

fn clean_piece(raw: &str, out: &mut Vec<String>) {
    let t = trim_edges(raw);
    if t.is_empty() {
        return;
    }
    if let Some(m) = meridiem_word(t) {
        out.push(m.to_string());
        return;
    }
    // Contractions and possessives.
    if let Some(stem) = t.strip_suffix("'s") {
        match stem {
            "what" | "that" | "it" | "how" | "where" | "when" | "there" | "here" | "who" => {
                out.push(stem.to_string());
                out.push("is".to_string());
            }
            "let" => {
                out.push("let".to_string());
                out.push("us".to_string());
            }
            _ => clean_piece(stem, out),
        }
        return;
    }
    match t {
        "i'm" | "im" => {
            out.push("i".to_string());
            out.push("am".to_string());
            return;
        }
        "i'd" => {
            out.push("i".to_string());
            out.push("would".to_string());
            return;
        }
        "o'clock" | "oclock" | "o'clock." => {
            out.push("oclock".to_string());
            return;
        }
        _ => {}
    }
    if t.contains('\'') {
        let joined: String = t.chars().filter(|&c| c != '\'').collect();
        clean_piece(&joined, out);
        return;
    }
    // Hyphenated words ("twenty-one", "point-reyes") become separate tokens.
    if t.contains('-') {
        for part in t.split('-') {
            clean_piece(part, out);
        }
        return;
    }
    // Digits glued to a meridiem or "oclock": "4pm", "4:30p.m.", "4am", "4oclock".
    if let Some(split) = split_number_suffix(t) {
        out.extend(split);
        return;
    }
    // Numeric forms are kept whole: 4:30, 11/21, 11/21/2026, 1.5, 21st.
    if is_numeric_form(t) {
        out.push(t.to_string());
        return;
    }
    // Words with internal dots ("mt.rainier") split; other symbols are dropped.
    if t.contains('.') || t.contains('/') || t.contains(':') {
        for part in t.split(['.', '/', ':']) {
            clean_piece(part, out);
        }
        return;
    }
    let word: String = t.chars().filter(|c| c.is_alphanumeric()).collect();
    if word.is_empty() {
        return;
    }
    if word != t {
        // Symbols were stripped ("0a$" → "0a"): clean the result again so that tokenising the
        // output gives the output back (the canonical form must be a fixed point, PRD §7.6).
        clean_piece(&word, out);
    } else {
        out.push(word);
    }
}

fn split_number_suffix(t: &str) -> Option<Vec<String>> {
    let idx = t.find(|c: char| !(c.is_ascii_digit() || c == ':'))?;
    if idx == 0 {
        return None;
    }
    let (num, rest) = t.split_at(idx);
    if !is_clockish(num) {
        return None;
    }
    let suffix = match rest {
        "am" | "a" | "a.m" | "a.m." => "am",
        "pm" | "p" | "p.m" | "p.m." => "pm",
        "oclock" | "o'clock" => "oclock",
        "h" | "hr" | "hrs" => "hours",
        "min" | "mins" => "minutes",
        _ => return None,
    };
    Some(vec![num.to_string(), suffix.to_string()])
}

fn is_numeric_form(t: &str) -> bool {
    if is_clockish(t) || is_digits(t) {
        return true;
    }
    // Ordinals: 1st 2nd 3rd 4th 21st.
    for suffix in ["st", "nd", "rd", "th"] {
        if let Some(n) = t.strip_suffix(suffix)
            && is_digits(n)
            && n.len() <= 2
        {
            return true;
        }
    }
    // Slash dates: 11/21 or 11/21/2026 or 11/21/26.
    let parts: Vec<&str> = t.split('/').collect();
    if (parts.len() == 2 || parts.len() == 3) && parts.iter().all(|p| is_digits(p) && p.len() <= 4)
    {
        return parts[0].len() <= 2 && parts[1].len() <= 2;
    }
    // Decimals: 1.5
    if let Some((a, b)) = t.split_once('.') {
        return is_digits(a) && is_digits(b);
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    fn toks(s: &str) -> String {
        tokenize(s).join(" ")
    }

    #[test]
    fn lowercases_and_strips_punctuation() {
        assert_eq!(
            toks("Is Rainier out tomorrow at 4?"),
            "is rainier out tomorrow at 4"
        );
        assert_eq!(toks("  POINT   REYES, now!  "), "point reyes now");
        assert_eq!(toks("Mt. Rainier"), "mt rainier");
    }

    #[test]
    fn meridiem_forms() {
        assert_eq!(toks("4:30 p.m."), "4:30 pm");
        assert_eq!(toks("4pm"), "4 pm");
        assert_eq!(toks("4:30PM"), "4:30 pm");
        assert_eq!(toks("9 A.M."), "9 am");
        assert_eq!(toks("7a.m."), "7 am");
        assert_eq!(toks("at 9p"), "at 9 pm");
    }

    #[test]
    fn contractions_and_possessives() {
        assert_eq!(toks("What's Rainier's view"), "what is rainier view");
        assert_eq!(toks("four o\u{2019}clock"), "four oclock");
        assert_eq!(toks("4 o'clock"), "4 oclock");
        assert_eq!(toks("4oclock"), "4 oclock");
        assert_eq!(toks("tomorrow's sunset"), "tomorrow sunset");
        assert_eq!(toks("I'm wondering"), "i am wondering");
        assert_eq!(toks("don't"), "dont");
    }

    #[test]
    fn hyphens_and_numbers() {
        assert_eq!(toks("twenty-one"), "twenty one");
        assert_eq!(toks("point-reyes"), "point reyes");
        assert_eq!(toks("11/21"), "11/21");
        assert_eq!(toks("11/21/2026"), "11/21/2026");
        assert_eq!(toks("the 21st"), "the 21st");
        assert_eq!(toks("in 1.5 hours"), "in 1.5 hours");
        assert_eq!(toks("in 2h"), "in 2 hours");
        assert_eq!(toks("16:00"), "16:00");
    }

    #[test]
    fn tokenising_tokens_gives_them_back() {
        for s in [
            "0a$:",
            "4p#",
            "Rainier!!",
            "ж-4pm",
            "11/21/2026.",
            "a.m.p.m.",
            "o'clock's",
        ] {
            let once = tokenize(s);
            assert_eq!(tokenize(&once.join(" ")), once, "{s:?}");
        }
    }

    #[test]
    fn disfluencies_are_dropped() {
        assert_eq!(toks("um, show me, uh, Rainier"), "show me rainier");
    }

    #[test]
    fn ampersand_and_dashes() {
        assert_eq!(toks("rainier & point reyes"), "rainier and point reyes");
        assert_eq!(toks("rainier \u{2014} tomorrow"), "rainier tomorrow");
    }

    #[test]
    fn filler_tables_hold_no_time_or_place_words() {
        // These words carry meaning for the time grammar or name places, so they must never be
        // fillers: removing them would change the answer. "moon" stays too: "the moon at
        // midnight" names a place Snow Globe does not have, and must miss rather than follow up.
        for w in [
            "from", "now", "next", "this", "half", "past", "noon", "mountain", "point", "grand",
            "rock", "moon", "smoky", "great",
        ] {
            assert!(!is_filler(w), "{w} must not be a filler word");
        }
    }

    #[test]
    fn the_product_names_are_fillers_but_snow_alone_stays_a_weather_word() {
        // "hey snow globe" and "hey lookout" address the app (D-080); "snow" on its own is a
        // weather word, and "globe" on its own is not a filler (Globe, Arizona is a place).
        assert!(MULTIWORD_FILLERS.contains(&&["snow", "globe"][..]));
        assert!(is_filler("snowglobe") && is_filler("lookout") && is_filler("snow"));
        assert!(!is_filler("globe"));
    }

    #[test]
    fn word_lists_are_sorted_and_disjoint() {
        for list in [FILLER_WORDS, FEATURE_WORDS, GENERIC_PLACE_WORDS] {
            for w in list {
                assert_eq!(tokenize(w), vec![w.to_string()], "{w:?} is not one token");
            }
        }
        for list in [FILLER_WORDS, FEATURE_WORDS] {
            assert!(list.windows(2).all(|p| p[0] < p[1]), "keep the list sorted");
        }
        for w in FEATURE_WORDS {
            assert!(!is_filler(w) && !is_generic_place_word(w), "{w}");
        }
    }
}
