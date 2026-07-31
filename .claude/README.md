# Claude Smart Starter

A small, reusable Claude Code configuration for a solo developer.

## What it adds

- one short always-on rule file;
- five narrow, on-demand skills;
- one read-only review subagent;
- routing and behavior cases for static review.

It intentionally contains no framework, language, provider, deployment, or project-specific assumptions.

## Install

Copy this `.claude` directory into the root of a Git repository, then start a new Claude Code session. If this is the first `.claude/agents` directory created during a running session, restart Claude Code once.

## Recommended use

Let Claude invoke skills automatically when descriptions match, or invoke manually:

- `/repository-change` — disciplined code changes;
- `/backend-security-guard` — security-sensitive backend work;
- `/product-idea-challenger` — product ideas and prioritization;
- `/frontend-product-quality` — product UI and UX work;
- `/context-handoff` — compact handoff before `/clear` or switching sessions.

Ask for the reviewer explicitly when useful:

> Use the critical-reviewer agent to review the current change.

## Design principles

- No automatic hooks.
- No bundled executable scripts.
- No network or production commands.
- No forced tool or framework choices.
- Skills load only when relevant.
- The reviewer is read-only.
- Project-specific rules belong in that project's `CLAUDE.md`, not in this reusable pack.
