import pytest
import os
import subprocess
from pathlib import Path

from no_human.review.reviewer import AdversarialReviewer
import eval.reviewer_recall.runner as rr

def test_gate_mode_uses_production_factory_and_backend(tmp_path, monkeypatch):
    """--mode gate constructs the reviewer through the production factory and honours the configured backend."""
    config_yaml = """
llm:
  role_backends:
    reviewer: custom-test-backend
"""
    nh_home = tmp_path / "home" / ".no_human"
    nh_home.mkdir(parents=True)
    (nh_home / "config.yaml").write_text(config_yaml)
    
    # Mock CONFIG_PATH where it's actually defined
    import no_human.config
    monkeypatch.setattr(no_human.config, "CONFIG_PATH", nh_home / "config.yaml")
    
    from no_human.review.reviewer import ReviewDecision, ChecklistItem

    calls = []
    from_config_calls = []
    
    original_from_config = AdversarialReviewer.from_config
    
    def mock_from_config(cls, data, **kw):
        from_config_calls.append(data)
        class MockReviewer:
            backend_name = "mocked"
            async def review(self, task, **kwargs):
                calls.append(kwargs)
                return ReviewDecision(passed=True, checklist=[ChecklistItem("a", True, "ok")])
        return MockReviewer()
        
    monkeypatch.setattr("no_human.review.reviewer.AdversarialReviewer.from_config", classmethod(mock_from_config))
    
    case = rr.CaseSpec(case_id="c1", dir=tmp_path/"c", base_ref="deadbeef", diff_text="diff", truth={"class": "logic"})
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo)
    
    gate_fn = rr._gate_reviewer_fn("model1")
    import asyncio
    asyncio.run(gate_fn(repo, "diff", case))
    
    assert len(from_config_calls) == 1
    assert len(calls) == 1
    assert "diff_override" not in calls[0]
    assert calls[0]["before_ref"] == "HEAD^"
    assert calls[0]["after_ref"] == "HEAD"

def test_gate_mode_receives_tools_and_evidence(tmp_path, monkeypatch):
    """Gate mode receives tools and evidence sections from the gate."""
    nh_home = tmp_path / "home" / ".no_human"
    nh_home.mkdir(parents=True)
    
    import no_human.config
    monkeypatch.setattr(no_human.config, "CONFIG_PATH", nh_home / "config.yaml")

    case = rr.CaseSpec(case_id="c1", dir=tmp_path/"c", base_ref="deadbeef", diff_text="diff", truth={"class": "logic"})
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo)
    (repo / "file.py").write_text("content")
    subprocess.run(["git", "add", "-A"], cwd=repo)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"], cwd=repo)
    
    agent_review_calls = []
    async def mock_agent_review(self, prompt, repo_path, **kwargs):
        agent_review_calls.append((prompt, kwargs))
        from no_human.review.reviewer import ReviewDecision, ChecklistItem
        return ReviewDecision(passed=True, checklist=[ChecklistItem("a", True, "ok")])
    
    # We monkeypatch the _agent_review so the LLM isn't called, but evidence IS gathered!
    monkeypatch.setattr("no_human.review.reviewer.AdversarialReviewer._agent_review", mock_agent_review)
    
    gate_fn = rr._gate_reviewer_fn("model1")
    import asyncio
    asyncio.run(gate_fn(repo, "diff", case))
    
    assert len(agent_review_calls) == 1
    prompt, kwargs = agent_review_calls[0]
    
    # Check for evidence sections (e.g. diff, lint, wiring)
    assert "Diff" in prompt or "diff" in prompt
    # Check tools were enabled by checking max_turns
    assert kwargs.get("max_turns", 0) > 1

