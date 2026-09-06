use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Component, Path, PathBuf};
use uuid::Uuid;

use crate::Result;

#[derive(Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub capture_content: bool,
    pub content_days: u64,
    pub metrics_days: u64,
    pub project_bytes: u64,
    pub inbox_bytes: u64,
    pub payload_bytes: u64,
    pub reserve_bytes: u64,
    pub log_files: u64,
    pub log_bytes: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            capture_content: true,
            content_days: 30,
            metrics_days: 180,
            project_bytes: 2 * 1024_u64.pow(3),
            inbox_bytes: 64 * 1024 * 1024,
            payload_bytes: 1024 * 1024,
            reserve_bytes: 1024 * 1024,
            log_files: 5,
            log_bytes: 10 * 1024 * 1024,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Project {
    pub id: Uuid,
    pub root: PathBuf,
    pub git_common_dir: Option<PathBuf>,
    #[serde(default = "empty_object")]
    overrides: Value,
}

fn empty_object() -> Value {
    serde_json::json!({})
}
fn version() -> u32 {
    1
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default = "version")]
    schema_version: u32,
    #[serde(default)]
    pub defaults: Limits,
    #[serde(default)]
    pub projects: Vec<Project>,
}

impl Project {
    pub fn limits(&self, defaults: &Limits) -> Result<Limits> {
        let mut value = serde_json::to_value(defaults)?;
        let overrides = self.overrides.as_object().ok_or("invalid overrides")?;
        for (key, item) in overrides {
            value[key] = item.clone();
        }
        let limits: Limits = serde_json::from_value(value)?;
        limits.validate()?;
        Ok(limits)
    }
}

impl Limits {
    fn validate(&self) -> Result<()> {
        if [
            self.content_days,
            self.metrics_days,
            self.project_bytes,
            self.inbox_bytes,
            self.payload_bytes,
            self.reserve_bytes,
            self.log_files,
            self.log_bytes,
        ]
        .contains(&0)
            || self.payload_bytes > self.inbox_bytes
            || self.inbox_bytes > self.project_bytes
        {
            return Err("invalid limits".into());
        }
        Ok(())
    }
}

impl Config {
    pub fn read(path: &Path) -> Result<Self> {
        let config: Self = toml::from_str(&std::fs::read_to_string(path)?)?;
        if config.schema_version != 1 {
            return Err("unsupported config".into());
        }
        config.defaults.validate()?;
        for (index, project) in config.projects.iter().enumerate() {
            for path in std::iter::once(&project.root).chain(project.git_common_dir.iter()) {
                if !path.is_absolute() || path.components().any(|c| c == Component::ParentDir) {
                    return Err("invalid project path".into());
                }
            }
            project.limits(&config.defaults)?;
            for other in &config.projects[..index] {
                let left = normalized(&project.root);
                let right = normalized(&other.root);
                let same_git = project
                    .git_common_dir
                    .as_ref()
                    .zip(other.git_common_dir.as_ref())
                    .is_some_and(|(a, b)| normalized(a) == normalized(b));
                let overlap = project.git_common_dir.is_none()
                    && other.git_common_dir.is_none()
                    && (left.starts_with(&right) || right.starts_with(&left));
                if project.id == other.id || left == right || same_git || overlap {
                    return Err("conflicting project identity".into());
                }
            }
        }
        Ok(config)
    }
}

pub fn normalized(path: &Path) -> PathBuf {
    let resolved = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    #[cfg(windows)]
    {
        let text = resolved.to_string_lossy();
        let text = if let Some(tail) = text.strip_prefix(r"\\?\UNC\") {
            format!(r"\\{tail}")
        } else {
            text.strip_prefix(r"\\?\").unwrap_or(&text).to_string()
        };
        PathBuf::from(text.replace('/', r"\").to_lowercase())
    }
    #[cfg(not(windows))]
    {
        resolved
    }
}
