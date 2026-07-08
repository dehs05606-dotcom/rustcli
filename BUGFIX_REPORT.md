# AIA Agent Bugfix Report

Date: 2026-07-08

## Checks performed in this workspace

- Static source scan across `src/`
- TOML parse check for `Cargo.toml`
- Bracket/brace balance check for the large TUI source
- Secret scan for previously pasted key prefixes
- Zip rebuild after fixes

> Rust/Cargo are not installed in this sandbox, so `cargo check` could not be executed here. Run `cargo fmt && cargo check` on a machine with Rust installed.

## Bugs fixed

### 1. TUI `handle_main_key` compile/type bug

The `Esc` and `q` match arms returned `true` inside a match otherwise used for side effects. This could cause a match arm type mismatch. Fixed by using early `return true`.

### 2. Prompt chip toggle unreachable bug

`KeyCode::Char(c)` matched before `KeyCode::Char(' ')`, so pressing Space never toggled a selected context chip when the prompt was empty. Reordered the match arms so Space is handled first.

### 3. Runtime panel displayed debug `Some("...")`

The TUI runtime panel showed values like `Some("200K")`. Fixed by formatting context/output token values cleanly as `200K`, `1M`, or `unknown`.

### 4. Unused/dead TUI fields and constants

Removed unused `active_tab`, `GREEN_DARK`, and `LINE` symbols. Used context chip labels in the UI to avoid stale fields and make chip display clearer.

### 5. File tool symlink escape vulnerability

The file tool previously blocked `..` and absolute paths but did not canonicalize existing files/parents, so a symlink inside the project could point outside the project root. Added canonical root checks for read/write/replace/preview operations.

### 6. Zip regenerated

`aia-agent.zip` was rebuilt after all fixes.

## Secret scan result

No occurrences of the previously pasted key prefixes were found inside the project files before zipping.
