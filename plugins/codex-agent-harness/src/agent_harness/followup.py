"""Journal-only coordination of corrections across a pinned branch chain."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .followup_git import (ancestor, baseline_unchanged, check_provenance, exact_sha,
                           git_text, preservation_seed, same_repo, snapshot,
                           verified_baseline, working_manifest)
from .followup_store import FollowupStore
from .git_repo import resolve_repo
from .store import RunStore
from .util import InputError, StateError, json_copy, require_string, sanitize_text


READY = {"mechanical_checked", "complete"}
TIME_BUCKETS = {"writing", "propagation", "checks", "review", "external_wait"}


def identifier(value: Any, name: str) -> str:
    value = require_string(value, name, maximum=128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise InputError(f"invalid {name}")
    return value


def fields(value: Any, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, Mapping) or set(value) - allowed or required - set(value):
        raise InputError("followup object has missing or unsupported fields")
    return dict(value)


def integer(value: Any, name: str, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise InputError(f"invalid {name}")
    return value


def names(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise InputError(f"{name} must be a nonempty bounded array")
    result = [identifier(item, name) for item in value]
    if len(result) != len(set(result)):
        raise InputError(f"{name} contains duplicates")
    return result


def source_snapshot(value: Any, common: str) -> dict:
    value = fields(value, {"workspace", "base_sha", "head_sha"}, {"workspace", "base_sha", "head_sha"})
    context = same_repo(value["workspace"], common)
    base, head = exact_sha(context, value["base_sha"]), exact_sha(context, value["head_sha"])
    if not ancestor(context, base, head):
        raise InputError("source base must be an ancestor of the pinned head")
    current = snapshot(str(context.repo_root), base, common)
    if not current["clean"] or current["head_sha"] != head:
        raise StateError("source must be a clean pinned candidate")
    return {**current, "base_sha": base}


def parent_heads(contract: Mapping, state: Mapping, task_id: str, cache: dict | None = None) -> dict[str, str]:
    cache = {} if cache is None else cache
    task = contract["tasks"][task_id]
    if not task["dependencies"]:
        return {"source": state["source"]["head_sha"]}
    result = {}
    for dependency in task["dependencies"]:
        current = state["tasks"][dependency]
        if current["status"] not in READY:
            raise StateError("dependency has not finished its adaptation")
        fresh_candidate(contract, state, dependency, cache)
        result[dependency] = current["candidate"]["head_sha"]
    return result


def fresh_candidate(contract: Mapping, state: Mapping, task_id: str, cache: dict | None = None) -> dict:
    cache = {} if cache is None else cache
    if task_id in cache:
        return cache[task_id]
    current = state["tasks"][task_id]
    task = effective_task(contract, state, task_id)
    candidate = current.get("candidate")
    if not candidate:
        raise StateError("candidate has not been recorded")
    baseline_unchanged(task, contract["git_common_dir"])
    actual = snapshot(candidate["workspace"], candidate["parent_sha"], contract["git_common_dir"])
    if any(candidate[key] != value for key, value in actual.items()):
        raise StateError("followup candidate changed after verification")
    if candidate["parent_heads"] != parent_heads(contract, state, task_id, cache):
        raise StateError("followup dependency changed")
    cache[task_id] = candidate
    return candidate


def new_task_state(task: Mapping) -> dict:
    return {"status": "awaiting_checkpoint" if task["active"] else "pending",
            "mode": None, "candidate": None, "checks": {}, "attempt": 0}


def effective_task(contract: Mapping, state: Mapping, task_id: str) -> dict:
    return {**contract["tasks"][task_id], **state["tasks"][task_id].get("checkpoint_baseline", {})}


class FollowupService:
    def __init__(self, workspace: str) -> None:
        self.context = resolve_repo(workspace)
        self.common = str(self.context.git_common_dir)
        self.store = FollowupStore(self.context)

    def _validate(self, contract: dict, state: dict) -> None:
        try:
            tasks = contract["tasks"]
            if (contract["git_common_dir"] != self.common or not isinstance(tasks, dict)
                    or not 1 <= len(tasks) <= 64 or set(tasks) != set(state["tasks"])
                    or integer(state["epoch"], "epoch") < 1
                    or set(state["durations_ms"]) != TIME_BUCKETS):
                raise ValueError
            for value in state["durations_ms"].values():
                integer(value, "duration")
            for field in ("queued_sources", "history", "events"):
                if not isinstance(state[field], list):
                    raise ValueError
            seen, visiting = set(), set()
            def visit(key: str) -> None:
                if key in visiting:
                    raise ValueError
                if key in seen:
                    return
                visiting.add(key)
                task, current = tasks[key], state["tasks"][key]
                if (not isinstance(task, dict) or not isinstance(current, dict)
                        or task["id"] != key or not isinstance(task["dependencies"], list)
                        or current["status"] not in READY | {"pending", "awaiting_checkpoint", "adapting", "checking", "blocked", "interrupted"}
                        or not isinstance(current["checks"], dict)):
                    raise ValueError
                integer(current["attempt"], "attempt")
                for name in ("workspace", "base_sha", "head_sha"):
                    require_string(task[name], name)
                for dependency in task["dependencies"]:
                    visit(dependency)
                visiting.remove(key)
                seen.add(key)
            for key in tasks:
                visit(key)
        except (InputError, KeyError, TypeError, ValueError, RecursionError) as exc:
            raise StateError("invalid correction-chain state") from exc

    def _read(self, followup_id: str) -> dict:
        result = self.store.read(followup_id)
        self._validate(result["contract"], result["state"])
        return result

    def create(self, arguments: Mapping) -> dict:
        args = fields(arguments, {"workspace", "followup_id", "owner_id", "summary", "source", "tasks"},
                      {"followup_id", "owner_id", "source", "tasks"})
        followup_id, owner_id = identifier(args["followup_id"], "followup_id"), identifier(args["owner_id"], "owner_id")
        request_hash = hashlib.sha256(json.dumps(
            {key: value for key, value in args.items() if key != "workspace"},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if (self.store.root / followup_id / "journal.json").exists():
            existing = self._read(followup_id)
            if existing["contract"].get("creation_request_hash") != request_hash:
                raise StateError("followup_id already belongs to a different request")
            return existing
        source = source_snapshot(args["source"], self.common)
        raw_tasks = args["tasks"]
        if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 64:
            raise InputError("tasks must contain 1 to 64 descendants")
        tasks, workspaces = {}, {source["workspace"]}
        for raw in raw_tasks:
            task = fields(raw, {"id", "workspace", "base_sha", "head_sha", "dependencies", "parent_task_id",
                                "published", "active", "run_id", "build_checks", "test_checks", "check_bindings"},
                          {"id", "workspace", "base_sha", "head_sha", "published", "active", "build_checks", "test_checks", "check_bindings"})
            task_id = identifier(task["id"], "task_id")
            context = same_repo(task["workspace"], self.common)
            workspace = str(context.repo_root)
            if task_id in tasks or workspace in workspaces:
                raise InputError("tasks require distinct identifiers and baseline worktrees")
            workspaces.add(workspace)
            base, head = exact_sha(context, task["base_sha"]), exact_sha(context, task["head_sha"])
            if not ancestor(context, base, head) or context.head_sha != head:
                raise InputError("task baseline must match its complete local Git range")
            dependencies = task.get("dependencies", [])
            if (not isinstance(dependencies, list) or len(dependencies) > 64
                    or any(not isinstance(value, str) for value in dependencies)
                    or len(dependencies) != len(set(dependencies))):
                raise InputError("dependencies must be unique task identifiers")
            if any(dependency not in tasks for dependency in dependencies):
                raise InputError("tasks must be topologically ordered without missing dependencies")
            parent_id = task.get("parent_task_id", dependencies[0] if len(dependencies) == 1 else None)
            if dependencies and parent_id not in dependencies:
                raise InputError("parent_task_id must select the Git parent dependency")
            if not dependencies and parent_id is not None:
                raise InputError("root descendants use the source as Git parent")
            old_parent = tasks[parent_id]["head_sha"] if dependencies else source["head_sha"]
            if not ancestor(context, base, old_parent):
                raise InputError("task base is unrelated to the declared parent chain")
            for field in ("published", "active"):
                if type(task[field]) is not bool:
                    raise InputError(f"{field} must be a boolean")
            own = git_text(context, "rev-list", "--reverse", "--topo-order", f"{base}..{head}").splitlines()
            if len(own) > 512:
                raise InputError("task range exceeds 512 commits; split the correction plan")
            tasks[task_id] = {"id": task_id, "workspace": workspace, "base_sha": base, "head_sha": head,
                              "dependencies": dependencies, "parent_task_id": parent_id,
                              "published": task["published"], "active": task["active"], "own_commits": own,
                              "build_checks": names(task["build_checks"], "build_checks"),
                              "test_checks": names(task["test_checks"], "test_checks"),
                              "baseline_snapshot": snapshot(workspace, base, self.common),
                              "verified_baseline": verified_baseline(context, base, head, task.get("run_id"))}
            tasks[task_id]["dirty_manifest"] = working_manifest(context) if not tasks[task_id]["baseline_snapshot"]["clean"] else None
            bindings = task["check_bindings"]
            if (not isinstance(bindings, dict) or set(bindings) != set(task["build_checks"] + task["test_checks"])
                    or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in bindings.values())):
                raise InputError("check_bindings must pin a SHA-256 command-and-conditions descriptor for every check")
            tasks[task_id]["check_bindings"] = dict(bindings)
        test_names = [name for task in tasks.values() for name in task["test_checks"]]
        build_names = {name for task in tasks.values() for name in task["build_checks"]}
        if len(test_names) != len(set(test_names)) or set(test_names) & build_names:
            raise InputError("test check names must be unique across tasks and distinct from build names")
        contract = {"schema_version": 1, "followup_id": followup_id, "owner_id": owner_id,
                    "git_common_dir": self.common, "source": source, "tasks": tasks,
                    "creation_request_hash": request_hash,
                    "summary": sanitize_text(args.get("summary"), maximum=1000)}
        state = {"schema_version": 1, "followup_id": followup_id, "revision": 0, "epoch": 1,
                 "source": source, "queued_sources": [], "history": [], "events": [],
                 "tasks": {key: new_task_state(task) for key, task in tasks.items()},
                 "durations_ms": {key: 0 for key in TIME_BUCKETS}}
        return self.store.create(followup_id, contract, state)

    def get(self, followup_id: str) -> dict:
        document = self._read(identifier(followup_id, "followup_id"))
        contract, state = document["contract"], document["state"]
        readiness, cache = {}, {}
        if state.get("terminal"):
            return {**document, "readiness": {key: {"ready": False, "reason": "followup is cancelled"} for key in state["tasks"]},
                    "transfer_ready_tasks": [], "progress_checkpoint_due": False, "external_actions_authorized": False}
        for task_id, current in state["tasks"].items():
            try:
                candidate = fresh_candidate(contract, state, task_id, cache)
                if current["status"] not in READY:
                    raise StateError("candidate checks or semantic review are incomplete")
                self._checks_green(current, set(contract["tasks"][task_id]["build_checks"]))
                self._chain_tests(contract, state, task_id, cache)
                if state["queued_sources"]:
                    raise StateError("a newer source is queued")
                source = state["source"]
                if snapshot(source["workspace"], source["base_sha"], self.common) != {
                    key: value for key, value in source.items() if key != "base_sha"
                }:
                    raise StateError("source changed without a recorded revision")
                readiness[task_id] = {"ready": True, "head_sha": candidate["head_sha"]}
            except (StateError, InputError) as exc:
                readiness[task_id] = {"ready": False, "reason": sanitize_text(str(exc), maximum=300)}
        active_ms = sum(value for key, value in state["durations_ms"].items() if key != "external_wait")
        transfer_ready = []
        for task_id, current in state["tasks"].items():
            if current["status"] not in {"pending", "blocked", "interrupted"}:
                continue
            try:
                baseline_unchanged(effective_task(contract, state, task_id), self.common)
                parents = parent_heads(contract, state, task_id, cache)
                transfer_ready.append({"task_id": task_id, "parent_heads": parents})
            except (StateError, InputError):
                continue
        return {**document, "readiness": readiness, "transfer_ready_tasks": transfer_ready,
                "progress_checkpoint_due": active_ms - state.get("reported_active_ms", 0) >= 900_000,
                "external_actions_authorized": False}

    @staticmethod
    def _chain_tests(contract: Mapping, state: Mapping, task_id: str,
                     cache: dict | None = None, visited: set | None = None) -> None:
        cache = {} if cache is None else cache
        visited = set() if visited is None else visited
        if task_id in visited:
            return
        visited.add(task_id)
        children = [key for key, task in contract["tasks"].items() if task_id in task["dependencies"]]
        if children:
            for child in children:
                fresh_candidate(contract, state, child, cache)
                if state["tasks"][child]["status"] not in READY:
                    raise StateError("a dependent candidate is not ready")
                FollowupService._chain_tests(contract, state, child, cache, visited)
        else:
            current = state["tasks"][task_id]
            required = {name for task in contract["tasks"].values() for name in task["test_checks"]
                        if task["id"] == task_id or FollowupService._depends(contract, task_id, task["id"])}
            FollowupService._checks_green(current, required)

    @staticmethod
    def _depends(contract: Mapping, child: str, parent: str) -> bool:
        pending, visited = list(contract["tasks"][child]["dependencies"]), set()
        while pending:
            current = pending.pop()
            if current == parent:
                return True
            if current not in visited:
                visited.add(current)
                pending.extend(contract["tasks"][current]["dependencies"])
        return False

    @staticmethod
    def _checks_green(current: Mapping, required: set[str]) -> None:
        fingerprint = current["candidate"]["diff_fingerprint"]
        if any(current["checks"].get(name, {}).get("exit_code") != 0
               or current["checks"][name].get("diff_fingerprint") != fingerprint
               or current["checks"][name].get("check_fingerprint") != current["check_bindings"][name] for name in required):
            raise StateError("fresh successful followup checks are required")

    def record(self, arguments: Mapping) -> dict:
        args = fields(arguments, {"workspace", "followup_id", "owner_id", "expected_revision", "request_id", "action", "data"},
                      {"followup_id", "owner_id", "expected_revision", "request_id", "action"})
        action = require_string(args["action"], "action", maximum=32)
        data = args.get("data", {})
        if not isinstance(data, dict):
            raise InputError("data must be an object")
        def update(contract: dict, state: dict) -> None:
            self._validate(contract, state)
            self._apply(contract, state, action, data)
            state["events"].append({"action": action, "epoch": state["epoch"],
                                    "revision": state["revision"] + 1})
        return self.store.mutate(identifier(args["followup_id"], "followup_id"),
                                 owner_id=identifier(args["owner_id"], "owner_id"),
                                 expected_revision=integer(args["expected_revision"], "expected_revision"),
                                 request_id=identifier(args["request_id"], "request_id"),
                                 payload={"action": action, "data": data}, update=update)

    def _completed_baseline(self, contract: dict, state: dict, task_id: str) -> dict:
        previous = state["tasks"][task_id]
        candidate = previous["candidate"]
        context = same_repo(candidate["workspace"], self.common)
        captured = snapshot(candidate["workspace"], candidate["parent_sha"], self.common)
        if captured != {key: candidate[key] for key in captured}:
            raise StateError("cannot reopen or advance past an altered candidate")
        prior = effective_task(contract, state, task_id)
        return {"workspace": candidate["workspace"], "base_sha": candidate["parent_sha"],
                "head_sha": candidate["head_sha"], "baseline_snapshot": captured,
                "own_commits": git_text(context, "rev-list", "--reverse", "--topo-order",
                                        f"{candidate['parent_sha']}..{candidate['head_sha']}").splitlines(),
                "verified_baseline": (verified_baseline(context, candidate["parent_sha"], candidate["head_sha"], previous["run_id"])
                                      if previous["status"] == "complete" else prior["verified_baseline"]),
                "dirty_manifest": None}

    def _apply(self, contract: dict, state: dict, action: str, data: dict) -> None:
        if state.get("terminal"):
            raise StateError("followup is cancelled; its history is read-only")
        if action == "abandon":
            fields(data, {"summary"}, {"summary"})
            if any(task["status"] in {"adapting", "checking", "awaiting_checkpoint"} for task in state["tasks"].values()):
                raise StateError("abandon requires safe checkpoints for all active writers")
            state["terminal"] = {"status": "cancelled", "summary": sanitize_text(
                require_string(data["summary"], "summary", maximum=1000), maximum=1000)}
            return
        if action == "queue_source":
            fields(data, {"source"}, {"source"})
            source = source_snapshot(data["source"], self.common)
            occupied = {item["workspace"] for item in contract["tasks"].values()}
            occupied |= {effective_task(contract, state, key)["workspace"] for key in contract["tasks"]}
            occupied |= {item["workspace"] for item in state["tasks"].values() if item.get("workspace")}
            if source["workspace"] in occupied:
                raise StateError("queued source requires a worktree separate from all task workspaces")
            previous = (state["queued_sources"] or [state["source"]])[-1]
            if source["head_sha"] == previous["head_sha"]:
                return
            if not ancestor(self.context, previous["head_sha"], source["head_sha"]):
                raise StateError("queued source must retain the accepted source history")
            state["queued_sources"].append(source)
            return
        if action == "advance":
            fields(data, set())
            if not state["queued_sources"] or any(task["status"] not in READY | {"pending", "blocked", "interrupted"}
                                                 for task in state["tasks"].values()):
                raise StateError("advance requires a queued source and safe task checkpoints")
            source = state["queued_sources"][0]
            if snapshot(source["workspace"], source["base_sha"], self.common) != {
                key: value for key, value in source.items() if key != "base_sha"
            }:
                raise StateError("queued source changed after it was pinned")
            state["history"].append({"epoch": state["epoch"], "source": state["source"], "tasks": json_copy(state["tasks"])})
            state["source"] = state["queued_sources"].pop(0)
            state["epoch"] += 1
            next_tasks = {}
            for key, task in contract["tasks"].items():
                previous = state["tasks"][key]
                next_tasks[key] = new_task_state({**task, "active": False})
                if previous.get("checkpoint_baseline"):
                    next_tasks[key]["checkpoint_baseline"] = previous["checkpoint_baseline"]
                if previous.get("checkpoint") and previous["status"] in {"blocked", "interrupted"}:
                    prior = effective_task(contract, state, key)
                    checkpoint = previous["checkpoint"]
                    if snapshot(previous["workspace"], prior["base_sha"], self.common) != checkpoint:
                        raise StateError("paused worktree changed after its checkpoint")
                    context = same_repo(previous["workspace"], self.common)
                    next_tasks[key]["checkpoint_baseline"] = {
                        "workspace": previous["workspace"], "base_sha": prior["base_sha"],
                        "head_sha": checkpoint["head_sha"], "baseline_snapshot": checkpoint,
                        "own_commits": git_text(context, "rev-list", "--reverse", "--topo-order",
                                                f"{prior['base_sha']}..{checkpoint['head_sha']}").splitlines(),
                        "verified_baseline": None,
                        "dirty_manifest": working_manifest(context) if not checkpoint["clean"] else None,
                    }
                elif previous.get("candidate"):
                    # Поздние исправления и опубликованная история становятся полной базой следующего прохода.
                    next_tasks[key]["checkpoint_baseline"] = self._completed_baseline(contract, state, key)
            state["tasks"] = next_tasks
            return
        if action == "progress":
            fields(data, {"bucket", "duration_ms", "summary"}, {"bucket", "duration_ms"})
            if data["bucket"] not in TIME_BUCKETS:
                raise InputError("unknown duration bucket")
            state["durations_ms"][data["bucket"]] += integer(data["duration_ms"], "duration_ms", 86_400_000)
            summary = sanitize_text(data.get("summary"), maximum=1000)
            if summary:
                state["last_progress"] = summary
                state["reported_active_ms"] = sum(value for key, value in state["durations_ms"].items()
                                                   if key != "external_wait")
            return
        task_id = identifier(data.get("task_id"), "task_id")
        if task_id not in contract["tasks"]:
            raise InputError("unknown followup task")
        task, current = effective_task(contract, state, task_id), state["tasks"][task_id]
        if action == "reopen":
            fields(data, {"task_id", "summary"}, {"task_id", "summary"})
            summary = sanitize_text(require_string(data["summary"], "summary", maximum=1000), maximum=1000)
            affected = [key for key in contract["tasks"] if key == task_id or self._depends(contract, key, task_id)]
            if current["status"] not in READY or any(state["tasks"][key]["status"] in {
                    "adapting", "checking", "awaiting_checkpoint"} for key in affected):
                raise StateError("reopen requires a finished task and safe checkpoints for its descendants")
            replacements = {}
            for key in affected:
                previous = state["tasks"][key]
                if previous["status"] in READY:
                    fresh_candidate(contract, state, key)
                    replacements[key] = {**new_task_state({"active": False}),
                                         "attempt": previous["attempt"],
                                         "checkpoint_baseline": self._completed_baseline(contract, state, key)}
                elif previous.get("checkpoint"):
                    prior = effective_task(contract, state, key)
                    if snapshot(previous["workspace"], prior["base_sha"], self.common) != previous["checkpoint"]:
                        raise StateError("paused worktree changed after its checkpoint")
                    replacements[key] = {**previous, "checks": {}, "candidate": None}
            state["history"].append({"epoch": state["epoch"], "action": "reopen", "task_id": task_id,
                                     "summary": summary, "tasks": {key: json_copy(state["tasks"][key]) for key in affected}})
            state["tasks"].update(replacements)
            return
        if action == "checkpoint":
            fields(data, {"task_id", "released"}, {"task_id", "released"})
            if data["released"] is not True or current["status"] != "awaiting_checkpoint":
                raise StateError("active writer must explicitly release its checkpoint")
            context = same_repo(task["workspace"], self.common)
            checkpoint = snapshot(task["workspace"], task["base_sha"], self.common)
            if not ancestor(context, task["head_sha"], context.head_sha):
                raise StateError("writer checkpoint must preserve the original task history")
            current["checkpoint_baseline"] = {
                "workspace": task["workspace"], "base_sha": task["base_sha"], "head_sha": context.head_sha,
                "baseline_snapshot": checkpoint,
                "own_commits": git_text(context, "rev-list", "--reverse", "--topo-order",
                                        f"{task['base_sha']}..{context.head_sha}").splitlines(),
                "verified_baseline": task["verified_baseline"] if checkpoint == task["baseline_snapshot"] else None,
                "dirty_manifest": working_manifest(context) if not checkpoint["clean"] else None,
            }
            current["status"] = "pending"
            return
        if action == "begin":
            fields(data, {"task_id", "workspace", "mode"}, {"task_id", "workspace", "mode"})
            if current["status"] not in {"pending", "interrupted", "blocked"}:
                raise StateError("task must reach a safe checkpoint before adaptation")
            if data["mode"] not in {"mechanical", "semantic"}:
                raise InputError("mode must be mechanical or semantic")
            if current.get("mode") == "semantic" and data["mode"] == "mechanical":
                raise StateError("a semantic attempt cannot resume without semantic review")
            if data["mode"] == "mechanical" and not task["verified_baseline"]:
                raise StateError("mechanical adaptation requires a verified baseline")
            baseline_unchanged(task, self.common)
            if current.get("checkpoint") and snapshot(current["workspace"], task["base_sha"], self.common) != current["checkpoint"]:
                raise StateError("interrupted worktree changed; inspect and preserve the checkpoint before resuming")
            parents = parent_heads(contract, state, task_id)
            parent = parents[task["parent_task_id"] or "source"]
            workspace = str(same_repo(data["workspace"], self.common).repo_root)
            reserved = {effective_task(contract, state, key)["workspace"] for key in contract["tasks"]}
            reserved |= {item["workspace"] for item in contract["tasks"].values()} | {state["source"]["workspace"]}
            reserved |= {item["candidate"]["workspace"] for key, item in state["tasks"].items()
                         if key != task_id and item.get("candidate")}
            reserved |= {item["workspace"] for key, item in state["tasks"].items()
                         if key != task_id and item.get("workspace")}
            reserved |= {item["workspace"] for item in state["queued_sources"]}
            if workspace in reserved:
                raise StateError("adaptation requires its own worktree; baseline and active writers are protected")
            if current.get("checkpoint"):
                checkpoint = current["checkpoint"]
                resumed = {**task, "head_sha": checkpoint["head_sha"],
                           "baseline_snapshot": checkpoint, "dirty_manifest": current.get("checkpoint_manifest")}
                context = same_repo(workspace, self.common)
                if not ancestor(context, checkpoint["head_sha"], context.head_sha):
                    raise StateError("resume must preserve the full interrupted checkpoint")
                seed = preservation_seed(resumed, workspace, self.common)
                checkpoint_commits = git_text(context, "rev-list", "--reverse", "--topo-order",
                                              f"{task['base_sha']}..{checkpoint['head_sha']}").splitlines()
                preserved = list(dict.fromkeys((current.get("preserved_commits") or task["own_commits"])
                                               + checkpoint_commits + (seed or [])))
            else:
                preserved = preservation_seed(task, workspace, self.common)
            bindings = dict(task["check_bindings"])
            for ancestor_id, ancestor_task in contract["tasks"].items():
                if self._depends(contract, task_id, ancestor_id):
                    bindings.update({name: ancestor_task["check_bindings"][name] for name in ancestor_task["test_checks"]})
            current.update({"status": "adapting", "mode": data["mode"], "checks": {}, "candidate": None,
                            "attempt": current["attempt"] + 1, "workspace": workspace, "parent_sha": parent,
                            "parent_heads": parents, "preserved_commits": preserved, "check_bindings": bindings})
            return
        if action == "candidate":
            fields(data, {"task_id", "replayed_commits", "summary"}, {"task_id"})
            if current["status"] not in {"adapting", "checking"}:
                raise StateError("candidate requires an active adaptation")
            if current["parent_heads"] != parent_heads(contract, state, task_id):
                raise StateError("dependency changed during adaptation")
            candidate = {**snapshot(current["workspace"], current["parent_sha"], self.common),
                         "parent_sha": current["parent_sha"], "parent_heads": current["parent_heads"]}
            provenance = {**task, "own_commits": current.get("preserved_commits") or task["own_commits"]}
            candidate["replayed_commits"] = check_provenance(provenance, candidate, data.get("replayed_commits", []), self.common)
            candidate["summary"] = sanitize_text(data.get("summary"), maximum=1000)
            current.update({"candidate": candidate, "checks": {}, "status": "checking"})
            return
        if action == "check":
            fields(data, {"task_id", "name", "diff_fingerprint", "check_fingerprint", "exit_code", "duration_ms"},
                   {"task_id", "name", "diff_fingerprint", "check_fingerprint", "exit_code", "duration_ms"})
            candidate = fresh_candidate(contract, state, task_id)
            if (current["status"] != "checking" or data["name"] not in current["check_bindings"]
                    or data["check_fingerprint"] != current["check_bindings"][data["name"]]
                    or data["diff_fingerprint"] != candidate["diff_fingerprint"]):
                raise StateError("check does not match the current candidate and frozen check plan")
            current["checks"][data["name"]] = {"diff_fingerprint": candidate["diff_fingerprint"],
                "check_fingerprint": data["check_fingerprint"],
                "exit_code": integer(data["exit_code"], "exit_code", 255),
                "duration_ms": integer(data["duration_ms"], "duration_ms", 86_400_000)}
            return
        if action == "finish":
            fields(data, {"task_id", "run_id"}, {"task_id"})
            candidate = fresh_candidate(contract, state, task_id)
            if current["status"] != "checking":
                raise StateError("finish requires current candidate checks")
            self._checks_green(current, set(task["build_checks"]))
            if not any(task_id in item["dependencies"] for item in contract["tasks"].values()):
                self._chain_tests(contract, state, task_id)
            if current["mode"] == "mechanical":
                if "run_id" in data:
                    raise InputError("mechanical_checked does not create a complete run")
                current["status"] = "mechanical_checked"
            else:
                store = RunStore.for_workspace(candidate["workspace"])
                run_id = require_string(data.get("run_id"), "run_id", maximum=128)
                run, result = store.read_contract(run_id), store.read_state(run_id)
                reference = run.get("followup_ref") or {}
                if (result.get("phase") != "complete"
                        or reference != self._reference(contract, state, task_id)
                        or run.get("base_sha") != candidate["parent_sha"]):
                    raise StateError("semantic adaptation requires its own current complete followup run")
                verified_baseline(store.context, candidate["parent_sha"], candidate["head_sha"], run_id)
                current.update({"status": "complete", "run_id": run_id})
            return
        if action == "pause":
            fields(data, {"task_id", "status", "summary"}, {"task_id", "status"})
            if data["status"] not in {"blocked", "interrupted"} or current["status"] not in {"adapting", "checking"}:
                raise StateError("only an active adaptation can pause")
            current["checkpoint"] = snapshot(current["workspace"], task["base_sha"], self.common)
            current["checkpoint_manifest"] = (working_manifest(same_repo(current["workspace"], self.common))
                                              if not current["checkpoint"]["clean"] else None)
            current.update({"status": data["status"], "summary": sanitize_text(data.get("summary"), maximum=1000)})
            return
        raise InputError("unsupported followup action")

    @staticmethod
    def _reference(contract: Mapping, state: Mapping, task_id: str) -> dict:
        task = state["tasks"][task_id]
        return {"workspace": contract["source"]["workspace"], "followup_id": contract["followup_id"],
                "task_id": task_id, "epoch": state["epoch"], "attempt": task["attempt"], "parent_sha": task["parent_sha"]}

    def run_context(self, reference: Mapping, workspace: str) -> dict:
        self.run_reference(reference, workspace)
        document = self._read(reference["followup_id"])
        task = effective_task(document["contract"], document["state"], reference["task_id"])
        return {"previous_base_sha": task["base_sha"], "previous_head_sha": task["head_sha"],
                "baseline_workspace": task["workspace"], "baseline_snapshot": task["baseline_snapshot"],
                "verified_baseline": task["verified_baseline"],
                "source_head_sha": document["state"]["source"]["head_sha"],
                "parent_heads": document["state"]["tasks"][reference["task_id"]]["parent_heads"]}

    def run_reference(self, value: Any, workspace: str, *, allow_complete: bool = False) -> dict:
        reference = fields(value, {"workspace", "followup_id", "task_id", "epoch", "attempt", "parent_sha"},
                           {"workspace", "followup_id", "task_id", "epoch", "attempt", "parent_sha"})
        document = self._read(identifier(reference["followup_id"], "followup_id"))
        contract, state = document["contract"], document["state"]
        if state.get("terminal"):
            raise StateError("followup is cancelled; no run can use its current authority")
        task_id = identifier(reference["task_id"], "task_id")
        current = state["tasks"].get(task_id, {})
        integer(reference["epoch"], "epoch")
        integer(reference["attempt"], "attempt")
        allowed = {"adapting", "checking"} | ({"complete"} if allow_complete else set())
        if (current.get("mode") != "semantic" or current.get("status") not in allowed
                or current.get("workspace") != str(resolve_repo(workspace).repo_root)
                or reference != self._reference(contract, state, task_id)):
            raise StateError("followup_ref must name the current semantic adaptation attempt")
        if current["parent_heads"] != parent_heads(contract, state, task_id):
            raise StateError("followup parent changed")
        baseline_unchanged(effective_task(contract, state, task_id), self.common)
        if current.get("status") == "complete":
            fresh_candidate(contract, state, task_id)
        return reference
