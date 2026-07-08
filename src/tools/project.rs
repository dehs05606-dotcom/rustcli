use anyhow::Result;
use ignore::WalkBuilder;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, fs, path::{Path, PathBuf}};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectSummary {
    pub root: String,
    pub file_count: usize,
    pub language_counts: BTreeMap<String, usize>,
    pub tree: String,
    pub important_files: Vec<ImportantFile>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportantFile {
    pub path: String,
    pub content: String,
    pub truncated: bool,
}

#[derive(Debug, Clone)]
pub struct ProjectScanner {
    root: PathBuf,
}

impl ProjectScanner {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn scan(&self, max_files: usize) -> Result<ProjectSummary> {
        let files = self.collect_files(max_files)?;
        let mut language_counts = BTreeMap::new();
        for path in &files {
            let lang = language_for_path(path);
            *language_counts.entry(lang).or_insert(0) += 1;
        }

        Ok(ProjectSummary {
            root: self.root.display().to_string(),
            file_count: files.len(),
            language_counts,
            tree: self.render_tree_from_files(&files),
            important_files: self.read_important_files()?,
        })
    }

    pub fn brief_context(&self) -> Result<String> {
        self.brief_context_with_limit(250)
    }

    pub fn brief_context_with_limit(&self, max_files: usize) -> Result<String> {
        let summary = self.scan(max_files)?;
        let mut out = String::new();
        out.push_str(&format!("Project root: {}\n", summary.root));
        out.push_str(&format!("Files scanned: {}\n", summary.file_count));
        out.push_str("Language/file counts:\n");
        for (lang, count) in &summary.language_counts {
            out.push_str(&format!("- {lang}: {count}\n"));
        }
        out.push_str("\nFile tree:\n");
        out.push_str(&summary.tree);
        if !summary.important_files.is_empty() {
            out.push_str("\nImportant files:\n");
            for f in summary.important_files {
                out.push_str(&format!("\n--- {}{} ---\n", f.path, if f.truncated { " (truncated)" } else { "" }));
                out.push_str(&f.content);
                if !f.content.ends_with('\n') {
                    out.push('\n');
                }
            }
        }
        Ok(out)
    }

    pub fn tree(&self, max_files: usize) -> Result<String> {
        let files = self.collect_files(max_files)?;
        Ok(self.render_tree_from_files(&files))
    }

    fn collect_files(&self, max_files: usize) -> Result<Vec<PathBuf>> {
        let walker = WalkBuilder::new(&self.root)
            .hidden(false)
            .git_ignore(true)
            .git_exclude(true)
            .parents(true)
            .filter_entry(|e| !is_ignored_dir(e.path()))
            .build();
        let mut files = Vec::new();
        for entry in walker {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };
            if entry.file_type().map(|ft| ft.is_file()).unwrap_or(false) {
                let path = entry.path().strip_prefix(&self.root).unwrap_or(entry.path()).to_path_buf();
                files.push(path);
                if files.len() >= max_files {
                    break;
                }
            }
        }
        files.sort();
        Ok(files)
    }

    fn render_tree_from_files(&self, files: &[PathBuf]) -> String {
        let mut out = String::new();
        for path in files {
            let depth = path.components().count().saturating_sub(1);
            let indent = "  ".repeat(depth);
            out.push_str(&format!("{}- {}\n", indent, path.display()));
        }
        out
    }

    fn read_important_files(&self) -> Result<Vec<ImportantFile>> {
        let names = [
            "Cargo.toml",
            "package.json",
            "pyproject.toml",
            "requirements.txt",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "README.md",
            ".aia/config.toml",
        ];
        let mut files = Vec::new();
        for name in names {
            let path = self.root.join(name);
            if !path.exists() || !path.is_file() {
                continue;
            }
            let bytes = fs::read(&path)?;
            let max = 16_000;
            let truncated = bytes.len() > max;
            let slice = if truncated { &bytes[..max] } else { &bytes[..] };
            files.push(ImportantFile {
                path: name.to_string(),
                content: String::from_utf8_lossy(slice).to_string(),
                truncated,
            });
        }
        Ok(files)
    }
}

fn language_for_path(path: &Path) -> String {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("") {
        "rs" => "Rust",
        "js" | "jsx" | "mjs" | "cjs" => "JavaScript",
        "ts" | "tsx" => "TypeScript",
        "py" => "Python",
        "go" => "Go",
        "java" => "Java",
        "kt" | "kts" => "Kotlin",
        "c" | "h" => "C",
        "cpp" | "cc" | "hpp" | "cxx" => "C++",
        "cs" => "C#",
        "php" => "PHP",
        "rb" => "Ruby",
        "swift" => "Swift",
        "md" => "Markdown",
        "toml" => "TOML",
        "json" => "JSON",
        "yaml" | "yml" => "YAML",
        "html" => "HTML",
        "css" => "CSS",
        "sql" => "SQL",
        other if other.is_empty() => "Other",
        other => other,
    }
    .to_string()
}

fn is_ignored_dir(path: &Path) -> bool {
    let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
    matches!(
        name,
        ".git" | "target" | "node_modules" | "dist" | "build" | ".next" | ".cache" | ".venv" | "coverage"
    )
}
