use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use std::{fs, path::PathBuf};

#[derive(Debug, Clone)]
pub struct MemoryStore {
    db_path: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryNote {
    pub id: i64,
    pub kind: String,
    pub text: String,
    pub created_at: DateTime<Utc>,
}

impl MemoryStore {
    pub fn new(db_path: PathBuf) -> Result<Self> {
        if let Some(parent) = db_path.parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("failed to create {}", parent.display()))?;
        }
        let store = Self { db_path };
        store.migrate()?;
        Ok(store)
    }

    fn conn(&self) -> Result<Connection> {
        Connection::open(&self.db_path)
            .with_context(|| format!("failed to open memory DB {}", self.db_path.display()))
    }

    fn migrate(&self) -> Result<()> {
        let conn = self.conn()?;
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            "#,
        )?;
        Ok(())
    }

    pub fn add_note(&self, kind: impl AsRef<str>, text: impl AsRef<str>) -> Result<i64> {
        let now = Utc::now().to_rfc3339();
        let conn = self.conn()?;
        conn.execute(
            "INSERT INTO notes(kind, text, created_at) VALUES (?1, ?2, ?3)",
            params![kind.as_ref(), text.as_ref(), now],
        )?;
        Ok(conn.last_insert_rowid())
    }

    pub fn list_notes(&self, limit: usize) -> Result<Vec<MemoryNote>> {
        let conn = self.conn()?;
        let mut stmt = conn.prepare(
            "SELECT id, kind, text, created_at FROM notes ORDER BY id DESC LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit as i64], |row| {
            let created: String = row.get(3)?;
            let created_at = DateTime::parse_from_rfc3339(&created)
                .map(|d| d.with_timezone(&Utc))
                .unwrap_or_else(|_| Utc::now());
            Ok(MemoryNote {
                id: row.get(0)?,
                kind: row.get(1)?,
                text: row.get(2)?,
                created_at,
            })
        })?;

        let mut notes = Vec::new();
        for row in rows {
            notes.push(row?);
        }
        Ok(notes)
    }

    pub fn set_preference(&self, key: impl AsRef<str>, value: impl AsRef<str>) -> Result<()> {
        let conn = self.conn()?;
        let now = Utc::now().to_rfc3339();
        conn.execute(
            r#"
            INSERT INTO preferences(key, value, updated_at) VALUES (?1, ?2, ?3)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            "#,
            params![key.as_ref(), value.as_ref(), now],
        )?;
        Ok(())
    }

    pub fn list_preferences(&self) -> Result<Vec<(String, String)>> {
        let conn = self.conn()?;
        let mut stmt = conn.prepare("SELECT key, value FROM preferences ORDER BY key ASC")?;
        let rows = stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
        let mut prefs = Vec::new();
        for row in rows {
            prefs.push(row?);
        }
        Ok(prefs)
    }

    pub fn context_block(&self) -> Result<String> {
        let prefs = self.list_preferences()?;
        let notes = self.list_notes(12)?;
        let mut out = String::new();
        if !prefs.is_empty() {
            out.push_str("User preferences:\n");
            for (k, v) in prefs {
                out.push_str(&format!("- {k}: {v}\n"));
            }
        }
        if !notes.is_empty() {
            out.push_str("Recent memory notes:\n");
            for n in notes {
                out.push_str(&format!("- [{}] {}\n", n.kind, n.text));
            }
        }
        Ok(out)
    }
}
