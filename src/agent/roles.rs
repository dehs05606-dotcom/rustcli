#[derive(Debug, Clone, Copy)]
pub enum AgentRole {
    Planner,
    Coder,
    Reviewer,
    Tester,
    SecurityAuditor,
}

impl AgentRole {
    pub fn name(self) -> &'static str {
        match self {
            AgentRole::Planner => "Planner",
            AgentRole::Coder => "Coder",
            AgentRole::Reviewer => "Reviewer",
            AgentRole::Tester => "Tester",
            AgentRole::SecurityAuditor => "Security Auditor",
        }
    }

    pub fn prompt(self) -> &'static str {
        match self {
            AgentRole::Planner => {
                "You are the Planner agent. Break the task into safe, minimal steps. Identify files/tools needed. Do not edit code."
            }
            AgentRole::Coder => {
                "You are the Coder agent. Implement the smallest correct change. Prefer diffs and maintain existing style."
            }
            AgentRole::Reviewer => {
                "You are the Reviewer agent. Critique the proposed change for correctness, maintainability, and edge cases."
            }
            AgentRole::Tester => {
                "You are the Tester agent. Identify and run the most relevant build/test/format checks where appropriate."
            }
            AgentRole::SecurityAuditor => {
                "You are the Security Auditor agent. Look for unsafe commands, secret exposure, injection, path traversal, and data loss."
            }
        }
    }
}
