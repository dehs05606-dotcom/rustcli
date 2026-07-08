use crate::{config::Config, model_catalog::normalize_provider};
use anyhow::{bail, Result};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::io::{self, Write};

pub mod anthropic;
pub mod gemini;
pub mod ollama;
pub mod openai;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: Role,
    pub content: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

#[derive(Debug, Clone, Copy)]
pub struct LlmRequestConfig {
    pub temperature: f32,
    pub max_output_tokens: Option<usize>,
}

impl LlmRequestConfig {
    pub fn from_config(cfg: &Config) -> Self {
        Self {
            temperature: cfg.temperature,
            max_output_tokens: cfg.effective_output_tokens(),
        }
    }
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::System => "system",
            Role::User => "user",
            Role::Assistant => "assistant",
            Role::Tool => "tool",
        }
    }
}

impl ChatMessage {
    pub fn system(content: impl Into<String>) -> Self {
        Self {
            role: Role::System,
            content: content.into(),
        }
    }

    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: Role::User,
            content: content.into(),
        }
    }

    pub fn assistant(content: impl Into<String>) -> Self {
        Self {
            role: Role::Assistant,
            content: content.into(),
        }
    }
}

#[async_trait]
pub trait StreamSink: Send {
    async fn on_delta(&mut self, delta: &str) -> Result<()>;
}

pub struct StdoutSink;

#[async_trait]
impl StreamSink for StdoutSink {
    async fn on_delta(&mut self, delta: &str) -> Result<()> {
        print!("{delta}");
        io::stdout().flush()?;
        Ok(())
    }
}

pub struct BufferSink {
    pub buffer: String,
}

impl BufferSink {
    pub fn new() -> Self {
        Self {
            buffer: String::new(),
        }
    }
}

#[async_trait]
impl StreamSink for BufferSink {
    async fn on_delta(&mut self, delta: &str) -> Result<()> {
        self.buffer.push_str(delta);
        Ok(())
    }
}

#[async_trait]
pub trait LlmClient: Send + Sync {
    fn name(&self) -> &str;

    async fn complete(&self, messages: &[ChatMessage]) -> Result<String>;

    async fn stream(&self, messages: &[ChatMessage], sink: &mut dyn StreamSink) -> Result<String> {
        let text = self.complete(messages).await?;
        sink.on_delta(&text).await?;
        Ok(text)
    }
}

pub fn from_config(cfg: &Config) -> Result<Box<dyn LlmClient>> {
    let request = LlmRequestConfig::from_config(cfg);
    match normalize_provider(&cfg.provider).as_str() {
        "openai" => {
            let key = cfg
                .openai
                .api_key
                .clone()
                .ok_or_else(|| anyhow::anyhow!("OPENAI_API_KEY is required for provider=openai"))?;
            Ok(Box::new(openai::OpenAiCompatibleClient::new(
                "openai",
                cfg.openai.base_url.clone(),
                key,
                cfg.model.clone(),
                request,
                cfg.request_timeout_secs,
            )))
        }
        "openrouter" => {
            let key = cfg.openrouter.api_key.clone().ok_or_else(|| {
                anyhow::anyhow!("OPENROUTER_API_KEY is required for provider=openrouter")
            })?;
            Ok(Box::new(openai::OpenAiCompatibleClient::new(
                "openrouter",
                cfg.openrouter.base_url.clone(),
                key,
                cfg.model.clone(),
                request,
                cfg.request_timeout_secs,
            )))
        }
        "opencode" => {
            let key = cfg.opencode.api_key.clone().ok_or_else(|| {
                anyhow::anyhow!("OPENCODE_API_KEY is required for provider=opencode")
            })?;
            Ok(Box::new(openai::OpenAiCompatibleClient::new(
                "opencode",
                cfg.opencode.base_url.clone(),
                key,
                cfg.model.clone(),
                request,
                cfg.request_timeout_secs,
            )))
        }
        "nvidia" => {
            let key = cfg.nvidia.api_key.clone().ok_or_else(|| {
                anyhow::anyhow!(
                    "NVIDIA_API_KEY or NVIDIA_NIM_API_KEY is required for provider=nvidia"
                )
            })?;
            Ok(Box::new(openai::OpenAiCompatibleClient::new(
                "nvidia",
                cfg.nvidia.base_url.clone(),
                key,
                cfg.model.clone(),
                request,
                cfg.request_timeout_secs,
            )))
        }
        "ollama" => Ok(Box::new(ollama::OllamaClient::new(
            cfg.ollama.base_url.clone(),
            cfg.model.clone(),
            cfg.request_timeout_secs,
        ))),
        "anthropic" | "claude" => {
            let key = cfg.anthropic.api_key.clone().ok_or_else(|| {
                anyhow::anyhow!("ANTHROPIC_API_KEY is required for provider=anthropic")
            })?;
            Ok(Box::new(anthropic::AnthropicClient::new(
                key,
                cfg.model.clone(),
                cfg.request_timeout_secs,
            )))
        }
        "gemini" | "google" => {
            let key = cfg
                .gemini
                .api_key
                .clone()
                .ok_or_else(|| anyhow::anyhow!("GEMINI_API_KEY is required for provider=gemini"))?;
            Ok(Box::new(gemini::GeminiClient::new(
                key,
                cfg.model.clone(),
                cfg.request_timeout_secs,
            )))
        }
        other => bail!(
            "unknown provider `{}`. Use ollama, openai, openrouter, opencode, nvidia, anthropic, or gemini",
            other
        ),
    }
}
