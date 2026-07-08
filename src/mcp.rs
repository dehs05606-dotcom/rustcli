use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, fs, path::{Path, PathBuf}};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpConfig {
    #[serde(default)]
    pub servers: BTreeMap<String, McpServer>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpServer {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct McpRegistry {
    pub path: PathBuf,
    pub config: McpConfig,
}

impl McpRegistry {
    pub fn load(project_root: &Path) -> Result<Self> {
        let path = project_root.join(".aia/mcp.toml");
        if !path.exists() {
            return Ok(Self {
                path,
                config: McpConfig {
                    servers: BTreeMap::new(),
                },
            });
        }
        let raw = fs::read_to_string(&path)
            .with_context(|| format!("failed to read MCP config {}", path.display()))?;
        let config: McpConfig = toml::from_str(&raw)
            .with_context(|| format!("failed to parse MCP config {}", path.display()))?;
        Ok(Self { path, config })
    }

    pub fn example() -> &'static str {
        r#"# .aia/mcp.toml
[servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]

[servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
"#
    }
}
