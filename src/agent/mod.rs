pub mod roles;
pub mod tool_protocol;

use crate::{
    config::Config,
    llm::{ChatMessage, LlmClient, StdoutSink},
    memory::MemoryStore,
    model_catalog::{render_catalog_markdown, EffortMode},
    terminal::suggestions::extract_shell_suggestions,
    tools::{
        file::FileTools,
        git::GitTool,
        project::ProjectScanner,
        search::SearchTool,
        shell::{ask_yes_no, ShellTool},
    },
};
use anyhow::{bail, Context, Result};
use chrono::Local;
use serde_json::{json, Value};
use std::{io::{self, Write}, str::FromStr};

use roles::AgentRole;
use tool_protocol::{parse_tool_calls, ToolCall};

pub struct Agent {
    cfg: Config,
    llm: Box<dyn LlmClient>,
    memory: MemoryStore,
}

impl Agent {
    pub fn new(cfg: Config, llm: Box<dyn LlmClient>, memory: MemoryStore) -> Self {
        Self { cfg, llm, memory }
    }

    pub async fn run_prompt(&mut self, user_prompt: String, no_tools: bool) -> Result<()> {
        if self.handle_slash_command(user_prompt.trim())? {
            return Ok(());
        }

        let mut messages = self.initial_messages(&user_prompt)?;
        let max_iters = if no_tools { 1 } else { self.cfg.max_tool_iterations.max(1) };

        for turn in 0..max_iters {
            if turn > 0 {
                println!("\n\nAIA continuing after tool results...\n");
            }

            let mut sink = StdoutSink;
            let answer = self.llm.stream(&messages, &mut sink).await?;
            println!();

            if no_tools {
                self.print_command_suggestions(&answer);
                return Ok(());
            }

            let tool_calls = parse_tool_calls(&answer);
            if tool_calls.is_empty() {
                self.print_command_suggestions(&answer);
                return Ok(());
            }

            let mut tool_results = Vec::new();
            for call in tool_calls {
                let result = self.execute_tool_call(&call).await;
                let rendered = match result {
                    Ok(value) => json!({"tool": call.tool, "ok": true, "result": value}),
                    Err(err) => json!({"tool": call.tool, "ok": false, "error": format!("{err:#}")}),
                };
                tool_results.push(rendered);
            }

            messages.push(ChatMessage::assistant(answer));
            messages.push(ChatMessage::user(format!(
                "Tool results JSON:\n{}\n\nContinue. If finished, provide final answer. If more tools are needed, request them using the tool protocol.",
                serde_json::to_string_pretty(&tool_results)?
            )));
        }

        println!(
            "\nReached max tool iterations ({}). Increase max_tool_iterations in config if needed.",
            self.cfg.max_tool_iterations
        );
        Ok(())
    }

    pub async fn run_multi_agent_prompt(&self, user_prompt: String) -> Result<()> {
        let base_system = self.cfg.load_system_prompt()?;
        let project = ProjectScanner::new(self.cfg.project_root.clone())
            .brief_context_with_limit(self.cfg.context_scan_files)
            .unwrap_or_else(|err| format!("Project scan unavailable: {err:#}"));
        let memory = self.memory.context_block().unwrap_or_default();
        let roles = [
            AgentRole::Planner,
            AgentRole::Coder,
            AgentRole::Reviewer,
            AgentRole::Tester,
            AgentRole::SecurityAuditor,
        ];
        let mut transcript = String::new();

        for role in roles {
            println!("\n================ {} ================\n", role.name());
            let messages = vec![
                ChatMessage::system(format!(
                    "{base_system}\n\n{}\n\nRuntime: {}",
                    role.prompt(),
                    self.cfg.runtime_summary()
                )),
                ChatMessage::user(format!(
                    "Task:\n{user_prompt}\n\nProject context:\n{project}\n\nMemory:\n{memory}\n\nPrevious role outputs:\n{transcript}"
                )),
            ];
            let answer = self.llm.complete(&messages).await?;
            println!("{answer}\n");
            transcript.push_str(&format!("\n## {}\n{}\n", role.name(), answer));
        }
        Ok(())
    }

    pub async fn interactive_chat(&mut self) -> Result<()> {
        println!("AIA interactive chat. Type /exit to quit, /help for commands.");
        println!("Runtime: {}\n", self.cfg.runtime_summary());
        loop {
            print!("you [{}]> ", self.cfg.effort);
            io::stdout().flush()?;
            let mut input = String::new();
            io::stdin().read_line(&mut input)?;
            let input = input.trim();
            if input.is_empty() {
                continue;
            }
            if self.handle_slash_command(input)? {
                continue;
            }
            self.run_prompt(input.to_string(), false).await?;
        }
        Ok(())
    }

    fn handle_slash_command(&mut self, input: &str) -> Result<bool> {
        if !input.starts_with('/') {
            return Ok(false);
        }
        let mut parts = input.split_whitespace();
        let command = parts.next().unwrap_or_default();
        match command {
            "/exit" | "/quit" => std::process::exit(0),
            "/help" => {
                println!(
                    "Commands:\n  /effort fast|balanced|deep|max  - switch latency/context profile\n  /status                         - show provider/model/runtime\n  /models                         - show built-in model catalog\n  /memory                         - show recent notes\n  /clear                          - visual separator\n  /exit                           - quit"
                );
                Ok(true)
            }
            "/status" => {
                println!("{}", self.cfg.runtime_summary());
                Ok(true)
            }
            "/models" => {
                println!("{}", render_catalog_markdown());
                Ok(true)
            }
            "/memory" => {
                let notes = self.memory.list_notes(20)?;
                if notes.is_empty() {
                    println!("No memory notes yet.");
                } else {
                    for note in notes {
                        println!("#{} [{}] {}", note.id, note.kind, note.text);
                    }
                }
                Ok(true)
            }
            "/clear" => {
                println!("\n{}\n", "─".repeat(80));
                Ok(true)
            }
            "/effort" | "/mode" => {
                let Some(mode_text) = parts.next() else {
                    println!("Current effort: {}", self.cfg.effort);
                    println!("Use: /effort fast | /effort balanced | /effort deep | /effort max");
                    return Ok(true);
                };
                let mode = EffortMode::from_str(mode_text).map_err(anyhow::Error::msg)?;
                self.cfg.apply_effort_with_recommended_model(mode);
                self.llm = crate::llm::from_config(&self.cfg)?;
                let profile = self.cfg.effort_profile();
                println!(
                    "Effort switched to {} ({}) | model={} scan_files={} tool_iters={} output_tokens={:?}",
                    self.cfg.effort,
                    profile.label,
                    self.cfg.model,
                    self.cfg.context_scan_files,
                    self.cfg.max_tool_iterations,
                    self.cfg.effective_output_tokens()
                );
                Ok(true)
            }
            _ => {
                println!("Unknown slash command `{command}`. Type /help.");
                Ok(true)
            }
        }
    }

    fn initial_messages(&self, user_prompt: &str) -> Result<Vec<ChatMessage>> {
        let system_prompt = self.cfg.load_system_prompt()?;
        let memory = self.memory.context_block().unwrap_or_default();
        let project = ProjectScanner::new(self.cfg.project_root.clone())
            .brief_context_with_limit(self.cfg.context_scan_files)
            .unwrap_or_else(|err| format!("Project scan unavailable: {err:#}"));
        let now = Local::now().format("%Y-%m-%d %H:%M:%S %Z");
        let context_window = self
            .cfg
            .context_window_tokens()
            .map(|v| v.to_string())
            .unwrap_or_else(|| "unknown".to_string());

        let system = format!(
            "{system_prompt}\n\nRuntime facts:\n- Local date/time: {now}\n- Provider: {}\n- Model: {}\n- Effort mode: {}\n- Context window metadata: {} tokens\n- Output token budget: {:?}\n- Project root: {}\n- File writes auto-apply: {}\n- Runtime summary: {}\n",
            self.cfg.provider,
            self.cfg.model,
            self.cfg.effort,
            context_window,
            self.cfg.effective_output_tokens(),
            self.cfg.project_root.display(),
            self.cfg.auto_apply,
            self.cfg.runtime_summary(),
        );

        let mut messages = vec![ChatMessage::system(system)];
        if !memory.trim().is_empty() {
            messages.push(ChatMessage::user(format!("Memory context:\n{memory}")));
        }
        messages.push(ChatMessage::user(format!("Project context snapshot:\n{project}")));
        messages.push(ChatMessage::user(user_prompt.to_string()));
        Ok(messages)
    }

    async fn execute_tool_call(&self, call: &ToolCall) -> Result<Value> {
        match call.tool.as_str() {
            "project.tree" => {
                let max_files = arg_usize(&call.args, "max_files").unwrap_or(self.cfg.context_scan_files);
                let summary = ProjectScanner::new(self.cfg.project_root.clone()).scan(max_files)?;
                Ok(serde_json::to_value(summary)?)
            }
            "file.read" => {
                let path = arg_str(&call.args, "path")?;
                let max_bytes = arg_usize(&call.args, "max_bytes").unwrap_or(200_000);
                let result = FileTools::new(self.cfg.project_root.clone()).read(path, max_bytes)?;
                Ok(serde_json::to_value(result)?)
            }
            "file.write" => {
                let path = arg_str(&call.args, "path")?;
                let content = arg_str(&call.args, "content")?;
                let file = FileTools::new(self.cfg.project_root.clone());
                let preview = file.preview_write(path, content)?;
                eprintln!("\nProposed write to {path}:\n{}", preview.diff);
                if !self.cfg.auto_apply && !ask_yes_no("Apply this file write?")? {
                    bail!("user denied file.write");
                }
                let applied = file.write(path, content)?;
                Ok(serde_json::to_value(applied)?)
            }
            "file.replace" => {
                let path = arg_str(&call.args, "path")?;
                let old = arg_str(&call.args, "old")?;
                let new = arg_str(&call.args, "new")?;
                let all = arg_bool(&call.args, "all").unwrap_or(false);
                let file = FileTools::new(self.cfg.project_root.clone());
                let preview = file.replace(path, old, new, all, false)?;
                eprintln!("\nProposed replacement in {path}:\n{}", preview.diff);
                if !self.cfg.auto_apply && !ask_yes_no("Apply this replacement?")? {
                    bail!("user denied file.replace");
                }
                let applied = file.replace(path, old, new, all, true)?;
                Ok(serde_json::to_value(applied)?)
            }
            "search.regex" => {
                let pattern = arg_str(&call.args, "pattern")?;
                let path = arg_str(&call.args, "path").unwrap_or(".");
                let max_results = arg_usize(&call.args, "max_results").unwrap_or(200);
                let matches = SearchTool::new(self.cfg.project_root.clone())
                    .regex_search(pattern, path, max_results)
                    .await?;
                Ok(serde_json::to_value(matches)?)
            }
            "shell.run" => {
                let command = arg_str(&call.args, "command")?;
                let output = ShellTool::new(self.cfg.project_root.clone())
                    .run(command, self.cfg.auto_apply)
                    .await?;
                Ok(serde_json::to_value(output)?)
            }
            "git.status" => {
                let output = GitTool::new(self.cfg.project_root.clone()).status().await?;
                Ok(serde_json::to_value(output)?)
            }
            "git.diff" => {
                let stat = arg_bool(&call.args, "stat").unwrap_or(false);
                let output = GitTool::new(self.cfg.project_root.clone()).diff(stat).await?;
                Ok(serde_json::to_value(output)?)
            }
            "memory.add" => {
                let kind = arg_str(&call.args, "kind").unwrap_or("note");
                let text = arg_str(&call.args, "text")?;
                let id = self.memory.add_note(kind, text)?;
                Ok(json!({"id": id}))
            }
            "memory.list" => {
                let limit = arg_usize(&call.args, "limit").unwrap_or(20);
                let notes = self.memory.list_notes(limit)?;
                Ok(serde_json::to_value(notes)?)
            }
            other => bail!("unknown tool `{other}`"),
        }
    }

    fn print_command_suggestions(&self, answer: &str) {
        let suggestions = extract_shell_suggestions(answer);
        if suggestions.is_empty() {
            return;
        }
        println!("\nSuggested shell commands detected:");
        for (idx, cmd) in suggestions.iter().enumerate() {
            println!("  {}. {}", idx + 1, cmd);
        }
        println!("Run safely with: aia shell \"<command>\"");
    }
}

fn arg_str<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .with_context(|| format!("missing or invalid string arg `{key}`"))
}

fn arg_usize(value: &Value, key: &str) -> Option<usize> {
    value.get(key).and_then(Value::as_u64).map(|v| v as usize)
}

fn arg_bool(value: &Value, key: &str) -> Option<bool> {
    value.get(key).and_then(Value::as_bool)
}
