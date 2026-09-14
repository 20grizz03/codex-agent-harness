"""Read-only provenance and freshness checks for a correction chain."""

from __future__ import annotations

import re
import stat
from typing import Any, Mapping

from .git_repo import diff_fingerprint, resolve_repo, run_git, status_snapshot
from .store import RunStore
from .verification import review_snapshot
from .util import InputError, StateError, require_string


def git_text(context: Any, *argv: str) -> str:
    result = run_git(context.repo_root, list(argv))
    if result.returncode:
        raise StateError("cannot verify followup Git provenance")
    return str(result.stdout).strip()


def exact_sha(context: Any, value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value):
        raise InputError("followup requires a full lowercase commit SHA")
    if git_text(context, "rev-parse", "--verify", f"{value}^{{commit}}") != value:
        raise InputError("followup SHA is not a commit")
    return value


def ancestor(context: Any, base: str, head: str) -> bool:
    result = run_git(context.repo_root, ["merge-base", "--is-ancestor", base, head])
    if result.returncode not in (0, 1):
        raise StateError("cannot verify followup ancestry")
    return result.returncode == 0


def same_repo(workspace: Any, common_dir: str) -> Any:
    context = resolve_repo(require_string(workspace, "workspace", maximum=4096))
    if str(context.git_common_dir) != common_dir:
        raise InputError("followup workspaces must belong to the same Git repository")
    return context


def snapshot(workspace: str, base_sha: str, common_dir: str) -> dict[str, Any]:
    context = same_repo(workspace, common_dir)
    fingerprint, paths = diff_fingerprint(context, base_sha=base_sha)
    return {
        "workspace": str(context.repo_root), "head_sha": context.head_sha,
        "diff_fingerprint": fingerprint, "changed_paths": paths,
        "clean": not status_snapshot(context)["dirty"],
    }


def verified_baseline(context: Any, base: str, head: str, run_id: Any) -> dict | None:
    if run_id is None:
        return None
    store = RunStore(context)
    run_id = require_string(run_id, "run_id", maximum=128)
    contract, state = store.read_contract(run_id), store.read_state(run_id)
    fingerprint, _ = diff_fingerprint(context, base_sha=base)
    same_result = state.get("diff_fingerprint") == fingerprint
    if not same_result and (state.get("planned_snapshot") or {}).get("files"):
        # Коммит меняет индекс и статус Git, но не отменяет проверку тех же файлов.
        same_result = snapshot_manifest(state["planned_snapshot"]) == working_manifest(context)
    if (contract.get("base_sha") != base or context.head_sha != head
            or state.get("phase") != "complete"
            or state.get("terminal", {}).get("status") != "complete"
            or not same_result
            or status_snapshot(context)["dirty"]):
        raise StateError("baseline requires an unchanged committed complete run")
    return {"run_id": run_id, "workspace": str(context.repo_root),
            "diff_fingerprint": state["diff_fingerprint"]}


def baseline_unchanged(task: Mapping, common_dir: str) -> None:
    original = task["baseline_snapshot"]
    if snapshot(task["workspace"], task["base_sha"], common_dir) != original:
        raise StateError("baseline worktree changed; preserve it before adapting")
    proof = task.get("verified_baseline")
    if proof:
        store = RunStore.for_workspace(proof["workspace"])
        state = store.read_state(proof["run_id"])
        if state.get("phase") != "complete" or state.get("diff_fingerprint") != proof["diff_fingerprint"]:
            raise StateError("baseline run evidence changed")


def working_manifest(context: Any) -> dict:
    return snapshot_manifest(review_snapshot(context))


def snapshot_manifest(captured: Mapping) -> dict:
    return {path: {"blob": entry["git_blob"], "mode": (
        "120000" if stat.S_ISLNK(entry["mode"]) else
        "100755" if entry["mode"] & stat.S_IXUSR else "100644")}
        for path, entry in captured["files"].items()}


def preservation_seed(task: Mapping, workspace: str, common_dir: str) -> list[str] | None:
    if task["baseline_snapshot"]["clean"]:
        return None
    context = same_repo(workspace, common_dir)
    if (status_snapshot(context)["dirty"] or not ancestor(context, task["head_sha"], context.head_sha)
            or working_manifest(context) != task.get("dirty_manifest")):
        raise StateError("dirty checkpoint requires a clean exact snapshot commit in its isolated worktree before adaptation")
    return git_text(context, "rev-list", "--reverse", "--topo-order",
                    f"{task['base_sha']}..{context.head_sha}").splitlines()


def check_provenance(task: Mapping, candidate: Mapping, mappings: Any,
                     common_dir: str) -> list[dict[str, str]]:
    context = same_repo(candidate["workspace"], common_dir)
    head, parent = candidate["head_sha"], candidate["parent_sha"]
    if not candidate["clean"]:
        raise StateError("adapted candidate must be a clean committed snapshot")
    for expected in candidate["parent_heads"].values():
        if not ancestor(context, expected, head):
            raise StateError("candidate is missing a pinned dependency")
    if task["published"] and not ancestor(context, task["head_sha"], head):
        raise StateError("published candidates must retain their previous history")
    if not isinstance(mappings, list) or len(mappings) > 512:
        raise InputError("replayed_commits must be a bounded array")
    replayed, used = {}, set()
    for item in mappings:
        if not isinstance(item, dict) or set(item) != {"old_sha", "new_sha"}:
            raise InputError("replayed_commits entries require old_sha and new_sha")
        old, new = exact_sha(context, item["old_sha"]), exact_sha(context, item["new_sha"])
        if old not in task["own_commits"] or old in replayed or new in used:
            raise InputError("replayed commit mapping is duplicated or outside the full task range")
        if not ancestor(context, new, head):
            raise StateError("replayed commit is not in the candidate")
        if ancestor(context, new, parent):
            # Только одинаковое дерево доказывает, что уже встроенный результат не потерян.
            if git_text(context, "rev-parse", f"{old}^{{tree}}") != git_text(context, "rev-parse", f"{new}^{{tree}}"):
                raise StateError("skipped integration commit must have an identical tree")
        replayed[old] = new
        used.add(new)
    if any(not ancestor(context, old, head) and old not in replayed for old in task["own_commits"]):
        raise StateError("candidate must preserve the full task range, including corrections")
    return [{"old_sha": old, "new_sha": new} for old, new in replayed.items()]
