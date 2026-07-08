use super::{ChatMessage, LlmClient, Role};
use anyhow::{Context, Result};
use async_trait::async_trait;
use reqwest::header::{HeaderMap, HeaderValue, CONTENT_TYPE};
use serde_json::{json, Value};
use std::time::Duration;

pub struct AnthropicClient {
    api_key: String,
    model: String,
    http: reqwest::Client,
}

impl AnthropicClient {
    pub fn new(api_key: impl Into<String>, model: impl Into<String>, timeout_secs: u64) -> Self {
        Self {
            api_key: api_key.into(),
            model: model.into(),
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(timeout_secs))
                .build()
                .expect("valid reqwest client"),
        }
    }

    fn headers(&self) -> Result<HeaderMap> {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        headers.insert("x-api-key", HeaderValue::from_str(&self.api_key)?);
        headers.insert("anthropic-version", HeaderValue::from_static("2023-06-01"));
        Ok(headers)
    }

    fn convert_messages(messages: &[ChatMessage]) -> (String, Vec<Value>) {
        let mut system = Vec::new();
        let mut out = Vec::new();
        for m in messages {
            match m.role {
                Role::System => system.push(m.content.clone()),
                Role::User | Role::Tool => out.push(json!({"role":"user", "content": m.content})),
                Role::Assistant => out.push(json!({"role":"assistant", "content": m.content})),
            }
        }
        (system.join("\n\n"), out)
    }
}

#[async_trait]
impl LlmClient for AnthropicClient {
    fn name(&self) -> &str {
        "anthropic"
    }

    async fn complete(&self, messages: &[ChatMessage]) -> Result<String> {
        let (system, converted) = Self::convert_messages(messages);
        let body = json!({
            "model": self.model,
            "max_tokens": 200000,
            "temperature": 0.2,
            "system": system,
            "messages": converted,
        });

        let response = self
            .http
            .post("https://api.anthropic.com/v1/messages")
            .headers(self.headers()?)
            .json(&body)
            .send()
            .await
            .context("failed to call Anthropic API")?;
        let status = response.status();
        let value: Value = response.json().await.context("invalid Anthropic JSON")?;
        if !status.is_success() {
            anyhow::bail!("Anthropic error {}: {}", status, value);
        }

        let text = value["content"]
            .as_array()
            .and_then(|arr| arr.iter().find_map(|p| p["text"].as_str()))
            .unwrap_or_default()
            .to_string();
        Ok(text)
    }
}
