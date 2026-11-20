//! Accelerator strings ("Control+Shift+Space") to macOS virtual key codes and `CGEventFlags`, for
//! the `CGEventPost` injector (PLAN.md M1 1a check 9; PRD §10.4 "Replay injects key events with
//! CGEventPost at the HID tap, so the real hotkey path fires").
//!
//! The token names follow `global-hotkey` 0.8's parser (what `tauri-plugin-global-shortcut`
//! 2.4.1 uses), and the key codes follow its macOS table, so the injector posts exactly the key
//! the app registered. Only the keys a hold-to-talk hotkey plausibly uses are supported.

use serde::Serialize;

/// Modifier keys of an accelerator.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Mods {
    pub control: bool,
    pub shift: bool,
    pub option: bool,
    pub command: bool,
}

/// `kCGEventFlagMaskShift` etc. (device-independent bits).
pub const FLAG_SHIFT: u64 = 0x0002_0000;
pub const FLAG_CONTROL: u64 = 0x0004_0000;
pub const FLAG_OPTION: u64 = 0x0008_0000;
pub const FLAG_COMMAND: u64 = 0x0010_0000;
/// Device-dependent bits a real left-hand modifier also sets (IOLLEvent.h NX_DEVICEL*KEYMASK).
pub const DEV_LCONTROL: u64 = 0x0000_0001;
pub const DEV_LSHIFT: u64 = 0x0000_0002;
pub const DEV_LCOMMAND: u64 = 0x0000_0008;
pub const DEV_LOPTION: u64 = 0x0000_0020;

/// Virtual key codes of the left-hand modifier keys (Events.h kVK_*).
pub const KVK_COMMAND: u16 = 0x37;
pub const KVK_SHIFT: u16 = 0x38;
pub const KVK_OPTION: u16 = 0x3a;
pub const KVK_CONTROL: u16 = 0x3b;

/// One modifier key: its key code and the flag bits it contributes while held.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct ModKey {
    pub key_code: u16,
    pub flags: u64,
    pub name: &'static str,
}

impl Mods {
    /// The modifier keys in the order a person usually presses them (Control, Option, Shift,
    /// Command); the injector presses them in this order and releases them in reverse.
    pub fn keys(&self) -> Vec<ModKey> {
        let mut v = Vec::new();
        if self.control {
            v.push(ModKey {
                key_code: KVK_CONTROL,
                flags: FLAG_CONTROL | DEV_LCONTROL,
                name: "control",
            });
        }
        if self.option {
            v.push(ModKey {
                key_code: KVK_OPTION,
                flags: FLAG_OPTION | DEV_LOPTION,
                name: "option",
            });
        }
        if self.shift {
            v.push(ModKey {
                key_code: KVK_SHIFT,
                flags: FLAG_SHIFT | DEV_LSHIFT,
                name: "shift",
            });
        }
        if self.command {
            v.push(ModKey {
                key_code: KVK_COMMAND,
                flags: FLAG_COMMAND | DEV_LCOMMAND,
                name: "command",
            });
        }
        v
    }

    /// All flag bits with every modifier held.
    pub fn flags(&self) -> u64 {
        self.keys().iter().fold(0, |acc, k| acc | k.flags)
    }

    /// True when there is at least one modifier.
    pub fn any(&self) -> bool {
        self.control || self.shift || self.option || self.command
    }

    /// macOS 15+ rejects global shortcuts whose only modifiers are Option or Option+Shift
    /// (PRD §5.4); this flags that case before the app tries to register it.
    pub fn is_option_only(&self) -> bool {
        self.option && !self.control && !self.command
    }
}

/// A parsed accelerator.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Accel {
    pub mods: Mods,
    pub key_code: u16,
    pub key_name: String,
}

/// Parses an accelerator with `global-hotkey`'s token names: modifiers `Control`/`Ctrl`,
/// `Shift`, `Option`/`Alt`, `Command`/`Cmd`/`Super`, `CmdOrCtrl` (Command on macOS), then one key.
pub fn parse(s: &str) -> Result<Accel, String> {
    let tokens: Vec<&str> = s.split('+').map(str::trim).collect();
    if tokens.iter().any(|t| t.is_empty()) {
        return Err(format!("empty token in {s:?}"));
    }
    let mut mods = Mods::default();
    let mut key: Option<(u16, String)> = None;
    for t in &tokens {
        if key.is_some() {
            return Err(format!("{s:?}: the key must come last and only once"));
        }
        match t.to_ascii_uppercase().as_str() {
            "CONTROL" | "CTRL" if tokens.len() > 1 => mods.control = true,
            "SHIFT" if tokens.len() > 1 => mods.shift = true,
            "OPTION" | "ALT" if tokens.len() > 1 => mods.option = true,
            "COMMAND" | "CMD" | "SUPER" | "COMMANDORCONTROL" | "COMMANDORCTRL" | "CMDORCTRL"
            | "CMDORCONTROL"
                if tokens.len() > 1 =>
            {
                mods.command = true
            }
            other => {
                let code =
                    key_code(other).ok_or_else(|| format!("unsupported key {t:?} in {s:?}"))?;
                key = Some((code, (*t).to_string()));
            }
        }
    }
    let (key_code, key_name) = key.ok_or_else(|| format!("{s:?} has no key"))?;
    Ok(Accel {
        mods,
        key_code,
        key_name,
    })
}

/// macOS virtual key code for a `global-hotkey` key token (upper case). Same values as
/// `global-hotkey` 0.8 `key_to_scancode`.
pub fn key_code(token_upper: &str) -> Option<u16> {
    let letters = [
        ('A', 0x00),
        ('S', 0x01),
        ('D', 0x02),
        ('F', 0x03),
        ('H', 0x04),
        ('G', 0x05),
        ('Z', 0x06),
        ('X', 0x07),
        ('C', 0x08),
        ('V', 0x09),
        ('B', 0x0b),
        ('Q', 0x0c),
        ('W', 0x0d),
        ('E', 0x0e),
        ('R', 0x0f),
        ('Y', 0x10),
        ('T', 0x11),
        ('O', 0x1f),
        ('U', 0x20),
        ('I', 0x22),
        ('P', 0x23),
        ('L', 0x25),
        ('J', 0x26),
        ('K', 0x28),
        ('N', 0x2d),
        ('M', 0x2e),
    ];
    let digits = [
        ('1', 0x12),
        ('2', 0x13),
        ('3', 0x14),
        ('4', 0x15),
        ('6', 0x16),
        ('5', 0x17),
        ('9', 0x19),
        ('7', 0x1a),
        ('8', 0x1c),
        ('0', 0x1d),
    ];
    let t = token_upper;
    // A bare letter or digit, or the global-hotkey spellings KeyX / DigitN.
    let bare = |prefix: &str| -> Option<char> {
        let rest = t.strip_prefix(prefix).unwrap_or(t);
        let mut it = rest.chars();
        let c = it.next()?;
        it.next().is_none().then_some(c)
    };
    let letter = if t.len() == 1 || (t.starts_with("KEY") && t.len() == 4) {
        bare("KEY")
    } else {
        None
    };
    if let Some(&(_, code)) = letter.and_then(|c| letters.iter().find(|(l, _)| *l == c)) {
        return Some(code);
    }
    let digit = if t.len() == 1 || (t.starts_with("DIGIT") && t.len() == 6) {
        bare("DIGIT")
    } else {
        None
    };
    if let Some(&(_, code)) = digit.and_then(|c| digits.iter().find(|(d, _)| *d == c)) {
        return Some(code);
    }
    let named = match t {
        "SPACE" => 0x31,
        "BACKQUOTE" | "`" => 0x32,
        "ENTER" => 0x24,
        "TAB" => 0x30,
        "ESCAPE" | "ESC" => 0x35,
        "F1" => 0x7a,
        "F2" => 0x78,
        "F3" => 0x63,
        "F4" => 0x76,
        "F5" => 0x60,
        "F6" => 0x61,
        "F7" => 0x62,
        "F8" => 0x64,
        "F9" => 0x65,
        "F10" => 0x6d,
        "F11" => 0x67,
        "F12" => 0x6f,
        "F13" => 0x69,
        "F14" => 0x6b,
        "F15" => 0x71,
        "F16" => 0x6a,
        "F17" => 0x40,
        "F18" => 0x4f,
        "F19" => 0x50,
        "F20" => 0x5a,
        _ => return None,
    };
    Some(named)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_default_hotkey() {
        let a = parse("Control+Shift+Space").expect("parse");
        assert_eq!(a.key_code, 0x31);
        assert!(a.mods.control && a.mods.shift && !a.mods.option && !a.mods.command);
        assert_eq!(
            a.mods.flags(),
            FLAG_CONTROL | FLAG_SHIFT | DEV_LCONTROL | DEV_LSHIFT
        );
        let keys = a.mods.keys();
        assert_eq!(
            keys.iter().map(|k| k.key_code).collect::<Vec<_>>(),
            vec![KVK_CONTROL, KVK_SHIFT]
        );
        assert!(!a.mods.is_option_only());
    }

    #[test]
    fn accepts_global_hotkey_token_spellings() {
        assert_eq!(parse("ctrl+alt+KeyL").expect("p").key_code, 0x25);
        assert_eq!(parse("CmdOrCtrl+Shift+5").expect("p").key_code, 0x17);
        assert_eq!(parse("Super+Digit0").expect("p").key_code, 0x1d);
        assert_eq!(parse("Control+`").expect("p").key_code, 0x32);
        assert_eq!(parse("F5").expect("p").key_code, 0x60);
        assert!(parse("CmdOrCtrl+K").expect("p").mods.command);
    }

    #[test]
    fn rejects_malformed_accelerators() {
        for bad in [
            "",
            "Control+",
            "Control+Shift",
            "Space+Control",
            "Control+Q+W",
            "Control+Hyper",
        ] {
            assert!(parse(bad).is_err(), "{bad:?} should be rejected");
        }
    }

    #[test]
    fn flags_option_only_shortcuts() {
        assert!(parse("Option+Space").expect("p").mods.is_option_only());
        assert!(
            parse("Option+Shift+Space")
                .expect("p")
                .mods
                .is_option_only()
        );
        assert!(
            !parse("Control+Option+Space")
                .expect("p")
                .mods
                .is_option_only()
        );
    }

    #[test]
    fn key_codes_match_events_h() {
        // Spot checks against Carbon Events.h kVK_ANSI_* values.
        assert_eq!(key_code("A"), Some(0x00));
        assert_eq!(key_code("M"), Some(0x2e));
        assert_eq!(key_code("KEYZ"), Some(0x06));
        assert_eq!(key_code("9"), Some(0x19));
        assert_eq!(key_code("F12"), Some(0x6f));
        assert_eq!(key_code("KEYAA"), None);
    }
}
