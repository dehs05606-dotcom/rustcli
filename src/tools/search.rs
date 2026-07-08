use anyhow::{Context, Result};
use ignore::WalkBuilder;
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::{fs, path::{Path, PathBuf}};
use tokio::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchMatch {
    pub path: String,
    pub line: usize,
    pub text: String,
}

#[derive(Debug, Clone)]
pub struct SearchTool {
    root: PathBuf,
}

impl SearchTool {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub async fn regex_search(
        &self,
        pattern: &str,
        path: impl AsRef<str>,
        max_results: usize,
    ) -> Result<Vec<SearchMatch>> {
        if which::which("rg").is_ok() {
            match self.search_with_rg(pattern, path.as_ref(), max_results).await {
                Ok(matches) => return Ok(matches),
                Err(err) => eprintln!("rg failed, falling back to internal search: {err:#}"),
            }
        }
        self.search_internal(pattern, path.as_ref(), max_results)
    }

    async fn search_with_rg(
        &self,
        pattern: &str,
        path: &str,
        max_results: usize,
    ) -> Result<Vec<SearchMatch>> {
        let max_count = max_results.to_string();
        let output = Command::new("rg")
            .args([
                "--line-number",
                "--color",
                "never",
                "--no-heading",
                "--max-count",
                max_count.as_str(),
                pattern,
                path,
            ])
            .current_dir(&self.root)
            .output()
            .await
            .context("failed to run ripgrep")?;

        // rg returns 1 when no matches, not an error for our use.
        if !output.status.success() && output.status.code() != Some(1) {
            anyhow::bail!("rg error: {}", String::from_utf8_lossy(&output.stderr));
        }

        let stdout = String::from_utf8_lossy(&output.stdout);
        let mut matches = Vec::new();
        for line in stdout.lines().take(max_results) {
            if let Some((path, rest)) = line.split_once(':') {
                if let Some((line_no, text)) = rest.split_once(':') {
                    matches.push(SearchMatch {
                        path: path.to_string(),
                        line: line_no.parse().unwrap_or(0),
                        text: text.to_string(),
                    });
                }
            }
        }
        Ok(matches)
    }

    fn search_internal(&self, pattern: &str, path: &str, max_results: usize) -> Result<Vec<SearchMatch>> {
        let regex = Regex::new(pattern).with_context(|| format!("invalid regex: {pattern}"))?;
        let start = self.root.join(path);
        let mut out = Vec::new();
        let walker = WalkBuilder::new(start)
            .hidden(false)
            .git_ignore(true)
            .git_exclude(true)
            .parents(true)
            .filter_entry(|e| !is_ignored_dir(e.path()))
            .build();

        for entry in walker {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };
            if !entry.file_type().map(|ft| ft.is_file()).unwrap_or(false) {
                continue;
            }
            let path = entry.path();
            if is_probably_binary(path) {
                continue;
            }
            let content = match fs::read_to_string(path) {
                Ok(c) => c,
                Err(_) => continue,
            };
            for (idx, line) in content.lines().enumerate() {
                if regex.is_match(line) {
                    let rel = path.strip_prefix(&self.root).unwrap_or(path);
                    out.push(SearchMatch {
                        path: rel.display().to_string(),
                        line: idx + 1,
                        text: line.trim_end().to_string(),
                    });
                    if out.len() >= max_results {
                        return Ok(out);
                    }
                }
            }
        }
        Ok(out)
    }
}

fn is_ignored_dir(path: &Path) -> bool {
    let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
    matches!(
        name,
        ".git" | "target" | "node_modules" | "dist" | "build" | ".next" | ".cache" | ".venv"
    )
}

fn is_probably_binary(path: &Path) -> bool {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    matches!(
        ext.as_str(),
        "png" | "jpg" | "jpeg" | "gif" | "webp" | "pdf" | "zip" | "tar" | "gz" | "xz" | "7z" | "exe" | "dll" | "so" | "dylib" | "class" | "jar" | "wasm"
    )
}
