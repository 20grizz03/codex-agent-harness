"""Agent Harness application service and lifecycle invariants."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Mapping

from . import claude_runtime
from .claude_runtime import ManagedStage
from .contract import build_contract
from .git_repo import diff_fingerprint, resolve_repo
from .policy import plan_checks as build_check_plan
from .review import build_stage_prompt, validate_review
from .store import RunStore, SCHEMA_VERSION
from .util import (
    InputError,
    StateError,
    require_string,
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
        for stage_id, stage in state.get("stages", {}).items():
            if stage.get("lifecycle_state") != "running":
                continue
            if self._stage_key(store, run_id, stage_id) in self._stages:
                continue
            stage["lifecycle_state"] = "interrupted"
            stage["finished_at"] = utc_now()
            stage["error"] = "MCP server restarted during model inference"
            changed = True
        if changed:
            self._transition(state, "interrupted")
            state["terminal"] = {
                "status": "interrupted",
                "at": utc_now(),
                "summary": "An in-flight model stage cannot be resumed safely",
            }
            state = store.save_state(run_id, state)
            store.append_event(
                run_id,
                {
                    "type": "run_interrupted",
                    "reason": "server_restart_during_inference",
                },
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

    def check_runtime(self, _arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return claude_runtime.check_runtime(self._effective_environ())

    def create_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(arguments)
        context = resolve_repo(workspace)
        contract, state = build_contract(dict(arguments), context)
        store = RunStore(context)
        with self._lock:
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

    def plan_checks(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        store = RunStore.for_workspace(self._workspace(arguments))
        run_id = self._run_id(arguments)
        with self._lock:
            contract = store.read_contract(run_id)
            state = self._recover_interrupted(
                store, run_id, store.read_state(run_id)
            )
            self._require_open(state)
            fingerprint, paths = diff_fingerprint(
                store.context, base_sha=str(contract["base_sha"])
            )
            previous = state.get("diff_fingerprint")
            if previous == fingerprint and state.get("planned_checks"):
                return {
                    "run_id": run_id,
                    "diff_fingerprint": fingerprint,
                    "changed_paths": state.get("changed_paths", []),
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
                },
            )
        return {
            "run_id": run_id,
            "diff_fingerprint": fingerprint,
            "changed_paths": paths,
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
                state = store.read_state(run_id)
                stage = state.get("stages", {}).get(stage_id)
                if not isinstance(stage, dict):
                    return
                lifecycle = terminal.get("lifecycle_state")
                stage["lifecycle_state"] = lifecycle
                stage["finished_at"] = utc_now()
                stage["telemetry"] = terminal.get("telemetry", {})
                if terminal.get("error"):
                    stage["error"] = sanitize_text(
                        terminal.get("error"), maximum=1_000
                    )
                profile = stage.get("profile")
                if lifecycle == "completed" and profile == "critic":
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
                else:
                    self._transition(state, "failed")
                    state["terminal"] = {
                        "status": "failed",
                        "at": utc_now(),
                        "summary": stage.get("error", "Claude stage failed"),
                    }
                store.save_state(run_id, state)
                store.append_event(
                    run_id,
                    {
                        "type": "stage_persisted",
                        "stage_id": stage_id,
                        "profile": profile,
                        "lifecycle_state": lifecycle,
                    },
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
                if supplied_review is not None:
                    raise InputError(
                        "Codex-written runs use the persisted Claude critic result"
                    )
                if not isinstance(review, dict):
                    raise StateError("Claude critic review is not complete")

            if review.get("diff_fingerprint") != state.get("diff_fingerprint"):
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
            }
            accepted = any(
                item.get("disposition") == "accepted"
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
            elif accepted:
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
        checks_ok, outstanding = self._check_gate(state)
        if not checks_ok:
            blockers.append("required checks are not green: " + ", ".join(outstanding))
        review = review_file.get("review")
        if not isinstance(review, dict):
            blockers.append("independent review is missing")
            return blockers
        expected_origin = "claude" if contract.get("writer") == "codex" else "codex"
        if review.get("origin") != expected_origin:
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
        if contract.get("writer") == "codex":
            critic = [
                stage
                for stage in state.get("stages", {}).values()
                if isinstance(stage, dict) and stage.get("profile") == "critic"
            ]
            if not critic or critic[0].get("lifecycle_state") != "completed":
                blockers.append("Claude critic stage is not complete")
        else:
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
            state = self._recover_interrupted(
                store, run_id, store.read_state(run_id)
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
