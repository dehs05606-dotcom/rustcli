mod agent;
mod cli;
mod config;
mod llm;
mod mcp;
mod memory;
mod model_catalog;
mod terminal;
mod tools;
mod tui;

use clap::Parser;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("aia_agent=info,warn")),
        )
        .init();

    let cli = cli::Cli::parse();
    cli.run().await
}
