# Cloud workbench validation

## Scope checked in the isolated cloud workspace

- Original Python application: 308 Pytest tests passed.
- Python lint: `ruff check app tests` passed.
- Original frontend: `node --check app/static/app.js` passed.
- Cloud relay: 78 mocked Node tests passed, including per-request key isolation,
  fixed upstreams, no redirects, sanitized failures, cancellation and bounded image streaming.
- Local project store: 41 mocked Node tests passed, including strict import validation,
  complete image roundtrip, revoked permissions, refresh recovery, concurrent edits and interrupted saves.
- Cloud workflow helpers: 28 tests passed, including atomic script snapshots and safe restoration.
- JSDOM workbench interaction suite: 18 subtests plus its parent passed, covering
  project-package mode, model-key isolation, proposal review, stale response rejection,
  image generation mocks, invalid imports, file-save races, unload protection and history controls.
- A separate folder UI integration test passed with real synthetic temporary files and mocked
  directory handles/IndexedDB, covering save/reopen, permission loss, explicit reconnect and refresh.
- Production export helpers: 14 tests passed, including formula-injection protection, safe filenames,
  exact quoting/newlines, strict project validation, image/history exclusion and expanded output limits.
- Offline sample project: 7 tests passed for validation, independent IDs, reference integrity,
  current UTC timestamps, no network/storage and portable package roundtrip.
- Shot editing helpers: 14 tests passed for identity-preserving moves, independent duplicates,
  300-shot limits, atomic metadata failures and package/export order.
- Static security-header policy: 1 test passed.
- Total cloud tests: 203 passed.
- GitHub Actions CI definition validates both editions without deployment or model credentials.
  YAML/security/shell checks passed locally. The initial integrated revision `a39192a` passed both
  jobs in [GitHub Actions run 38124826315](https://github.com/thomaschaochao/filmAgent/actions/runs/38124826315),
  including fresh dependency installs and Node 22. Later changes must rerun this workflow.
- All cloud JavaScript entrypoints passed syntax checks.
- Wrangler 4.149.0 dry-run bundled successfully with only the `ASSETS` binding.

All model responses were mocked. No real provider API key or user project was used.
Test configuration prevents reading local dotenv/credential files.

## Remaining live acceptance checks

These checks are not implied by the test results above:

1. Actual deployed Workers Free CPU/quota behavior, particularly larger model responses.
2. Real browser visual layout, browser-native folder picker, permission dialogs, and download prompts.
3. Cross-browser behavior on desktop Chrome/Edge and package fallback on Safari/Firefox.
4. Real provider credentials, available models, billing and provider-generated image responses.
5. Published HTTPS endpoint and target-account authorization.

Shell-launched Chromium was blocked by the execution environment's socket restrictions,
including an escalated attempt. JSDOM was used for interaction regression tests; it does not
render visual layout or grant actual filesystem permissions. No substitute environment or
actual user's computer was accessed.

## Recommended deployment smoke checks

- Homepage and local static resources load over HTTPS with the configured security headers.
- `/api/chat` without a key returns 401; foreign Origin is rejected before any provider call.
- No model key or project data is present in static build assets or the Worker configuration.
- A synthetic project can save, reopen and export/import; canceling the picker preserves current work.
- Revoking permission preserves dirty work; explicit reconnect restores authorized access.
- A delayed AI response cannot overwrite a newer project or be lost during a concurrent file save.
- Live model testing occurs only after the user safely enters their own key and accepts provider charges.

For a final release, rerun the complete suite against the final commit and record the deployed
version and actual smoke results separately. A successful build is not a successful deployment.
