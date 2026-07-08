# AIA Neon TUI + Prompt Box Spec v0.3

This document describes the target terminal interface based on the uploaded reference image. The current Rust implementation in `src/tui.rs` implements this layout as a real interactive `ratatui` control deck scaffold.

## Screen layout

```txt
┌──────────────────────────────────────────────────────────────────────────────┐
│ AI TERMINAL AGENT | Session | Provider:Model | Effort | Context | Uptime    │
├────────────────────────────────┬───────────────┬────────────────────────────┤
│ Terminal History               │ FILES         │ AGENTS                     │
│ Chat + tool output             │ indexed tree  │ planner/coder/reviewer     │
│ scrollable with PgUp/PgDn      │ repo scan     │ tester/security status      │
│                                ├───────────────┼────────────────────────────┤
│ Diff Viewer + Agent Graph      │ Tool Logs     │ Enhanced Context Activity   │
│ before/after patches           │ command logs  │ heatmap + CPU/RAM/GPU       │
│                                │               ├────────────────────────────┤
│                                │               │ Active Tasks progress bars  │
│                                │               ├────────────────────────────┤
│                                │               │ Model Router + token meter  │
├────────────────────────────────┴───────────────┴────────────────────────────┤
│ Prompt Box: @terminal $ user text █                                         │
│ Context chips: @files @git @tests @security @docs @shell                    │
│ Composer status: active context, char count, effort, shortcut hints         │
│ Smart autocomplete: slash commands, model routes, workflows, chips          │
├──────────────────────────────────────────────────────────────────────────────┤
│ Ctrl+C quit | Ctrl+P palette | Ctrl+F/G/T/S chips | PgUp/PgDn history       │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Color system

- Cyan: active model, terminal cursor, context routing
- Green: successful tools, safe shell, agent ready state
- Purple: orchestrator, reviewer, graph links
- Orange: prompt hints, model context metadata, warnings
- Red: security auditor, blocked commands, risky diffs
- Blue: file/context panels
- Deep navy/black background for high contrast

## Prompt box behavior

The prompt box now behaves like a command palette plus context router:

- Type normally; `Enter` submits.
- `Tab` accepts selected autocomplete.
- `Up`/`Down` changes autocomplete selection.
- `Left`/`Right` selects a context chip.
- `Space` toggles selected chip when input is empty.
- `Ctrl+P` opens/closes command palette overlay.
- `Ctrl+F`, `Ctrl+G`, `Ctrl+T`, `Ctrl+S` toggle `@files`, `@git`, `@tests`, `@security`.
- `PageUp`/`PageDown` scroll terminal history.
- `F1` help overlay.
- `F2` palette.
- `F3` fast mode.
- `F4` max mode.

## Slash commands

- `/effort fast` switches to low latency mode.
- `/effort balanced` returns to default.
- `/effort deep` increases inspection/testing.
- `/effort max` uses maximum context profile.
- `/provider opencode|nvidia|ollama|openai|openrouter`
- `/model <name>` switches the current model shown in the header.
- `/status` shows provider/model/effort/token settings.
- `/chips` shows active context chips.
- `/clear` clears terminal history.
- `/help` opens help overlay.

## Context chips

- `@files`: repo tree + important files
- `@git`: git status/diff review
- `@tests`: test planning and safe test execution
- `@security`: command/file/secret risk analysis
- `@docs`: documentation/prompt updates
- `@shell`: safe command suggestions

## Implemented now

- Real alternate-screen `ratatui` UI
- Header, terminal history, file explorer, diff viewer, agent graph, tool logs
- Enhanced context panel with animated neural heatmap and sparkline
- Active task gauges
- Model router + token meter panel
- Interactive prompt composer with chips, suggestions, history recall, palette, help overlay
- Fast/balanced/deep/max effort switching inside TUI
- Browser preview in `docs/neon_tui_preview.html` with interactive chips, suggestions, and command updates

## Next integration step

Wire the TUI prompt to the existing async `Agent` streaming loop so chat responses update the terminal history in real time and file diffs open in the diff panel before approval.
