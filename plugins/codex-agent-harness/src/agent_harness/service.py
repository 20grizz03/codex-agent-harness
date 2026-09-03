"""Agent Harness application service and lifecycle invariants."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from . import claude_runtime
from .budget import (
    effective_review_budget,
    normalize_review_budget,
    review_budget_status,
)
from .campaign import (
    CAMPAIGN_TERMINAL_PHASES,
    COMPARISON_DIMENSIONS,
    TASK_STATUSES,
    CampaignStore,
    build_campaign,
    verify_openspec_reference,
)
from .claude_runtime import ManagedStage
from .contract import build_contract
from .git_repo import diff_fingerprint, diff_stats, resolve_repo, status_snapshot
from .policy import RISK_RANK, plan_checks as build_check_plan, validate_risk
from .review import build_stage_prompt, validate_review
from .store import RunStore, SCHEMA_VERSION
from .util import (
    InputError,
    StateError,
    require_string,
    sanitize_string_list,
    sanitize_text,
    utc_now,
)


TERMINAL_PHASES = {
    "complete",
    "needs_human",
    "blocked",
    "failed",
    "interrupted",
}
RESOLUTION_VALUES = {"accepted", "rejected", "unverified"}


def _runtime_version() -> str:
    manifest = Path(__file__).resolve().parents[2] / ".codex-plugin" / "plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    version = value.get("version") if isinstance(value, dict) else None
    return version if isinstance(version, str) and version else "unknown"


def _runtime_version_order(value: str) -> tuple[int, int, int, int] | None:
    release, separator, build = value.partition("+codex.")
    parts = release.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    if separator and (len(build) != 14 or not build.isdigit()):
        return None
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch, int(build or "0")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


class HarnessService:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = dict(environ) if environ is not None else None
        self._lock = threading.RLock()
        self._stages: dict[tuple[str, str, str], ManagedStage] = {}

    def _effective_environ(self) -> Mapping[str, str] | None:
        return self.environ

    @staticmethod
    def _workspace(arguments: Mapping[str, Any]) -> str:
        return require_string(
            arguments.get("workspace"), "workspace", maximum=4_096
        )

    @staticmethod
    def _run_id(arguments: Mapping[str, Any]) -> str:
        return require_string(arguments.get("run_id"), "run_id", maximum=128)

    @staticmethod
    def _campaign_id(arguments: Mapping[str, Any]) -> str:
        return require_string(
            arguments.get("campaign_id"), "campaign_id", maximum=128
        )

    @staticmethod
    def _transition(state: dict[str, Any], phase: str) -> None:
        if phase == state.get("phase"):
            return
        state["phase"] = phase
        state.setdefault("phase_history", []).append(
            {"phase": phase, "at": utc_now()}
        )

    @staticmethod
    def _public_state(
        contract: dict[str, Any],
        state: dict[str, Any],
        review: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contract": contract,
            "state": state,
            "review": review,
        }

    @staticmethod
    def _stage_key(store: RunStore, run_id: str, stage_id: str) -> tuple[str, str, str]:
        return (str(store.context.git_dir), run_id, stage_id)

    def _recover_interrupted(
        self, store: RunStore, run_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        changed = False
        interrupted_stage_ids: list[str] = []
        for stage_id, stage in state.get("stages", {}).items():
            if stage.get("lifecycle_state") != "running":
                continue
            if self._stage_key(store, run_id, stage_id) in self._stages:
                continue
            stage["lifecycle_state"] = "interrupted"
            stage["finished_at"] = utc_now()
            stage["error"] = "MCP server restarted during model inference"
            changed = True
            interrupted_stage_ids.append(stage_id)
        if changed:
            self._transition(state, "interrupted")
            state["terminal"] = {
                "status": "interrupted",
                "at": utc_now(),
                "summary": "An in-flight model stage cannot be resumed safely",
                "source": "stage_recovery",
                "stage_ids": interrupted_stage_ids,
            }
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {
                    "type": "run_interrupted",
                    "reason": "server_restart_during_inference",
                },
            )
            contract = store.read_contract(run_id)
            for stage_id in interrupted_stage_ids:
                self._record_campaign_provider_terminal(
                    contract,
                    run_id,
                    stage_id,
                    {"lifecycle_state": "interrupted"},
                )
        elif (
            state.get("phase") not in TERMINAL_PHASES
            and isinstance(state.get("terminal"), dict)
            and state["terminal"].get("status") == "interrupted"
            and state["terminal"].get("source") == "stage_recovery"
        ):
            state["terminal"] = None
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {"type": "stale_interruption_cleared"},
            )
        return state

    @staticmethod
    def _require_open(state: Mapping[str, Any]) -> None:
        if state.get("phase") in TERMINAL_PHASES:
            raise StateError(f"run is already terminal: {state.get('phase')}")

    @staticmethod
    def _check_results_for_current(state: Mapping[str, Any]) -> dict[str, Any]:
        fingerprint = state.get("diff_fingerprint")
        if not isinstance(fingerprint, str):
            return {}
        all_results = state.get("check_results", {})
        if not isinstance(all_results, dict):
            return {}
        results = all_results.get(fingerprint, {})
        return results if isinstance(results, dict) else {}

    @classmethod
    def _check_gate(cls, state: Mapping[str, Any]) -> tuple[bool, list[str]]:
        planned = state.get("planned_checks", [])
        results = cls._check_results_for_current(state)
        missing_or_failed: list[str] = []
        for check in planned if isinstance(planned, list) else []:
            name = check.get("name") if isinstance(check, dict) else None
            result = results.get(name) if isinstance(name, str) else None
            if not isinstance(result, dict) or result.get("status") != "passed":
                if isinstance(name, str):
                    missing_or_failed.append(name)
        return bool(planned) and not missing_or_failed, missing_or_failed

    @staticmethod
    def _codex_fallback_allowed(state: Mapping[str, Any]) -> bool:
        return any(
            isinstance(stage, dict)
            and stage.get("profile") == "critic"
            and stage.get("lifecycle_state") == "failed"
            and stage.get("failure_kind") == "anthropic_limit"
            for stage in state.get("stages", {}).values()
        )

    def check_runtime(self, _arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return claude_runtime.check_runtime(self._effective_environ())

    def _prepare_campaign_run(
        self,
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any] | None:
        raw = arguments.get("campaign")
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or set(raw) != {
            "workspace",
            "campaign_id",
            "task_id",
        }:
            raise InputError(
                "campaign must contain exactly workspace, campaign_id, and task_id"
            )
        campaign_workspace = require_string(
            raw.get("workspace"), "campaign.workspace", maximum=4_096
        )
        campaign_id = require_string(
            raw.get("campaign_id"), "campaign.campaign_id", maximum=128
        )
        task_id = require_string(
            raw.get("task_id"), "campaign.task_id", maximum=80
        )
        store = CampaignStore.for_workspace(campaign_workspace)
        campaign_contract = store.read_contract(campaign_id)
        campaign_state = store.read_state(campaign_id)
        self._require_campaign_open(campaign_state)
        definition = self._campaign_task_definition(campaign_contract, task_id)
        if definition.get("kind") != "implementation":
            raise StateError("campaign-linked run requires an implementation task")
        task_state = campaign_state.get("tasks", {}).get(task_id)
        if not isinstance(task_state, dict) or task_state.get("status") != "in_progress":
            raise StateError(
                "campaign task must be in_progress before its run is created"
            )
        expected_workspace = definition.get("workspace")
        if not isinstance(expected_workspace, str) or (
            Path(expected_workspace).resolve() != context.repo_root
        ):
            raise StateError("campaign task workspace does not match run workspace")

        base_from_task = definition.get("base_from_task")
        if isinstance(base_from_task, str):
            predecessor = campaign_state.get("tasks", {}).get(base_from_task)
            predecessor_run = (
                predecessor.get("run") if isinstance(predecessor, dict) else None
            )
            expected_base = (
                predecessor_run.get("head_sha")
                if isinstance(predecessor_run, dict)
                else None
            )
            if not isinstance(expected_base, str):
                raise StateError(
                    "base_from_task requires a completed committed predecessor"
                )
        else:
            expected_base = definition.get("base_sha")
        if not isinstance(expected_base, str):
            raise StateError("campaign task has no resolved base_sha")
        supplied_base = arguments.get("base_sha")
        if isinstance(supplied_base, str) and not expected_base.lower().startswith(
            supplied_base.lower()
        ) and not supplied_base.lower().startswith(expected_base.lower()):
            raise InputError("run base_sha conflicts with the campaign task")
        arguments["base_sha"] = expected_base

        campaign_risk = validate_risk(campaign_contract.get("risk", "high"))
        requested_risk = validate_risk(arguments.get("risk", "medium"))
        if RISK_RANK[requested_risk] < RISK_RANK[campaign_risk]:
            arguments["risk"] = campaign_risk
        task_budget = definition.get("review_budget")
        supplied_budget = arguments.get("review_budget")
        if isinstance(task_budget, Mapping):
            normalized_task_budget = normalize_review_budget(task_budget)
            if supplied_budget is not None and normalize_review_budget(
                supplied_budget
            ) != normalized_task_budget:
                raise InputError(
                    "run review_budget conflicts with the campaign task"
                )
            arguments["review_budget"] = normalized_task_budget
            arguments["_review_budget_mode"] = (
                "report_only"
                if definition.get("role") in {"integration", "finalizer"}
                else "advisory"
            )
        else:
            if supplied_budget is not None:
                raise InputError(
                    "a legacy campaign task cannot acquire an advisory review_budget"
                )
            arguments["review_budget"] = normalize_review_budget(None)
            arguments["_review_budget_mode"] = "legacy_report_only"
        return {
            "reference": {
                "workspace": str(store.context.repo_root),
                "campaign_id": campaign_id,
                "task_id": task_id,
            },
            "store": store,
            "contract": campaign_contract,
            "state": campaign_state,
        }

    def _adopt_campaign_runtime(
        self,
        store: CampaignStore,
        contract: Mapping[str, Any],
        _state: Mapping[str, Any],
        task_id: str,
        version: str,
    ) -> None:
        campaign_id = require_string(
            contract.get("campaign_id"), "campaign_id", maximum=128
        )
        state = store.read_state(campaign_id)
        history = state.setdefault("runtime_versions", [])
        if not isinstance(history, list):
            raise StateError("campaign runtime version history is corrupt")
        current = history[-1].get("version") if history else contract.get(
            "runtime_version"
        )
        if current == version:
            return
        if version == "unknown":
            raise StateError("campaign plugin runtime version is unknown")
        current_order = (
            _runtime_version_order(current) if isinstance(current, str) else None
        )
        version_order = _runtime_version_order(version)
        if (
            current_order is not None
            and version_order is not None
            and version_order < current_order
        ):
            raise StateError("campaign plugin runtime cannot be downgraded")
        tasks = state.get("tasks", {})
        active = [
            other_id
            for other_id, task in tasks.items()
            if other_id != task_id
            and isinstance(task, dict)
            and task.get("status") == "in_progress"
        ]
        circuit = state.get("provider_circuits", {}).get("anthropic", {})
        if active or (isinstance(circuit, dict) and circuit.get("probe")):
            raise StateError(
                "plugin runtime may change only between task waves with no active probe"
            )
        entry = {
            "version": version,
            "at": utc_now(),
            "reason": (
                "safe_upgrade"
                if current_order is not None and version_order is not None
                else "safe_checkpoint_unordered"
            ),
        }
        history.append(entry)
        store.save_state(campaign_id, state)
        store.append_event(
            campaign_id,
            {"type": "runtime_version_adopted", "version": version},
        )

    def create_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(arguments)
        context = resolve_repo(workspace)
        normalized = dict(arguments)
        parent = self._prepare_campaign_run(normalized, context)
        contract, state = build_contract(normalized, context)
        contract["runtime_version"] = _runtime_version()
        if parent is not None:
            contract["campaign"] = parent["reference"]
        store = RunStore(context)
        with self._lock:
            if parent is not None:
                self._adopt_campaign_runtime(
                    parent["store"],
                    parent["contract"],
                    parent["state"],
                    parent["reference"]["task_id"],
                    contract["runtime_version"],
                )
            store.create(contract, state)
            store.append_event(
                contract["run_id"],
                {"type": "phase_changed", "from": "prepared", "to": "writing"},
            )
        return self._public_state(
            contract,
            state,
            store.read_review(contract["run_id"]),
        )

    def get_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        with self._lock:
            contract = store.read_contract(run_id)
            state = self._recover_interrupted(
                store, run_id, store.read_state(run_id)
            )
            review = store.read_review(run_id)
        return self._public_state(contract, state, review)

    def list_runs(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        raw_limit = arguments.get("limit", 25)
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
            raise InputError("limit must be an integer")
        limit = max(1, min(raw_limit, 100))
        runs: list[dict[str, Any]] = []
        with self._lock:
            for run_id in list(store.list_run_ids())[:limit]:
                try:
                    contract = store.read_contract(run_id)
                    state = self._recover_interrupted(
                        store, run_id, store.read_state(run_id)
                    )
                except StateError as exc:
                    runs.append({"run_id": run_id, "error": str(exc)})
                    continue
                runs.append(
                    {
                        "run_id": run_id,
                        "goal": contract.get("goal"),
                        "phase": state.get("phase"),
                        "risk": state.get("risk"),
                        "writer": state.get("writer"),
                        "created_at": state.get("created_at"),
                        "updated_at": state.get("updated_at"),
                    }
                )
        return {"runs": runs}

    @staticmethod
    def _measure_current_diff(
        store: RunStore,
        contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        fingerprint, paths = diff_fingerprint(
            store.context,
            base_sha=str(contract["base_sha"]),
        )
        statistics = diff_stats(
            store.context,
            base_sha=str(contract["base_sha"]),
        )
        return {
            "diff_fingerprint": fingerprint,
            "changed_paths": paths,
            "diff_stats": statistics,
            "review_budget": effective_review_budget(contract),
            "budget_status": review_budget_status(contract, statistics),
        }

    def measure_diff(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Measure the current diff without changing persisted run state."""

        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        with self._lock:
            contract = store.read_contract(run_id)
            measurement = self._measure_current_diff(store, contract)
        return {"run_id": run_id, **measurement}

    @staticmethod
    def _public_campaign(
        contract: dict[str, Any],
        state: dict[str, Any],
        comparison: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contract": contract,
            "state": state,
            "comparison": comparison,
        }

    @staticmethod
    def _require_campaign_open(state: Mapping[str, Any]) -> None:
        if state.get("phase") in CAMPAIGN_TERMINAL_PHASES:
            raise StateError(
                f"campaign is already terminal: {state.get('phase')}"
            )

    def create_campaign(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(arguments)
        context = resolve_repo(workspace)
        contract, state, spec_files = build_campaign(dict(arguments), context)
        version = _runtime_version()
        contract["runtime_version"] = version
        state["runtime_versions"] = [
            {"version": version, "at": state["created_at"], "reason": "created"}
        ]
        store = CampaignStore(context)
        with self._lock:
            store.create(contract, state, spec_files)
            store.append_event(
                contract["campaign_id"],
                {"type": "phase_changed", "from": "prepared", "to": "executing"},
            )
        return self._public_campaign(
            contract,
            state,
            store.read_comparison(contract["campaign_id"]),
        )

    def get_campaign(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = CampaignStore.for_workspace(self._workspace(arguments))
        campaign_id = self._campaign_id(arguments)
        with self._lock:
            contract = store.read_contract(campaign_id)
            state = store.read_state(campaign_id)
            comparison = store.read_comparison(campaign_id)
        return self._public_campaign(contract, state, comparison)

    def list_campaigns(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = CampaignStore.for_workspace(self._workspace(arguments))
        raw_limit = arguments.get("limit", 25)
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
            raise InputError("limit must be an integer")
        limit = max(1, min(raw_limit, 100))
        campaigns: list[dict[str, Any]] = []
        with self._lock:
            for campaign_id in list(store.list_campaign_ids())[:limit]:
                try:
                    contract = store.read_contract(campaign_id)
                    state = store.read_state(campaign_id)
                except StateError as exc:
                    campaigns.append(
                        {"campaign_id": campaign_id, "error": str(exc)}
                    )
                    continue
                task_states = state.get("tasks", {})
                campaigns.append(
                    {
                        "campaign_id": campaign_id,
                        "title": contract.get("title"),
                        "mode": contract.get("mode"),
                        "phase": state.get("phase"),
                        "task_count": len(task_states)
                        if isinstance(task_states, dict)
                        else 0,
                        "completed_tasks": sum(
                            1
                            for task in task_states.values()
                            if isinstance(task, dict)
                            and task.get("status") == "complete"
                        )
                        if isinstance(task_states, dict)
                        else 0,
                        "created_at": state.get("created_at"),
                        "updated_at": state.get("updated_at"),
                    }
                )
        return {"campaigns": campaigns}

    @staticmethod
    def _campaign_task_definition(
        contract: Mapping[str, Any], task_id: str
    ) -> dict[str, Any]:
        for task in contract.get("tasks", []):
            if isinstance(task, dict) and task.get("id") == task_id:
                return task
        raise StateError(f"unknown campaign task: {task_id}")

    @staticmethod
    def _completed_run_reference(
        workspace: str,
        run_id: str,
        definition: Mapping[str, Any],
        task_states: Mapping[str, Any],
        task_id: str,
        campaign_contract: Mapping[str, Any],
        campaign_id: str,
    ) -> dict[str, Any]:
        run_store = RunStore.for_workspace(workspace)
        run_contract = run_store.read_contract(run_id)
        run_state = run_store.read_state(run_id)
        terminal = run_state.get("terminal")
        if run_state.get("phase") != "complete" or not (
            isinstance(terminal, dict) and terminal.get("status") == "complete"
        ):
            raise StateError("implementation task requires a completed v1 run")

        contract_repo_root = run_contract.get("repo_root")
        if not isinstance(contract_repo_root, str) or (
            Path(contract_repo_root).resolve() != run_store.context.repo_root
        ):
            raise StateError("v1 run workspace does not match run_workspace")

        expected_workspace = definition.get("workspace")
        if not isinstance(expected_workspace, str) or (
            Path(expected_workspace).resolve() != run_store.context.repo_root
        ):
            raise StateError(
                "v1 run workspace does not match the campaign task contract"
            )

        base_from_task = definition.get("base_from_task")
        if isinstance(base_from_task, str):
            predecessor = task_states.get(base_from_task)
            predecessor_run = (
                predecessor.get("run") if isinstance(predecessor, dict) else None
            )
            expected_base = (
                predecessor_run.get("head_sha")
                if isinstance(predecessor_run, dict)
                else None
            )
        else:
            expected_base = definition.get("base_sha")
        actual_base = run_contract.get("base_sha")
        if not isinstance(expected_base, str) or not isinstance(actual_base, str):
            raise StateError("campaign task and v1 run require a base_sha")
        if not actual_base.lower().startswith(expected_base.lower()):
            raise StateError(
                "v1 run base_sha does not match the campaign task contract"
            )

        campaign_risk = validate_risk(campaign_contract.get("risk", "high"))
        run_risk = validate_risk(run_state.get("risk"), "v1 run risk")
        if RISK_RANK[run_risk] < RISK_RANK[campaign_risk]:
            raise StateError("v1 run risk is lower than campaign risk")

        parent = run_contract.get("campaign")
        if isinstance(parent, Mapping) and (
            parent.get("campaign_id") != campaign_id
            or parent.get("task_id") != task_id
        ):
            raise StateError("v1 run is linked to another campaign task")

        for other_task_id, other_state in task_states.items():
            if other_task_id == task_id or not isinstance(other_state, dict):
                continue
            other_run = other_state.get("run")
            if isinstance(other_run, dict) and other_run.get("run_id") == run_id:
                raise StateError(
                    f"v1 run is already linked to campaign task {other_task_id}"
                )

        fingerprint = run_state.get("diff_fingerprint")
        if not isinstance(fingerprint, str):
            raise StateError("completed v1 run is missing its diff fingerprint")
        head_sha = run_store.context.head_sha
        is_predecessor = any(
            isinstance(task, Mapping)
            and task.get("base_from_task") == task_id
            for task in campaign_contract.get("tasks", [])
        )
        if is_predecessor:
            if status_snapshot(run_store.context).get("dirty"):
                raise StateError(
                    "a base_from_task predecessor must end in a clean atomic commit"
                )
            if head_sha == actual_base:
                raise StateError(
                    "a base_from_task predecessor must advance HEAD with a commit"
                )
        return {
            "workspace": str(run_store.context.repo_root),
            "run_id": run_id,
            "base_sha": actual_base,
            "head_sha": head_sha,
            "diff_fingerprint": fingerprint,
            "risk": run_risk,
            "runtime_version": run_contract.get("runtime_version", "unknown"),
            "changed_paths": list(run_state.get("changed_paths", [])),
            "diff_stats": run_state.get("diff_stats"),
            "budget_status": run_state.get("budget_status"),
        }

    @staticmethod
    def _candidate_git_snapshots(
        contract: Mapping[str, Any],
        task_states: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        primary_workspace = require_string(
            contract.get("repo_root"), "campaign.repo_root", maximum=4_096
        )
        primary_base = require_string(
            contract.get("base_sha"), "campaign.base_sha", maximum=64
        )
        repositories: dict[str, dict[str, Any]] = {
            str(Path(primary_workspace).resolve()): {
                "base_sha": primary_base,
                "covered_paths": set(),
            }
        }

        for task_state in task_states.values():
            if not isinstance(task_state, dict):
                continue
            run = task_state.get("run")
            if not isinstance(run, dict):
                continue
            workspace = require_string(
                run.get("workspace"), "campaign task run workspace", maximum=4_096
            )
            base_sha = require_string(
                run.get("base_sha"), "campaign task run base_sha", maximum=64
            )
            key = str(Path(workspace).resolve())
            repository = repositories.setdefault(
                key,
                {"base_sha": base_sha, "covered_paths": set()},
            )
            changed = run.get("changed_paths", [])
            if not isinstance(changed, list) or not all(
                isinstance(path, str) for path in changed
            ):
                raise StateError("campaign task run changed_paths are corrupt")
            repository["covered_paths"].update(changed)

        snapshots: list[dict[str, Any]] = []
        for workspace, repository in repositories.items():
            context = resolve_repo(workspace)
            fingerprint, changed = diff_fingerprint(
                context,
                base_sha=str(repository["base_sha"]),
            )
            uncovered = sorted(
                set(changed) - set(repository["covered_paths"])
            )
            if uncovered:
                raise StateError(
                    "candidate contains paths not covered by completed v1 runs in "
                    f"{workspace}: {', '.join(uncovered[:8])}"
                )
            snapshots.append(
                {
                    "workspace": workspace,
                    "base_sha": repository["base_sha"],
                    "head_sha": context.head_sha,
                    "diff_fingerprint": fingerprint,
                    "changed_paths": changed,
                }
            )
        return snapshots

    def record_campaign_task(
        self, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        store = CampaignStore.for_workspace(self._workspace(arguments))
        campaign_id = self._campaign_id(arguments)
        task_id = require_string(arguments.get("task_id"), "task_id", maximum=80)
        status = require_string(arguments.get("status"), "status", maximum=32)
        if status not in TASK_STATUSES - {"pending"}:
            raise InputError(
                "status must be in_progress, complete, needs_human, blocked, failed, or interrupted"
            )
        summary = sanitize_text(arguments.get("summary"), maximum=2_000)
        with self._lock:
            contract = store.read_contract(campaign_id)
            state = store.read_state(campaign_id)
            definition = self._campaign_task_definition(contract, task_id)
            task_states = state.get("tasks")
            if not isinstance(task_states, dict):
                raise StateError("campaign task state is corrupt")
            task_state = task_states.get(task_id)
            if not isinstance(task_state, dict):
                raise StateError(f"missing campaign task state: {task_id}")
            current = task_state.get("status")
            if current == status:
                return {
                    "campaign_id": campaign_id,
                    "task_id": task_id,
                    "task": task_state,
                    "deduplicated": True,
                }
            self._require_campaign_open(state)
            if status == "needs_human" and not self._unresolved_campaign_blockers(
                state
            ):
                raise StateError(
                    "needs_human requires an unresolved blocking human intervention; "
                    "use blocked or interrupted for operational failures"
                )
            allowed = {
                "pending": TASK_STATUSES - {"pending"},
                "in_progress": TASK_STATUSES - {"pending"},
                "needs_human": {"in_progress", "complete", "blocked", "failed"},
                "blocked": {"in_progress"},
                "failed": {"in_progress"},
                "interrupted": {"in_progress"},
            }
            if status not in allowed.get(str(current), set()):
                raise StateError(
                    f"campaign task cannot transition from {current} to {status}"
                )
            if status in {"in_progress", "complete"}:
                verify_openspec_reference(
                    contract,
                    store.context,
                    store.spec_dir(campaign_id),
                )
                outstanding_dependencies = [
                    dependency
                    for dependency in definition.get("dependencies", [])
                    if not (
                        isinstance(task_states.get(dependency), dict)
                        and task_states[dependency].get("status") == "complete"
                    )
                ]
                if outstanding_dependencies:
                    raise StateError(
                        "campaign task dependencies are not complete: "
                        + ", ".join(outstanding_dependencies)
                    )

            run_reference = None
            if status == "complete" and definition.get("kind") == "implementation":
                run_workspace = require_string(
                    arguments.get("run_workspace"),
                    "run_workspace",
                    maximum=4_096,
                )
                run_id = self._run_id(arguments)
                run_reference = self._completed_run_reference(
                    run_workspace,
                    run_id,
                    definition,
                    task_states,
                    task_id,
                    contract,
                    campaign_id,
                )
            elif arguments.get("run_workspace") is not None or arguments.get(
                "run_id"
            ) is not None:
                raise InputError(
                    "run_workspace and run_id are only valid for a completed implementation task"
                )

            task_state["status"] = status
            task_state["summary"] = summary
            task_state["run"] = run_reference
            task_state["updated_at"] = utc_now()
            transition_counts = state.setdefault("task_transition_counts", {})
            if not isinstance(transition_counts, dict):
                raise StateError("campaign task transition counts are corrupt")
            transition_counts[status] = int(transition_counts.get(status, 0)) + 1
            state = store.save_state(campaign_id, state)
            store.append_event(
                campaign_id,
                {
                    "type": "campaign_task_recorded",
                    "task_id": task_id,
                    "status": status,
                    "run_id": run_reference.get("run_id")
                    if isinstance(run_reference, dict)
                    else None,
                },
            )
        return {
            "campaign_id": campaign_id,
            "task_id": task_id,
            "task": state["tasks"][task_id],
            "deduplicated": False,
        }

    def record_campaign_intervention(
        self, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        store = CampaignStore.for_workspace(self._workspace(arguments))
        campaign_id = self._campaign_id(arguments)
        intervention_id = require_string(
            arguments.get("intervention_id"), "intervention_id", maximum=80
        )
        kind = require_string(arguments.get("kind"), "kind", maximum=32)
        if kind not in {
            "blocking_question",
            "approval",
            "correction",
            "context",
            "external_unblock",
        }:
            raise InputError(
                "kind must be blocking_question, approval, correction, context, "
                "or external_unblock"
            )
        blocking = arguments.get("blocking", False)
        resolved = arguments.get("resolved", False)
        if not isinstance(blocking, bool) or not isinstance(resolved, bool):
            raise InputError("blocking and resolved must be booleans")
        reason = sanitize_text(
            require_string(arguments.get("reason"), "reason", maximum=2_000),
            maximum=2_000,
        )
        outcome = sanitize_text(arguments.get("outcome"), maximum=2_000)
        if resolved and not outcome:
            raise InputError("a resolved intervention requires an outcome summary")
        normalized = {
            "intervention_id": intervention_id,
            "kind": kind,
            "blocking": blocking,
            "resolved": resolved,
            "reason": reason,
            "outcome": outcome,
        }
        with self._lock:
            state = store.read_state(campaign_id)
            interventions = state.get("interventions")
            if not isinstance(interventions, dict):
                raise StateError("campaign interventions are corrupt")
            existing = interventions.get(intervention_id)
            if isinstance(existing, dict):
                comparable = {key: existing.get(key) for key in normalized}
                if comparable == normalized:
                    return {
                        "campaign_id": campaign_id,
                        "intervention": existing,
                        "deduplicated": True,
                    }
                self._require_campaign_open(state)
                immutable = ("intervention_id", "kind", "blocking", "reason")
                if (
                    all(existing.get(key) == normalized[key] for key in immutable)
                    and existing.get("resolved") is False
                    and resolved is True
                ):
                    existing["resolved"] = True
                    existing["outcome"] = outcome
                    existing["resolved_at"] = utc_now()
                    state = store.save_state(campaign_id, state)
                    store.append_event(
                        campaign_id,
                        {
                            "type": "campaign_intervention_resolved",
                            "intervention_id": intervention_id,
                        },
                    )
                    return {
                        "campaign_id": campaign_id,
                        "intervention": state["interventions"][intervention_id],
                        "deduplicated": False,
                    }
                raise StateError(f"intervention_id already exists: {intervention_id}")
            self._require_campaign_open(state)
            entry = {**normalized, "recorded_at": utc_now()}
            interventions[intervention_id] = entry
            state = store.save_state(campaign_id, state)
            store.append_event(
                campaign_id,
                {
                    "type": "campaign_intervention_recorded",
                    "intervention_id": intervention_id,
                    "kind": kind,
                    "blocking": blocking,
                    "resolved": resolved,
                },
            )
        return {
            "campaign_id": campaign_id,
            "intervention": state["interventions"][intervention_id],
            "deduplicated": False,
        }

    @staticmethod
    def _unresolved_campaign_blockers(state: Mapping[str, Any]) -> list[str]:
        interventions = state.get("interventions", {})
        if not isinstance(interventions, dict):
            return ["campaign interventions are corrupt"]
        return [
            intervention_id
            for intervention_id, intervention in interventions.items()
            if isinstance(intervention, dict)
            and intervention.get("blocking") is True
            and intervention.get("resolved") is not True
        ]

    @staticmethod
    def _integration_gaps(
        contract: Mapping[str, Any], state: Mapping[str, Any]
    ) -> list[str]:
        if contract.get("integration_policy") != "combined-review-required":
            return []
        definitions = [
            task
            for task in contract.get("tasks", [])
            if isinstance(task, Mapping) and task.get("kind") == "implementation"
        ]
        task_states = state.get("tasks", {})
        if not isinstance(task_states, Mapping):
            return ["campaign task state is corrupt"]
        repositories = {
            str(task.get("repository_key") or task.get("workspace"))
            for task in definitions
            if task.get("role", "task") == "task"
        }
        gaps: list[str] = []
        for repository in sorted(repositories):
            feature_ids = {
                str(task.get("id"))
                for task in definitions
                if task.get("role", "task") == "task"
                and str(task.get("repository_key") or task.get("workspace"))
                == repository
            }
            if len(feature_ids) < 2:
                continue
            integrations = [
                task
                for task in definitions
                if task.get("role") == "integration"
                and str(task.get("repository_key") or task.get("workspace"))
                == repository
                and feature_ids.issubset(set(task.get("dependencies", [])))
            ]
            valid = False
            feature_paths: set[str] = set()
            for feature_id in feature_ids:
                feature_state = task_states.get(feature_id)
                feature_run = (
                    feature_state.get("run")
                    if isinstance(feature_state, Mapping)
                    else None
                )
                if isinstance(feature_run, Mapping):
                    feature_paths.update(feature_run.get("changed_paths", []))
            for integration in integrations:
                integration_state = task_states.get(str(integration.get("id")))
                integration_run = (
                    integration_state.get("run")
                    if isinstance(integration_state, Mapping)
                    else None
                )
                if not isinstance(integration_run, Mapping):
                    continue
                covered = set(integration_run.get("changed_paths", []))
                if feature_paths.issubset(covered):
                    valid = True
                    break
            if not valid:
                gaps.append(repository)
        return gaps

    def seal_campaign_candidate(
        self, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        store = CampaignStore.for_workspace(self._workspace(arguments))
        campaign_id = self._campaign_id(arguments)
        summary = sanitize_text(
            require_string(arguments.get("summary"), "summary", maximum=4_000),
            maximum=4_000,
        )
        with self._lock:
            contract = store.read_contract(campaign_id)
            state = store.read_state(campaign_id)
            existing = state.get("candidate")
            if isinstance(existing, dict):
                if existing.get("summary") != summary:
                    raise StateError("campaign candidate is already sealed")
                return {
                    "campaign_id": campaign_id,
                    "candidate": existing,
                    "deduplicated": True,
                }
            self._require_campaign_open(state)
            verify_openspec_reference(
                contract,
                store.context,
                store.spec_dir(campaign_id),
            )
            task_states = state.get("tasks")
            if not isinstance(task_states, dict):
                raise StateError("campaign task state is corrupt")
            incomplete = [
                task_id
                for task_id, task in task_states.items()
                if not (
                    isinstance(task, dict) and task.get("status") == "complete"
                )
            ]
            if incomplete:
                raise StateError(
                    "candidate cannot be sealed before all tasks complete: "
                    + ", ".join(incomplete)
                )
            unresolved = self._unresolved_campaign_blockers(state)
            if unresolved:
                raise StateError(
                    "candidate has unresolved blocking interventions: "
                    + ", ".join(unresolved)
                )
            integration_gaps = self._integration_gaps(contract, state)
            if integration_gaps:
                raise StateError(
                    "multi-task repositories require a completed combined "
                    "integration run: " + ", ".join(integration_gaps)
                )
            interventions = state.get("interventions", {})
            run_references = [
                task["run"]
                for task in task_states.values()
                if isinstance(task, dict) and isinstance(task.get("run"), dict)
            ]
            git_snapshots = self._candidate_git_snapshots(
                contract,
                task_states,
            )
            candidate = {
                "summary": summary,
                "sealed_at": utc_now(),
                "readiness": (
                    "pending_evaluation"
                    if contract.get("mode") == "replay"
                    else "ready"
                ),
                "task_count": len(task_states),
                "run_references": run_references,
                "git_snapshots": git_snapshots,
                "human_interventions": {
                    "total": len(interventions),
                    "blocking_questions": sum(
                        1
                        for item in interventions.values()
                        if isinstance(item, dict)
                        and item.get("kind") == "blocking_question"
                    ),
                    "corrections": sum(
                        1
                        for item in interventions.values()
                        if isinstance(item, dict)
                        and item.get("kind") == "correction"
                    ),
                    "external_unblocks": sum(
                        1
                        for item in interventions.values()
                        if isinstance(item, dict)
                        and item.get("kind") == "external_unblock"
                    ),
                },
                "operational_task_transitions": dict(
                    state.get("task_transition_counts", {})
                ),
            }
            state["candidate"] = candidate
            self._transition(state, "candidate_ready")
            state = store.save_state(campaign_id, state)
            store.append_event(
                campaign_id,
                {
                    "type": "campaign_candidate_sealed",
                    "task_count": len(task_states),
                    "intervention_count": len(interventions),
                },
            )
        return {
            "campaign_id": campaign_id,
            "candidate": state["candidate"],
            "deduplicated": False,
        }

    def record_campaign_comparison(
        self, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        store = CampaignStore.for_workspace(self._workspace(arguments))
        campaign_id = self._campaign_id(arguments)
        def normalize_rubric(name: str) -> dict[str, int]:
            raw = arguments.get(name)
            if not isinstance(raw, Mapping):
                raise InputError(f"{name} must be an object")
            if set(raw) != set(COMPARISON_DIMENSIONS):
                raise InputError(
                    f"{name} must contain exactly: "
                    + ", ".join(COMPARISON_DIMENSIONS)
                )
            result: dict[str, int] = {}
            for dimension in COMPARISON_DIMENSIONS:
                value = raw.get(dimension)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 0 <= value <= 4
                ):
                    raise InputError(
                        f"{name}.{dimension} must be an integer from 0 to 4"
                    )
                result[dimension] = value
            return result

        rubric = normalize_rubric("rubric")
        cutoff_rubric = normalize_rubric("cutoff_rubric")
        candidate_readiness = require_string(
            arguments.get("candidate_readiness"),
            "candidate_readiness",
            maximum=32,
        )
        if candidate_readiness not in {"unsafe", "partial", "ready"}:
            raise InputError(
                "candidate_readiness must be unsafe, partial, or ready"
            )
        raw_attribution = arguments.get("gap_attribution", [])
        if not isinstance(raw_attribution, list) or len(raw_attribution) > 64:
            raise InputError("gap_attribution must contain at most 64 entries")
        gap_attribution: list[dict[str, str]] = []
        for index, raw in enumerate(raw_attribution):
            if not isinstance(raw, Mapping) or set(raw) != {"gap", "category"}:
                raise InputError(
                    f"gap_attribution[{index}] must contain gap and category"
                )
            category = require_string(
                raw.get("category"),
                f"gap_attribution[{index}].category",
                maximum=32,
            )
            if category not in {
                "derivable_miss",
                "underspecified",
                "historical_only",
                "intentional_alternative",
            }:
                raise InputError(f"gap_attribution[{index}].category is invalid")
            gap_attribution.append(
                {
                    "gap": sanitize_text(
                        require_string(
                            raw.get("gap"),
                            f"gap_attribution[{index}].gap",
                            maximum=2_000,
                        ),
                        maximum=2_000,
                    ),
                    "category": category,
                }
            )
        normalized = {
            "rubric": rubric,
            "cutoff_rubric": cutoff_rubric,
            "candidate_readiness": candidate_readiness,
            "gap_attribution": gap_attribution,
            "similarities": sanitize_string_list(
                arguments.get("similarities"), item_maximum=2_000
            ),
            "differences": sanitize_string_list(
                arguments.get("differences"), item_maximum=2_000
            ),
            "residual_risks": sanitize_string_list(
                arguments.get("residual_risks"), item_maximum=2_000
            ),
            "historical_refs": sanitize_string_list(
                arguments.get("historical_refs"), item_maximum=1_000
            ),
        }
        with self._lock:
            contract = store.read_contract(campaign_id)
            state = store.read_state(campaign_id)
            comparison_file = store.read_comparison(campaign_id)
            existing = comparison_file.get("comparison")
            if isinstance(existing, dict):
                comparable = {key: existing.get(key) for key in normalized}
                if comparable != normalized:
                    raise StateError("campaign comparison is already recorded")
                return {
                    "campaign_id": campaign_id,
                    "comparison": existing,
                    "deduplicated": True,
                }
            self._require_campaign_open(state)
            if contract.get("mode") != "replay":
                raise StateError("historical comparison is only valid for replay campaigns")
            if state.get("phase") != "candidate_ready" or not isinstance(
                state.get("candidate"), dict
            ):
                raise StateError(
                    "seal the replay candidate before reading or recording historical evidence"
                )
            comparison = {
                "recorded_at": utc_now(),
                **normalized,
                "overall_percent": round(
                    sum(rubric.values()) / (4 * len(COMPARISON_DIMENSIONS)) * 100
                ),
                "historical_similarity_percent": round(
                    sum(rubric.values()) / (4 * len(COMPARISON_DIMENSIONS)) * 100
                ),
                "cutoff_fidelity_percent": round(
                    sum(cutoff_rubric.values())
                    / (4 * len(COMPARISON_DIMENSIONS))
                    * 100
                ),
            }
            comparison_file["comparison"] = comparison
            store.save_comparison(campaign_id, comparison_file)
            self._transition(state, "comparing")
            state = store.save_state(campaign_id, state)
            store.append_event(
                campaign_id,
                {
                    "type": "campaign_comparison_recorded",
                    "overall_percent": comparison["overall_percent"],
                },
            )
        return {
            "campaign_id": campaign_id,
            "comparison": comparison,
            "deduplicated": False,
        }

    def finish_campaign(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = CampaignStore.for_workspace(self._workspace(arguments))
        campaign_id = self._campaign_id(arguments)
        status = require_string(arguments.get("status"), "status", maximum=32)
        if status not in CAMPAIGN_TERMINAL_PHASES:
            raise InputError(
                "status must be complete, needs_human, blocked, failed, or interrupted"
            )
        summary = sanitize_text(arguments.get("summary"), maximum=2_000)
        with self._lock:
            contract = store.read_contract(campaign_id)
            state = store.read_state(campaign_id)
            existing = state.get("terminal")
            if isinstance(existing, dict):
                if existing.get("status") == status:
                    return {
                        "campaign_id": campaign_id,
                        "phase": state.get("phase"),
                        "terminal": existing,
                        "deduplicated": True,
                    }
                raise StateError(
                    f"campaign is already terminal: {existing.get('status')}"
                )
            if status == "complete":
                verify_openspec_reference(
                    contract,
                    store.context,
                    store.spec_dir(campaign_id),
                )
                if not isinstance(state.get("candidate"), dict):
                    raise StateError("campaign candidate is not sealed")
                unresolved = self._unresolved_campaign_blockers(state)
                if unresolved:
                    raise StateError(
                        "campaign has unresolved blocking interventions: "
                        + ", ".join(unresolved)
                    )
                if contract.get("mode") == "replay" and not isinstance(
                    store.read_comparison(campaign_id).get("comparison"), dict
                ):
                    raise StateError("replay campaign requires a historical comparison")
            elif status in {"needs_human", "blocked"}:
                if not summary:
                    raise InputError(f"summary is required for {status}")
                if status == "needs_human" and not self._unresolved_campaign_blockers(
                    state
                ):
                    raise StateError(
                        "needs_human requires an unresolved blocking human intervention; "
                        "use blocked or interrupted for operational failures"
                    )
            self._transition(state, status)
            state["terminal"] = {
                "status": status,
                "campaign_status": (
                    "evaluated"
                    if status == "complete" and contract.get("mode") == "replay"
                    else "locally_ready"
                    if status == "complete"
                    else status
                ),
                "candidate_readiness": (
                    store.read_comparison(campaign_id)
                    .get("comparison", {})
                    .get("candidate_readiness", "unknown")
                    if status == "complete" and contract.get("mode") == "replay"
                    else "ready"
                    if status == "complete"
                    else "unknown"
                ),
                "at": utc_now(),
                "summary": summary or "All local epic campaign gates passed",
                "external_actions_authorized": False,
            }
            state = store.save_state(campaign_id, state)
            store.append_event(
                campaign_id,
                {
                    "type": "campaign_finished",
                    "status": status,
                    "external_actions_authorized": False,
                },
            )
        return {
            "campaign_id": campaign_id,
            "phase": state.get("phase"),
            "terminal": state.get("terminal"),
            "deduplicated": False,
        }

    def plan_checks(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        with self._lock:
            contract = store.read_contract(run_id)
            state = self._recover_interrupted(
                store, run_id, store.read_state(run_id)
            )
            self._require_open(state)
            measurement = self._measure_current_diff(store, contract)
            fingerprint = str(measurement["diff_fingerprint"])
            paths = list(measurement["changed_paths"])
            statistics = dict(measurement["diff_stats"])
            budget_status = str(measurement["budget_status"])
            previous = state.get("diff_fingerprint")
            if previous == fingerprint and isinstance(state.get("diff_stats"), dict):
                return {
                    "run_id": run_id,
                    "diff_fingerprint": fingerprint,
                    "changed_paths": state.get("changed_paths", []),
                    "diff_stats": state.get("diff_stats"),
                    "review_budget": effective_review_budget(contract),
                    "budget_status": state.get("budget_status"),
                    "risk": state.get("risk"),
                    "checks": state.get("planned_checks", []),
                    "deduplicated": True,
                }

            phase = state.get("phase")
            if phase == "reviewing":
                raise StateError(
                    "record review dispositions before changing a reviewed diff"
                )
            if phase == "correcting":
                review_summary = state.get("review_summary") or {}
                reviewed_fingerprint = review_summary.get("diff_fingerprint")
                if reviewed_fingerprint == fingerprint:
                    raise StateError("correction pass has not changed the reviewed diff")
                if state.get("correction_passes", 0) >= contract.get(
                    "max_correction_passes", 1
                ):
                    self._transition(state, "needs_human")
                    state["terminal"] = {
                        "status": "needs_human",
                        "at": utc_now(),
                        "summary": "The single correction pass is exhausted",
                    }
                    store.save_state(run_id, state)
                    store.append_event(
                        run_id,
                        {"type": "correction_limit_exhausted"},
                    )
                    raise StateError("the single correction pass is exhausted")
                state["correction_passes"] = int(
                    state.get("correction_passes", 0)
                ) + 1

            checks, risk, matched = build_check_plan(
                repo_root=store.context.repo_root,
                frozen_checks=list(contract.get("required_checks", [])),
                initial_risk=str(contract.get("risk", "medium")),
                changed_paths=paths,
            )
            state["diff_fingerprint"] = fingerprint
            state["changed_paths"] = paths
            state["diff_stats"] = statistics
            state["budget_status"] = budget_status
            state["planned_checks"] = checks
            state["matched_policy_rules"] = matched
            state["risk"] = risk
            state.setdefault("check_results", {}).setdefault(fingerprint, {})
            self._transition(state, "checking")
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {
                    "type": "checks_planned",
                    "diff_fingerprint": fingerprint,
                    "changed_paths": paths,
                    "check_names": [check["name"] for check in checks],
                    "risk": risk,
                    "matched_policy_rules": matched,
                    "budget_status": budget_status,
                    "production_lines": statistics["production"]["total"],
                },
            )
        return {
            "run_id": run_id,
            "diff_fingerprint": fingerprint,
            "changed_paths": paths,
            "diff_stats": statistics,
            "review_budget": effective_review_budget(contract),
            "budget_status": budget_status,
            "risk": risk,
            "checks": checks,
            "deduplicated": False,
        }

    def record_check(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        check_name = require_string(
            arguments.get("check_name"), "check_name", maximum=80
        )
        exit_code = arguments.get("exit_code")
        duration_ms = arguments.get("duration_ms")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise InputError("exit_code must be an integer")
        if (
            not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or duration_ms < 0
        ):
            raise InputError("duration_ms must be a non-negative integer")
        summary = sanitize_text(arguments.get("summary"), maximum=2_000)
        with self._lock:
            contract = store.read_contract(run_id)
            state = self._recover_interrupted(
                store, run_id, store.read_state(run_id)
            )
            self._require_open(state)
            current, _paths = diff_fingerprint(
                store.context, base_sha=str(contract["base_sha"])
            )
            if current != state.get("diff_fingerprint"):
                raise StateError(
                    "diff changed after checks were planned; call plan_checks again"
                )
            planned = {
                check.get("name"): check
                for check in state.get("planned_checks", [])
                if isinstance(check, dict)
            }
            if check_name not in planned:
                raise StateError(f"check was not in the required plan: {check_name}")
            result = {
                "name": check_name,
                "status": "passed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "summary": summary,
                "recorded_at": utc_now(),
            }
            state.setdefault("check_results", {}).setdefault(current, {})[
                check_name
            ] = result
            self._transition(state, "checking")
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {
                    "type": "check_recorded",
                    "diff_fingerprint": current,
                    "check_name": check_name,
                    "status": result["status"],
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                },
            )
            gate_ok, outstanding = self._check_gate(state)
        return {
            "run_id": run_id,
            "check": result,
            "all_required_checks_passed": gate_ok,
            "outstanding_checks": outstanding,
        }

    @staticmethod
    def _campaign_store_for_run(
        contract: Mapping[str, Any],
    ) -> tuple[CampaignStore, str] | None:
        parent = contract.get("campaign")
        if not isinstance(parent, Mapping):
            return None
        workspace = parent.get("workspace")
        campaign_id = parent.get("campaign_id")
        if not isinstance(workspace, str) or not isinstance(campaign_id, str):
            raise StateError("run campaign reference is corrupt")
        return CampaignStore.for_workspace(workspace), campaign_id

    def _campaign_limit_action(
        self,
        contract: Mapping[str, Any],
        run_id: str,
        stage_id: str,
    ) -> str:
        linked = self._campaign_store_for_run(contract)
        if linked is None:
            return "normal"
        store, campaign_id = linked
        state = store.read_state(campaign_id)
        circuit = state.setdefault("provider_circuits", {}).setdefault(
            "anthropic",
            {
                "status": "closed",
                "cooldown_until": None,
                "probe": None,
                "limit_count": 0,
                "updated_at": utc_now(),
            },
        )
        if not isinstance(circuit, dict) or circuit.get("status") != "open":
            return "normal"
        now = datetime.now(UTC)
        cooldown_until = _parse_timestamp(circuit.get("cooldown_until"))
        if cooldown_until is not None and now < cooldown_until:
            return "skip"
        probe = circuit.get("probe")
        if isinstance(probe, Mapping):
            started_at = _parse_timestamp(probe.get("started_at"))
            if started_at is None or now - started_at < timedelta(hours=4):
                return "skip"
        circuit["probe"] = {
            "run_id": run_id,
            "stage_id": stage_id,
            "started_at": utc_now(),
        }
        circuit["updated_at"] = utc_now()
        store.save_state(campaign_id, state)
        store.append_event(
            campaign_id,
            {"type": "anthropic_probe_started", "run_id": run_id},
        )
        return "probe"

    def _record_campaign_provider_terminal(
        self,
        contract: Mapping[str, Any],
        run_id: str,
        stage_id: str,
        terminal: Mapping[str, Any],
    ) -> None:
        linked = self._campaign_store_for_run(contract)
        if linked is None:
            return
        store, campaign_id = linked
        state = store.read_state(campaign_id)
        circuit = state.setdefault("provider_circuits", {}).setdefault(
            "anthropic",
            {
                "status": "closed",
                "cooldown_until": None,
                "probe": None,
                "limit_count": 0,
                "updated_at": utc_now(),
            },
        )
        if not isinstance(circuit, dict):
            raise StateError("campaign Anthropic circuit is corrupt")
        probe = circuit.get("probe")
        probe_matches = isinstance(probe, Mapping) and (
            probe.get("run_id") == run_id and probe.get("stage_id") == stage_id
        )
        lifecycle = terminal.get("lifecycle_state")
        failure_kind = terminal.get("failure_kind")
        event: dict[str, Any] | None = None
        if failure_kind == "anthropic_limit":
            effective = dict(self._effective_environ() or os.environ)
            cooldown = claude_runtime.anthropic_cooldown_seconds(effective)
            circuit.update(
                {
                    "status": "open",
                    "cooldown_until": (
                        datetime.now(UTC) + timedelta(seconds=cooldown)
                    ).isoformat().replace("+00:00", "Z"),
                    "probe": None,
                    "limit_count": int(circuit.get("limit_count", 0)) + 1,
                    "updated_at": utc_now(),
                }
            )
            event = {
                "type": "anthropic_cooldown_opened",
                "cooldown_until": circuit["cooldown_until"],
            }
        elif lifecycle == "completed" and probe_matches:
            circuit.update(
                {
                    "status": "closed",
                    "cooldown_until": None,
                    "probe": None,
                    "updated_at": utc_now(),
                }
            )
            event = {"type": "anthropic_cooldown_closed"}
        elif probe_matches:
            effective = dict(self._effective_environ() or os.environ)
            cooldown = claude_runtime.anthropic_cooldown_seconds(effective)
            circuit.update(
                {
                    "status": "open",
                    "cooldown_until": (
                        datetime.now(UTC) + timedelta(seconds=cooldown)
                    ).isoformat().replace("+00:00", "Z"),
                    "probe": None,
                    "updated_at": utc_now(),
                }
            )
            event = {"type": "anthropic_probe_failed_without_fallback"}
        if event is None:
            return
        store.save_state(campaign_id, state)
        store.append_event(campaign_id, event)

    def _on_stage_event(
        self, workspace: str, run_id: str, event: dict[str, Any]
    ) -> None:
        try:
            store = RunStore.for_workspace(workspace)
            with self._lock:
                store.append_event(run_id, event)
        except (InputError, StateError, OSError):
            return

    def _on_stage_terminal(
        self,
        workspace: str,
        run_id: str,
        stage_id: str,
        terminal: dict[str, Any],
    ) -> None:
        try:
            store = RunStore.for_workspace(workspace)
            with self._lock:
                contract = store.read_contract(run_id)
                state = store.read_state(run_id)
                stage = state.get("stages", {}).get(stage_id)
                if not isinstance(stage, dict):
                    return
                lifecycle = terminal.get("lifecycle_state")
                stage["lifecycle_state"] = lifecycle
                stage["finished_at"] = utc_now()
                stage["telemetry"] = terminal.get("telemetry", {})
                if lifecycle == "completed":
                    stage.pop("error", None)
                    stage.pop("failure_kind", None)
                if terminal.get("error"):
                    stage["error"] = sanitize_text(
                        terminal.get("error"), maximum=1_000
                    )
                if terminal.get("failure_kind") == "anthropic_limit":
                    stage["failure_kind"] = "anthropic_limit"
                profile = stage.get("profile")
                run_terminal = state.get("terminal")
                recovered_stage_ids = (
                    run_terminal.get("stage_ids", [])
                    if isinstance(run_terminal, dict)
                    else []
                )
                recovery_terminal_cleared = bool(
                    isinstance(run_terminal, dict)
                    and run_terminal.get("status") == "interrupted"
                    and run_terminal.get("source") == "stage_recovery"
                    and stage_id in recovered_stage_ids
                )
                preserve_run_terminal = (
                    isinstance(run_terminal, dict)
                    and not recovery_terminal_cleared
                )
                if recovery_terminal_cleared:
                    state["terminal"] = None

                if preserve_run_terminal:
                    pass
                elif lifecycle == "completed" and profile == "critic":
                    review_value = terminal.get("result")
                    review = validate_review(review_value, origin="claude")
                    review_file = store.read_review(run_id)
                    review_file["review"] = {
                        **review,
                        "diff_fingerprint": stage.get("diff_fingerprint"),
                        "recorded_at": utc_now(),
                    }
                    review_file["resolutions"] = {}
                    store.save_review(run_id, review_file)
                    state["review_summary"] = {
                        "origin": "claude",
                        "verdict": review["verdict"],
                        "finding_count": len(review["findings"]),
                        "blocking_question": bool(review["blocking_question"]),
                        "diff_fingerprint": stage.get("diff_fingerprint"),
                    }
                    self._transition(state, "reviewing")
                elif lifecycle == "completed" and profile == "implement":
                    stage["result"] = terminal.get("result", {})
                    self._transition(state, "writing")
                elif lifecycle == "interrupted":
                    self._transition(state, "interrupted")
                    state["terminal"] = {
                        "status": "interrupted",
                        "at": utc_now(),
                        "summary": "Claude stage was interrupted",
                    }
                elif (
                    lifecycle == "failed"
                    and profile == "critic"
                    and stage.get("failure_kind") == "anthropic_limit"
                ):
                    self._transition(state, "reviewing")
                else:
                    self._transition(state, "failed")
                    state["terminal"] = {
                        "status": "failed",
                        "at": utc_now(),
                        "summary": stage.get("error", "Claude stage failed"),
                    }
                store.save_state(run_id, state)
                if recovery_terminal_cleared:
                    store.append_event(
                        run_id,
                        {
                            "type": "stale_interruption_cleared",
                            "stage_id": stage_id,
                        },
                    )
                event = {
                    "type": "stage_persisted",
                    "stage_id": stage_id,
                    "profile": profile,
                    "lifecycle_state": lifecycle,
                }
                if stage.get("failure_kind") == "anthropic_limit":
                    event["failure_kind"] = "anthropic_limit"
                store.append_event(run_id, event)
                self._record_campaign_provider_terminal(
                    contract,
                    run_id,
                    stage_id,
                    terminal,
                )
        except (InputError, StateError, OSError):
            return

    def start_stage(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(arguments)
        store = RunStore.for_workspace(workspace)
        run_id = self._run_id(arguments)
        profile = require_string(
            arguments.get("profile"), "profile", maximum=32
        )
        if profile not in ("critic", "implement"):
            raise InputError("profile must be critic or implement")
        with self._lock:
            contract = store.read_contract(run_id)
            state = self._recover_interrupted(
                store, run_id, store.read_state(run_id)
            )
            self._require_open(state)
            if profile == "critic":
                if contract.get("writer") != "codex":
                    raise StateError("Claude cannot criticise its own implementation")
                current_fingerprint, _paths = diff_fingerprint(
                    store.context, base_sha=str(contract["base_sha"])
                )
                if current_fingerprint != state.get("diff_fingerprint"):
                    raise StateError(
                        "diff changed after checks were recorded; call plan_checks again"
                    )
                gate_ok, outstanding = self._check_gate(state)
                if not gate_ok:
                    raise StateError(
                        "required checks are not green: " + ", ".join(outstanding)
                    )
                if state.get("phase") != "checking":
                    raise StateError("critic may start only after the checking phase")
            else:
                if not (
                    contract.get("writer") == "claude"
                    and contract.get("writer_explicit") is True
                ):
                    raise StateError(
                        "implement profile requires explicit Claude writer authority"
                    )
                if state.get("phase") != "writing":
                    raise StateError("implement stage may start only in writing phase")

            stage_id = f"{run_id}:{profile}:1"
            existing = state.get("stages", {}).get(stage_id)
            key = self._stage_key(store, run_id, stage_id)
            if isinstance(existing, dict):
                active = self._stages.get(key)
                if active is not None:
                    snapshot = active.poll()
                    snapshot["deduplicated"] = True
                    return snapshot
                return {
                    "stage_id": stage_id,
                    "profile": profile,
                    "status": existing.get("lifecycle_state"),
                    "terminal": existing,
                    "deduplicated": True,
                }

            readiness = claude_runtime.check_runtime(self._effective_environ())
            if not readiness.get("ok"):
                raise StateError(str(readiness.get("error") or "Claude is not ready"))
            claude_info = readiness.get("claude")
            if not isinstance(claude_info, dict) or not claude_info.get("path"):
                raise StateError("Claude executable is unavailable")
            effective = dict(self._effective_environ() or os.environ)
            model = claude_runtime.resolve_model(effective)
            timeout, heartbeat, stall = claude_runtime.runtime_timing(effective)
            limit_action = self._campaign_limit_action(contract, run_id, stage_id)
            if limit_action == "skip":
                stage_record = {
                    "stage_id": stage_id,
                    "profile": profile,
                    "lifecycle_state": "failed",
                    "started_at": utc_now(),
                    "finished_at": utc_now(),
                    "diff_fingerprint": state.get("diff_fingerprint"),
                    "requested_model": model,
                    "requested_effort": "high",
                    "runtime_version": contract.get("runtime_version", "unknown"),
                    "failure_kind": "anthropic_limit",
                    "failure_source": "campaign_cooldown",
                    "error": "Anthropic campaign cooldown is active",
                    "telemetry": {},
                }
                state.setdefault("stages", {})[stage_id] = stage_record
                if profile == "critic":
                    self._transition(state, "reviewing")
                else:
                    self._transition(state, "failed")
                    state["terminal"] = {
                        "status": "failed",
                        "at": utc_now(),
                        "summary": "Claude implementation skipped during Anthropic cooldown",
                    }
                store.save_state(run_id, state)
                store.append_event(
                    run_id,
                    {
                        "type": "stage_skipped",
                        "stage_id": stage_id,
                        "profile": profile,
                        "failure_kind": "anthropic_limit",
                        "failure_source": "campaign_cooldown",
                    },
                )
                return {
                    "run_id": run_id,
                    "stage_id": stage_id,
                    "profile": profile,
                    "status": "failed",
                    "claude_invoked": False,
                    "terminal": stage_record,
                    "deduplicated": False,
                }
            command = claude_runtime.build_command(
                str(claude_info["path"]), profile=profile, model=model
            )
            prompt = build_stage_prompt(profile=profile, contract=contract, state=state)
            stage_record = {
                "stage_id": stage_id,
                "profile": profile,
                "lifecycle_state": "running",
                "started_at": utc_now(),
                "diff_fingerprint": state.get("diff_fingerprint"),
                "requested_model": model,
                "requested_effort": "high",
                "runtime_version": contract.get("runtime_version", "unknown"),
                "campaign_probe": limit_action == "probe",
            }
            state.setdefault("stages", {})[stage_id] = stage_record
            if profile == "critic":
                self._transition(state, "reviewing")
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {
                    "type": "stage_launching",
                    "stage_id": stage_id,
                    "profile": profile,
                    "diff_fingerprint": state.get("diff_fingerprint"),
                },
            )
            try:
                stage = ManagedStage(
                    stage_id=stage_id,
                    run_id=run_id,
                    profile=profile,
                    command=command,
                    cwd=Path(contract["repo_root"]),
                    prompt=prompt,
                    environ=effective,
                    requested_model=model,
                    timeout_seconds=timeout,
                    heartbeat_seconds=heartbeat,
                    stall_seconds=stall,
                    on_event=lambda event: self._on_stage_event(
                        workspace, run_id, event
                    ),
                    on_terminal=lambda terminal: self._on_stage_terminal(
                        workspace, run_id, stage_id, terminal
                    ),
                )
            except (InputError, OSError) as exc:
                current = store.read_state(run_id)
                current["stages"][stage_id]["lifecycle_state"] = "failed"
                current["stages"][stage_id]["error"] = sanitize_text(
                    str(exc), maximum=1_000
                )
                self._transition(current, "failed")
                current["terminal"] = {
                    "status": "failed",
                    "at": utc_now(),
                    "summary": "Claude process failed before inference",
                }
                store.save_state(run_id, current)
                self._record_campaign_provider_terminal(
                    contract,
                    run_id,
                    stage_id,
                    {"lifecycle_state": "failed"},
                )
                raise
            self._stages[key] = stage
        return {
            "run_id": run_id,
            "stage_id": stage_id,
            "profile": profile,
            "status": "running",
            "claude_invoked": True,
            "deduplicated": False,
        }

    def poll_stage(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        stage_id = require_string(
            arguments.get("stage_id"), "stage_id", maximum=128
        )
        wait_seconds = arguments.get("wait_seconds", 0)
        if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool):
            raise InputError("wait_seconds must be a number")
        key = self._stage_key(store, run_id, stage_id)
        with self._lock:
            active = self._stages.get(key)
            if active is None:
                state = self._recover_interrupted(
                    store, run_id, store.read_state(run_id)
                )
                stage = state.get("stages", {}).get(stage_id)
                if not isinstance(stage, dict):
                    raise StateError("unknown stage_id")
                return {
                    "run_id": run_id,
                    "stage_id": stage_id,
                    "profile": stage.get("profile"),
                    "status": "terminal",
                    "updates": [],
                    "terminal": stage,
                }
        return active.poll(float(wait_seconds))

    def cancel_stage(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        stage_id = require_string(
            arguments.get("stage_id"), "stage_id", maximum=128
        )
        key = self._stage_key(store, run_id, stage_id)
        with self._lock:
            active = self._stages.get(key)
            if active is None:
                state = self._recover_interrupted(
                    store, run_id, store.read_state(run_id)
                )
                stage = state.get("stages", {}).get(stage_id)
                if not isinstance(stage, dict):
                    raise StateError("unknown stage_id")
                return {
                    "run_id": run_id,
                    "stage_id": stage_id,
                    "status": "terminal",
                    "terminal": stage,
                    "deduplicated": True,
                }
        return active.cancel()

    @staticmethod
    def _normalize_resolutions(
        values: Any, finding_ids: set[str]
    ) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 64:
            raise InputError("resolutions must contain at most 64 entries")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise InputError(f"resolutions[{index}] must be an object")
            finding_id = require_string(
                value.get("finding_id"), "resolution.finding_id", maximum=80
            )
            if finding_id not in finding_ids:
                raise InputError(f"unknown finding_id: {finding_id}")
            if finding_id in seen:
                raise InputError(f"duplicate resolution: {finding_id}")
            seen.add(finding_id)
            disposition = value.get("disposition")
            if disposition not in RESOLUTION_VALUES:
                raise InputError(
                    "resolution.disposition must be accepted, rejected, or unverified"
                )
            resolved = value.get("resolved")
            if not isinstance(resolved, bool):
                raise InputError("resolution.resolved must be a boolean")
            if disposition == "unverified" and resolved:
                raise InputError("an unverified finding cannot be resolved")
            evidence = sanitize_text(
                require_string(
                    value.get("evidence"), "resolution.evidence", maximum=4_000
                ),
                maximum=4_000,
            )
            normalized.append(
                {
                    "finding_id": finding_id,
                    "disposition": disposition,
                    "resolved": resolved,
                    "evidence": evidence,
                    "recorded_at": utc_now(),
                }
            )
        return normalized

    def record_review_resolution(
        self, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        with self._lock:
            contract = store.read_contract(run_id)
            state = self._recover_interrupted(
                store, run_id, store.read_state(run_id)
            )
            self._require_open(state)
            gate_ok, outstanding = self._check_gate(state)
            if not gate_ok:
                raise StateError(
                    "review cannot be recorded until checks pass: "
                    + ", ".join(outstanding)
                )
            review_file = store.read_review(run_id)
            review = review_file.get("review")
            supplied_review = arguments.get("review")
            if contract.get("writer") == "claude":
                implement_stages = [
                    stage
                    for stage in state.get("stages", {}).values()
                    if isinstance(stage, dict) and stage.get("profile") == "implement"
                ]
                if not implement_stages or implement_stages[0].get(
                    "lifecycle_state"
                ) != "completed":
                    raise StateError("Claude implementation stage is not complete")
                if review is None:
                    if supplied_review is None:
                        raise InputError(
                            "Codex must supply an independent structured review"
                        )
                    review = {
                        **validate_review(supplied_review, origin="codex"),
                        "diff_fingerprint": state.get("diff_fingerprint"),
                        "recorded_at": utc_now(),
                    }
                    review_file["review"] = review
                elif supplied_review is not None:
                    raise StateError("independent review is already recorded")
            else:
                fallback_allowed = self._codex_fallback_allowed(state)
                if review is None and supplied_review is not None and fallback_allowed:
                    review = {
                        **validate_review(supplied_review, origin="codex_fallback"),
                        "diff_fingerprint": state.get("diff_fingerprint"),
                        "recorded_at": utc_now(),
                    }
                    review_file["review"] = review
                elif supplied_review is not None:
                    raise InputError(
                        "a Codex fallback review is accepted only after a confirmed "
                        "Anthropic usage limit"
                    )
                elif not isinstance(review, dict):
                    if fallback_allowed:
                        raise StateError(
                            "a fresh Codex fallback review must be supplied"
                        )
                    raise StateError("Claude critic review is not complete")

            correction_review = (
                int(state.get("correction_passes", 0)) > 0
                and state.get("phase") in ("checking", "reviewing")
            )
            if (
                review.get("diff_fingerprint") != state.get("diff_fingerprint")
                and not correction_review
            ):
                raise StateError("review does not describe the current diff fingerprint")
            finding_ids = {
                finding["id"]
                for finding in review.get("findings", [])
                if isinstance(finding, dict) and isinstance(finding.get("id"), str)
            }
            resolutions = self._normalize_resolutions(
                arguments.get("resolutions", []), finding_ids
            )
            stored_resolutions = review_file.setdefault("resolutions", {})
            for resolution in resolutions:
                stored_resolutions[resolution["finding_id"]] = resolution
            store.save_review(run_id, review_file)

            state["review_summary"] = {
                "origin": review.get("origin"),
                "verdict": review.get("verdict"),
                "finding_count": len(finding_ids),
                "blocking_question": bool(review.get("blocking_question")),
                "diff_fingerprint": review.get("diff_fingerprint"),
                "resolution_diff_fingerprint": state.get("diff_fingerprint"),
            }
            accepted_unresolved = any(
                item.get("disposition") == "accepted"
                and item.get("resolved") is not True
                for item in stored_resolutions.values()
                if isinstance(item, dict)
            )
            unresolved_high = [
                finding["id"]
                for finding in review.get("findings", [])
                if finding.get("severity") in ("P0", "P1")
                and not (
                    isinstance(stored_resolutions.get(finding["id"]), dict)
                    and stored_resolutions[finding["id"]].get("resolved") is True
                    and stored_resolutions[finding["id"]].get("disposition")
                    != "unverified"
                )
            ]
            if review.get("blocking_question"):
                self._transition(state, "needs_human")
                state["terminal"] = {
                    "status": "needs_human",
                    "at": utc_now(),
                    "summary": "Independent review returned a blocking question",
                    "blocking_question": review.get("blocking_question"),
                }
            elif unresolved_high and contract.get("max_correction_passes", 1) == 0:
                self._transition(state, "needs_human")
                state["terminal"] = {
                    "status": "needs_human",
                    "at": utc_now(),
                    "summary": "Unresolved P0/P1 findings require human attention",
                }
            elif accepted_unresolved:
                self._transition(state, "correcting")
            else:
                self._transition(state, "reviewing")
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {
                    "type": "review_resolutions_recorded",
                    "finding_ids": [item["finding_id"] for item in resolutions],
                    "phase": state.get("phase"),
                },
            )
        return {
            "run_id": run_id,
            "phase": state.get("phase"),
            "review": review_file,
        }

    def _completion_blockers(
        self,
        store: RunStore,
        contract: dict[str, Any],
        state: dict[str, Any],
        review_file: dict[str, Any],
    ) -> list[str]:
        blockers: list[str] = []
        current, _paths = diff_fingerprint(
            store.context, base_sha=str(contract["base_sha"])
        )
        if current != state.get("diff_fingerprint"):
            blockers.append("current diff does not match the checked fingerprint")
        if "review_budget" in contract:
            budget_status = state.get("budget_status")
            if budget_status is None:
                blockers.append("review budget has not been measured")
        checks_ok, outstanding = self._check_gate(state)
        if not checks_ok:
            blockers.append("required checks are not green: " + ", ".join(outstanding))
        review = review_file.get("review")
        if not isinstance(review, dict):
            blockers.append("independent review is missing")
            return blockers
        origin = review.get("origin")
        if contract.get("writer") == "codex":
            if origin == "claude":
                critic = [
                    stage
                    for stage in state.get("stages", {}).values()
                    if isinstance(stage, dict) and stage.get("profile") == "critic"
                ]
                if not critic or critic[0].get("lifecycle_state") != "completed":
                    blockers.append("Claude critic stage is not complete")
            elif origin == "codex_fallback":
                if not self._codex_fallback_allowed(state):
                    blockers.append(
                        "Codex fallback review lacks a confirmed Anthropic usage limit"
                    )
            else:
                blockers.append("review origin is not allowed for a Codex writer")
        elif origin != "codex":
            blockers.append("review origin is not independent from the writer")
        if (
            review.get("diff_fingerprint") != current
            and int(state.get("correction_passes", 0)) == 0
        ):
            blockers.append("review does not describe the current diff")
        if review.get("blocking_question"):
            blockers.append("review has a blocking question")
        resolutions = review_file.get("resolutions", {})
        for finding in review.get("findings", []):
            finding_id = finding.get("id")
            resolution = resolutions.get(finding_id)
            if not isinstance(resolution, dict):
                blockers.append(f"finding {finding_id} has no disposition")
                continue
            if resolution.get("disposition") == "unverified":
                blockers.append(f"finding {finding_id} remains unverified")
            elif resolution.get("resolved") is not True:
                blockers.append(f"finding {finding_id} is not resolved")
        if contract.get("writer") != "codex":
            implement = [
                stage
                for stage in state.get("stages", {}).values()
                if isinstance(stage, dict) and stage.get("profile") == "implement"
            ]
            if not implement or implement[0].get("lifecycle_state") != "completed":
                blockers.append("Claude implementation stage is not complete")
        return blockers

    def finish_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        status = require_string(arguments.get("status"), "status", maximum=32)
        if status not in TERMINAL_PHASES:
            raise InputError(
                "status must be complete, needs_human, blocked, failed, or interrupted"
            )
        summary = sanitize_text(arguments.get("summary"), maximum=2_000)
        blocking_question = sanitize_text(
            arguments.get("blocking_question"), maximum=2_000
        )
        with self._lock:
            contract = store.read_contract(run_id)
            raw_state = store.read_state(run_id)
            active_stage_ids = [
                stage_id
                for stage_id, stage in raw_state.get("stages", {}).items()
                if isinstance(stage, dict)
                and stage.get("lifecycle_state") == "running"
                and self._stage_key(store, run_id, stage_id) in self._stages
            ]
            if active_stage_ids:
                raise StateError(
                    "cannot finish run while a model stage is active; "
                    "call cancel_stage first"
                )
            state = self._recover_interrupted(
                store, run_id, raw_state
            )
            existing_terminal = state.get("terminal")
            if isinstance(existing_terminal, dict):
                if existing_terminal.get("status") == status:
                    return {
                        "run_id": run_id,
                        "phase": state.get("phase"),
                        "terminal": existing_terminal,
                        "deduplicated": True,
                    }
                raise StateError(
                    f"run is already terminal: {existing_terminal.get('status')}"
                )
            review_file = store.read_review(run_id)
            if status == "complete":
                blockers = self._completion_blockers(
                    store, contract, state, review_file
                )
                if blockers:
                    review = review_file.get("review") or {}
                    resolutions = review_file.get("resolutions") or {}
                    high_unresolved = any(
                        finding.get("severity") in ("P0", "P1")
                        and not (
                            isinstance(resolutions.get(finding.get("id")), dict)
                            and resolutions[finding.get("id")].get("resolved") is True
                            and resolutions[finding.get("id")].get("disposition")
                            != "unverified"
                        )
                        for finding in review.get("findings", [])
                        if isinstance(finding, dict)
                    )
                    if high_unresolved:
                        self._transition(state, "needs_human")
                        state["terminal"] = {
                            "status": "needs_human",
                            "at": utc_now(),
                            "summary": "Unresolved P0/P1 findings block completion",
                        }
                        store.save_state(run_id, state)
                    raise StateError("completion gate failed: " + "; ".join(blockers))
            elif status in ("needs_human", "blocked") and not summary:
                raise InputError(f"summary is required for {status}")

            self._transition(state, status)
            state["terminal"] = {
                "status": status,
                "at": utc_now(),
                "summary": summary or "All local implementation gates passed",
            }
            if blocking_question:
                state["terminal"]["blocking_question"] = blocking_question
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {
                    "type": "run_finished",
                    "status": status,
                    "risk": state.get("risk"),
                },
            )
        return {
            "run_id": run_id,
            "phase": state.get("phase"),
            "terminal": state.get("terminal"),
            "deduplicated": False,
        }
