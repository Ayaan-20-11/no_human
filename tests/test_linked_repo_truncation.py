import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.core.task import Task
from no_human.review.diff_coverage import COVERAGE_REJECTION_PREFIX
from no_human.review.reviewer import AdversarialReviewer, ReviewerUnavailable


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

def _passing_block() -> str:
    return (
        "REVIEW_JSON_START\n"
        '{"passed": true, "items": [{"label": "ok", "passed": true, '
        '"severity": "low", "evidence": "covered"}]}\n'
        "REVIEW_JSON_END\n"
    )

class FakeBackendNoInspect:
    def __init__(self):
        self.calls = []
    
    async def run(self, prompt, *, cwd, max_turns, **kwargs):
        self.calls.append(prompt)
        return AgentResult(
            final_text=_passing_block(),
            num_turns=1,
            is_error=False,
            tokens_used=10,
            session_id="f",
            stop_reason="end_turn",
        )

@pytest.mark.asyncio
async def test_linked_repo_truncated_diff_routes_through_reviewer_unavailable(tmp_path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    
    for r in (primary, linked):
        r.mkdir()
        _git(r, "init", "-q")
        _git(r, "config", "user.email", "t@t.t")
        _git(r, "config", "user.name", "t")
        (r / "base.py").write_text("print('base')")
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "base")
    
    # Create a huge change in linked to exceed _DIFF_CAP (60_000)
    huge = "a" * 70_000
    (linked / "huge.py").write_text(huge)
    _git(linked, "add", "-A")
    _git(linked, "commit", "-qm", "change")
    
    reviewer = AdversarialReviewer(backend=FakeBackendNoInspect(), timeout=1)
    t = Task.new("multi-repo change", repo_path=str(primary))
    t.linked_repos = [str(linked)]
    
    with pytest.raises(ReviewerUnavailable) as exc:
        await reviewer.review(
            t, repo_path=primary, before_ref="HEAD",
            linked_repos=[(linked, "HEAD~1")],
        )
    
    msg = str(exc.value)
    assert COVERAGE_REJECTION_PREFIX in msg
    assert str(linked) in msg
    assert "huge.py" in msg

