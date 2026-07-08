use crate::{
    agent::Agent,
    config::Config,
    llm,
    mcp::McpRegistry,
    memory::MemoryStore,
    model_catalog::{render_catalog_markdown, EffortMode},
    terminal::markdown::print_markdown,
    tools::{
        file::FileTools,
        parser::parse_file_symbols,
        project::ProjectScanner,
        search::SearchTool,
        shell::ShellTool,
    },
};
use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::{fs, path::PathBuf};

#[derive(Debug, Parser)]
#[command(name = "aia", version, about = "Advanced terminal AI coding agent in Rust")]
pub struct Cli {
    #[arg(long, env = "AIA_CONFIG")]
    pub config: Option<PathBuf>,

    #[arg(long, env = "AIA_PROVIDER")]
    pub provider: Option<String>,

    #[arg(long, env = "AIA_MODEL")]
    pub model: Option<String>,

    /// Agent effort profile: fast, balanced, deep, max.
    #[arg(long, env = "AIA_EFFORT")]
    pub effort: Option<EffortMode>,

    #[command(subcommand)]
    pub command: Option<Commands>,
}

#[derive(Debug, Subcommand)]
pub enum Commands {
    /// Chat with the agent. If no prompt is provided, starts interactive mode.
    Chat {
        #[arg(long)]
        no_tools: bool,
        #[arg(trailing_var_arg = true)]
        prompt: Vec<String>,
    },

    /// Run Planner, Coder, Reviewer, Tester, and Security Auditor agents sequentially.
    Multi {
        #[arg(trailing_var_arg = true, required = true)]
        prompt: Vec<String>,
    },

    /// Show built-in model catalog and context windows.
    Models,

    /// Switch/preview effort profile settings.
    Effort {
        #[arg(value_enum)]
        mode: Option<EffortMode>,
    },

    /// Scan project tree, languages, and important files.
    Scan {
        #[arg(default_value = ".")]
        path: PathBuf,
        #[arg(long, default_value_t = 300)]
        max_files: usize,
    },

    /// Search with regex using ripgrep when available.
    Search {
        pattern: String,
        #[arg(default_value = ".")]
        path: String,
        #[arg(long, default_value_t = 200)]
        max_results: usize,
    },

    /// Safely run a shell command from project root.
    Shell {
        #[arg(long, short = 'y')]
        yes: bool,
        #[arg(trailing_var_arg = true, required = true)]
        command: Vec<String>,
    },

    /// Parse a code file. Build with --features code-parsing for tree-sitter.
    Parse {
        path: String,
    },

    /// Open the neon terminal UI.
    Tui,

    /// Print the active system prompt.
    Prompt,

    /// Memory commands.
    Memory {
        #[command(subcommand)]
        command: MemoryCommand,
    },

    /// MCP registry commands.
    Mcp {
        #[command(subcommand)]
        command: McpCommand,
    },
}

#[derive(Debug, Subcommand)]
pub enum MemoryCommand {
    Add { kind: String, text: Vec<String> },
    List { #[arg(long, default_value_t = 20)] limit: usize },
    Pref { key: String, value: Vec<String> },
}

#[derive(Debug, Subcommand)]
pub enum McpCommand {
    List,
    Example,
}

impl Cli {
    pub async fn run(self) -> Result<()> {
        let mut cfg = Config::load(self.config)?;
        if let Some(provider) = self.provider {
            cfg.provider = provider;
        }
        if let Some(model) = self.model {
            cfg.model = model;
        }
        if let Some(effort) = self.effort {
            cfg.apply_effort(effort);
        }

        match self.command.unwrap_or(Commands::Tui) {
            Commands::Chat { no_tools, prompt } => {
                let prompt = prompt.join(" ");
                if prompt.trim().is_empty() {
                    crate::tui::run(cfg)
                } else {
                    let memory = MemoryStore::new(cfg.memory_db_path())?;
                    let llm = llm::from_config(&cfg)?;
                    let mut agent = Agent::new(cfg, llm, memory);
                    agent.run_prompt(prompt, no_tools).await
                }
            }
            Commands::Multi { prompt } => {
                let memory = MemoryStore::new(cfg.memory_db_path())?;
                let llm = llm::from_config(&cfg)?;
                let agent = Agent::new(cfg, llm, memory);
                agent.run_multi_agent_prompt(prompt.join(" ")).await
            }
            Commands::Models => {
                println!("{}", render_catalog_markdown());
                Ok(())
            }
            Commands::Effort { mode } => {
                if let Some(mode) = mode {
                    cfg.apply_effort(mode);
                }
                let profile = cfg.effort_profile();
                println!("Effort: {} ({})", cfg.effort, profile.label);
                println!("temperature: {}", cfg.temperature);
                println!("tool iterations: {}", cfg.max_tool_iterations);
                println!("context scan files: {}", cfg.context_scan_files);
                println!("output tokens: {:?}", cfg.effective_output_tokens());
                println!("latency bias: {}", profile.command_latency_bias);
                Ok(())
            }
            Commands::Scan { path, max_files } => {
                let root = absolutize_from(&cfg.project_root, &path);
                let summary = ProjectScanner::new(root).scan(max_files)?;
                println!("{}", serde_json::to_string_pretty(&summary)?);
                Ok(())
            }
            Commands::Search {
                pattern,
                path,
                max_results,
            } => {
                let matches = SearchTool::new(cfg.project_root.clone())
                    .regex_search(&pattern, path, max_results)
                    .await?;
                println!("{}", serde_json::to_string_pretty(&matches)?);
                Ok(())
            }
            Commands::Shell { yes, command } => {
                let command = command.join(" ");
                let out = ShellTool::new(cfg.project_root.clone()).run(&command, yes).await?;
                println!("{}", serde_json::to_string_pretty(&out)?);
                Ok(())
            }
            Commands::Parse { path } => {
                let file = FileTools::new(cfg.project_root.clone()).resolve(&path)?;
                let source = fs::read_to_string(&file)
                    .with_context(|| format!("failed to read {}", file.display()))?;
                let summary = parse_file_symbols(std::path::Path::new(&path), &source)?;
                println!("{}", serde_json::to_string_pretty(&summary)?);
                Ok(())
            }
            Commands::Tui => crate::tui::run(cfg),
            Commands::Prompt => {
                print_markdown(&cfg.load_system_prompt()?);
                Ok(())
            }
            Commands::Memory { command } => {
                let memory = MemoryStore::new(cfg.memory_db_path())?;
                match command {
                    MemoryCommand::Add { kind, text } => {
                        let id = memory.add_note(kind, text.join(" "))?;
                        println!("added memory note #{id}");
                    }
                    MemoryCommand::List { limit } => {
                        let notes = memory.list_notes(limit)?;
                        println!("{}", serde_json::to_string_pretty(&notes)?);
                    }
                    MemoryCommand::Pref { key, value } => {
                        memory.set_preference(key, value.join(" "))?;
                        println!("preference saved");
                    }
                }
                Ok(())
            }
            Commands::Mcp { command } => match command {
                McpCommand::List => {
                    let registry = McpRegistry::load(&cfg.project_root)?;
                    if registry.config.servers.is_empty() {
                        println!(
                            "No MCP servers configured at {}\n\n{}",
                            registry.path.display(),
                            McpRegistry::example()
                        );
                    } else {
                        println!("{}", serde_json::to_string_pretty(&registry.config)?);
                    }
                    Ok(())
                }
                McpCommand::Example => {
                    println!("{}", McpRegistry::example());
                    Ok(())
                }
            },
        }
    }
}

fn absolutize_from(root: &std::path::Path, path: &std::path::Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    }
}
