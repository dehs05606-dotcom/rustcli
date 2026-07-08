use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCall {
    pub tool: String,
    #[serde(default)]
    pub args: Value,
}

pub fn parse_tool_calls(text: &str) -> Vec<ToolCall> {
    let mut calls = Vec::new();
    let mut in_tool_block = false;
    let mut buf = String::new();

    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("```") {
            if !in_tool_block {
                let info = trimmed.trim_start_matches("```").trim().to_ascii_lowercase();
                if info == "tool" || info == "json tool" || info == "tool json" {
                    in_tool_block = true;
                    buf.clear();
                }
            } else {
                extend_calls_from_json(&buf, &mut calls);
                in_tool_block = false;
                buf.clear();
            }
            continue;
        }

        if in_tool_block {
            buf.push_str(line);
            buf.push('\n');
        }
    }

    // Also support XML-ish tags for models that dislike markdown fences.
    let mut rest = text;
    while let Some(start) = rest.find("<tool_call>") {
        rest = &rest[start + "<tool_call>".len()..];
        if let Some(end) = rest.find("</tool_call>") {
            let json = &rest[..end];
            extend_calls_from_json(json, &mut calls);
            rest = &rest[end + "</tool_call>".len()..];
        } else {
            break;
        }
    }

    calls
}

fn extend_calls_from_json(raw: &str, calls: &mut Vec<ToolCall>) {
    let raw = raw.trim();
    if raw.is_empty() {
        return;
    }
    if let Ok(call) = serde_json::from_str::<ToolCall>(raw) {
        calls.push(call);
        return;
    }
    if let Ok(many) = serde_json::from_str::<Vec<ToolCall>>(raw) {
        calls.extend(many);
    }
}
