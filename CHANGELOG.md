# Changelog

All notable changes to FilmPilot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Independent Cloudflare Free web workbench with per-session BYOK and stateless fixed-provider relay.
- User-selected local project folders, permission-aware refresh recovery, portable project packages, and conflict-safe saves.
- Review-first cloud script, asset, shot, prompt, and reference-image workflows, with stale-result protection.
- Named script history with safe previews, deliberate draft restoration, and automatic pre-replacement snapshots.
- Local production-brief TXT and formula-safe shot-list CSV downloads, separate from restorable project backups.
- Environment-only configuration mode that disables dotenv and legacy credential-file loading.
- Least-permission GitHub CI for isolated Python/cloud tests and an unauthenticated Worker dry-run.

### Fixed

- Enforce upload limits while receiving image bodies instead of buffering oversized uploads first.
- Preserve DeepSeek provider/transport errors without duplicate JSON retries or raw error-body logging.
- Keep the optional lightweight CrewAI tool entrypoint compatible without the CrewAI dependency.
- Bound local HTTP research downloads while streaming, reject credential-bearing/malformed URLs, and apply a transfer deadline.

### Previously added

- Master Production Agent workspace for guiding users from a rough idea to a staged AI-video prompt workflow.
- Agent session history, suggested reply choices, plan approvals, stage approvals, checkpoints, cancellation, and resume controls.
- Local retrieval foundation with optional Qdrant/FlagEmbedding support and keyword fallback.
- Optional CrewAI-style role catalog, runtime preflight, tool descriptors, and controlled workflow tool execution.
- Agent research tools for web search, page fetch, source adoption, and project-grounded context retrieval.
- Prompt strategy classification for shot-prompt work and director-overhead guidance for production planning.
- DeepSeek thinking controls for chat, agent planning, and monitored model calls.

### Changed

- Agent chat can continue an approved workflow through later stages instead of stopping at the initial plan.
- Agent UI now surfaces guided choices above the composer and keeps fallback choices user-facing.

## [1.0.0] - 2026-07-04

### Added

- Local-first project, screenplay, storyboard, asset, and prompt management.
- DeepSeek-powered screenplay generation, asset extraction, storyboard design, prompt generation, and scoped AI editing chat.
- Reviewable chat proposals with apply, reject, and revert workflows.
- GPT Image and Seedream asset image generation with local candidate management.
- Storyboard snapshots, agent-run monitoring, validation metrics, latency, and token usage reporting.
- Dialogue-aware shot durations and support for longer continuous shots.
- Deterministic local repair for screenplay references, sequence numbering, and duplicate shots.
- Configurable DeepSeek output limits with explicit truncation diagnostics.

### Security

- Provider credentials remain server-side and local secrets, databases, uploads, generated assets, and caches are excluded from Git.
