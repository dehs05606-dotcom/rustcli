use super::{ChatMessage, LlmClient, Role, StreamSink};
use anyhow::{Context, Result};
use async_trait::async_trait;
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::time::Duration;

pub struct OllamaClient {
    base_url: String,
    model: String,
    http: reqwest::Client,
}

impl OllamaClient {
    pub fn new(base_url: impl Into<String>, model: impl Into<String>, timeout_secs: u64) -> Self {
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            model: model.into(),
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(timeout_secs))
                .build()
                .expect("valid reqwest client"),
        }
    }

    fn endpoint(&self) -> String {
        format!("{}/api/chat", self.base_url)
    }

    fn convert_messages(messages: &[ChatMessage]) -> Vec<Value> {
        messages
            .iter()
            .map(|m| {
                let role = match m.role {
                    Role::Tool => "user",
                    other => other.as_str(),
                };
                json!({"role": role, "content": m.content})
            })
            .collect()
    }
}

#[async_trait]
impl LlmClient for OllamaClient {
    fn name(&self) -> &str {
        "ollama"
    }

    async fn complete(&self, messages: &[ChatMessage]) -> Result<String> {
        let body = json!({
            "model": self.model,
            "messages": Self::convert_messages(messages),
            "stream": false,
        });

        let response = self
            .http
            .post(self.endpoint())
            .json(&body)
            .send()
            .await
            .context("failed to call Ollama. Is `ollama serve` running?")?;
        let status = response.status();
        let value: Value = response.json().await.context("invalid Ollama JSON response")?;
        if !status.is_success() {
            anyhow::bail!("Ollama error {}: {}", status, value);
        }

        Ok(value["message"]["content"]
            .as_str()
            .unwrap_or_default()
            .to_string())
    }

    async fn stream(&self, messages: &[ChatMessage], sink: &mut dyn StreamSink) -> Result<String> {
        let body = json!({
            "model": self.model,
            "messages": Self::convert_messages(messages),
            "stream": true,
        });

        let response = self
            .http
            .post(self.endpoint())
            .json(&body)
            .send()
            .await
            .context("failed to call Ollama streaming API. Is `ollama serve` running?")?;
        let status = response.status();
        if !status.is_success() {
            let text = response.text().await.unwrap_or_default();
            anyhow::bail!("Ollama error {}: {}", status, text);
        }

        let mut stream = response.bytes_stream();
        let mut pending = String::new();
        let mut full = String::new();

        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            pending.push_str(&String::from_utf8_lossy(&chunk));

            while let Some(newline) = pending.find('\n') {
                let line = pending[..newline].trim().to_string();
                pending = pending[newline + 1..].to_string();

                if line.is_empty() {
                    continue;
                }
                let value: Value = match serde_json::from_str(&line) {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                if let Some(delta) = value["message"]["content"].as_str() {
                    full.push_str(delta);
                    sink.on_delta(delta).await?;
                }
                if value["done"].as_bool().unwrap_or(false) {
                    return Ok(full);
                }
            }
        }

        Ok(full)
    }
}
