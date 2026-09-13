#!/usr/bin/env python3
"""Pre-commit gate: block a commit that changes a pinned file without re-pinning.

The recurring defect this catches
---------------------------------
`RELEASE_MANIFEST.txt` pins every shipped file's content with a SHA-256, and it
is at once the release inventory AND the approval ledger `build_public_export.py`
gates on (see `scripts/check_release_manifest.py`). The defect that keeps landing
a red main and tripping the export gate is a *split commit*: a shipped file's
bytes change but its manifest row is left on the old hash — the two halves of one
change committed apart. CI's `inventory` job catches it, but only on the public
tree at push time; by then main is already red.

This gate moves that same check to commit time, in the source repo, scoped to
exactly the hazard: it looks only at files that are ALREADY pinned and STAGED,
and compares the sha256 of each one's *staged* content against the pin in the
*staged* manifest. So:

  * change a pinned file and forget to re-pin it (or forget to `git add` the
    re-pinned manifest)  -> BLOCKED, with the file(s) named and the fix printed;
  * change a pinned file and stage the updated manifest row in the same commit
    -> PASSES (the staged manifest already carries the new hash);
  * touch only unpinned or brand-new files                       -> PASSES;
  * a commit that stages nothing pinned                          -> PASSES.

It is deliberately narrow. New shipped files with no pin yet, deletions, and the
manifest's own completeness are NOT this gate's job — `check_release_manifest.py
--strict` on the release tree owns those. This gate owns one thing: a pinned
file's staged bytes must match its staged pin.

It reads the git INDEX, not the working tree, because a commit records the index.
`git cat-file blob :<path>` yields the staged content of a file (for a symlink,
the blob is its target string, so hashing it matches the manifest's own symlink
convention). The manifest is read the same way (`:RELEASE_MANIFEST.txt`), so a
row re-pinned and staged in this very commit is what the comparison sees.

Reuses `parse_manifest` from `check_release_manifest.py` rather than a second
copy of the manifest grammar. Standard library + `git` only, so it runs without
the project venv. Exit 0 when clean (or nothing to check), 1 when a pinned file's
staged bytes diverge from its staged pin.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_check_release_manifest():
    """Import the sibling `check_release_manifest.py` to reuse its manifest
    grammar. Loaded by path so this works whatever the cwd or sys.path is when
    git invokes the hook."""
    path = _HERE / "check_release_manifest.py"
    spec = importlib.util.spec_from_file_location("_nh_check_release_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_crm = _load_check_release_manifest()
parse_manifest = _crm.parse_manifest
MANIFEST_NAME = _crm.MANIFEST_NAME
CLASSIFICATION_NAME = _crm.CLASSIFICATION_NAME

APPROVE_CMD_PREFIX = "uv run python scripts/export_guard.py approve"
WRITE_CMD = "python scripts/check_release_manifest.py --write"


def remedy_command(root: Path, paths: str) -> str:
    """The re-pin command that actually exists in THIS tree.

    `export_guard.py` and `EXPORT_CLASSIFICATION.txt` are classified `drop` and
    do not ship, so on the public tree the only sanctioned re-pin is `--write`
    — which `check_release_manifest.py` sanctions precisely when no
    classification sits beside the tree. Where the classification IS present,
    `--write` refuses (it would forge approvals) and `approve` is the one
    write path. This file ships to both trees, so the branch stays forever.
    """
    try:
        present = (root / CLASSIFICATION_NAME).exists()
    except OSError:
        present = True  # unreadable root is only plausible in the private
        # tree; a harmless extra command beats pointing at --write there.
    return f"{APPROVE_CMD_PREFIX} {paths}" if present else WRITE_CMD


def repo_root() -> Path:
    return Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True).strip())


def staged_paths(root: Path) -> list[str]:
    """Paths added or modified in the index (not deleted). A deletion cannot be
    a content-vs-pin mismatch, so it is out of scope here."""
    out = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=AM", "-z"],
        cwd=root, text=True)
    return [p for p in out.split("\0") if p]


def staged_blob(root: Path, rel: str) -> bytes:
    """The bytes of `rel` as staged in the index. For a symlink this is the
    target string, which is exactly what `check_release_manifest.hash_path`
    hashes for a symlink, so the two agree."""
    return subprocess.check_output(
        ["git", "cat-file", "blob", f":{rel}"], cwd=root)


def staged_pins(root: Path) -> dict[str, str] | None:
    """`{path: sha256}` from the manifest AS STAGED, or None if the manifest is
    not tracked/staged (then there is nothing pinned to gate)."""
    proc = subprocess.run(
        ["git", "cat-file", "blob", f":{MANIFEST_NAME}"],
        cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return parse_manifest(proc.stdout)


def find_unpinned_changes(root: Path) -> list[tuple[str, str, str]]:
    """Staged pinned files whose staged content diverges from their staged pin.

    Returns `(path, pinned_sha, actual_sha)` for each offender. Empty list means
    the commit is clean for this gate.
    """
    pins = staged_pins(root)
    if not pins:
        return []
    offenders: list[tuple[str, str, str]] = []
    for rel in staged_paths(root):
        if rel == MANIFEST_NAME or rel not in pins:
            continue
        actual = hashlib.sha256(staged_blob(root, rel)).hexdigest()
        if actual != pins[rel]:
            offenders.append((rel, pins[rel], actual))
    return offenders


def main(argv: list[str] | None = None) -> int:
    try:
        root = repo_root()
    except (subprocess.CalledProcessError, OSError):
        # Not in a git repo (or git unavailable): nothing to gate, do not block.
        return 0

    offenders = find_unpinned_changes(root)
    if not offenders:
        return 0

    print("no_human pre-commit gate: REFUSED.", file=sys.stderr)
    print("", file=sys.stderr)
    print("These staged file(s) are pinned in %s, but their staged content does"
          % MANIFEST_NAME, file=sys.stderr)
    print("not match their pin — the file changed and its manifest row did not,"
          , file=sys.stderr)
    print("so this commit would ship a file the export ledger has not approved:",
          file=sys.stderr)
    for rel, pinned, actual in offenders:
        print(f"  {rel}", file=sys.stderr)
        print(f"      pinned {pinned[:12]}…  staged {actual[:12]}…",
              file=sys.stderr)
    print("", file=sys.stderr)
    print("  FIX: re-pin each file and stage the manifest in the SAME commit:",
          file=sys.stderr)
    paths = " ".join(rel for rel, _, _ in offenders)
    print(f"    {remedy_command(root, paths)}", file=sys.stderr)
    print(f"    git add {MANIFEST_NAME}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  The re-pin command above writes the new pin into the WORKING TREE "
          "only — the `git add`", file=sys.stderr)
    print("  above is what puts it in THIS commit. Then commit again.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
