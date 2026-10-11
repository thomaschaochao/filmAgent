# Security and recovery boundaries

## Which edition to publish

Publish only `cloud/` as a static-assets Worker. The original FastAPI app is a
single-user local application: its SQLite database, upload directory and API are
not a public multi-tenant service. Keep its default loopback listener private.

## Keys and model requests

- Every user supplies their own key. The app keeps it in page memory, clears it
  on page exit/recovery and never uses a shared deployment key.
- A generation request sends that key over HTTPS to this site's Worker and then
  to the chosen fixed official provider endpoint. The site operator and the
  delivered frontend code are part of the trust boundary. Use a deployment you trust.
- The application does not persist keys in project files, browser storage, URLs,
  application logs or provider-error messages. It does not control browser
  extensions, password managers, device memory inspection or provider retention.
- There is no automatic paid retry. Canceling aborts local waiting/transfer where
  possible; it cannot reverse a request already accepted or billed by a provider.
- Prompts and relevant current project context leave the device when the user
  explicitly starts generation. Offline editing, the sample project, saves and
  exports do not contact a model provider.
- Do not paste any credential into creative text. Current known keys and common
  credential-shaped strings are rejected, but arbitrary unknown secrets in prose
  cannot be detected reliably.

## Public relay and free limits

The relay accepts only allowlisted operations at fixed endpoints. It does not
follow provider redirects, accept arbitrary upstream URLs or copy provider
cookies/debug headers into responses. API responses disable caching.

Origin checks and no permissive CORS isolate normal cross-site browser calls.
They are not authentication: a non-browser client can supply its own key and
consume the site's free request quota. There is no user account, durable rate
limiter, server-side project store or cross-device synchronization in this edition.
A deployment must stay on Workers Free; quota exhaustion can make it unavailable.

Application observability is disabled in the checked-in configuration. Do not
add request-header/body logging, shared model secrets or a catch-all proxy while
debugging. Infrastructure and providers still process requests according to their
own policies; the project does not promise end-to-end secrecy from them.

## Files, permissions and backups

- Folder access begins with an explicit picker. Refresh remembers only the
  directory handle; if permission is not already granted, a user click is needed
  before reconnecting and reading its declared project files.
- Projects and images are ordinary local files, not encrypted by this app.
  Protect and back them up using the device's normal security and backup tools.
- Saves write immutable image files before closing the new project metadata.
  Failed writes preserve dirty state; old image files are not automatically deleted.
- Web Locks coordinate tabs on the same origin, and source checks detect prior
  outside changes. They cannot lock another browser profile, a different origin
  or an external editor against every last-moment write. Avoid concurrent editing
  of the same folder in those tools; export a backup before resolving a conflict.
- A complete `.filmpilot.json` package is portable and restorable. Production TXT
  and CSV files are readable handoffs only: they omit images and script history.
  Starting a browser download does not prove it finished successfully.

If a folder permission expires, keep the tab open, use explicit reconnect or
export a project package. If the folder has changed externally, export current
work first, then reopen the folder or select a new dedicated empty folder.
Never discard the only remaining copy of unsaved work to resolve a conflict.

## Validation limits

Imports reject unsupported fields, dangerous paths, bad references, malformed
images and oversized data. Error text is sanitized. Those checks reduce accidental
exposure and corruption; they cannot recover a deleted device file or authenticate
an untrusted deployment.

Read [VALIDATION.md](VALIDATION.md) for actual automated results and outstanding
live-browser/provider/deployment acceptance checks. A dry-run build does not mean
the application is deployed or that real browser permissions were tested.
