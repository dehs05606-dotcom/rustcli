pub fn extract_shell_suggestions(markdown: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut in_shell = false;

    for line in markdown.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("```") {
            let info = trimmed.trim_start_matches("```").trim().to_ascii_lowercase();
            if in_shell {
                in_shell = false;
            } else if matches!(info.as_str(), "bash" | "sh" | "shell" | "zsh" | "fish") {
                in_shell = true;
            }
            continue;
        }

        if in_shell {
            let cmd = trimmed.strip_prefix("$ ").unwrap_or(trimmed);
            if !cmd.is_empty() && !cmd.starts_with('#') {
                out.push(cmd.to_string());
            }
        } else if let Some(cmd) = trimmed.strip_prefix("$ ") {
            if !cmd.trim().is_empty() {
                out.push(cmd.trim().to_string());
            }
        }
    }

    out.sort();
    out.dedup();
    out
}
