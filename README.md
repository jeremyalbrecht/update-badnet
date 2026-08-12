# update-badnet

Knative Python HTTP function that logs into [Badnet](https://badnet.fr) as a club
administrator, downloads the **"Excel du détail des tournois des licenciés"**
export, and replaces a Google Sheet tab with it for
[Augny Badminton](https://augny-badminton.fr).

## How it works

An HTTP request to the function triggers one full pass:

1. **Login** — `GET /connexion`, scrape the form (including the per-action
   `ic_a` token), then `POST` credentials to `/index.php` with `remember=1`.
   A successful login replies with **68 bytes of JavaScript, not a page**:
   `<script type="text/javascript">location='/validation-code' </script>`.
   Those script redirects are followed (capped at 5 hops) to reach the real
   page. Note that `/tableau-de-bord` serves the *login* page while 2FA is
   still pending, so it is only a valid session probe once the code is
   accepted.
2. **2FA** — Badnet emails a 6-digit code from `contact@badnet.fr`. The function
   polls the club mailbox over IMAP for a message that arrived *after* the login
   was submitted, so a code from an earlier session can never be reused, then
   submits it. **Once Badnet accepts the code, the email is moved to Trash**;
   if the code is rejected the message is left in place for retry or diagnosis,
   and a failed deletion never fails the run.
3. **Navigate** — Badnet's pages are driven by a bespoke `iclick` library: any
   element may carry `data-ic_a` (a server-generated action token) plus a
   `data-ic_url` JSON blob holding query parameters. The function finds elements
   by their visible French label and re-issues them as
   `GET /index.php?ic_ajax=1&ic_a=…&<params>`. Both the *Statistiques* page and
   the export button go through the same primitive.
4. **Download** — fetch the export and detect its format rather than assume it:
   CSV delimiter (`;`, `,`, tab), encoding (UTF-8 with or without BOM, cp1252),
   or XLSX by magic bytes. Badnet actually returns
   `stats_players.xlsx` under the content-type `application/xls`, so detection
   is driven by magic bytes — trusting the header would reject a valid workbook
   as a legacy `.xls`.
5. **Normalise** — the workbook pads every row to 100 columns and puts a merged
   title banner above the real header. Both are stripped; the surviving columns
   are exactly what Badnet sent. The live export is 14 columns:
   `Nom prénom, Licence, Catégorie, Tournoi, Lieu, Date, Matchs, Vainqueur,
   Finaliste, Troisième, Montant dû, Paiement joueur, Paiement club, Rbs joueur`.
6. **Sync** — full replacement of the target tab: read the old rows for the diff,
   drop any tables, grow the grid if the export is wider, clear, write every
   column verbatim, re-add the table. Responds with a JSON row-count summary.

Tokens are **never hardcoded** — `ic_a` is per-action and per-session, and the
export's `assoid`/`season` parameters come from the button's own `data-ic_url`.

## Environment variables

Read in `start(cfg)`; configured via `func.yaml` `run.envs`.

| Variable | Required | Purpose |
|---|---|---|
| `BADNET_USERNAME` | yes | Badnet login (email, licence, or numéro fédéral) |
| `BADNET_PASSWORD` | yes | Badnet password |
| `GMAIL_ADDRESS` | yes | Mailbox that receives the 2FA code |
| `GMAIL_APP_PASSWORD` | yes | Gmail **app password** for that mailbox (see below) |
| `GOOGLE_SHEETS_ID` | yes | Target spreadsheet ID |
| `GOOGLE_SHEET_NAME` | yes | Target sheet tab name |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | Full service-account JSON payload |
| `BADNET_2FA_TIMEOUT` | no | Seconds to wait for the code (default `120`) |
| `LOG_LEVEL` | no | `DEBUG` traces every request/response (default `INFO`) |
| `BADNET_SEASON` | no | Pull a past season instead of the current one |

### Choosing a season

Left unset, the export uses whatever season the button advertises — the current
one. `BADNET_SEASON` takes the *option value* from the site's season picker, not
a year:

| Value | Season |
|---|---|
| `20` | 2025-2026 (current) |
| `19` | 2024-2025 |
| `18` | 2023-2024 |

To read the full list, open **Statistiques** on badnet.fr and inspect the
`#lstSeason` dropdown — each `<option value="…">` is a valid setting.

The override only *substitutes* a season the action already takes; it never
introduces the parameter where it does not belong. If you set it and the action
has no season, the run logs a warning rather than silently doing nothing.

At runtime the secret values are injected from the `update-badnet-secrets`
Kubernetes secret in the `augny-badminton` namespace:

```bash
kubectl create secret generic update-badnet-secrets \
  --namespace augny-badminton \
  --from-literal=BADNET_PASSWORD='…' \
  --from-literal=GMAIL_APP_PASSWORD='…' \
  --from-literal=GOOGLE_SHEETS_ID='…' \
  --from-literal=GOOGLE_SHEET_NAME='…' \
  --from-file=GOOGLE_SERVICE_ACCOUNT_JSON=key.json
```

### Why an app password, not the Gmail API

`csjbad.augny@gmail.com` is a consumer Gmail account. Service accounts can only
impersonate mailboxes through Google Workspace domain-wide delegation, which
consumer accounts do not support — so the service account that writes the
spreadsheet cannot read the inbox. IMAP with a
[Gmail app password](https://myaccount.google.com/apppasswords) (requires 2-Step
Verification on the account) is the supported path.

The spreadsheet must be shared with the service account's `client_email` as an
Editor.

### A trap in the 2FA email

Badnet sends a multipart message whose **`text/plain` part is actually HTML**,
including a `<style>` block containing `color: #222222`. That hex colour is a
six-digit run appearing *before* the real code, so a naive `\d{6}` search
extracts `222222` and the login fails. `_extract_code` therefore strips
`<style>`/`<script>` blocks first and prefers the phrase
`code d'authentification est : NNNNNN` (with either apostrophe) before falling
back to a bare six-digit match. `tests/fixtures/badnet_2fa_email.eml` is the
real message, sanitized, and pins this behaviour down.

## Development

```bash
# Install dependencies (runtime + test)
pip install -e '.[dev]'

# Run tests — no network access required
pytest -q

# Run a single test
pytest tests/test_func.py::test_login_posts_scraped_ic_a_with_credentials

# Run locally, outside the container
func run --builder=host
curl -i localhost:8080/health/readiness
curl -i -X POST localhost:8080/
```

### Tests

`tests/test_func.py` runs under `pytest-asyncio` in strict mode and never touches
the network. Every outbound dependency — the HTTP session, the IMAP mailbox, and
the Sheets API — is injected through a factory on `BadnetUpdate.__init__`, with
fakes supplied by `tests/conftest.py`.

`tests/fixtures/` holds the login, 2FA and dashboard pages captured verbatim from
a real session (tokens replaced, addresses scrubbed), alongside a synthetic
statistics page and CSV exports in several delimiter/encoding combinations.

Both the statistics fragment and the export were verified against the live site
on 2026-08-11. If the export button is ever renamed, the error message lists
every label found on the page — paste the correct one into `EXPORT_LABEL`.

## Deployment

```bash
func deploy --registry docker.io/jeremyalbrecht
```

CI builds and pushes the image on every push to `main` that touches
`function/`, `tests/`, `pyproject.toml`, or `func.yaml`, gated on the test suite
passing (`.github/workflows/image-build.yml`).

## License

MIT
