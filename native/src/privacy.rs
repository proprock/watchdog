use crate::Result;
use regex::Regex;
use serde_json::{Map, Value};

pub struct Redactor {
    key: Regex,
    patterns: Vec<Regex>,
}

impl Redactor {
    pub fn new() -> Result<Self> {
        let patterns = [
            r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END (?:[A-Z ]*PRIVATE KEY)-----|\z)",
            r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b",
            r#"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>]+"#,
            r#"(?i)(?:password|passwd|secret(?:[_-]access[_-]key)?|token|api[_-]?key|authorization|cookie)[\"']?\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"#,
            r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@",
        ];
        Ok(Self {
            key: Regex::new(
                r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key)",
            )?,
            patterns: patterns
                .into_iter()
                .map(Regex::new)
                .collect::<std::result::Result<_, _>>()?,
        })
    }

    pub fn text(&self, value: &str) -> String {
        self.patterns
            .iter()
            .fold(value.to_string(), |value, pattern| {
                pattern.replace_all(&value, "[REDACTED]").into_owned()
            })
    }

    pub fn value(&self, value: &Value) -> Value {
        match value {
            Value::String(text) => Value::String(self.text(text)),
            Value::Array(items) => Value::Array(items.iter().map(|v| self.value(v)).collect()),
            Value::Object(items) => {
                let mut result = Map::new();
                for (key, item) in items {
                    let mut safe_key = self.text(key);
                    if result.contains_key(&safe_key) {
                        safe_key = format!("{safe_key}:{}", result.len());
                    }
                    result.insert(
                        safe_key,
                        if self.key.is_match(key) && !matches!(item, Value::Null | Value::Number(_))
                        {
                            Value::String("[REDACTED]".into())
                        } else {
                            self.value(item)
                        },
                    );
                }
                Value::Object(result)
            }
            _ => value.clone(),
        }
    }
}
