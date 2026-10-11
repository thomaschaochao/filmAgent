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
- Browser fixture server allowlist: 1 test passed, including refusal of API, credential-like and traversal paths.
- Total cloud Node tests: 204 passed.
- Seven Playwright Chromium user-flow tests passed on a standard public GitHub-hosted Ubuntu runner:
  offline sample, script snapshots/confirmed restore, real Chinese-named project download/import,
  cancellation with late responses, safe provider errors/explicit apply, responsive layout, and
  save/revoke/reconnect/reload using real OPFS handles with a synthetic picker/permission adapter.
- Desktop (1440 px) and mobile (390 px) synthetic-demo screenshots were visually reviewed after the
  responsive test passed. Navigation labels remain readable without horizontal page overflow.
  This is a Chromium layout check, not native mobile-device or assistive-technology acceptance.
- Revision `46191dd7da65b57d9e3debe74df0ab78335b96f4` passed all three jobs in
  [GitHub Actions run 38126872966](https://github.com/thomaschaochao/filmAgent/actions/runs/38126872966)
  on 2026-10-11, including fresh dependency installs, Node 22, Python 3.12, all tests and the dry-run
  bundle. Screenshots contain only synthetic data and expire after one day. Later changes must rerun CI.
- All cloud JavaScript entrypoints passed syntax checks.
- Wrangler 4.149.0 dry-run bundled successfully with only the `ASSETS` binding.

All model responses were mocked. No real provider API key or user project was used.
Test configuration prevents reading local dotenv/credential files.

## Remaining live acceptance checks

These checks are not implied by the test results above:

1. Actual deployed Workers Free CPU/quota behavior, particularly larger model responses.
2. Browser-native operating-system folder picker, permission dialogs, and interactive download prompts.
3. Cross-browser behavior on desktop Chrome/Edge and package fallback on Safari/Firefox.
4. Real provider credentials, available models, billing and provider-generated image responses.
5. Published HTTPS endpoint and target-account authorization.

Shell-launched Chromium was blocked by the development workspace's socket restrictions.
The authorized public GitHub Actions job subsequently supplied real Chromium coverage; JSDOM
remains a separate fast regression suite. OPFS tests use genuine browser file handles and IndexedDB
structured cloning, but picker and permission decisions are test adapters. They do not grant or test
access to an actual user's local folder. No user's computer, real project or model key was accessed.

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
