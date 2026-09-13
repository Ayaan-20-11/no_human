"""The commit-time half of the manifest-pin invariant.

`RELEASE_MANIFEST.txt` pins every shipped file with a sha256 and is also the
approval ledger the export gate enforces. The recurring defect is a split
commit: a pinned file's bytes change but its manifest row is left on the old
hash, so the two halves of one change land apart. CI's inventory job catches it,
but only on the public tree at push time -- by then main is red. This gate moves
the same check to `git commit`.

These tests drive REAL git against REAL temp repos, and the end-to-end ones run
the ACTUAL committed hook through `git commit`. A mocked git would assume away
the very thing under test: what git stages, and what it does with a pre-commit
hook. The gate reads the INDEX (`git cat-file blob :<path>`), not the working
tree, so the tests stage deliberately to exercise that.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE_SRC = REPO / "scripts" / "precommit_manifest_gate.py"
CRM_SRC = REPO / "scripts" / "check_release_manifest.py"
HOOK_SRC = REPO / "scripts" / "hooks" / "pre-commit"
INSTALL_SRC = REPO / "scripts" / "hooks" / "install.sh"

MANIFEST = "RELEASE_MANIFEST.txt"
HEADER = "# RELEASE_MANIFEST.txt\n"
CLASSIFICATION = "EXPORT_CLASSIFICATION.txt"

WRITE_CMD = "python scripts/check_release_manifest.py --write"
APPROVE_CMD = "export_guard.py approve"


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_manifest(root: Path, pins: dict[str, str]) -> None:
    body = "".join(f"{d}  {p}\n" for p, d in sorted(pins.items()))
    (root / MANIFEST).write_text(HEADER + body, encoding="utf-8")


def _classify(root: Path) -> None:
    """Add `EXPORT_CLASSIFICATION.txt` at the repo root, committed on its own —
    `remedy_command` only checks existence, but a separate commit keeps the
    offending edit's staged-paths string exactly `shipped.txt`, matching the
    without-classification branch's assertions byte-for-byte apart from the
    remedy line."""
    (root / CLASSIFICATION).write_text(
        "ship  1  shipped.txt\n"
        "ship  1  RELEASE_MANIFEST.txt\n"
        f"drop  1  {CLASSIFICATION}\n",
        encoding="utf-8")
    _git(root, "add", CLASSIFICATION)
    _git(root, "commit", "-qm", "add classification")


@pytest.fixture
def repo(tmp_path):
    """A git repo carrying the two scripts the gate needs, plus a pinned file
    `shipped.txt` already committed and matching its manifest row."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(GATE_SRC, root / "scripts" / "precommit_manifest_gate.py")
    shutil.copy(CRM_SRC, root / "scripts" / "check_release_manifest.py")

    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    body = b"shipped v1\n"
    (root / "shipped.txt").write_bytes(body)
    _write_manifest(root, {"shipped.txt": _sha(body)})
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _run_gate(root: Path):
    """Invoke the gate script directly (as the hook would), returning the proc."""
    return subprocess.run(
        ["python3", str(root / "scripts" / "precommit_manifest_gate.py")],
        cwd=str(root), capture_output=True, text=True)


# --------------------------------------------------------------------------
# The gate script itself (index-driven), independent of hook installation.
# --------------------------------------------------------------------------

def test_changed_pin_not_repinned_is_blocked_without_classification(repo):
    """No `EXPORT_CLASSIFICATION.txt` beside the tree: `export_guard.py` and the
    classification are `drop`-classified and do not ship, so the only remedy
    that exists in a tree shaped like this one is `--write`."""
    (repo / "shipped.txt").write_bytes(b"shipped v2\n")
    _git(repo, "add", "shipped.txt")          # file staged, manifest NOT re-pinned
    proc = _run_gate(repo)
    assert proc.returncode == 1, proc.stderr
    assert "shipped.txt" in proc.stderr
    assert WRITE_CMD in proc.stderr
    assert APPROVE_CMD not in proc.stderr


def test_changed_pin_not_repinned_is_blocked_with_classification(repo):
    """`EXPORT_CLASSIFICATION.txt` present: this is the private tree shape, where
    `--write` would refuse (it never forges an approval) and `approve` is the
    one write path, so the remedy must name it instead."""
    _classify(repo)
    (repo / "shipped.txt").write_bytes(b"shipped v2\n")
    _git(repo, "add", "shipped.txt")          # file staged, manifest NOT re-pinned
    proc = _run_gate(repo)
    assert proc.returncode == 1, proc.stderr
    assert "shipped.txt" in proc.stderr
    assert "uv run python scripts/export_guard.py approve shipped.txt" in proc.stderr
    assert WRITE_CMD not in proc.stderr


def test_changed_pin_repinned_in_same_commit_passes(repo):
    body = b"shipped v2\n"
    (repo / "shipped.txt").write_bytes(body)
    _write_manifest(repo, {"shipped.txt": _sha(body)})   # re-pinned...
    _git(repo, "add", "shipped.txt", MANIFEST)           # ...and staged together
    proc = _run_gate(repo)
    assert proc.returncode == 0, proc.stderr


def test_repin_written_but_manifest_not_staged_is_blocked(repo):
    """`export_guard approve` writes the pin to the WORKING TREE only. Forgetting
    the `git add` of the manifest is the exact split this gate exists to catch."""
    body = b"shipped v2\n"
    (repo / "shipped.txt").write_bytes(body)
    _write_manifest(repo, {"shipped.txt": _sha(body)})   # working tree re-pinned
    _git(repo, "add", "shipped.txt")                     # but manifest NOT staged
    proc = _run_gate(repo)
    assert proc.returncode == 1, proc.stderr
    assert "shipped.txt" in proc.stderr


def _repin(root: Path):
    """Regenerate the manifest and stage it, as the gate's own remedy says."""
    subprocess.run(
        [sys.executable, str(root / "scripts" / "check_release_manifest.py"),
         "--write"],
        cwd=root, check=True, capture_output=True)
    _git(root, "add", "RELEASE_MANIFEST.txt")


def test_a_brand_new_tracked_file_with_no_manifest_row_is_refused(repo):
    """A newly tracked file must carry its pin in the SAME commit.

    CONTRACT CHANGED 2026-09-13, and the old one is the reason. This test
    previously asserted the opposite -- that a brand-new unpinned file PASSES
    -- because the gate was scoped to the split commit, a pinned file whose
    row went stale. The other half was left open and it shipped: a hand
    landing finished with `git add -A`, which swept in `.nh-local`, an
    untracked-but-not-ignored symlink pointing at an absolute path on the
    operator's machine. Brand-new, so the gate passed it, and it reached the
    PUBLIC repository. CI's `File inventory` job caught it with
    "`.nh-local`: tracked but not listed" -- by which time main was red and
    the content was published.

    `check_release_manifest.py --strict` already enforces exactly this in CI.
    The gate now enforces it at commit time, which is the last moment it is
    free to fix. A file genuinely meant to ship passes as soon as its row is
    written and staged, which is the same one-step remedy the sibling check
    already asks for -- see the pairing test below.
    """
    (repo / "notes.md").write_bytes(b"just notes\n")     # brand-new, unpinned
    _git(repo, "add", "notes.md")
    proc = _run_gate(repo)
    assert proc.returncode == 1, proc.stderr
    assert "notes.md" in proc.stderr
    assert "never approved" in proc.stderr


def test_a_brand_new_file_passes_once_its_row_is_staged(repo):
    """The pairing control for the test above.

    Without this, the gate could be satisfied by refusing everything new, and
    the refusal test alone could not tell that apart from a working gate.
    """
    (repo / "notes.md").write_bytes(b"just notes\n")
    _git(repo, "add", "notes.md")
    _repin(repo)
    proc = _run_gate(repo)
    assert proc.returncode == 0, proc.stderr


def test_an_ignored_file_cannot_reach_the_gate(repo):
    """A gitignored file is never staged, so the new check cannot fire on it.

    This is what keeps the refusal from becoming noise: the remedy the gate
    prints for a file that should NOT ship is "unstage it and gitignore it",
    and this pins that the remedy actually works.
    """
    (repo / ".gitignore").write_bytes(b"scratch.tmp\n")
    (repo / "scratch.tmp").write_bytes(b"local only\n")
    _git(repo, "add", "-A")
    _repin(repo)
    proc = _run_gate(repo)
    assert proc.returncode == 0, proc.stderr


def test_gate_reads_index_not_working_tree(repo):
    """A pinned file dirtied in the working tree but NOT staged is not part of
    the commit, so the gate must ignore it."""
    (repo / "shipped.txt").write_bytes(b"unstaged edit\n")   # working tree only
    proc = _run_gate(repo)                                     # nothing staged
    assert proc.returncode == 0, proc.stderr


def test_empty_staging_passes(repo):
    proc = _run_gate(repo)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# End-to-end: the committed hook, driven through a real `git commit`.
# --------------------------------------------------------------------------

def _install(root: Path):
    shutil.copytree(HOOK_SRC.parent, root / "scripts" / "hooks")
    _git(root, "add", "scripts/hooks")
    _git(root, "commit", "-qm", "add hooks")
    proc = subprocess.run(["bash", str(root / "scripts" / "hooks" / "install.sh")],
                          cwd=str(root), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_hook_blocks_real_commit(repo):
    _install(repo)
    (repo / "shipped.txt").write_bytes(b"shipped v2\n")
    _git(repo, "add", "shipped.txt")
    proc = _git(repo, "commit", "-m", "change shipped, forgot to re-pin",
                check=False)
    assert proc.returncode != 0
    assert "shipped.txt" in proc.stderr


def test_hook_allows_repinned_real_commit(repo):
    _install(repo)
    body = b"shipped v2\n"
    (repo / "shipped.txt").write_bytes(body)
    _write_manifest(repo, {"shipped.txt": _sha(body)})
    _git(repo, "add", "shipped.txt", MANIFEST)
    proc = _git(repo, "commit", "-m", "change shipped + re-pin", check=False)
    assert proc.returncode == 0, proc.stderr


def test_hook_noop_when_not_installed(repo):
    """Without installation the gate must not run: a split commit goes through
    (the safety net is opt-in and must not surprise a clone that never enabled
    it)."""
    (repo / "shipped.txt").write_bytes(b"shipped v2\n")
    _git(repo, "add", "shipped.txt")
    proc = _git(repo, "commit", "-m", "no hook installed", check=False)
    assert proc.returncode == 0, proc.stderr


def test_install_and_uninstall_roundtrip(repo):
    shutil.copytree(HOOK_SRC.parent, repo / "scripts" / "hooks")
    hooks_dir = Path(_git(repo, "rev-parse", "--git-path", "hooks").stdout.strip())
    if not hooks_dir.is_absolute():
        hooks_dir = repo / hooks_dir
    dest = hooks_dir / "pre-commit"

    subprocess.run(["bash", str(repo / "scripts" / "hooks" / "install.sh")],
                   cwd=str(repo), check=True, capture_output=True, text=True)
    assert dest.is_symlink()

    subprocess.run(["bash", str(repo / "scripts" / "hooks" / "install.sh"),
                    "--uninstall"], cwd=str(repo), check=True,
                   capture_output=True, text=True)
    assert not dest.exists()
