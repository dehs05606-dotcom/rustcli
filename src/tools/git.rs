use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tokio::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitOutput {
    pub command: String,
    pub status: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

#[derive(Debug, Clone)]
pub struct GitTool {
    root: PathBuf,
}

impl GitTool {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub async fn status(&self) -> Result<GitOutput> {
        self.git(&["status", "--short"]).await
    }

    pub async fn diff(&self, stat: bool) -> Result<GitOutput> {
        if stat {
            self.git(&["diff", "--stat"]).await
        } else {
            self.git(&["diff"]).await
        }
    }

    async fn git(&self, args: &[&str]) -> Result<GitOutput> {
        let output = Command::new("git")
            .args(args)
            .current_dir(&self.root)
            .output()
            .await
            .with_context(|| format!("failed to run git {}", args.join(" ")))?;
        Ok(GitOutput {
            command: format!("git {}", args.join(" ")),
            status: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}
