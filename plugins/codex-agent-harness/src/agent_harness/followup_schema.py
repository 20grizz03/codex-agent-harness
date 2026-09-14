"""Public input contracts for journal-only correction tools."""

ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"}
SHA = {"type": "string", "pattern": "^([a-f0-9]{40}|[a-f0-9]{64})$"}
PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
COUNT = {"type": "integer", "minimum": 0}
SUMMARY = {"type": "string", "maxLength": 1000}
DIGEST = {"type": "string", "pattern": "^[a-f0-9]{64}$"}


def object_schema(properties, required):
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


SOURCE = object_schema({"workspace": PATH, "base_sha": SHA, "head_sha": SHA},
                       ["workspace", "base_sha", "head_sha"])
NAMES = {"type": "array", "minItems": 1, "maxItems": 64, "uniqueItems": True, "items": ID}
TASK = object_schema({
    "id": ID, "workspace": PATH, "base_sha": SHA, "head_sha": SHA,
    "dependencies": {"type": "array", "maxItems": 64, "uniqueItems": True, "items": ID},
    "parent_task_id": ID, "published": {"type": "boolean"}, "active": {"type": "boolean"},
    "run_id": ID, "build_checks": NAMES, "test_checks": NAMES,
    "check_bindings": {"type": "object", "minProperties": 1, "maxProperties": 128,
                       "propertyNames": ID, "additionalProperties": DIGEST},
}, ["id", "workspace", "base_sha", "head_sha", "published", "active", "build_checks", "test_checks", "check_bindings"])
FOLLOWUP_REF_SCHEMA = object_schema({
    "workspace": PATH, "followup_id": ID, "task_id": ID, "epoch": COUNT,
    "attempt": COUNT, "parent_sha": SHA,
}, ["workspace", "followup_id", "task_id", "epoch", "attempt", "parent_sha"])
DATA = object_schema({
    "task_id": ID, "workspace": PATH, "source": SOURCE,
    "released": {"type": "boolean"}, "mode": {"type": "string", "enum": ["mechanical", "semantic"]},
    "status": {"type": "string", "enum": ["blocked", "interrupted"]},
    "run_id": ID, "name": ID, "summary": SUMMARY,
    "diff_fingerprint": DIGEST, "check_fingerprint": DIGEST,
    "exit_code": {"type": "integer", "minimum": 0, "maximum": 255},
    "duration_ms": {"type": "integer", "minimum": 0, "maximum": 86400000},
    "bucket": {"type": "string", "enum": ["writing", "propagation", "checks", "review", "external_wait"]},
    "replayed_commits": {"type": "array", "maxItems": 512, "items": object_schema(
        {"old_sha": SHA, "new_sha": SHA}, ["old_sha", "new_sha"])},
}, [])


def tool(name, description, properties, required, read_only=False):
    return {"name": name, "description": description,
            "inputSchema": object_schema(properties, required),
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False}}


FOLLOWUP_TOOLS = [
    tool("create_followup", "Freeze one correction-chain owner, complete task ranges and check names in shared Git metadata. "
         "Source and task baselines need separate worktrees. Does not run Git mutations, models, tests or external actions.",
         {"workspace": PATH, "followup_id": ID, "owner_id": ID, "summary": SUMMARY, "source": SOURCE,
          "tasks": {"type": "array", "minItems": 1, "maxItems": 64, "items": TASK}},
         ["workspace", "followup_id", "owner_id", "source", "tasks"]),
    tool("get_followup", "Read correction state and freshly computed per-candidate publication readiness. "
         "mechanical_checked is not an independently reviewed complete run. Never authorizes publication.",
         {"workspace": PATH, "followup_id": ID}, ["workspace", "followup_id"], True),
    tool("record_followup", "Record a single-owner revision-checked idempotent correction action. "
         "checkpoint releases an active writer; begin pins a distinct worktree; candidate verifies full commit provenance; "
         "check records a current result; finish yields mechanical_checked or validates a semantic run; "
         "pause preserves work; reopen retries a finished task and invalidates its descendants at safe checkpoints; "
         "queue_source and advance serialize new parents; abandon releases reservations at safe checkpoints "
         "without deleting history; progress records bounded timings. "
         "Does not perform Git mutations, models, tests, messages or publication.",
         {"workspace": PATH, "followup_id": ID, "owner_id": ID, "expected_revision": COUNT, "request_id": ID,
          "action": {"type": "string", "enum": ["checkpoint", "begin", "candidate", "check", "finish", "pause", "reopen", "abandon",
                                                    "queue_source", "advance", "progress"]}, "data": DATA},
         ["workspace", "followup_id", "owner_id", "expected_revision", "request_id", "action"]),
]
