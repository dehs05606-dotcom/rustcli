use crate::model_catalog::{default_model_for, lookup_model, normalize_provider, EffortMode, EffortProfile, ModelSpec};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{env, fs, path::PathBuf, str::FromStr};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub provider: String,
    pub model: String,
    pub effort: EffortMode,
    pub project_root: PathBuf,
    pub system_prompt_path: PathBuf,
    pub auto_apply: bool,
    pub max_tool_iterations: usize,
    pub context_scan_files: usize,
    pub max_output_tokens: Option<usize>,
    pub temperature: f32,
    pub request_timeout_secs: u64,
    pub openai: OpenAiSection,
    pub openrouter: OpenAiSection,
    pub opencode: OpenAiSection,
    pub nvidia: OpenAiSection,
    pub ollama: OllamaSection,
    pub anthropic: AnthropicSection,
    pub gemini: GeminiSection,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OpenAiSection {
    pub base_url: String,
    pub api_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OllamaSection {
    pub base_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnthropicSection {
    pub api_key: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GeminiSection {
    pub api_key: Option<String>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct PartialConfig {
    provider: Option<String>,
    model: Option<String>,
    effort: Option<EffortMode>,
    project_root: Option<PathBuf>,
    system_prompt_path: Option<PathBuf>,
    auto_apply: Option<bool>,
    max_tool_iterations: Option<usize>,
    context_scan_files: Option<usize>,
    max_output_tokens: Option<usize>,
    temperature: Option<f32>,
    request_timeout_secs: Option<u64>,
    openai: Option<PartialOpenAiSection>,
    openrouter: Option<PartialOpenAiSection>,
    opencode: Option<PartialOpenAiSection>,
    nvidia: Option<PartialOpenAiSection>,
    ollama: Option<PartialOllamaSection>,
    anthropic: Option<PartialApiKeySection>,
    gemini: Option<PartialApiKeySection>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct PartialOpenAiSection {
    base_url: Option<String>,
    api_key: Option<String>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct PartialOllamaSection {
    base_url: Option<String>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct PartialApiKeySection {
    api_key: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        let effort = env::var("AIA_EFFORT")
            .ok()
            .and_then(|v| EffortMode::from_str(&v).ok())
            .unwrap_or_default();
        let profile = EffortProfile::for_mode(effort);
        Self {
            provider: env::var("AIA_PROVIDER").unwrap_or_else(|_| "opencode".to_string()),
            model: env::var("AIA_MODEL").unwrap_or_else(|_| "deepseek-v4-flash-free".to_string()),
            effort,
            project_root: env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
            system_prompt_path: PathBuf::from("prompts/system_prompt.md"),
            auto_apply: parse_bool_env("AIA_AUTO_APPLY").unwrap_or(false),
            max_tool_iterations: env_usize("AIA_MAX_TOOL_ITERATIONS").unwrap_or(profile.max_tool_iterations),
            context_scan_files: env_usize("AIA_CONTEXT_SCAN_FILES").unwrap_or(profile.context_scan_files),
            max_output_tokens: env_usize("AIA_MAX_OUTPUT_TOKENS").or(Some(profile.default_output_tokens)),
            temperature: env::var("AIA_TEMPERATURE")
                .ok()
                .and_then(|v| v.parse::<f32>().ok())
                .unwrap_or(profile.temperature),
            request_timeout_secs: env_usize("AIA_REQUEST_TIMEOUT_SECS").unwrap_or(4000) as u64,
            openai: OpenAiSection {
                base_url: env::var("AIA_OPENAI_BASE_URL")
                    .unwrap_or_else(|_| "https://api.openai.com/v1".to_string()),
                api_key: env::var("OPENAI_API_KEY").ok(),
            },
            openrouter: OpenAiSection {
                base_url: env::var("AIA_OPENROUTER_BASE_URL")
                    .unwrap_or_else(|_| "https://openrouter.ai/api/v1".to_string()),
                api_key: env::var("OPENROUTER_API_KEY").ok(),
            },
            opencode: OpenAiSection {
                base_url: env::var("AIA_OPENCODE_BASE_URL")
                    .unwrap_or_else(|_| "https://opencode.ai/zen/v1/chat/completions".to_string()),
                api_key: env::var("OPENCODE_API_KEY")
                    .or_else(|_| env::var("AIA_OPENCODE_API_KEY"))
                    .ok()
                    .or_else(|| Some("sk-uM0oXXKJGFhyn3bk9kwvjF0RfZ2MSOfKMW5kyDrYXEGZnO1ZJT8BptOX6i6ry7Ue".to_string())),
            },
            nvidia: OpenAiSection {
                base_url: env::var("AIA_NVIDIA_BASE_URL")
                    .unwrap_or_else(|_| "https://integrate.api.nvidia.com/v1".to_string()),
                api_key: env::var("NVIDIA_API_KEY")
                    .or_else(|_| env::var("NVIDIA_NIM_API_KEY"))
                    .or_else(|_| env::var("NVAPI_KEY"))
                    .ok(),
            },
            ollama: OllamaSection {
                base_url: env::var("AIA_OLLAMA_BASE_URL")
                    .unwrap_or_else(|_| "http://localhost:11434".to_string()),
            },
            anthropic: AnthropicSection {
                api_key: env::var("ANTHROPIC_API_KEY").ok(),
            },
            gemini: GeminiSection {
                api_key: env::var("GEMINI_API_KEY").ok(),
            },
        }
    }
}

impl Config {
    pub fn load(path: Option<PathBuf>) -> Result<Self> {
        let mut cfg = Config::default();

        let config_path = path.or_else(Self::default_project_config).or_else(Self::home_config);

        if let Some(config_path) = config_path {
            if config_path.exists() {
                let raw = fs::read_to_string(&config_path)
                    .with_context(|| format!("failed to read {}", config_path.display()))?;
                let partial: PartialConfig = toml::from_str(&raw)
                    .with_context(|| format!("failed to parse {}", config_path.display()))?;
                cfg.merge(partial);
            }
        }

        cfg.apply_env_overrides();

        if !cfg.project_root.is_absolute() {
            cfg.project_root = env::current_dir()?.join(&cfg.project_root);
        }
        cfg.project_root = cfg
            .project_root
            .canonicalize()
            .unwrap_or_else(|_| cfg.project_root.clone());

        Ok(cfg)
    }

    fn merge(&mut self, partial: PartialConfig) {
        if let Some(v) = partial.provider {
            self.provider = v;
        }
        if let Some(v) = partial.model {
            self.model = v;
        }
        if let Some(v) = partial.effort {
            self.apply_effort(v);
        }
        if let Some(v) = partial.project_root {
            self.project_root = v;
        }
        if let Some(v) = partial.system_prompt_path {
            self.system_prompt_path = v;
        }
        if let Some(v) = partial.auto_apply {
            self.auto_apply = v;
        }
        if let Some(v) = partial.max_tool_iterations {
            self.max_tool_iterations = v;
        }
        if let Some(v) = partial.context_scan_files {
            self.context_scan_files = v;
        }
        if let Some(v) = partial.max_output_tokens {
            self.max_output_tokens = Some(v);
        }
        if let Some(v) = partial.temperature {
            self.temperature = v;
        }
        if let Some(v) = partial.request_timeout_secs {
            self.request_timeout_secs = v;
        }
        merge_openai(&mut self.openai, partial.openai);
        merge_openai(&mut self.openrouter, partial.openrouter);
        merge_openai(&mut self.opencode, partial.opencode);
        merge_openai(&mut self.nvidia, partial.nvidia);
        if let Some(v) = partial.ollama {
            if let Some(base_url) = v.base_url {
                self.ollama.base_url = base_url;
            }
        }
        if let Some(v) = partial.anthropic {
            if let Some(api_key) = v.api_key {
                self.anthropic.api_key = Some(api_key);
            }
        }
        if let Some(v) = partial.gemini {
            if let Some(api_key) = v.api_key {
                self.gemini.api_key = Some(api_key);
            }
        }
    }

    fn apply_env_overrides(&mut self) {
        if let Ok(v) = env::var("AIA_PROVIDER") {
            self.provider = v;
        }
        if let Ok(v) = env::var("AIA_MODEL") {
            self.model = v;
        }
        if let Ok(v) = env::var("AIA_EFFORT") {
            if let Ok(mode) = EffortMode::from_str(&v) {
                self.apply_effort(mode);
            }
        }
        if let Some(v) = parse_bool_env("AIA_AUTO_APPLY") {
            self.auto_apply = v;
        }
        if let Some(v) = env_usize("AIA_MAX_TOOL_ITERATIONS") {
            self.max_tool_iterations = v;
        }
        if let Some(v) = env_usize("AIA_CONTEXT_SCAN_FILES") {
            self.context_scan_files = v;
        }
        if let Some(v) = env_usize("AIA_MAX_OUTPUT_TOKENS") {
            self.max_output_tokens = Some(v);
        }
        if let Ok(v) = env::var("AIA_TEMPERATURE") {
            if let Ok(v) = v.parse::<f32>() {
                self.temperature = v;
            }
        }
        if let Some(v) = env_usize("AIA_REQUEST_TIMEOUT_SECS") {
            self.request_timeout_secs = v as u64;
        }
        if let Ok(v) = env::var("AIA_OPENAI_BASE_URL") {
            self.openai.base_url = v;
        }
        if let Ok(v) = env::var("AIA_OPENROUTER_BASE_URL") {
            self.openrouter.base_url = v;
        }
        if let Ok(v) = env::var("AIA_OPENCODE_BASE_URL") {
            self.opencode.base_url = v;
        }
        if let Ok(v) = env::var("AIA_NVIDIA_BASE_URL") {
            self.nvidia.base_url = v;
        }
        if let Ok(v) = env::var("AIA_OLLAMA_BASE_URL") {
            self.ollama.base_url = v;
        }
        if let Ok(v) = env::var("OPENAI_API_KEY") {
            self.openai.api_key = Some(v);
        }
        if let Ok(v) = env::var("OPENROUTER_API_KEY") {
            self.openrouter.api_key = Some(v);
        }
        if let Ok(v) = env::var("OPENCODE_API_KEY").or_else(|_| env::var("AIA_OPENCODE_API_KEY")) {
            self.opencode.api_key = Some(v);
        }
        if let Ok(v) = env::var("NVIDIA_API_KEY")
            .or_else(|_| env::var("NVIDIA_NIM_API_KEY"))
            .or_else(|_| env::var("NVAPI_KEY"))
        {
            self.nvidia.api_key = Some(v);
        }
        if let Ok(v) = env::var("ANTHROPIC_API_KEY") {
            self.anthropic.api_key = Some(v);
        }
        if let Ok(v) = env::var("GEMINI_API_KEY") {
            self.gemini.api_key = Some(v);
        }
    }

    pub fn apply_effort(&mut self, mode: EffortMode) {
        let profile = EffortProfile::for_mode(mode);
        self.effort = mode;
        self.temperature = profile.temperature;
        self.max_tool_iterations = profile.max_tool_iterations;
        self.context_scan_files = profile.context_scan_files;
        self.max_output_tokens = Some(profile.default_output_tokens);
    }

    pub fn apply_effort_with_recommended_model(&mut self, mode: EffortMode) {
        self.apply_effort(mode);
        if let Some(model) = default_model_for(&self.provider, mode) {
            self.model = model.to_string();
        }
    }

    pub fn effort_profile(&self) -> EffortProfile {
        EffortProfile::for_mode(self.effort)
    }

    pub fn model_spec(&self) -> Option<ModelSpec> {
        lookup_model(&normalize_provider(&self.provider), &self.model)
    }

    pub fn effective_output_tokens(&self) -> Option<usize> {
        self.max_output_tokens.or_else(|| {
            self.model_spec()
                .map(|spec| spec.recommended_output_tokens)
                .or(Some(self.effort_profile().default_output_tokens))
        })
    }

    pub fn context_window_tokens(&self) -> Option<usize> {
        self.model_spec().map(|spec| spec.max_context_tokens)
    }

    pub fn memory_db_path(&self) -> PathBuf {
        self.project_root.join(".aia/aia.sqlite")
    }

    pub fn load_system_prompt(&self) -> Result<String> {
        let candidates = [
            self.project_root.join(&self.system_prompt_path),
            env::current_dir()?.join(&self.system_prompt_path),
            PathBuf::from("prompts/system_prompt.md"),
        ];

        for path in candidates {
            if path.exists() {
                return fs::read_to_string(&path)
                    .with_context(|| format!("failed to read system prompt {}", path.display()));
            }
        }

        Ok(include_str!("../prompts/system_prompt.md").to_string())
    }

    pub fn runtime_summary(&self) -> String {
        let spec = self.model_spec();
        let context = spec
            .map(|s| format!("{} tokens", s.max_context_tokens))
            .unwrap_or_else(|| "unknown".to_string());
        format!(
            "provider={} model={} effort={} context={} output_tokens={:?} scan_files={} tool_iters={}",
            self.provider,
            self.model,
            self.effort,
            context,
            self.effective_output_tokens(),
            self.context_scan_files,
            self.max_tool_iterations
        )
    }

    fn default_project_config() -> Option<PathBuf> {
        let path = env::current_dir().ok()?.join(".aia/config.toml");
        path.exists().then_some(path)
    }

    fn home_config() -> Option<PathBuf> {
        let path = dirs::config_dir()?.join("aia/config.toml");
        path.exists().then_some(path)
    }
}

fn merge_openai(target: &mut OpenAiSection, incoming: Option<PartialOpenAiSection>) {
    if let Some(v) = incoming {
        if let Some(base_url) = v.base_url {
            target.base_url = base_url;
        }
        if let Some(api_key) = v.api_key {
            target.api_key = Some(api_key);
        }
    }
}

fn env_usize(key: &str) -> Option<usize> {
    env::var(key).ok().and_then(|v| v.parse::<usize>().ok())
}

fn parse_bool_env(key: &str) -> Option<bool> {
    env::var(key).ok().map(|v| {
        matches!(
            v.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on" | "y"
        )
    })
}
