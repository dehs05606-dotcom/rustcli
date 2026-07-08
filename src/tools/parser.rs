use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParseSummary {
    pub language: String,
    pub named_node_count: usize,
    pub symbols: Vec<String>,
    pub note: Option<String>,
}

#[cfg(feature = "code-parsing")]
pub fn parse_file_symbols(path: &Path, source: &str) -> Result<ParseSummary> {
    use anyhow::Context;
    use tree_sitter::{Language, Node, Parser};

    let (language_name, language) = match language_for_extension(path) {
        Some(v) => v,
        None => {
            return Ok(ParseSummary {
                language: "unknown".to_string(),
                named_node_count: 0,
                symbols: Vec::new(),
                note: Some("no tree-sitter language configured for this file extension".to_string()),
            });
        }
    };

    let mut parser = Parser::new();
    parser
        .set_language(&language)
        .with_context(|| format!("failed to set tree-sitter language {language_name}"))?;
    let tree = parser
        .parse(source, None)
        .context("tree-sitter parser returned no tree")?;
    let root = tree.root_node();
    let mut symbols = Vec::new();
    let mut count = 0;
    collect_symbols(root, source.as_bytes(), &mut count, &mut symbols);
    symbols.truncate(300);
    Ok(ParseSummary {
        language: language_name.to_string(),
        named_node_count: count,
        symbols,
        note: None,
    })
}

#[cfg(feature = "code-parsing")]
fn language_for_extension(path: &Path) -> Option<(&'static str, tree_sitter::Language)> {
    let ext = path.extension()?.to_str()?;
    match ext {
        "rs" => Some(("rust", tree_sitter_rust::LANGUAGE.into())),
        "js" | "jsx" | "mjs" | "cjs" => Some(("javascript", tree_sitter_javascript::LANGUAGE.into())),
        "py" => Some(("python", tree_sitter_python::LANGUAGE.into())),
        "go" => Some(("go", tree_sitter_go::LANGUAGE.into())),
        "java" => Some(("java", tree_sitter_java::LANGUAGE.into())),
        _ => None,
    }
}

#[cfg(feature = "code-parsing")]
fn collect_symbols(node: tree_sitter::Node, source: &[u8], count: &mut usize, symbols: &mut Vec<String>) {
    if node.is_named() {
        *count += 1;
        if is_symbol_kind(node.kind()) {
            let text = node
                .utf8_text(source)
                .unwrap_or("")
                .lines()
                .next()
                .unwrap_or("")
                .trim();
            let pos = node.start_position();
            symbols.push(format!("{}:{} {}", pos.row + 1, node.kind(), text));
        }
    }
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        collect_symbols(child, source, count, symbols);
    }
}

#[cfg(feature = "code-parsing")]
fn is_symbol_kind(kind: &str) -> bool {
    matches!(
        kind,
        "function_item"
            | "struct_item"
            | "enum_item"
            | "trait_item"
            | "impl_item"
            | "mod_item"
            | "function_declaration"
            | "method_definition"
            | "class_declaration"
            | "lexical_declaration"
            | "function_definition"
            | "class_definition"
            | "type_declaration"
            | "method_declaration"
            | "interface_declaration"
    )
}

#[cfg(not(feature = "code-parsing"))]
pub fn parse_file_symbols(_path: &Path, _source: &str) -> Result<ParseSummary> {
    Ok(ParseSummary {
        language: "disabled".to_string(),
        named_node_count: 0,
        symbols: Vec::new(),
        note: Some(
            "tree-sitter parser is disabled. Build with `cargo run --features code-parsing -- parse <file>`"
                .to_string(),
        ),
    })
}
