---
name: review
description: Independently review a repository diff with a fresh read-only Claude critic, then have Codex verify and disposition every finding. Use when the user asks for an independent review, a Claude second opinion, adversarial bug hunting, or review through Agent Harness. Do not use for implementing fixes unless the user separately asks for changes.
---

# Agent Harness Review

Codex owns the review target, final judgment, and user-facing report. Read [references/review-contract.md](references/review-contract.md) completely before invoking the critic.

Create a run if the review is not already part of an active workflow. Record applicable deterministic checks first. Delegate the model lifecycle to one native tracking subagent; Claude must use the read-only `critic` profile and inspect the repository itself.

Verify and deduplicate every finding before reporting it. Record each disposition and evidence through `record_review_resolution`. Do not edit files, resolve review threads, post comments, commit, or push unless the user explicitly expands the request.
