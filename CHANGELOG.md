# Changelog

All notable changes to FilmPilot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

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
