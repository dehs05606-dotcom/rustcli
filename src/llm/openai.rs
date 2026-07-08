use super::{ChatMessage, LlmClient, LlmRequestConfig, Role, StreamSink};
use anyhow::{Context, Result};
use async_trait::async_trait;
use futures_util::StreamExt;
use reqwest::header::{HeaderMap, HeaderValue, ACCEPT, AUTHORIZATION, CONTENT_TYPE};
use serde_json::{json, Value};
use std::time::Duration;

pub struct OpenAiCompatibleClient {
    name: String,
    endpoint: String,
    api_key: String,
    model: String,
    request: LlmRequestConfig,
    http: reqwest::Client,
}

impl OpenAiCompatibleClient {
    pub fn new(
        name: impl Into<String>,
        base_url_or_endpoint: impl Into<String>,
        api_key: impl Into<String>,
        model: impl Into<String>,
        request: LlmRequestConfig,
        timeout_secs: u64,
    ) -> Self {
        Self {
            name: name.into(),
            endpoint: normalize_chat_completions_endpoint(&base_url_or_endpoint.into()),
            api_key: api_key.into(),
            model: model.into(),
            request,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(timeout_secs))
                .build()
                .expect("valid reqwest client"),
        }
    }

    fn headers(&self) -> Result<HeaderMap> {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
        headers.insert(
            AUTHORIZATION,
            HeaderValue::from_str(&format!("Bearer {}", self.api_key))?,
        );
        match self.name.as_str() {
            "openrouter" => {
                headers.insert("HTTP-Referer", HeaderValue::from_static("https://arena.ai"));
                headers.insert("X-Title", HeaderValue::from_static("AIA Agent"));
            }
            "opencode" => {
                headers.insert("X-Title", HeaderValue::from_static("AIA Agent Zen"));
            }
            "nvidia" => {
                headers.insert("User-Agent", HeaderValue::from_static("aia-agent-rust/0.2"));
            }
            _ => {}
        }
        Ok(headers)
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

    fn request_body(&self, messages: &[ChatMessage], stream: bool) -> Value {
        let mut body = json!({
            "model": self.model,
            "messages": Self::convert_messages(messages),
            "temperature": self.request.temperature,
            "stream": stream,
        });
        if let Some(max_tokens) = self.request.max_output_tokens {
            body["max_tokens"] = json!(max_tokens);
        }
        body
    }

}

#[async_trait]
impl LlmClient for OpenAiCompatibleClient {
    fn name(&self) -> &str {
        &self.name
    }

    async fn complete(&self, messages: &[ChatMessage]) -> Result<String> {
        let body = self.request_body(messages, false);

        let response = self
            .http
            .post(&self.endpoint)
            .headers(self.headers()?)
            .json(&body)
            .send()
            .await
            .with_context(|| format!("failed to call {} API at {}", self.name, self.endpoint))?;

        let status = response.status();
        let text = response.text().await.context("failed to read API response")?;
        if !status.is_success() {
            anyhow::bail!("{} API error {}: {}", self.name, status, text);
        }
        let value: Value = serde_json::from_str(&text).context("invalid API JSON response")?;
        Ok(extract_completion_text(&value))
    }

    async fn stream(&self, messages: &[ChatMessage], sink: &mut dyn StreamSink) -> Result<String> {
        let body = self.request_body(messages, true);

        let response = self
            .http
            .post(&self.endpoint)
            .headers(self.headers()?)
            .json(&body)
            .send()
            .await
            .with_context(|| {
                format!(
                    "failed to call {} streaming API at {}",
                    self.name, self.endpoint
                )
            })?;

        let status = response.status();
        if !status.is_success() {
            let text = response.text().await.unwrap_or_default();
            anyhow::bail!("{} API error {}: {}", self.name, status, text);
        }

        let mut stream = response.bytes_stream();
        let mut pending = String::new();
        let mut full = String::new();
        let mut done = false;

        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            pending.push_str(&String::from_utf8_lossy(&chunk));

            while let Some(newline) = pending.find('\n') {
                let line = pending[..newline].trim().to_string();
                pending = pending[newline + 1..].to_string();

                if line.is_empty() || line.starts_with(':') {
                    continue;
                }

                let data = line.strip_prefix("data:").map(str::trim).unwrap_or(line.as_str());
                if data == "[DONE]" {
                    done = true;
                    break;
                }
                if data.is_empty() {
                    continue;
                }

                let value: Value = match serde_json::from_str(data) {
                    Ok(v) => v,
                    Err(_) => continue,
                };

                if let Some(delta) = extract_stream_delta(&value) {
                    full.push_str(&delta);
                    sink.on_delta(&delta).await?;
                }
            }

            if done {
                break;
            }
        }

        Ok(full)
    }
}

fn normalize_chat_completions_endpoint(base_url_or_endpoint: &str) -> String {
    let trimmed = base_url_or_endpoint.trim().trim_end_matches('/');
    if trimmed.ends_with("/chat/completions") {
        trimmed.to_string()
    } else {
        format!("{trimmed}/chat/completions")
    }
}

fn extract_completion_text(value: &Value) -> String {
    if let Some(text) = value["choices"][0]["message"]["content"].as_str() {
        return text.to_string();
    }
    if let Some(text) = value["choices"][0]["text"].as_str() {
        return text.to_string();
    }
    if let Some(text) = value["output_text"].as_str() {
        return text.to_string();
    }
    value.to_string()
}

fn extract_stream_delta(value: &Value) -> Option<String> {
    let delta = &value["choices"][0]["delta"];
    if let Some(text) = delta["content"].as_str() {
        return Some(text.to_string());
    }
    if let Some(text) = delta["reasoning_content"].as_str() {
        return Some(text.to_string());
    }
    if let Some(text) = value["choices"][0]["message"]["content"].as_str() {
        return Some(text.to_string());
    }
    if let Some(text) = value["choices"][0]["text"].as_str() {
        return Some(text.to_string());
    }
    None
}
