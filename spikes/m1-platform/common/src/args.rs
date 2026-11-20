//! A tiny command-line parser for the spike binaries: `<command> [--key value | --flag]...`.
//! Unknown options are an error, so a typo never silently changes a measured run.

use std::collections::BTreeMap;
use std::str::FromStr;

/// Parsed arguments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    /// The first positional argument, if any.
    pub command: Option<String>,
    /// Further positional arguments.
    pub positional: Vec<String>,
    values: BTreeMap<String, String>,
    flags: Vec<String>,
    used: std::cell::RefCell<Vec<String>>,
}

impl Args {
    /// Parses `argv` (without the program name). `flag_names` lists options that take no value.
    pub fn parse<I: IntoIterator<Item = String>>(
        argv: I,
        flag_names: &[&str],
    ) -> Result<Self, String> {
        let mut command = None;
        let mut positional = Vec::new();
        let mut values = BTreeMap::new();
        let mut flags = Vec::new();
        let mut it = argv.into_iter().peekable();
        while let Some(a) = it.next() {
            if let Some(name) = a.strip_prefix("--") {
                if name.is_empty() {
                    return Err("empty option name".into());
                }
                let (name, inline) = match name.split_once('=') {
                    Some((n, v)) => (n.to_string(), Some(v.to_string())),
                    None => (name.to_string(), None),
                };
                if flag_names.contains(&name.as_str()) {
                    if inline.is_some() {
                        return Err(format!("--{name} takes no value"));
                    }
                    flags.push(name);
                } else {
                    let v = match inline {
                        Some(v) => v,
                        None => it.next().ok_or_else(|| format!("--{name} needs a value"))?,
                    };
                    if values.insert(name.clone(), v).is_some() {
                        return Err(format!("--{name} given twice"));
                    }
                }
            } else if command.is_none() {
                command = Some(a);
            } else {
                positional.push(a);
            }
        }
        Ok(Self {
            command,
            positional,
            values,
            flags,
            used: std::cell::RefCell::new(Vec::new()),
        })
    }

    fn mark(&self, name: &str) {
        self.used.borrow_mut().push(name.to_string());
    }

    /// The value of `--name`, if given.
    pub fn get(&self, name: &str) -> Option<&str> {
        self.mark(name);
        self.values.get(name).map(String::as_str)
    }

    /// The value of `--name` parsed as `T`, or `default` when absent.
    pub fn get_or<T: FromStr>(&self, name: &str, default: T) -> Result<T, String>
    where
        T::Err: std::fmt::Display,
    {
        match self.get(name) {
            None => Ok(default),
            Some(v) => v.parse::<T>().map_err(|e| format!("--{name} {v:?}: {e}")),
        }
    }

    /// True when the flag `--name` was given.
    pub fn flag(&self, name: &str) -> bool {
        self.mark(name);
        self.flags.iter().any(|f| f == name)
    }

    /// Errors if any option was given that the program never asked about.
    pub fn finish(&self) -> Result<(), String> {
        let used = self.used.borrow();
        for k in self.values.keys().chain(self.flags.iter()) {
            if !used.iter().any(|u| u == k) {
                return Err(format!("unknown option --{k}"));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(s: &str, flags: &[&str]) -> Result<Args, String> {
        Args::parse(s.split_whitespace().map(str::to_string), flags)
    }

    #[test]
    fn parses_command_values_and_flags() {
        let a = parse(
            "hotkey --trials 4 --hotkey=Control+Shift+Space --print-text",
            &["print-text"],
        )
        .expect("parse");
        assert_eq!(a.command.as_deref(), Some("hotkey"));
        assert_eq!(a.get_or("trials", 10u32), Ok(4));
        assert_eq!(a.get("hotkey"), Some("Control+Shift+Space"));
        assert!(a.flag("print-text"));
        assert_eq!(a.get_or("idle-secs", 30u64), Ok(30));
        assert!(a.finish().is_ok());
    }

    #[test]
    fn rejects_unknown_missing_and_repeated_options() {
        let a = parse("cpal --trails 3", &[]).expect("parse");
        assert_eq!(a.get_or("trials", 5u32), Ok(5));
        assert_eq!(a.finish(), Err("unknown option --trails".into()));
        assert!(parse("cpal --trials", &[]).is_err());
        assert!(parse("cpal --trials 1 --trials 2", &[]).is_err());
        assert!(parse("cpal --flag=x", &["flag"]).is_err());
        let bad = parse("cpal --trials many", &[]).expect("parse");
        assert!(bad.get_or("trials", 1u32).is_err());
    }
}
