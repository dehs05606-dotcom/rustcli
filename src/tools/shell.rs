use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::{io::{self, Write}, path::PathBuf};
use tokio::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum SafetyLevel {
    Safe,
    NeedsApproval,
    Blocked,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SafetyDecision {
    pub level: SafetyLevel,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShellOutput {
    pub command: String,
    pub status: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

#[derive(Debug, Clone)]
pub struct ShellTool {
    root: PathBuf,
}

impl ShellTool {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn check(command: &str) -> SafetyDecision {
        let lowered = command.to_ascii_lowercase();
        let compact = lowered.split_whitespace().collect::<Vec<_>>().join(" ");

        let blocked_patterns = [
            "rm -rf /",
            "rm -fr /",
            "rm -rf /*",
            "rm -fr /*",
            "mkfs",
            "dd if=",
            "dd of=/dev/",
            ":(){ :|:& };:",
            "chmod -r 777 /",
            "chown -r",
            "shutdown",
            "reboot",
            "poweroff",
            "git reset --hard",
            "git clean -fdx",
            "git clean -xdf",
            "curl ",
            "wget ",
        ];

        if blocked_patterns.iter().any(|p| compact.contains(p))
            && (compact.contains("| sh")
                || compact.contains("| bash")
                || compact.contains("rm -rf /")
                || compact.contains("mkfs")
                || compact.contains("dd of=/dev/")
                || compact.contains("git reset --hard")
                || compact.contains("git clean -fdx")
                || compact.contains("git clean -xdf")
                || compact.contains(":(){ :|:& };:"))
        {
            return SafetyDecision {
                level: SafetyLevel::Blocked,
                reason: "blocked as destructive or remote-code-execution command".to_string(),
            };
        }

        let secret_patterns = [
            ".env",
            "id_rsa",
            "id_ed25519",
            "aws_secret_access_key",
            "github_token",
            "ghp_",
            "authorization:",
        ];
        if secret_patterns.iter().any(|p| compact.contains(p))
            && (compact.contains("cat ") || compact.contains("grep ") || compact.contains("curl "))
        {
            return SafetyDecision {
                level: SafetyLevel::Blocked,
                reason: "blocked because it may expose secrets or credential files".to_string(),
            };
        }

        let approval_patterns = [
            "rm ",
            "mv ",
            "cp ",
            "chmod ",
            "chown ",
            "sudo ",
            "apt ",
            "brew ",
            "npm install",
            "pnpm install",
            "yarn add",
            "pip install",
            "cargo install",
            "docker ",
            "kubectl ",
            "git push",
            "git commit",
            "git checkout",
            "git switch",
            "git merge",
            "git rebase",
            "curl ",
            "wget ",
        ];

        if approval_patterns.iter().any(|p| compact.contains(p)) {
            return SafetyDecision {
                level: SafetyLevel::NeedsApproval,
                reason: "command can modify files/system/network and needs approval".to_string(),
            };
        }

        SafetyDecision {
            level: SafetyLevel::Safe,
            reason: "read-only or low-risk command".to_string(),
        }
    }

    pub async fn run(&self, command: &str, assume_yes: bool) -> Result<ShellOutput> {
        let decision = Self::check(command);
        match decision.level {
            SafetyLevel::Blocked => bail!("blocked command: {}", decision.reason),
            SafetyLevel::NeedsApproval | SafetyLevel::Safe => {
                if !assume_yes {
                    eprintln!("\nShell command: {command}");
                    eprintln!("Safety: {:?} — {}", decision.level, decision.reason);
                    if !ask_yes_no("Run this command?")? {
                        bail!("user denied shell command");
                    }
                }
            }
        }

        #[cfg(windows)]
        let mut child = {
            let mut c = Command::new("cmd");
            c.arg("/C").arg(command);
            c
        };

        #[cfg(not(windows))]
        let mut child = {
            let mut c = Command::new("sh");
            c.arg("-c").arg(command);
            c
        };

        let output = child
            .current_dir(&self.root)
            .output()
            .await
            .with_context(|| format!("failed to run shell command: {command}"))?;

        Ok(ShellOutput {
            command: command.to_string(),
            status: output.status.code(),
            stdout: limit_string(String::from_utf8_lossy(&output.stdout).to_string(), 30_000),
            stderr: limit_string(String::from_utf8_lossy(&output.stderr).to_string(), 30_000),
        })
    }
}

pub fn ask_yes_no(question: &str) -> Result<bool> {
    print!("{question} [y/N] ");
    io::stdout().flush()?;
    let mut input = String::new();
    io::stdin().read_line(&mut input)?;
    Ok(matches!(input.trim().to_ascii_lowercase().as_str(), "y" | "yes"))
}

pub fn limit_string(mut s: String, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        return s;
    }
    s = s.chars().take(max_chars).collect::<String>();
    s.push_str("\n... [truncated]\n");
    s
}
