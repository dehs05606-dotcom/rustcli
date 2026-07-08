# Security Policy

## API keys

AIA Agent must never hard-code API keys in source code, examples, prompts, screenshots, or docs.

Use environment variables only:

- `OPENCODE_API_KEY`
- `NVIDIA_API_KEY` or `NVIDIA_NIM_API_KEY`
- `OPENAI_API_KEY`
- `OPENROUTER_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

If a key was pasted into chat, committed to git, or shown in logs, rotate/revoke it immediately.

## Dangerous commands

The shell tool blocks known destructive commands and asks approval for commands that mutate files, git state, packages, containers, clusters, or network installs.

## File writes

AIA previews diffs and stores backups in `.aia/undo/` before replacing existing files.
