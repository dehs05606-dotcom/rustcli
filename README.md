# AIA Agent — Advanced Terminal AI Coding Agent in Rust

AIA Agent is a terminal AI coding agent with safe tools, memory, model routing, effort modes, multi-agent review, and a neon `ratatui` interface.

## Quick Install

```bash
curl -sSf https://raw.githubusercontent.com/dehs05606-dotcom/rsclitest/main/install.sh | bash
```

Or download the binary directly:

```bash
curl -sSfL https://github.com/dehs05606-dotcom/rsclitest/releases/download/v1.0.0/aia-agent -o /usr/local/bin/aia-agent
chmod +x /usr/local/bin/aia-agent
```

### Setup

```bash
export OPENCODE_API_KEY="your_key_here"
export AIA_PROVIDER=opencode
export AIA_MODEL=deepseek-v4-flash-free
aia-agent chat
```

## Important security note

Do **not** hard-code API keys. If a key was pasted into chat, logs, screenshots, or git, rotate/revoke it. AIA uses environment variables and `.env` only. `.env` is ignored by git.

## Features now implemented

- Terminal chat agent with streaming for OpenAI-compatible APIs and Ollama
- Providers:
  - Ollama
  - OpenAI
  - OpenRouter
  - Anthropic Claude
  - Gemini
  - OpenCode Zen / opencode.ai
  - NVIDIA NIM / integrate API
- Effort modes:
  - `fast` — low latency, fewer tool loops, smaller scan
  - `balanced` — default
  - `deep` — complex coding/refactor tasks
  - `max` — huge context profile
- Model catalog with context metadata:
  - `deepseek-v4-flash-free` — 200K context
  - `mimo-v2.5-free` — 1M context
  - `big-pickle` — 200K context
  - `z-ai/glm-5.2` — 1M context
  - `z-ai/glm-5.1` — 1M context
  - `deepseek-ai/deepseek-v4-pro` — 1M context
  - `stepfun-ai/step-3.7-flash` — 200K context
- Agentic tool protocol for:
  - project tree
  - file read/write/replace
  - regex search
  - safe shell
  - git status/diff
  - memory add/list
- File write diff preview and undo backup in `.aia/undo/`
- Dangerous shell command blocking
- SQLite memory
- Multi-agent sequential mode:
  - Planner
  - Coder
  - Reviewer
  - Tester
  - Security Auditor
- MCP registry skeleton
- Optional tree-sitter parsing
- Neon TUI control deck v0.3:
  - terminal history with PageUp/PageDown scroll
  - advanced prompt composer
  - context chips: `@files`, `@git`, `@tests`, `@security`, `@docs`, `@shell`
  - smart autocomplete with selectable suggestions
  - Ctrl+P command palette overlay
  - F1 help overlay
  - F3 fast mode and F4 max mode shortcuts
  - file explorer
  - diff viewer
  - agent graph
  - enhanced context activity
  - model router/token meter
  - tool logs
  - task progress gauges

## Quick start

### Binary (recommended)

```bash
# Install
curl -sSf https://raw.githubusercontent.com/dehs05606-dotcom/rsclitest/main/install.sh | bash

# Run with OpenCode
export OPENCODE_API_KEY="your_key_here"
export AIA_PROVIDER=opencode
export AIA_MODEL=deepseek-v4-flash-free
aia-agent chat
```

### From source

```bash
git clone https://github.com/dehs05606-dotcom/rsclitest.git
cd rsclitest
cp .env.example .env
cargo build --release
./target/release/aia-agent chat
```

Local Ollama:

```bash
ollama pull qwen2.5-coder:7b
AIA_PROVIDER=ollama AIA_MODEL=qwen2.5-coder:7b aia-agent chat "Explain this repo"
```

OpenCode Zen fast mode:

```bash
OPENCODE_API_KEY="your_key_here" \
AIA_PROVIDER=opencode \
AIA_MODEL=deepseek-v4-flash-free \
AIA_EFFORT=fast \
aia-agent chat "Fast review this Rust project"
```

NVIDIA large-context mode:

```bash
NVIDIA_API_KEY="your_key_here" \
AIA_PROVIDER=nvidia \
AIA_MODEL=z-ai/glm-5.2 \
AIA_EFFORT=max \
aia-agent chat "Analyze the whole codebase deeply"
"}

> Base URL note: for OpenCode you may set either `https://opencode.ai/zen/v1` or `https://opencode.ai/zen/v1/chat/completions`. AIA normalizes both.

## CLI commands

```bash
# Interactive chat with slash commands
cargo run -- chat

# One-shot prompt
cargo run -- --effort fast chat "Give a very fast answer"

# Slash command also works in chat input
/effort fast
/status
/models

# Multi-agent reasoning pass
cargo run -- multi "Plan, code, review, test, and security audit this change"

# Model catalog
cargo run -- models

# Effort profile preview
cargo run -- effort fast
cargo run -- effort max

# Project scan
cargo run -- scan . --max-files 500

# Search
cargo run -- search "TODO|panic!|unwrap\(" .

# Safe shell execution
cargo run -- shell "cargo test"

# Neon TUI
cargo run -- tui

# System prompt
cargo run -- prompt

# Memory
cargo run -- memory add preference "Always show diffs before writing files"
cargo run -- memory list

# Optional parser with tree-sitter
cargo run --features code-parsing -- parse src/main.rs
```

## `.env` examples

OpenCode fast:

```env
AIA_PROVIDER=opencode
AIA_MODEL=deepseek-v4-flash-free
AIA_EFFORT=fast
OPENCODE_API_KEY=your_key_here
AIA_OPENCODE_BASE_URL=https://opencode.ai/zen/v1/chat/completions
```

OpenCode 1M context:

```env
AIA_PROVIDER=opencode
AIA_MODEL=mimo-v2.5-free
AIA_EFFORT=max
OPENCODE_API_KEY=your_key_here
```

NVIDIA 1M context:

```env
AIA_PROVIDER=nvidia
AIA_MODEL=z-ai/glm-5.2
AIA_EFFORT=max
NVIDIA_API_KEY=your_key_here
AIA_NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
```

NVIDIA flash:

```env
AIA_PROVIDER=nvidia
AIA_MODEL=stepfun-ai/step-3.7-flash
AIA_EFFORT=fast
NVIDIA_API_KEY=your_key_here
```

## Tool protocol used by the LLM

AIA asks the model to request tools with fenced JSON:

````markdown
```tool
{"tool":"file.read","args":{"path":"src/main.rs"}}
```
````

Multiple calls:

````markdown
```tool
[
  {"tool":"project.tree","args":{"max_files":200}},
  {"tool":"search.regex","args":{"pattern":"TODO|panic!","path":"."}}
]
```
````

Write and shell tools ask for approval unless `auto_apply = true`.

## TUI design

See `docs/TUI_PROMPT_BOX_SPEC.md` and the browser preview `docs/neon_tui_preview.html`.

## Roadmap

- Wire TUI prompt to live `Agent` streaming loop
- Full MCP JSON-RPC stdio client
- Token-aware context packing and summarization
- Vector memory with Qdrant
- Autonomous test-fix loop with git checkpoints
- Claude Code-style full-screen diff approval widget
