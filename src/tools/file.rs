use anyhow::{bail, Context, Result};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use similar::TextDiff;
use std::{fs, path::{Component, Path, PathBuf}};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileReadResult {
    pub path: String,
    pub bytes: usize,
    pub truncated: bool,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileWritePreview {
    pub path: String,
    pub diff: String,
    pub backup_path: Option<String>,
}

#[derive(Debug, Clone)]
pub struct FileTools {
    root: PathBuf,
}

impl FileTools {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn resolve(&self, rel: impl AsRef<str>) -> Result<PathBuf> {
        resolve_in_root(&self.root, rel.as_ref())
    }

    pub fn read(&self, rel: impl AsRef<str>, max_bytes: usize) -> Result<FileReadResult> {
        let rel = rel.as_ref();
        let path = self.resolve(rel)?;
        self.ensure_inside_project(&path)?;
        let bytes = fs::read(&path).with_context(|| format!("failed to read {}", rel))?;
        let truncated = bytes.len() > max_bytes;
        let slice = if truncated { &bytes[..max_bytes] } else { &bytes[..] };
        let content = String::from_utf8_lossy(slice).to_string();
        Ok(FileReadResult {
            path: rel.to_string(),
            bytes: bytes.len(),
            truncated,
            content,
        })
    }

    pub fn preview_write(&self, rel: impl AsRef<str>, new_content: &str) -> Result<FileWritePreview> {
        let rel = rel.as_ref();
        let path = self.resolve(rel)?;
        self.ensure_inside_project(&path)?;
        let old = fs::read_to_string(&path).unwrap_or_default();
        let diff = unified_diff(&old, new_content, rel);
        Ok(FileWritePreview {
            path: rel.to_string(),
            diff,
            backup_path: None,
        })
    }

    pub fn write(&self, rel: impl AsRef<str>, new_content: &str) -> Result<FileWritePreview> {
        let rel = rel.as_ref();
        let path = self.resolve(rel)?;
        self.ensure_inside_project(&path)?;
        let old = fs::read_to_string(&path).unwrap_or_default();
        let diff = unified_diff(&old, new_content, rel);
        let backup_path = if path.exists() {
            Some(self.backup_existing(rel, &path)?)
        } else {
            None
        };
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&path, new_content).with_context(|| format!("failed to write {}", rel))?;
        Ok(FileWritePreview {
            path: rel.to_string(),
            diff,
            backup_path: backup_path.map(|p| p.display().to_string()),
        })
    }

    pub fn replace(
        &self,
        rel: impl AsRef<str>,
        old: &str,
        new: &str,
        all: bool,
        apply: bool,
    ) -> Result<FileWritePreview> {
        let rel = rel.as_ref();
        let path = self.resolve(rel)?;
        self.ensure_inside_project(&path)?;
        let current = fs::read_to_string(&path)
            .with_context(|| format!("failed to read {} for replacement", rel))?;
        if !current.contains(old) {
            bail!("old text not found in {}", rel);
        }
        let next = if all {
            current.replace(old, new)
        } else {
            current.replacen(old, new, 1)
        };
        let diff = unified_diff(&current, &next, rel);
        if !apply {
            return Ok(FileWritePreview {
                path: rel.to_string(),
                diff,
                backup_path: None,
            });
        }
        let backup_path = self.backup_existing(rel, &path)?;
        fs::write(&path, next).with_context(|| format!("failed to write {}", rel))?;
        Ok(FileWritePreview {
            path: rel.to_string(),
            diff,
            backup_path: Some(backup_path.display().to_string()),
        })
    }

    fn ensure_inside_project(&self, path: &Path) -> Result<()> {
        let root = self
            .root
            .canonicalize()
            .with_context(|| format!("failed to canonicalize project root {}", self.root.display()))?;

        if path.exists() {
            let canon = path
                .canonicalize()
                .with_context(|| format!("failed to canonicalize {}", path.display()))?;
            if !canon.starts_with(&root) {
                bail!(
                    "path escapes project root through symlink: {} -> {}",
                    path.display(),
                    canon.display()
                );
            }
            return Ok(());
        }

        let mut ancestor = path.parent();
        while let Some(parent) = ancestor {
            if parent.exists() {
                let canon = parent
                    .canonicalize()
                    .with_context(|| format!("failed to canonicalize parent {}", parent.display()))?;
                if !canon.starts_with(&root) {
                    bail!(
                        "new file parent escapes project root through symlink: {} -> {}",
                        parent.display(),
                        canon.display()
                    );
                }
                return Ok(());
            }
            ancestor = parent.parent();
        }

        bail!("could not find an existing parent directory for {}", path.display())
    }

    fn backup_existing(&self, rel: &str, path: &Path) -> Result<PathBuf> {
        let stamp = Utc::now().format("%Y%m%dT%H%M%S%.3fZ");
        let safe = rel
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_' { c } else { '_' })
            .collect::<String>();
        let backup_dir = self.root.join(".aia/undo");
        fs::create_dir_all(&backup_dir)?;
        let backup_path = backup_dir.join(format!("{stamp}_{safe}"));
        fs::copy(path, &backup_path)?;
        Ok(backup_path)
    }
}

pub fn unified_diff(old: &str, new: &str, path: &str) -> String {
    TextDiff::from_lines(old, new)
        .unified_diff()
        .header(&format!("a/{path}"), &format!("b/{path}"))
        .to_string()
}

pub fn resolve_in_root(root: &Path, rel: &str) -> Result<PathBuf> {
    if rel.contains('\0') {
        bail!("path contains NUL byte");
    }
    let rel_path = Path::new(rel);
    if rel_path.is_absolute() {
        bail!("absolute paths are not allowed: {}", rel);
    }
    let mut clean = PathBuf::new();
    for component in rel_path.components() {
        match component {
            Component::Normal(part) => clean.push(part),
            Component::CurDir => {}
            Component::ParentDir => bail!("parent directory `..` is not allowed: {}", rel),
            Component::RootDir | Component::Prefix(_) => bail!("invalid project-relative path: {}", rel),
        }
    }
    Ok(root.join(clean))
}
