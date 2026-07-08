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
    widgets::{Block, Borders, Clear, List, ListItem, Paragraph},
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

struct Msg {
    role: &'static str,
    color: Color,
    time: String,
    text: String,
}

struct App {
    cfg: Config,
    msgs: Vec<Msg>,
    input: String,
    cursor_on: bool,
    tick: u64,
    show_popup: bool,
    sel: usize,
    cmds: Vec<Cmd>,
    history: Vec<String>,
    hist_pos: Option<usize>,
    scroll: usize,
    file: FileTools,
    shell: ShellTool,
}

#[derive(Clone)]
struct Cmd {
    label: String,
    desc: String,
    insert: String,
    color: Color,
}

impl App {
    fn new(cfg: Config) -> Self {
        let root = cfg.project_root.clone();
        let mut msgs = Vec::new();
        msgs.push(Msg::new("SYSTEM", GREEN, &format!(
            "rustcli v0.4 — {} / {} / effort={}", cfg.provider, cfg.model, cfg.effort
        )));
        msgs.push(Msg::new("AIA", CYAN, "Type / for commands — read, edit, shell, search, and more."));
        App {
            cfg,
            msgs,
            input: String::new(),
            cursor_on: true,
            tick: 0,
            show_popup: false,
            sel: 0,
            cmds: Vec::new(),
            history: Vec::new(),
            hist_pos: None,
            scroll: 0,
            file: FileTools::new(root.clone()),
            shell: ShellTool::new(root),
        }
    }

    fn run(&mut self, terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<()> {
        loop {
            terminal.draw(|f| self.draw(f))?;
            self.tick = self.tick.wrapping_add(1);
            self.cursor_on = self.tick % 12 < 6;
            if event::poll(Duration::from_millis(50))? {
                match event::read()? {
                    Event::Key(k) => {
                        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL);
                        if ctrl && k.code == KeyCode::Char('c') { break; }
                        if ctrl && k.code == KeyCode::Char('l') { self.clear(); continue; }
                        if ctrl && k.code == KeyCode::Char('d') { break; }
                        if !self.key(k.code) { break; }
                    }
                    _ => {}
                }
            }
        }
        Ok(())
    }

    fn key(&mut self, code: KeyCode) -> bool {
        match code {
            KeyCode::Esc => return false,
            KeyCode::Char('q') if self.input.is_empty() => return false,
            KeyCode::Enter => self.submit(),
            KeyCode::Backspace => { self.input.pop(); self.build_cmds(); }
            KeyCode::Tab => self.accept(),
            KeyCode::Up => self.hist_bk(),
            KeyCode::Down => self.hist_fw(),
            KeyCode::PageUp => {
                if self.show_popup && !self.cmds.is_empty() {
                    let n = self.cmds.len();
                    self.sel = if self.sel == 0 { n - 1 } else { self.sel - 1 };
                } else {
                    self.scroll = self.scroll.saturating_add(3);
                }
            }
            KeyCode::PageDown => {
                if self.show_popup && !self.cmds.is_empty() {
                    let n = self.cmds.len();
                    self.sel = (self.sel + 1) % n;
                } else {
                    self.scroll = self.scroll.saturating_sub(3);
                }
            }
            KeyCode::Home => self.scroll = self.msgs.len(),
            KeyCode::End => self.scroll = 0,
            KeyCode::Char(c) => { self.input.push(c); self.build_cmds(); }
            _ => {}
        }
        true
    }

    fn build_cmds(&mut self) {
        let q = self.input.to_ascii_lowercase();
        let is_slash = q.starts_with('/');
        let mut all = Vec::new();
        all.push(Cmd::new("/help", "Show all commands", "/help", GREEN));
        all.push(Cmd::new("/clear", "Clear history", "/clear", RED));
        all.push(Cmd::new("/status", "Show runtime config", "/status", CYAN));
        all.push(Cmd::new("/effort fast", "Low-latency mode", "/effort fast", GREEN));
        all.push(Cmd::new("/effort balanced", "Default mode", "/effort balanced", BLUE));
        all.push(Cmd::new("/effort deep", "Deep analysis", "/effort deep", PURPLE));
        all.push(Cmd::new("/effort max", "Max context", "/effort max", ORANGE));
        all.push(Cmd::new("/model <name>", "Switch model", "/model ", PURPLE));
        all.push(Cmd::new("/provider <name>", "Switch provider", "/provider ", CYAN));
        all.push(Cmd::new("/read <file>", "Read a file", "/read ", BLUE));
        all.push(Cmd::new("/edit <file> <old> <new>", "Replace in file", "/edit ", ORANGE));
        all.push(Cmd::new("/write <file> <text>", "Write a file", "/write ", YELLOW));
        all.push(Cmd::new("/shell <cmd>", "Run shell command", "/shell ", GREEN));
        all.push(Cmd::new("/search <regex>", "Search codebase", "/search ", PURPLE));
        if is_slash {
            self.show_popup = true;
            self.cmds = all.into_iter().filter(|c| c.label.to_ascii_lowercase().contains(&q) || q == "/").collect();
        } else {
            self.show_popup = false;
            self.cmds.clear();
        }
        if self.sel >= self.cmds.len() { self.sel = self.cmds.len().saturating_sub(1); }
    }

    fn accept(&mut self) {
        if !self.show_popup || self.cmds.is_empty() {
            if self.input.starts_with('/') && !self.input.contains(' ') {
                for c in &[
                    "/help","/clear","/status","/effort","/model","/provider",
                    "/read","/edit","/write","/shell","/search",
                ] {
                    if c.starts_with(&self.input) && c != &self.input {
                        self.input = c.to_string() + " ";
                        self.build_cmds();
                        return;
                    }
                }
            }
            return;
        }
        let i = self.sel.min(self.cmds.len() - 1);
        self.input = self.cmds[i].insert.clone();
        self.show_popup = false;
        self.cmds.clear();
        self.sel = 0;
    }

    fn hist_bk(&mut self) {
        if !self.input.is_empty() || self.history.is_empty() { return; }
        let p = self.hist_pos.unwrap_or(self.history.len()).saturating_sub(1);
        self.hist_pos = Some(p);
        self.input = self.history[p].clone();
        self.show_popup = false;
    }

    fn hist_fw(&mut self) {
        if let Some(p) = self.hist_pos {
            let n = p + 1;
            if n < self.history.len() {
                self.hist_pos = Some(n);
                self.input = self.history[n].clone();
            } else {
                self.hist_pos = None;
                self.input.clear();
            }
            self.show_popup = false;
        }
    }

    fn submit(&mut self) {
        let s = self.input.trim().to_string();
        if s.is_empty() { return; }
        self.input.clear();
        self.show_popup = false;
        self.cmds.clear();
        self.sel = 0;
        self.hist_pos = None;
        if self.history.last().map(String::as_str) != Some(&s) {
            self.history.push(s.clone());
            if self.history.len() > 100 { self.history.remove(0); }
        }
        self.msgs.push(Msg::new("USER", ORANGE, &s));
        self.scroll = self.msgs.len();
        if s.starts_with('/') { self.exec(&s); }
        else { self.msgs.push(Msg::new("AIA", CYAN, "Type / for commands")); }
    }

    fn exec(&mut self, s: &str) {
        let mut parts = s.splitn(3, ' ');
        let cmd = parts.next().unwrap_or_default();
        let rest = parts.collect::<Vec<&str>>().join(" ");

        match cmd {
            "/help" => {
                self.msgs.push(Msg::new("AIA", GREEN, &[
                    "/help       — Show this help",
                    "/clear      — Clear (Ctrl+L)",
                    "/status     — Runtime info",
                    "/effort     — fast|balanced|deep|max",
                    "/model      — Switch model",
                    "/provider   — Switch provider",
                    "/read       — /read <file>",
                    "/edit       — /edit <file> <old> <new>",
                    "/write      — /write <file> <text>",
                    "/shell      — /shell <command>",
                    "/search     — /search <regex>",
                ].join("\n")));
            }
            "/clear" => self.clear(),
            "/status" => {
                self.msgs.push(Msg::new("AIA", CYAN, &self.cfg.runtime_summary()));
            }
            "/effort" => {
                let m = rest.trim();
                if m.is_empty() {
                    self.msgs.push(Msg::new("AIA", CYAN, &format!("Effort: {}. Options: fast, balanced, deep, max", self.cfg.effort)));
                } else if let Ok(mode) = m.parse::<crate::model_catalog::EffortMode>() {
                    self.cfg.apply_effort_with_recommended_model(mode);
                    self.msgs.push(Msg::new("AIA", GREEN, &format!("Effort: {} | model: {}", self.cfg.effort, self.cfg.model)));
                } else {
                    self.msgs.push(Msg::new("AIA", RED, &format!("Invalid: {}", m)));
                }
            }
            "/model" => {
                let m = rest.trim();
                if m.is_empty() { self.msgs.push(Msg::new("AIA", CYAN, &format!("Model: {}", self.cfg.model))); }
                else { let o = self.cfg.model.clone(); self.cfg.model = m.to_string(); self.msgs.push(Msg::new("AIA", GREEN, &format!("Model: {} → {}", o, m))); }
            }
            "/provider" => {
                let p = rest.trim();
                if p.is_empty() { self.msgs.push(Msg::new("AIA", CYAN, &format!("Provider: {}", self.cfg.provider))); }
                else { let o = self.cfg.provider.clone(); self.cfg.provider = p.to_string(); self.msgs.push(Msg::new("AIA", GREEN, &format!("Provider: {} → {}", o, p))); }
            }
            "/read" => {
                let p = rest.trim();
                if p.is_empty() { self.msgs.push(Msg::new("AIA", ORANGE, "Usage: /read <file>")); }
                else {
                    match self.file.read(p, 200_000) {
                        Ok(r) => { let mut c = format!("📄 {} ({}b)", r.path, r.bytes); if r.truncated { c.push_str(" [truncated]"); } c.push('\n'); c.push_str(&r.content); self.msgs.push(Msg::new("FILE", BLUE, &c)); }
                        Err(e) => { self.msgs.push(Msg::new("AIA", RED, &format!("{:#}", e))); }
                    }
                }
            }
            "/edit" => {
                let a: Vec<&str> = rest.splitn(3, ' ').collect();
                if a.len() < 3 { self.msgs.push(Msg::new("AIA", ORANGE, "Usage: /edit <file> <old> <new>")); }
                else {
                    match self.file.replace(a[0], a[1], a[2], false, true) {
                        Ok(p) => { self.msgs.push(Msg::new("FILE", GREEN, &format!("Edited {}:\n{}", a[0], p.diff))); }
                        Err(e) => { self.msgs.push(Msg::new("AIA", RED, &format!("{:#}", e))); }
                    }
                }
            }
            "/write" => {
                let a: Vec<&str> = rest.splitn(2, ' ').collect();
                if a.len() < 2 { self.msgs.push(Msg::new("AIA", ORANGE, "Usage: /write <file> <text>")); }
                else {
                    match self.file.write(a[0], a[1]) {
                        Ok(p) => { let m = if p.backup_path.is_some() { format!("Written {} (backup saved)", p.path) } else { format!("Created {}", p.path) }; self.msgs.push(Msg::new("FILE", GREEN, &m)); }
                        Err(e) => { self.msgs.push(Msg::new("AIA", RED, &format!("{:#}", e))); }
                    }
                }
            }
            "/shell" => {
                let c = rest.trim();
                if c.is_empty() { self.msgs.push(Msg::new("AIA", ORANGE, "Usage: /shell <cmd>")); }
                else {
                    self.msgs.push(Msg::new("SHELL", YELLOW, &format!("$ {}", c)));
                    if let Ok(rt) = tokio::runtime::Runtime::new() {
                        match rt.block_on(self.shell.run(c, true)) {
                            Ok(o) => { let mut out = String::new(); if let Some(s) = o.status { out.push_str(&format!("exit: {}\n", s)); } out.push_str(&o.stdout); out.push_str(&o.stderr); self.msgs.push(Msg::new("SHELL", GREEN, &out)); }
                            Err(e) => { self.msgs.push(Msg::new("AIA", RED, &format!("{:#}", e))); }
                        }
                    }
                }
            }
            "/search" => {
                let p = rest.trim();
                if p.is_empty() { self.msgs.push(Msg::new("AIA", ORANGE, "Usage: /search <regex>")); }
                else if let Ok(rt) = tokio::runtime::Runtime::new() {
                    let search = SearchTool::new(self.cfg.project_root.clone());
                    match rt.block_on(search.regex_search(p, ".", 100)) {
                        Ok(m) => { if m.is_empty() { self.msgs.push(Msg::new("AIA", DIM, &format!("No results: {}", p))); } else { let l: Vec<String> = m.iter().map(|m| format!("{}:{}:{}", m.path, m.line, m.text)).collect(); self.msgs.push(Msg::new("SEARCH", PURPLE, &l.join("\n"))); } }
                        Err(e) => { self.msgs.push(Msg::new("AIA", RED, &format!("{:#}", e))); }
                    }
                }
            }
            _ => { self.msgs.push(Msg::new("AIA", RED, &format!("Unknown: {}. Type /help", cmd))); }
        }
        self.scroll = self.msgs.len();
        if self.msgs.len() > 500 { self.msgs.drain(0..(self.msgs.len() - 500)); }
    }

    fn clear(&mut self) {
        self.msgs.clear();
        self.msgs.push(Msg::new("SYSTEM", GREEN, "Cleared"));
        self.scroll = self.msgs.len();
    }

    // ─── DRAW ────────────────────────────────────────────────

    fn draw(&self, f: &mut Frame) {
        let area = f.area();
        f.render_widget(Block::default().style(Style::default().bg(DEEP_BG)), area);

        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Min(1), Constraint::Length(1)])
            .split(area);

        self.draw_top(f, rows[0]);
        self.draw_msgs(f, rows[1]);
        self.draw_bar(f, rows[2]);

        if self.show_popup && !self.cmds.is_empty() {
            let pop = self.popup_area(area);
            f.render_widget(Clear, pop);
            self.draw_popup(f, pop);
        }
    }

    fn draw_top(&self, f: &mut Frame, area: Rect) {
        let n = self.msgs.len();
        let s = if self.scroll > 0 { format!(" +{} scroll", self.scroll) } else { String::new() };
        let msgs_str = format!("{} msgs{}", n, s);
        let model_str = format!("  {} | {}", self.cfg.provider, self.cfg.model);
        let text = Line::from(vec![
            Span::styled(" rustcli ", Style::default().fg(CYAN).add_modifier(Modifier::BOLD)),
            Span::styled(msgs_str, Style::default().fg(DIM)),
            Span::styled(model_str, Style::default().fg(DIM)),
        ]);
        f.render_widget(Paragraph::new(text).style(Style::default().bg(DEEP_BG)), area);
    }

    fn draw_msgs(&self, f: &mut Frame, area: Rect) {
        let max_h = area.height.saturating_sub(1) as usize;
        let all: Vec<Line> = self.msgs.iter().flat_map(|m| m.to_lines(area.width as usize)).collect();
        let total = all.len();
        let end = total.saturating_sub(self.scroll.min(total));
        let start = end.saturating_sub(max_h.max(1));
        let lines: Vec<Line> = if start < end { all[start..end].to_vec() } else { vec![Line::from(Span::styled("", Style::default().fg(DIM)))] };
        let panel = Paragraph::new(lines).block(Block::default().borders(Borders::TOP).border_style(Style::default().fg(DIM)).style(Style::default().bg(DEEP_BG)));
        f.render_widget(panel, area);
    }

    fn draw_bar(&self, f: &mut Frame, area: Rect) {
        let w = area.width as usize;
        let cursor = if self.cursor_on { "█" } else { " " };
        let input_str = if self.input.chars().count() > w.saturating_sub(4) {
            let cut = self.input.chars().skip(self.input.chars().count().saturating_sub(w.saturating_sub(6))).collect::<String>();
            format!("{}", cut)
        } else {
            self.input.clone()
        };
        let text = Line::from(vec![
            Span::styled("> ", Style::default().fg(CYAN).add_modifier(Modifier::BOLD)),
            Span::styled(&input_str, Style::default().fg(Color::White)),
            Span::styled(cursor, Style::default().fg(CYAN)),
        ]);
        let bar = Paragraph::new(text).style(Style::default().bg(PANEL_BG));
        f.render_widget(bar, area);
    }

    fn popup_area(&self, area: Rect) -> Rect {
        let h = (self.cmds.len().min(10) as u16).saturating_add(2).min(area.height.saturating_sub(6));
        let w = area.width.saturating_sub(4).min(64).max(40);
        let y = area.height.saturating_sub(4 + h);
        Rect { x: 2, y: y.max(2), width: w, height: h }
    }

    fn draw_popup(&self, f: &mut Frame, area: Rect) {
        let max = area.height.saturating_sub(2) as usize;
        let items: Vec<ListItem> = self.cmds.iter().take(max).enumerate().map(|(i, c)| {
            let selected = i == self.sel % self.cmds.len();
            let marker = if selected { "▸" } else { " " };
            let style = if selected { Style::default().fg(Color::Black).bg(CYAN) } else { Style::default().fg(c.color) };
            let ws = area.width.saturating_sub(8) as usize;
            ListItem::new(Line::from(vec![
                Span::styled(format!("{} ", marker), style),
                Span::styled(truncate(&c.label, 24), style),
                Span::styled("  ", if selected { style } else { Style::default().fg(DIM) }),
                Span::styled(truncate(&c.desc, ws.saturating_sub(28)), if selected { style } else { Style::default().fg(DIM) }),
            ]))
        }).collect();
        let popup = List::new(items).block(Block::default().title(" / ").borders(Borders::ALL).border_style(Style::default().fg(CYAN)).style(Style::default().bg(PANEL_BG)));
        f.render_widget(popup, area);
    }
}

impl Msg {
    fn new(role: &'static str, color: Color, text: &str) -> Self {
        Msg { role, color, time: Local::now().format("%H:%M").to_string(), text: text.to_string() }
    }

    fn to_lines(&self, w: usize) -> Vec<Line<'static>> {
        let mut out = Vec::new();
        let prefix = format!(" {} ", self.role);
        let plen = prefix.chars().count() + 2;
        let wrap = w.saturating_sub(plen).max(24);
        let text = self.text.clone();
        let lines: Vec<String> = if wrap < 24 {
            vec![text]
        } else {
            text.lines().flat_map(|l| {
                if l.chars().count() <= wrap { vec![l.to_string()].into_iter() }
                else {
                    let mut chunks = Vec::new();
                    let mut s = l;
                    while !s.is_empty() {
                        let take = wrap.min(s.chars().count());
                        let end = s.char_indices().take(take).last().map(|(i, c)| i + c.len_utf8()).unwrap_or(s.len());
                        chunks.push(s[..end].to_string());
                        s = &s[end..];
                    }
                    chunks.into_iter()
                }
            }).collect()
        };
        for (i, line) in lines.into_iter().enumerate() {
            if i == 0 {
                out.push(Line::from(vec![
                    Span::styled(format!("[{}]", self.time), Style::default().fg(DIM)),
                    Span::styled(format!(" {} ", self.role), Style::default().fg(self.color).add_modifier(Modifier::BOLD)),
                    Span::raw(line),
                ]));
            } else {
                out.push(Line::from(vec![
                    Span::styled(" ".repeat(plen.saturating_sub(2)), Style::default().fg(DIM)),
                    Span::raw(line),
                ]));
            }
        }
        out
    }
}

impl Cmd {
    fn new(label: &str, desc: &str, insert: &str, color: Color) -> Self {
        Cmd { label: label.to_string(), desc: desc.to_string(), insert: insert.to_string(), color }
    }
}

fn truncate(s: &str, w: usize) -> String {
    let limit = w.saturating_sub(2).max(4);
    if s.chars().count() <= limit { s.to_string() }
    else { let mut o: String = s.chars().take(limit.saturating_sub(1)).collect(); o.push('…'); o }
}
