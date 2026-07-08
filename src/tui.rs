use crate::{
    config::Config,
    tools::{
        file::FileTools,
        search::SearchTool,
        shell::ShellTool,
    },
};
use anyhow::Result;
use chrono::Local;
use crossterm::{
    event::{self, Event, KeyCode, KeyModifiers},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, List, ListItem, Paragraph, Wrap},
    Frame, Terminal,
};
use std::{io, time::Duration};

const CYAN: Color = Color::Rgb(64, 235, 255);
const GREEN: Color = Color::Rgb(91, 255, 143);
const PURPLE: Color = Color::Rgb(187, 134, 252);
const ORANGE: Color = Color::Rgb(255, 190, 90);
const RED: Color = Color::Rgb(255, 94, 120);
const BLUE: Color = Color::Rgb(94, 166, 255);
const DIM: Color = Color::Rgb(88, 108, 128);
const PANEL_BG: Color = Color::Rgb(9, 16, 24);
const DEEP_BG: Color = Color::Rgb(3, 7, 12);
const YELLOW: Color = Color::Rgb(255, 230, 100);

pub fn run(cfg: Config) -> Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let result = App::new(cfg).run(&mut terminal);

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    result
}

struct Message {
    time: String,
    role: String,
    color: Color,
    content: String,
}

struct App {
    cfg: Config,
    messages: Vec<Message>,
    input: String,
    cursor_visible: bool,
    tick: u64,
    show_suggestions: bool,
    selected_suggestion: usize,
    suggestions: Vec<CmdSuggestion>,
    command_history: Vec<String>,
    history_cursor: Option<usize>,
    scroll: usize,
    file_tools: FileTools,
    shell_tool: ShellTool,
}

#[derive(Clone)]
struct CmdSuggestion {
    label: String,
    detail: String,
    insert: String,
    color: Color,
}

impl App {
    fn new(cfg: Config) -> Self {
        let root = cfg.project_root.clone();
        let mut messages = Vec::new();
        messages.push(Message::new("SYSTEM", GREEN, &format!(
            "AIA Terminal v0.3 — {} | {} | effort={}",
            cfg.provider, cfg.model, cfg.effort
        )));
        messages.push(Message::new("AIA", CYAN, &format!(
            "Type / for commands, or just ask me anything. I can read/write files, run shell commands, and search code."
        )));

        App {
            cfg,
            messages,
            input: String::new(),
            cursor_visible: true,
            tick: 0,
            show_suggestions: false,
            selected_suggestion: 0,
            suggestions: Vec::new(),
            command_history: Vec::new(),
            history_cursor: None,
            scroll: 0,
            file_tools: FileTools::new(root.clone()),
            shell_tool: ShellTool::new(root),
        }
    }

    fn run(&mut self, terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<()> {
        loop {
            terminal.draw(|frame| self.draw(frame))?;
            self.tick = self.tick.wrapping_add(1);
            self.cursor_visible = self.tick % 12 < 6;

            if event::poll(Duration::from_millis(50))? {
                match event::read()? {
                    Event::Key(key) => {
                        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
                        if ctrl && key.code == KeyCode::Char('c') {
                            break;
                        }
                        if ctrl {
                            match key.code {
                                KeyCode::Char('l') => self.clear_history(),
                                KeyCode::Char('d') => { break; }
                                _ => {}
                            }
                            continue;
                        }
                        if !self.handle_key(key.code) {
                            break;
                        }
                    }
                    Event::Resize(_, _) => {}
                    _ => {}
                }
            }
        }
        Ok(())
    }

    fn handle_key(&mut self, code: KeyCode) -> bool {
        match code {
            KeyCode::Esc => return false,
            KeyCode::Char('q') if self.input.is_empty() => return false,
            KeyCode::Enter => self.submit(),
            KeyCode::Backspace => { self.input.pop(); self.update_suggestions(); }
            KeyCode::Tab => self.accept_suggestion(),
            KeyCode::Up => self.history_back(),
            KeyCode::Down => self.history_forward(),
            KeyCode::PageUp => {
                if self.show_suggestions && !self.suggestions.is_empty() {
                    let len = self.suggestions.len();
                    self.selected_suggestion = if self.selected_suggestion == 0 { len - 1 } else { self.selected_suggestion - 1 };
                } else {
                    self.scroll = self.scroll.saturating_add(5);
                }
            }
            KeyCode::PageDown => {
                if self.show_suggestions && !self.suggestions.is_empty() {
                    let len = self.suggestions.len();
                    self.selected_suggestion = (self.selected_suggestion + 1) % len;
                } else {
                    self.scroll = self.scroll.saturating_sub(5);
                }
            }
            KeyCode::Home => self.scroll = self.messages.len(),
            KeyCode::End => self.scroll = 0,
            KeyCode::Char(c) => { self.input.push(c); self.update_suggestions(); }
            _ => {}
        }
        true
    }

    fn update_suggestions(&mut self) {
        let q = self.input.to_ascii_lowercase();
        let is_slash = q.starts_with('/');

        let mut all = Vec::new();

        all.push(CmdSuggestion::new("/help", "Show available commands", "/help", GREEN));
        all.push(CmdSuggestion::new("/clear", "Clear terminal history", "/clear", RED));
        all.push(CmdSuggestion::new("/status", "Show runtime config", "/status", CYAN));
        all.push(CmdSuggestion::new("/effort fast", "Fast mode — low latency", "/effort fast", GREEN));
        all.push(CmdSuggestion::new("/effort balanced", "Balanced mode", "/effort balanced", BLUE));
        all.push(CmdSuggestion::new("/effort deep", "Deep analysis mode", "/effort deep", PURPLE));
        all.push(CmdSuggestion::new("/effort max", "Maximum context mode", "/effort max", ORANGE));
        all.push(CmdSuggestion::new("/model <name>", "Switch AI model", "/model ", PURPLE));
        all.push(CmdSuggestion::new("/provider <name>", "Switch provider", "/provider ", CYAN));
        all.push(CmdSuggestion::new("/read <file>", "Read a file", "/read ", BLUE));
        all.push(CmdSuggestion::new("/edit <file> <old> <new>", "Replace text in a file", "/edit ", ORANGE));
        all.push(CmdSuggestion::new("/write <file> <content>", "Write content to a file", "/write ", YELLOW));
        all.push(CmdSuggestion::new("/shell <command>", "Run a shell command", "/shell ", GREEN));
        all.push(CmdSuggestion::new("/search <pattern>", "Search codebase with regex", "/search ", PURPLE));

        if is_slash {
            self.show_suggestions = true;
            self.suggestions = all.into_iter()
                .filter(|s| s.label.to_ascii_lowercase().contains(&q) || q == "/")
                .collect();
        } else if !q.is_empty() {
            self.show_suggestions = false;
            self.suggestions.clear();
        } else {
            self.show_suggestions = false;
            self.suggestions.clear();
        }

        if self.selected_suggestion >= self.suggestions.len() {
            self.selected_suggestion = self.suggestions.len().saturating_sub(1);
        }
    }

    fn accept_suggestion(&mut self) {
        if self.suggestions.is_empty() || !self.show_suggestions {
            if !self.input.is_empty() {
                let cmd = self.input.trim().to_string();
                if cmd.starts_with('/') {
                    let parts: Vec<&str> = cmd.splitn(2, ' ').collect();
                    if parts.len() == 1 {
                        let completed = self.complete_slash(&cmd);
                        if let Some(completed) = completed {
                            self.input = completed;
                            self.update_suggestions();
                        }
                    }
                }
            }
            return;
        }
        let idx = self.selected_suggestion.min(self.suggestions.len() - 1);
        let suggestion = self.suggestions[idx].clone();
        self.input = suggestion.insert;
        self.show_suggestions = false;
        self.suggestions.clear();
        self.selected_suggestion = 0;
    }

    fn complete_slash(&self, cmd: &str) -> Option<String> {
        let slash_commands = [
            "/help", "/clear", "/status", "/effort", "/model", "/provider",
            "/read", "/edit", "/write", "/shell", "/search",
        ];
        for sc in slash_commands {
            if sc.starts_with(cmd) && sc != cmd {
                return Some(sc.to_string() + " ");
            }
        }
        None
    }

    fn history_back(&mut self) {
        if self.input.is_empty() && !self.command_history.is_empty() {
            let prev = self.history_cursor.unwrap_or(self.command_history.len()).saturating_sub(1);
            self.history_cursor = Some(prev);
            self.input = self.command_history[prev].clone();
            self.show_suggestions = false;
        }
    }

    fn history_forward(&mut self) {
        if let Some(idx) = self.history_cursor {
            let next = idx + 1;
            if next < self.command_history.len() {
                self.history_cursor = Some(next);
                self.input = self.command_history[next].clone();
            } else {
                self.history_cursor = None;
                self.input.clear();
            }
            self.show_suggestions = false;
        }
    }

    fn submit(&mut self) {
        let input = self.input.trim().to_string();
        if input.is_empty() {
            return;
        }
        self.input.clear();
        self.show_suggestions = false;
        self.suggestions.clear();
        self.selected_suggestion = 0;
        self.history_cursor = None;

        if self.command_history.last().map(String::as_str) != Some(&input) {
            self.command_history.push(input.clone());
            if self.command_history.len() > 100 {
                self.command_history.remove(0);
            }
        }

        self.messages.push(Message::new("USER", ORANGE, &input));
        self.scroll = self.messages.len();

        if input.starts_with('/') {
            self.execute_command(&input);
        } else {
            self.messages.push(Message::new("AIA", CYAN, &format!(
                "Use / commands for file ops, shell, search. Type / to see all commands."
            )));
        }
    }

    fn execute_command(&mut self, input: &str) {
        let mut parts = input.splitn(3, ' ');
        let cmd = parts.next().unwrap_or_default();
        let rest = parts.collect::<Vec<&str>>().join(" ");

        match cmd {
            "/help" => {
                let help = vec![
                    "/help       — Show this help",
                    "/clear      — Clear terminal history (Ctrl+L)",
                    "/status     — Show runtime configuration",
                    "/effort     — fast | balanced | deep | max",
                    "/model      — Switch AI model",
                    "/provider   — Switch provider",
                    "/read       — Read a file: /read src/main.rs",
                    "/edit       — Replace text: /edit file <old> <new>",
                    "/write      — Write file: /write file <content>",
                    "/shell      — Run a shell command",
                    "/search     — Search code with regex",
                ];
                self.messages.push(Message::new("AIA", GREEN, &help.join("\n")));
            }
            "/clear" => self.clear_history(),
            "/status" => {
                self.messages.push(Message::new("AIA", CYAN, &self.cfg.runtime_summary()));
            }
            "/effort" => {
                let mode = rest.trim();
                if mode.is_empty() {
                    self.messages.push(Message::new("AIA", CYAN, &format!(
                        "Current effort: {}. Options: fast, balanced, deep, max", self.cfg.effort
                    )));
                } else {
                    use crate::model_catalog::EffortMode;
                    if let Ok(mode) = mode.parse::<EffortMode>() {
                        self.cfg.apply_effort_with_recommended_model(mode);
                        self.messages.push(Message::new("AIA", GREEN, &format!(
                            "Effort switched to {} | model={}", self.cfg.effort, self.cfg.model
                        )));
                    } else {
                        self.messages.push(Message::new("ERROR", RED, &format!("Invalid effort mode: {}", mode)));
                    }
                }
            }
            "/model" => {
                let model = rest.trim();
                if model.is_empty() {
                    self.messages.push(Message::new("AIA", CYAN, &format!(
                        "Current model: {}. Use /model <name>", self.cfg.model
                    )));
                } else {
                    let old = self.cfg.model.clone();
                    self.cfg.model = model.to_string();
                    self.messages.push(Message::new("AIA", GREEN, &format!(
                        "Model changed: {} -> {}", old, self.cfg.model
                    )));
                }
            }
            "/provider" => {
                let provider = rest.trim();
                if provider.is_empty() {
                    self.messages.push(Message::new("AIA", CYAN, &format!(
                        "Current provider: {}. Use /provider <name>", self.cfg.provider
                    )));
                } else {
                    let old = self.cfg.provider.clone();
                    self.cfg.provider = provider.to_string();
                    self.messages.push(Message::new("AIA", GREEN, &format!(
                        "Provider changed: {} -> {}", old, self.cfg.provider
                    )));
                }
            }
            "/read" => {
                let path = rest.trim();
                if path.is_empty() {
                    self.messages.push(Message::new("AIA", ORANGE, "Usage: /read <filepath>"));
                } else {
                    match self.file_tools.read(path, 200_000) {
                        Ok(result) => {
                            let mut content = format!("📄 {} ({} bytes)", result.path, result.bytes);
                            if result.truncated {
                                content.push_str(" [truncated]");
                            }
                            content.push('\n');
                            content.push_str(&result.content);
                            self.messages.push(Message::new("FILE", BLUE, &content));
                        }
                        Err(e) => {
                            self.messages.push(Message::new("ERROR", RED, &format!("Failed to read: {:#}", e)));
                        }
                    }
                }
            }
            "/edit" => {
                let args: Vec<&str> = rest.splitn(3, ' ').collect();
                if args.len() < 3 {
                    self.messages.push(Message::new("AIA", ORANGE, "Usage: /edit <file> <old> <new>"));
                } else {
                    let file = args[0];
                    let old = args[1];
                    let new = args[2];
                    match self.file_tools.replace(file, old, new, false, true) {
                        Ok(preview) => {
                            self.messages.push(Message::new("FILE", GREEN, &format!(
                                "Edited {}:\n{}", file, preview.diff
                            )));
                        }
                        Err(e) => {
                            self.messages.push(Message::new("ERROR", RED, &format!("Edit failed: {:#}", e)));
                        }
                    }
                }
            }
            "/write" => {
                let args: Vec<&str> = rest.splitn(2, ' ').collect();
                if args.len() < 2 {
                    self.messages.push(Message::new("AIA", ORANGE, "Usage: /write <file> <content>"));
                } else {
                    match self.file_tools.write(args[0], args[1]) {
                        Ok(preview) => {
                            let msg = if let Some(backup) = preview.backup_path {
                                format!("Written to {} (backup: {})", preview.path, backup)
                            } else {
                                format!("Created {}", preview.path)
                            };
                            self.messages.push(Message::new("FILE", GREEN, &msg));
                        }
                        Err(e) => {
                            self.messages.push(Message::new("ERROR", RED, &format!("Write failed: {:#}", e)));
                        }
                    }
                }
            }
            "/shell" => {
                let command = rest.trim();
                if command.is_empty() {
                    self.messages.push(Message::new("AIA", ORANGE, "Usage: /shell <command>"));
                } else {
                    self.messages.push(Message::new("SHELL", YELLOW, &format!("$ {}", command)));
                    match tokio::runtime::Runtime::new() {
                        Ok(rt) => {
                            match rt.block_on(self.shell_tool.run(command, true)) {
                                Ok(output) => {
                                    let mut out = String::new();
                                    if let Some(status) = output.status {
                                        out.push_str(&format!("Exit code: {}\n", status));
                                    }
                                    if !output.stdout.is_empty() {
                                        out.push_str(&output.stdout);
                                    }
                                    if !output.stderr.is_empty() {
                                        out.push_str(&output.stderr);
                                    }
                                    self.messages.push(Message::new("SHELL", GREEN, &out));
                                }
                                Err(e) => {
                                    self.messages.push(Message::new("ERROR", RED, &format!("{:#}", e)));
                                }
                            }
                        }
                        Err(e) => {
                            self.messages.push(Message::new("ERROR", RED, &format!("Runtime error: {:#}", e)));
                        }
                    }
                }
            }
            "/search" => {
                let pattern = rest.trim();
                if pattern.is_empty() {
                    self.messages.push(Message::new("AIA", ORANGE, "Usage: /search <regex-pattern>"));
                } else {
                    match tokio::runtime::Runtime::new() {
                        Ok(rt) => {
                            let search = SearchTool::new(self.cfg.project_root.clone());
                            match rt.block_on(search.regex_search(pattern, ".", 100)) {
                                Ok(matches) => {
                                    if matches.is_empty() {
                                        self.messages.push(Message::new("SEARCH", DIM, &format!("No results for: {}", pattern)));
                                    } else {
                                        let lines: Vec<String> = matches.iter().map(|m| {
                                            format!("{}:{}:{}", m.path, m.line, m.text)
                                        }).collect();
                                        self.messages.push(Message::new("SEARCH", PURPLE, &lines.join("\n")));
                                    }
                                }
                                Err(e) => {
                                    self.messages.push(Message::new("ERROR", RED, &format!("Search failed: {:#}", e)));
                                }
                            }
                        }
                        Err(e) => {
                            self.messages.push(Message::new("ERROR", RED, &format!("Runtime error: {:#}", e)));
                        }
                    }
                }
            }
            _ => {
                self.messages.push(Message::new("AIA", RED, &format!(
                    "Unknown command: {}. Type /help for available commands.", cmd
                )));
            }
        }
        self.scroll = self.messages.len();
        self.prune();
    }

    fn clear_history(&mut self) {
        self.messages.clear();
        self.messages.push(Message::new("SYSTEM", GREEN, "Terminal history cleared"));
        self.scroll = self.messages.len();
    }

    fn prune(&mut self) {
        if self.messages.len() > 500 {
            self.messages.drain(0..(self.messages.len() - 500));
        }
    }

    // ─── Drawing ───────────────────────────────────────────────

    fn draw(&self, frame: &mut Frame) {
        let area = frame.area();
        frame.render_widget(Block::default().style(Style::default().bg(DEEP_BG)), area);

        let layout = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1),
                Constraint::Min(2),
                Constraint::Length(3),
            ])
            .split(area);

        self.draw_title(frame, layout[0]);
        self.draw_chat(frame, layout[1]);
        self.draw_prompt(frame, layout[2]);

        if self.show_suggestions && !self.suggestions.is_empty() {
            let popup_area = self.suggestions_popup_area(area);
            frame.render_widget(Clear, popup_area);
            self.draw_suggestions_popup(frame, popup_area);
        }
    }

    fn draw_title(&self, frame: &mut Frame, area: Rect) {
        let elapsed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() % 10000)
            .unwrap_or(0);
        let text = Line::from(vec![
            Span::styled(" AIA ", Style::default().fg(CYAN).add_modifier(Modifier::BOLD)),
            Span::styled(format!("v0.3  {}:{}  effort={}", self.cfg.provider, self.cfg.model, self.cfg.effort), Style::default().fg(DIM)),
            Span::styled(format!("  session #{}", elapsed), Style::default().fg(ORANGE)),
        ]);
        frame.render_widget(Paragraph::new(text).style(Style::default().bg(DEEP_BG)), area);
    }

    fn draw_chat(&self, frame: &mut Frame, area: Rect) {
        let max_h = area.height.saturating_sub(2) as usize;

        let all_lines: Vec<Line> = self.messages.iter().flat_map(|msg| msg.to_lines(area.width as usize)).collect();

        let total = all_lines.len();
        let visible_end = total.saturating_sub(self.scroll.min(total));
        let visible_start = visible_end.saturating_sub(max_h.max(1));

        let lines: Vec<Line> = if visible_start < visible_end {
            all_lines[visible_start..visible_end].to_vec()
        } else {
            vec![Line::from(Span::styled("  (no messages)", Style::default().fg(DIM)))]
        };

        let scroll_info = if total > max_h && self.scroll > 0 {
            format!(" Chat  {} lines  ↑ scroll +{} ", total, self.scroll)
        } else {
            format!(" Chat  {} lines ", total)
        };

        let panel = Paragraph::new(lines)
            .block(Block::default()
                .title(scroll_info)
                .borders(Borders::TOP)
                .border_style(Style::default().fg(DIM))
                .style(Style::default().bg(DEEP_BG)))
            .wrap(Wrap { trim: false });
        frame.render_widget(panel, area);
    }

    fn draw_prompt(&self, frame: &mut Frame, area: Rect) {
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Length(2)])
            .split(area);

        let cursor = if self.cursor_visible { "█" } else { " " };

        let prompt_style = Style::default().bg(PANEL_BG).fg(Color::White);
        let input_line = Line::from(vec![
            Span::styled(" > ", Style::default().fg(CYAN).add_modifier(Modifier::BOLD)),
            Span::styled(&self.input, Style::default().fg(Color::White)),
            Span::styled(cursor, Style::default().fg(CYAN).add_modifier(Modifier::BOLD)),
        ]);

        let prompt_widget = Paragraph::new(input_line)
            .block(Block::default()
                .borders(Borders::TOP)
                .border_style(Style::default().fg(DIM))
                .style(prompt_style));
        frame.render_widget(prompt_widget, chunks[0]);

        let input_chars = self.input.chars().count();
        let mut hint = String::from(" Ctrl+C quit");
        if self.show_suggestions && !self.suggestions.is_empty() {
            hint.push_str("  PgUp/PgDn select  Tab accept");
        } else {
            hint.push_str("  ↑↓ history  Tab complete  / commands");
        }
        hint.push_str(&format!("  chars: {}", input_chars));

        let status = Paragraph::new(Line::from(Span::styled(&hint, Style::default().fg(DIM))))
            .style(Style::default().bg(PANEL_BG));
        frame.render_widget(status, chunks[1]);
    }

    fn suggestions_popup_area(&self, area: Rect) -> Rect {
        let popup_height = (self.suggestions.len().min(12) as u16).saturating_add(2).min(area.height.saturating_sub(8));
        let popup_width = area.width.saturating_sub(8).min(72).max(40);

        let y = area.height.saturating_sub(6 + popup_height);
        let x = (area.width.saturating_sub(popup_width)) / 2;

        Rect {
            x,
            y: y.max(2),
            width: popup_width,
            height: popup_height,
        }
    }

    fn draw_suggestions_popup(&self, frame: &mut Frame, area: Rect) {
        let max_items = area.height.saturating_sub(2) as usize;
        let items: Vec<ListItem> = self.suggestions
            .iter()
            .take(max_items)
            .enumerate()
            .map(|(idx, s)| {
                let selected = idx == self.selected_suggestion % self.suggestions.len();
                let marker = if selected { "▶" } else { " " };
                let style = if selected {
                    Style::default().fg(Color::Black).bg(CYAN).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(s.color)
                };
                let detail_style = if selected {
                    Style::default().fg(Color::Black).bg(CYAN)
                } else {
                    Style::default().fg(DIM)
                };
                let label_w = 22usize.min(area.width.saturating_sub(6) as usize);
                let detail_w = area.width.saturating_sub(label_w as u16 + 6) as usize;
                ListItem::new(Line::from(vec![
                    Span::styled(format!("{marker} "), style),
                    Span::styled(truncate(&s.label, label_w), style),
                    Span::styled("  ", detail_style),
                    Span::styled(truncate(&s.detail, detail_w), detail_style),
                ]))
            })
            .collect();

        let popup = List::new(items)
            .block(Block::default()
                .title(" Commands ")
                .borders(Borders::ALL)
                .border_style(Style::default().fg(CYAN))
                .style(Style::default().bg(PANEL_BG)));
        frame.render_widget(popup, area);
    }
}

impl Message {
    fn new(role: &str, color: Color, content: &str) -> Self {
        Self {
            time: Local::now().format("%H:%M:%S").to_string(),
            role: role.to_string(),
            color,
            content: content.to_string(),
        }
    }

    fn to_lines(&self, width: usize) -> Vec<Line<'static>> {
        let mut out = Vec::new();
        let prefix = format!(" {} ", self.role);
        let prefix_len = prefix.chars().count() + 2;
        let wrap_w = width.saturating_sub(prefix_len).max(24);

        let content = self.content.clone();
        let lines: Vec<String> = if wrap_w < 24 {
            vec![content]
        } else {
            content.lines().flat_map(|l| {
                if l.chars().count() <= wrap_w {
                    vec![l.to_string()].into_iter()
                } else {
                    let mut chunks = Vec::new();
                    let mut remaining = l;
                    while !remaining.is_empty() {
                        let take = wrap_w.min(remaining.chars().count());
                        let end = remaining.char_indices().take(take).last().map(|(i, c)| i + c.len_utf8()).unwrap_or(remaining.len());
                        chunks.push(remaining[..end].to_string());
                        remaining = &remaining[end..];
                    }
                    chunks.into_iter()
                }
            }).collect()
        };

        for (idx, line) in lines.into_iter().enumerate() {
            if idx == 0 {
                out.push(Line::from(vec![
                    Span::styled(format!("[{}]", self.time), Style::default().fg(DIM)),
                    Span::styled(format!(" {} ", self.role), Style::default().fg(self.color).add_modifier(Modifier::BOLD)),
                    Span::raw(line),
                ]));
            } else {
                out.push(Line::from(vec![
                    Span::styled(" ".repeat(prefix_len.saturating_sub(2)), Style::default().fg(DIM)),
                    Span::raw(line),
                ]));
            }
        }
        out
    }
}

impl CmdSuggestion {
    fn new(label: &str, detail: &str, insert: &str, color: Color) -> Self {
        Self {
            label: label.to_string(),
            detail: detail.to_string(),
            insert: insert.to_string(),
            color,
        }
    }
}

fn truncate(text: &str, width: usize) -> String {
    let limit = width.saturating_sub(2).max(4);
    if text.chars().count() <= limit {
        text.to_string()
    } else {
        let mut out: String = text.chars().take(limit.saturating_sub(1)).collect();
        out.push('…');
        out
    }
}
