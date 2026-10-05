"""CLAUDE.md §2.1: src/harness must not import any agent framework (mini-swe-agent is
only a baseline, installed in the dev group)."""

import ast
from pathlib import Path

FORBIDDEN = {
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "llama_index",
    "crewai",
    "autogen",
    "agents",  # OpenAI Agents SDK
    "claude_agent_sdk",
    "minisweagent",
}
SRC = Path(__file__).resolve().parents[2] / "src" / "harness"


def imported_top_level(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_agent_framework_imports():
    bad = {
        str(p.relative_to(SRC)): sorted(imported_top_level(p) & FORBIDDEN)
        for p in SRC.rglob("*.py")
        if imported_top_level(p) & FORBIDDEN
    }
    assert not bad, bad
