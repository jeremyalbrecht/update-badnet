# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (runtime + test)
pip install -e '.[dev]'

# Run tests
pytest -q

# Run a single test
pytest tests/test_func.py::test_login_posts_scraped_ic_a_with_credentials

# Run locally (outside container, recommended for development)
func run --builder=host

# Deploy to Knative cluster
func deploy --registry docker.io/jeremyalbrecht
```

## Architecture

A **Knative Python HTTP function** that authenticates against
[Badnet](https://badnet.fr) as a club administrator, downloads the per-licensee
tournament export, and full-replaces a Google Sheet tab with it.

### Knative lifecycle

`function/func.py` exposes `new()` returning a `BadnetUpdate`. The runtime calls:

- `start(cfg)` — receives `os.environ.copy()` as a dict on ASGI lifespan startup;
  reads config and builds the Sheets client.
- `handle(scope, receive, send)` — a **raw ASGI handler**, not a convenience
  wrapper. It must `await send(...)` a `http.response.start` then a
  `http.response.body`. Every request runs the full pipeline unconditionally.
- `stop()` / `alive()` / `ready()` — lifecycle and health hooks. `/health/liveness`
  and `/health/readiness` are routed by the middleware, not by `handle`.

### Pipeline

`handle()` → `_fetch_export()` → `_sniff_rows()` → `_update_sheet()`.

`_fetch_export()` drives an authenticated session against badnet.fr:

1. **Login** (`_login`) — `GET /connexion`, `_parse_form` scrapes the hidden
   `ic_a` / `ic_ajax` fields, then `POST` to `/index.php` with credentials and
   `remember=1`. **A successful POST returns neither a page nor JSON** — it
   returns 68 bytes of script:
   `<script type="text/javascript">location='/validation-code' </script>`.
   `_follow_script_redirects` chases those (capped at 5 hops) to the real page.
   Only bodies consisting *solely* of script tags count, so analytics JS on a
   real page is never mistaken for a redirect.
2. **2FA** (`_submit_2fa`) — Badnet emails a 6-digit code from
   `contact@badnet.fr`. `_await_2fa_code` polls the mailbox over **IMAP** with
   backoff until a message that arrived *after* the login submit shows up, then
   posts the code with the challenge page's own `ic_a`. On acceptance,
   `_delete_message` trashes the mail (Gmail `X-GM-LABELS \Trash`, falling back
   to `\Deleted` + expunge). Deletion happens **only after** Badnet confirms the
   code, uses UIDs because it runs on a fresh connection, and is never fatal.
3. **Navigate** (`_ic_click`) — see below.
4. **Download** — the export button's action, fetched through the same primitive.

### The iclick navigation model

Badnet uses a bespoke library at `/vendor/iclick/`, **not** Intercooler.js — do
not send `X-IC-*` headers. Any element may carry:

- `data-ic_a` — a 32-hex server-generated token identifying the *action*. It is
  per-action and per-session, so it **must be scraped every run**, never cached
  or hardcoded.
- `data-ic_url` — a JSON blob whose `url` field carries the query parameters.

The rule, implemented once in `_ic_get`: `GET /index.php?ic_ajax=1&ic_a=<token>`
merged with the parameters parsed out of `data-ic_url`. `_find_ic_action` locates
the element by its visible French label (accent- and case-insensitive), searching
*any* element with `data-ic_a` rather than a fixed tag. On no match it raises
listing every label it did find — that message is the intended debugging path.

### Finding the 2FA code

One Gmail-native search: `UID SEARCH X-GM-RAW "from:badnet.fr newer_than:1d"`.
X-GM-RAW runs a real Gmail query, so unlike `FROM …` over INBOX it still finds
the mail if a filter has labelled or archived it.

**Gmail's INTERNALDATE does not survive `imaplib.Internaldate2tuple` here** — in
practice every message resolves via the `Date:` header instead. Dropping
messages whose INTERNALDATE was unreadable is what made a delivered code look
like it never arrived. Keep the fallback in `_message_datetime`.

`_poll_inbox_once` records every rejection in `self._poll_report` (logged each
poll at DEBUG, embedded in the `TimeoutError`), distinguishing no search hits,
a message older than the cutoff, a fresh message with no extractable code, and
IMAP exceptions such as a bad app password. Report timestamps are normalised to
UTC — the mail carries a `+0200` offset, so raw values look an hour ahead.

### Constraints worth knowing

- **`/tableau-de-bord` serves the login page while 2FA is pending.** It is a
  valid "are we authenticated" probe only *after* the challenge is cleared —
  going there straight from the login POST looks exactly like a failed login.

- **A service account cannot read the 2FA email.** The mailbox is a consumer
  Gmail account, and service accounts need Workspace domain-wide delegation to
  impersonate a user. IMAP with an app password is the only workable route.
- **Every invocation triggers a 2FA email.** There is no session persistence
  today. If invocation frequency grows, persisting the session cookie (Secret
  Manager or a GCS object) is the fix; the no-2FA branch of `_login` is already
  exercised by tests.
- **The 2FA email's `text/plain` part is HTML**, and its `<style>` block holds
  `color: #222222` — a six-digit run that precedes the real code. `_extract_code`
  strips style/script blocks and anchors on `code d'authentification est :`
  before falling back to a bare `\d{6}`. Do not "simplify" that regex.
- **The export is written verbatim** — no column mapping. `_sniff_rows` detects
  delimiter, encoding and CSV-vs-XLSX at runtime and refuses an empty export
  rather than wiping the sheet.
- **Badnet serves XLSX under `content-type: application/xls`**
  (`content-disposition: attachment; filename=stats_players.xlsx`). Detection
  must stay magic-byte-driven; trusting the header would reject the file as a
  legacy `.xls`.
- **The workbook is padded to 100 columns and carries a merged title banner**
  above the real 14-column header. `_normalize_grid` strips both — without it
  `addTable` fails on the blank header names and the diff is meaningless.
- **Two parameter conventions.** Sidebar nav links use a `data-ic_url` JSON blob;
  the export buttons use flat `data-season` / `data-assoid` / `data-popup`
  attributes. `_element_params` reads both — handling only the JSON form
  silently drops the season and club id.
- **`id="btnXls"` is duplicated** on two different export buttons, so a CSS-id
  selector picks the wrong one. Match on the visible label instead.

### Environment variables

See the table in `README.md`. Configured via `func.yaml` `run.envs`, injected
from the `update-badnet-secrets` Kubernetes secret.

### Tests

`tests/test_func.py` uses `pytest-asyncio` in strict mode and makes **no network
calls**. The HTTP session, IMAP mailbox and Sheets API all arrive through
factories on `BadnetUpdate.__init__` (`client_factory`, `imap_factory`,
`sheets_factory`), with `sleep` and `now` injected so backoff and the 2FA
staleness cutoff are deterministic. Fakes live in `tests/conftest.py`; HTML and
CSV fixtures in `tests/fixtures/`.

When adding behaviour, write the failing test first — the suite is organised in
phases matching the pipeline (contract, login, 2FA, navigation, retrieval,
decoding, sync, integration).
