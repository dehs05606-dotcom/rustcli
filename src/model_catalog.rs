use serde::{Deserialize, Serialize};
use std::{fmt, str::FromStr};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, clap::ValueEnum)]
#[serde(rename_all = "kebab-case")]
pub enum EffortMode {
    /// Lowest latency: smaller context scan, fewer tool loops, low output budget.
    Fast,
    /// Good default: enough context, safe tool loop, medium output budget.
    Balanced,
    /// Heavy reasoning and larger context budget for complex refactors.
    Deep,
    /// Maximum context/patience profile for huge codebases and audits.
    Max,
}

impl Default for EffortMode {
    fn default() -> Self {
        Self::Balanced
    }
}

impl fmt::Display for EffortMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = match self {
            EffortMode::Fast => "fast",
            EffortMode::Balanced => "balanced",
            EffortMode::Deep => "deep",
            EffortMode::Max => "max",
        };
        f.write_str(text)
    }
}

impl FromStr for EffortMode {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.trim().to_ascii_lowercase().as_str() {
            "fast" | "quick" | "turbo" | "flash" => Ok(Self::Fast),
            "balanced" | "normal" | "auto" | "default" => Ok(Self::Balanced),
            "deep" | "reason" | "reasoning" | "think" => Ok(Self::Deep),
            "max" | "maximum" | "ultra" | "million" | "1m" => Ok(Self::Max),
            other => Err(format!(
                "unknown effort mode `{other}`. Use fast, balanced, deep, or max"
            )),
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct EffortProfile {
    pub mode: EffortMode,
    pub label: &'static str,
    pub temperature: f32,
    pub max_tool_iterations: usize,
    pub context_scan_files: usize,
    pub default_output_tokens: usize,
    pub command_latency_bias: &'static str,
}

impl EffortProfile {
    pub fn for_mode(mode: EffortMode) -> Self {
        match mode {
            EffortMode::Fast => Self {
                mode,
                label: "FAST / flash latency",
                temperature: 0.12,
                max_tool_iterations: 3,
                context_scan_files: 120,
                default_output_tokens: 200000,
                command_latency_bias: "Prefer direct answers, no long plans unless required.",
            },
            EffortMode::Balanced => Self {
                mode,
                label: "BALANCED / coding default",
                temperature: 0.20,
                max_tool_iterations: 8,
                context_scan_files: 300,
                default_output_tokens: 200000,
                command_latency_bias: "Balance correctness and speed.",
            },
            EffortMode::Deep => Self {
                mode,
                label: "DEEP / complex engineering",
                temperature: 0.18,
                max_tool_iterations: 14,
                context_scan_files: 900,
                default_output_tokens: 200000,
                command_latency_bias: "Spend more time on repo inspection, tests, and review.",
            },
            EffortMode::Max => Self {
                mode,
                label: "MAX / huge context",
                temperature: 0.16,
                max_tool_iterations: 24,
                context_scan_files: 2_000,
                default_output_tokens: 200000,
                command_latency_bias: "Use maximum context budget and multi-agent verification.",
            },
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct ModelSpec {
    pub provider: &'static str,
    pub model: &'static str,
    pub max_context_tokens: usize,
    pub recommended_output_tokens: usize,
    pub best_effort: EffortMode,
    pub notes: &'static str,
}

pub const MODEL_CATALOG: &[ModelSpec] = &[
    ModelSpec {
        provider: "opencode",
        model: "deepseek-v4-flash-free",
        max_context_tokens: 200_000,
        recommended_output_tokens: 200_000,
        best_effort: EffortMode::Fast,
        notes: "Fast free DeepSeek-style flash model through OpenCode Zen endpoint.",
    },
    ModelSpec {
        provider: "opencode",
        model: "mimo-v2.5-free",
        max_context_tokens: 200_000,
        recommended_output_tokens: 200_000,
        best_effort: EffortMode::Max,
        notes: "Large context free model for huge repo context windows.",
    },
    ModelSpec {
        provider: "opencode",
        model: "big-pickle",
        max_context_tokens: 200_000,
        recommended_output_tokens: 200_000,
        best_effort: EffortMode::Deep,
        notes: "High-capacity coding/reasoning profile through OpenCode Zen endpoint.",
    },
    ModelSpec {
        provider: "nvidia",
        model: "z-ai/glm-5.2",
        max_context_tokens: 200_000,
        recommended_output_tokens: 200_000,
        best_effort: EffortMode::Max,
        notes: "NVIDIA NIM/OpenAI-compatible model with very large context.",
    },
    ModelSpec {
        provider: "nvidia",
        model: "z-ai/glm-5.1",
        max_context_tokens: 200_000,
        recommended_output_tokens: 200_000,
        best_effort: EffortMode::Max,
        notes: "NVIDIA NIM/OpenAI-compatible large context GLM model.",
    },
    ModelSpec {
        provider: "nvidia",
        model: "deepseek-ai/deepseek-v4-pro",
        max_context_tokens: 200_000,
        recommended_output_tokens: 200_000,
        best_effort: EffortMode::Deep,
        notes: "NVIDIA-hosted DeepSeek Pro model for heavy coding tasks.",
    },
    ModelSpec {
        provider: "nvidia",
        model: "stepfun-ai/step-3.7-flash",
        max_context_tokens: 200_000,
        recommended_output_tokens: 200_000,
        best_effort: EffortMode::Fast,
        notes: "Fast flash model for low-latency agent replies.",
    },
];

pub fn lookup_model(provider: &str, model: &str) -> Option<ModelSpec> {
    MODEL_CATALOG
        .iter()
        .copied()
        .find(|spec| spec.provider.eq_ignore_ascii_case(provider) && spec.model.eq_ignore_ascii_case(model))
}

pub fn provider_models(provider: &str) -> Vec<ModelSpec> {
    MODEL_CATALOG
        .iter()
        .copied()
        .filter(|spec| spec.provider.eq_ignore_ascii_case(provider))
        .collect()
}

pub fn default_model_for(provider: &str, effort: EffortMode) -> Option<&'static str> {
    let provider = normalize_provider(provider);
    match (provider.as_str(), effort) {
        ("opencode", EffortMode::Fast) => Some("deepseek-v4-flash-free"),
        ("opencode", EffortMode::Balanced) => Some("big-pickle"),
        ("opencode", EffortMode::Deep) => Some("big-pickle"),
        ("opencode", EffortMode::Max) => Some("mimo-v2.5-free"),
        ("nvidia", EffortMode::Fast) => Some("stepfun-ai/step-3.7-flash"),
        ("nvidia", EffortMode::Balanced) => Some("deepseek-ai/deepseek-v4-pro"),
        ("nvidia", EffortMode::Deep) => Some("deepseek-ai/deepseek-v4-pro"),
        ("nvidia", EffortMode::Max) => Some("z-ai/glm-5.2"),
        _ => None,
    }
}

pub fn normalize_provider(provider: &str) -> String {
    match provider.trim().to_ascii_lowercase().as_str() {
        "zen" | "opencode-zen" | "opencode.ai" => "opencode".to_string(),
        "nim" | "nvidia-nim" | "integrate-nvidia" => "nvidia".to_string(),
        other => other.to_string(),
    }
}

pub fn render_catalog_markdown() -> String {
    let mut out = String::new();
    out.push_str("# AIA Model Catalog\n\n");
    out.push_str("| Provider | Model | Max context | Best effort | Notes |\n");
    out.push_str("|---|---:|---:|---|---|\n");
    for spec in MODEL_CATALOG {
        out.push_str(&format!(
            "| {} | `{}` | {} | {} | {} |\n",
            spec.provider,
            spec.model,
            format_tokens(spec.max_context_tokens),
            spec.best_effort,
            spec.notes
        ));
    }
    out
}

pub fn format_tokens(tokens: usize) -> String {
    if tokens >= 1_000_000 {
        format!("{}M", tokens / 1_000_000)
    } else if tokens >= 1_000 {
        format!("{}K", tokens / 1_000)
    } else {
        tokens.to_string()
    }
}
