use super::{ChatMessage, LlmClient, Role};
use anyhow::{Context, Result};
use async_trait::async_trait;
use serde_json::{json, Value};
use std::time::Duration;

pub struct GeminiClient {
    api_key: String,
    model: String,
    http: reqwest::Client,
}

impl GeminiClient {
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

    fn endpoint(&self) -> String {
        format!(
            "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}",
            self.model, self.api_key
        )
    }

    fn convert_messages(messages: &[ChatMessage]) -> (String, Vec<Value>) {
        let mut system = Vec::new();
        let mut contents = Vec::new();
        for m in messages {
            match m.role {
                Role::System => system.push(m.content.clone()),
                Role::User | Role::Tool => {
                    contents.push(json!({"role":"user", "parts":[{"text": m.content}]}));
                }
                Role::Assistant => {
                    contents.push(json!({"role":"model", "parts":[{"text": m.content}]}));
                }
            }
        }
        (system.join("\n\n"), contents)
    }
}

#[async_trait]
impl LlmClient for GeminiClient {
    fn name(&self) -> &str {
        "gemini"
    }

    async fn complete(&self, messages: &[ChatMessage]) -> Result<String> {
        let (system, contents) = Self::convert_messages(messages);
        let mut body = json!({
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 200000
            }
        });
        if !system.is_empty() {
            body["systemInstruction"] = json!({"parts":[{"text": system}]});
        }

        let response = self
            .http
            .post(self.endpoint())
            .json(&body)
            .send()
            .await
            .context("failed to call Gemini API")?;
        let status = response.status();
        let value: Value = response.json().await.context("invalid Gemini JSON")?;
        if !status.is_success() {
            anyhow::bail!("Gemini error {}: {}", status, value);
        }

        let text = value["candidates"][0]["content"]["parts"]
            .as_array()
            .and_then(|arr| arr.iter().find_map(|p| p["text"].as_str()))
            .unwrap_or_default()
            .to_string();
        Ok(text)
    }
}
