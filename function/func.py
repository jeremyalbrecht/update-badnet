"""Knative HTTP function: sync the Badnet tournament export into a Google Sheet.

One request runs the whole pass: log in to badnet.fr, clear the emailed 2FA
challenge over IMAP, follow the ic-click navigation to the statistics page,
download the per-licensee tournament export, and replace a sheet tab with it.

Everything here is shaped by how Badnet actually behaves, which is often not
how it looks:

* A successful POST returns ``<script>location='…'</script>``, not a page.
* ``/tableau-de-bord`` serves the *login* page while 2FA is pending.
* Buttons carry their parameters as flat ``data-*`` attributes; nav links use a
  ``data-ic_url`` JSON blob. ``ic_a`` tokens are per-action and per-session.
* The export is XLSX served as ``content-type: application/xls``.
* The 2FA mail's ``text/plain`` part is HTML whose CSS contains ``#222222`` — a
  six-digit run sitting before the real code.
* Gmail's INTERNALDATE does not survive ``Internaldate2tuple``; the ``Date``
  header is what actually resolves.
"""

import asyncio
import email
import imaplib
import io
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build

BASE_URL = "https://badnet.fr"
INDEX_URL = f"{BASE_URL}/index.php"
LOGIN_URL = f"{BASE_URL}/connexion"
SHELL_URL = f"{BASE_URL}/tableau-de-bord"

STATISTIQUES_LABEL = "Statistiques"
EXPORT_LABEL = "Excel du détail des tournois des licenciés"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

IMAP_HOST = "imap.gmail.com"
# A real Gmail query, so it finds the mail even once a filter has archived it.
SEARCH_QUERY = ("X-GM-RAW", '"from:badnet.fr newer_than:1d"')
# Badnet's clock and ours can disagree by a little.
CLOCK_SKEW = timedelta(seconds=60)
DEFAULT_2FA_TIMEOUT = 120
MAX_POLLS = 40

CODE_RE = re.compile(r"code\s+d['’]authentification\s+est\s*:?\s*(\d{6})", re.I)
SIX_DIGITS_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
SCRIPT_ONLY_RE = re.compile(r"\s*(?:<script[^>]*>.*?</script>\s*)+\Z", re.S | re.I)
LOCATION_RE = re.compile(r"""location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""", re.I)
MAX_REDIRECTS = 5

# data-* attributes that drive iclick or Bootstrap; anything else is a request
# parameter. `ic_ajax` is excluded here because _ic_get always sets it.
NON_PARAM_ATTRS = frozenset({
    "ic_a", "ic_t", "ic_cb", "ic_url", "ic_ajax", "ic_datatype", "ic_select_search",
    "toggle", "target", "dialog", "tooltip", "placement", "original-title",
    "select2-id", "label", "dismiss",
})

XLSX_MAGIC = b"PK\x03\x04"
XLS_MAGIC = b"\xd0\xcf\x11\xe0"


def new():
    return BadnetUpdate()


# --------------------------------------------------------------------------
# Pure helpers — no I/O, so they are the easy half to test.
# --------------------------------------------------------------------------


def parse_form(html):
    """First form's resolved action URL and its pre-filled fields."""
    form = BeautifulSoup(html, "html.parser").find("form")
    if form is None:
        raise ValueError("No form found in the Badnet response")
    data = {
        str(f["name"]): f.get("value", "") or ""
        for f in form.find_all(("input", "select", "textarea"))
        if f.get("name")
    }
    return urljoin(f"{BASE_URL}/", form.get("action") or ""), data


def is_login_page(html):
    return 'name="pwd"' in html and 'name="login"' in html


def is_2fa_page(html):
    return 'name="code"' in html and "Valider le code" in html


def redirect_target(html):
    """URL of a ``<script>location='…'</script>`` body, else None.

    Only script-only bodies count, so analytics JS on a real page is never
    mistaken for a redirect.
    """
    if not html or not SCRIPT_ONLY_RE.fullmatch(html):
        return None
    match = LOCATION_RE.search(html)
    return match.group(1) if match else None


def normalize(text):
    """Casefolded, accent-stripped, whitespace-collapsed comparison key."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join(
        "".join(c for c in decomposed if not unicodedata.combining(c)).lower().split()
    )


def find_action(html, label):
    """``(ic_a, params)`` for the ic-click element showing ``label``.

    Matched on the visible label rather than a selector: two different export
    buttons share ``id="btnXls"``, so an id lookup picks the wrong one.
    """
    elements = BeautifulSoup(html, "html.parser").select("[data-ic_a]")
    wanted = normalize(label)
    for element in elements:
        text = element.get_text(" ", strip=True)
        if wanted == normalize(text) or wanted in normalize(text):
            return element["data-ic_a"], action_params(element)

    seen = sorted({e.get_text(" ", strip=True) for e in elements if e.get_text(strip=True)})
    raise ValueError(
        f"No ic-click action labelled {label!r}. Found {len(elements)} element(s) "
        f"with data-ic_a, labels: {seen}. Page starts: {' '.join(html[:300].split())!r}"
    )


def action_params(element):
    """Request parameters for an ic-click element.

    Nav links carry a ``data-ic_url`` JSON blob holding a query string; buttons
    carry flat ``data-season`` / ``data-assoid`` / ``data-popup`` attributes.
    Reading only the JSON form silently drops the season and club id.
    """
    params = {}
    raw = element.get("data-ic_url")
    if raw:
        try:
            params.update(parse_qsl(urlparse(json.loads(raw).get("url") or "").query))
        except (json.JSONDecodeError, TypeError):
            logging.warning("Unparseable data-ic_url: %r", raw)

    params.update({
        name[5:]: value
        for name, value in element.attrs.items()
        if name.startswith("data-") and name[5:] not in NON_PARAM_ATTRS
    })
    return params


def extract_code(text):
    """The 6-digit 2FA code.

    Badnet labels its HTML body ``text/plain`` and its ``<style>`` block holds
    ``color: #222222`` — a six-digit run appearing *before* the real code — so
    style and script blocks go first, and the introducing phrase wins over a
    bare match.
    """
    if not text:
        return None
    body = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", text)
    plain = unescape(re.sub(r"(?s)<[^>]+>", " ", body))
    match = CODE_RE.search(plain) or SIX_DIGITS_RE.search(plain)
    return match.group(1) if match else None


def message_text(message):
    """Best-effort text of a message, preferring text/plain parts."""
    if not message.is_multipart():
        payload = message.get_payload(decode=True) or b""
        return payload.decode(message.get_content_charset() or "utf-8", errors="replace")

    parts = {"text/plain": [], "text/html": []}
    for part in message.walk():
        if part.get_content_type() in parts:
            payload = part.get_payload(decode=True) or b""
            parts[part.get_content_type()].append(
                payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            )
    return "\n".join(parts["text/plain"] or parts["text/html"])


def message_date(message):
    """When a message was sent, or None.

    Gmail's INTERNALDATE does not reliably survive ``Internaldate2tuple``, so
    the Date header is what actually resolves here.
    """
    header = message.get("Date")
    if not header:
        return None
    try:
        sent = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    return sent.replace(tzinfo=timezone.utc) if sent.tzinfo is None else sent


def read_export(content):
    """Decode the export workbook into rows.

    Format is decided by magic bytes, never by content-type: Badnet serves
    genuine XLSX as ``application/xls``, which would otherwise be rejected as a
    legacy workbook.

    Badnet pads every row out to 100 columns, puts a merged title banner above
    the real header and leaves blank spacer rows in the sheet. All three are
    spreadsheet decoration, so stripping them is not a schema mapping — the
    surviving rows and columns are whatever Badnet sent.
    """
    if not content or not content.strip():
        raise ValueError("Badnet returned an empty export; refusing to clear the sheet")
    if content.startswith(XLS_MAGIC):
        raise ValueError("Badnet returned a legacy .xls workbook, which is not supported")
    if not content.startswith(XLSX_MAGIC):
        raise ValueError(f"Export is not an XLSX workbook (starts {content[:8]!r})")

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in workbook.active.iter_rows(values_only=True)
        ]
    finally:
        workbook.close()

    width = max((i + 1 for row in rows for i, c in enumerate(row) if c.strip()), default=0)
    if not width:
        raise ValueError("Badnet returned an empty export; refusing to clear the sheet")
    rows = [(row + [""] * width)[:width] for row in rows]

    populated = [sum(1 for c in row if c.strip()) for row in rows]
    if any(count > 1 for count in populated):
        first = next(i for i, count in enumerate(populated) if count > 1)
        if first:
            logging.info("Dropped %d preamble row(s) above the header", first)
        rows = rows[first:]
        populated = populated[first:]

    blank = sum(1 for count in populated if not count)
    if blank:
        logging.info("Dropped %d blank row(s)", blank)
        rows = [row for row, count in zip(rows, populated) if count]

    logging.info("Decoded export: %d rows x %d columns", len(rows), len(rows[0]))
    return rows


def diff_rows(old, new):
    """Row-count summary. The export has no reliable id column, so rows are
    compared whole rather than keyed."""
    old_set = {tuple(row) for row in old[1:]} if old else set()
    new_set = {tuple(row) for row in new[1:]}
    return {
        "total": len(new) - 1,
        "added": len(new_set - old_set),
        "removed": len(old_set - new_set),
        "columns": len(new[0]),
    }


def describe(resp):
    """Compact response summary for logs and error messages."""
    body = resp.text or ""
    return (
        f"status={getattr(resp, 'status_code', '?')} "
        f"type={resp.headers.get('content-type', '?')!r} chars={len(body)} "
        f"body={' '.join(body[:300].split())!r}"
    )


# --------------------------------------------------------------------------


class BadnetUpdate:
    """The function instance held by the Knative Python middleware."""

    _initialized = False

    def __init__(self, *, client_factory=None, sheets_factory=None,
                 imap_factory=None, sleep=None, now=None):
        # Outbound dependencies arrive as factories so tests run without a network.
        self._client = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=True, headers=HEADERS, timeout=60.0)
        )
        self._sheets = sheets_factory or _build_sheets
        self._imap = imap_factory or (lambda: imaplib.IMAP4_SSL(IMAP_HOST))
        self._sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.season = None
        self.poll_report = "not polled yet"

    # -- Knative lifecycle -------------------------------------------------

    def start(self, cfg):
        # LOG_LEVEL=DEBUG traces every request/response, which is what you want
        # the first time a scrape breaks against a changed page.
        logging.getLogger().setLevel(
            getattr(logging, (cfg.get("LOG_LEVEL") or "INFO").upper(), logging.INFO)
        )
        self.username = cfg["BADNET_USERNAME"]
        self.password = cfg["BADNET_PASSWORD"]
        self.gmail_address = cfg["GMAIL_ADDRESS"]
        self.gmail_app_password = cfg["GMAIL_APP_PASSWORD"]
        self.sheet_id = cfg["GOOGLE_SHEETS_ID"]
        self.sheet_name = cfg["GOOGLE_SHEET_NAME"]
        self.timeout = int(cfg.get("BADNET_2FA_TIMEOUT") or DEFAULT_2FA_TIMEOUT)
        # Optional: the season the export button advertises is used otherwise.
        self.season = cfg.get("BADNET_SEASON") or None
        self.sheet = self._sheets(json.loads(cfg["GOOGLE_SERVICE_ACCOUNT_JSON"]))

        self._initialized = True
        logging.info("Badnet Update started for %s -> sheet %s/%s",
                     self.username, self.sheet_id, self.sheet_name)

    async def handle(self, scope, receive, send):
        try:
            rows = read_export(await self._fetch_export())
            body = json.dumps(self._update_sheet(rows), ensure_ascii=False).encode()
            status, content_type = 200, b"application/json"
        except Exception as e:
            logging.exception("Failed to update the tournament sheet")
            status, content_type, body = 500, b"text/plain", str(e).encode()

        await send({"type": "http.response.start", "status": status,
                    "headers": [[b"content-type", content_type]]})
        await send({"type": "http.response.body", "body": body})

    def stop(self):
        logging.info("Function stopping")

    def alive(self):
        return True, "Alive"

    def ready(self):
        return (True, "Ready") if self._initialized else (False, "Not initialized")

    # -- scrape ------------------------------------------------------------

    async def _fetch_export(self):
        client = self._client()
        try:
            shell = await self._login(client)
            stats = await self._ic_click(client, shell, STATISTIQUES_LABEL)
            download = await self._ic_click(client, stats.text, EXPORT_LABEL)
            logging.info("Downloaded export: %d bytes (%s)", len(download.content),
                         download.headers.get("content-type"))
            return download.content
        finally:
            await client.aclose()

    async def _login(self, client):
        """Authenticate and return the app shell's HTML."""
        resp = await client.get(LOGIN_URL)
        logging.debug("GET login page -> %s", describe(resp))
        action, data = parse_form(resp.text)
        data |= {"login": self.username, "pwd": self.password, "remember": "1"}

        submitted_at = self._now()
        logging.info("Submitting Badnet login for %s", self.username)
        resp = await client.post(action, data=data)
        logging.debug("POST login -> %s", describe(resp))

        landing = await self._follow_redirects(client, resp)
        if is_2fa_page(landing.text):
            logging.info("Badnet raised an email 2FA challenge")
            await self._submit_2fa(client, landing.text, submitted_at)

        # Only meaningful once any challenge is cleared: while 2FA is pending
        # this URL serves the login page.
        shell = await client.get(SHELL_URL)
        logging.debug("GET shell -> %s", describe(shell))
        if is_login_page(shell.text):
            raise ValueError(f"Badnet authentication failed. Shell: {describe(shell)}")
        return shell.text

    async def _submit_2fa(self, client, html, submitted_at):
        action, data = parse_form(html)
        code, uid = await self._await_code(since=submitted_at)

        resp = await client.post(action, data=data | {"code": code})
        logging.debug("POST 2FA code -> %s", describe(resp))
        resp = await self._follow_redirects(client, resp)
        if is_2fa_page(resp.text) or is_login_page(resp.text):
            # Keep the mail: the code was never consumed, so it is still useful.
            raise ValueError("Badnet rejected the 2FA code")

        await asyncio.to_thread(self._trash_message, uid)

    async def _follow_redirects(self, client, resp):
        """Chase ``<script>location='…'</script>`` bodies to the real page."""
        for _ in range(MAX_REDIRECTS):
            target = redirect_target(resp.text)
            if not target:
                return resp
            url = urljoin(f"{BASE_URL}/", target)
            logging.info("Following script redirect to %s", url)
            resp = await client.get(url)
            logging.debug("GET %s -> %s", url, describe(resp))
        logging.warning("Gave up after %d script redirects", MAX_REDIRECTS)
        return resp

    async def _ic_click(self, client, html, label):
        """Follow the ic-click action carrying ``label``."""
        ic_a, params = find_action(html, label)
        if self.season:
            # Only substitute a season the action already takes; inventing one
            # would send a parameter this action does not understand.
            if "season" in params:
                params["season"] = self.season
            else:
                logging.warning("BADNET_SEASON=%s ignored: %r takes no season",
                                self.season, label)
        query = params | {"ic_ajax": "1", "ic_a": ic_a}
        logging.debug("ic GET %s %s", INDEX_URL, query)

        resp = await client.get(INDEX_URL, params=query)
        content_type = (resp.headers.get("content-type") or "").lower()
        if "html" not in content_type:
            logging.debug("ic GET -> %s, %d bytes", content_type, len(resp.content or b""))
            return resp

        logging.debug("ic GET -> %s", describe(resp))
        # An expired session answers with the login page, not a 401.
        if is_login_page(resp.text):
            raise ValueError("Badnet authentication failed: the session was rejected")
        return resp

    # -- 2FA code over IMAP -------------------------------------------------

    async def _await_code(self, since):
        """Poll until a code newer than ``since`` appears; ``(code, uid)``."""
        deadline = since + timedelta(seconds=self.timeout)
        delay = 3
        # Bounded by attempts as well as by the clock: a stopped or skewed
        # clock must not turn this into an endless loop inside a request.
        for _ in range(MAX_POLLS):
            found = await asyncio.to_thread(self._poll_inbox, since)
            if found:
                logging.info("Retrieved 2FA code from the mailbox")
                return found
            if self._now() >= deadline:
                break
            await self._sleep(delay)
            delay = min(delay * 2, 15)

        raise TimeoutError(
            f"No Badnet 2FA code within {self.timeout}s (wanted mail newer than "
            f"{since:%H:%M:%S}Z). Last poll: {self.poll_report}"
        )

    def _poll_inbox(self, since):
        """One IMAP round-trip. Every rejection is recorded in ``poll_report``
        so a timeout can say *why* nothing was found."""
        notes = []
        cutoff = since - CLOCK_SKEW
        imap = self._imap()
        try:
            imap.login(self.gmail_address, self.gmail_app_password)
            imap.select("INBOX")
            status, payload = imap.uid("SEARCH", None, *SEARCH_QUERY)
            uids = payload[0].split() if status == "OK" and payload and payload[0] else []
            notes.append(f"search -> {status} {len(uids)} hit(s)")

            best = None
            for uid in uids[-15:]:
                label = uid.decode(errors="replace")
                status, data = imap.uid("FETCH", uid, "(RFC822)")
                if status != "OK" or not data or not isinstance(data[0], tuple):
                    notes.append(f"uid {label}: FETCH={status}")
                    continue

                message = email.message_from_bytes(data[0][1])
                sent = message_date(message)
                if sent is None:
                    notes.append(f"uid {label}: no readable Date header")
                elif sent < cutoff:
                    notes.append(f"uid {label}: {sent.astimezone(timezone.utc):%H:%M:%S}Z "
                                 f"older than cutoff {cutoff:%H:%M:%S}Z")
                elif code := extract_code(message_text(message)):
                    notes.append(f"uid {label}: code found "
                                 f"({sent.astimezone(timezone.utc):%H:%M:%S}Z)")
                    if best is None or sent > best[0]:
                        best = (sent, code, label)
                else:
                    notes.append(f"uid {label}: fresh but no 6-digit code")

            return (best[1], best[2]) if best else None
        except Exception as e:
            logging.exception("IMAP poll failed")
            notes.append(f"EXCEPTION {type(e).__name__}: {e}")
            return None
        finally:
            self.poll_report = "; ".join(notes) or "no diagnostics"
            logging.debug("IMAP poll: %s", self.poll_report)
            self._logout(imap)

    def _trash_message(self, uid):
        """Bin a consumed 2FA mail. Never fatal — the code already worked."""
        imap = self._imap()
        try:
            imap.login(self.gmail_address, self.gmail_app_password)
            imap.select("INBOX")
            # Gmail's IMAP "deleted" behaviour is account-configurable and may
            # merely archive, so ask for the Trash label first.
            status, _ = imap.uid("STORE", uid, "+X-GM-LABELS", "\\Trash")
            if status != "OK":
                status, _ = imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
                if status == "OK":
                    imap.expunge()
            logging.info("Binned the used 2FA email (uid %s): %s", uid, status)
        except Exception:
            logging.exception("Could not delete the 2FA email (uid %s)", uid)
        finally:
            self._logout(imap)

    @staticmethod
    def _logout(imap):
        try:
            imap.logout()
        except Exception:
            pass

    # -- Google Sheet -------------------------------------------------------

    def _update_sheet(self, rows):
        """Replace the target tab with ``rows`` and report what changed."""
        spreadsheet = self.sheet.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        meta = next((s for s in spreadsheet["sheets"]
                     if s["properties"]["title"] == self.sheet_name), None)
        if meta is None:
            titles = [s["properties"]["title"] for s in spreadsheet["sheets"]]
            raise ValueError(f"Sheet tab {self.sheet_name!r} not found. Tabs: {titles}")

        sheet_id = meta["properties"]["sheetId"]
        api = self.sheet.spreadsheets().values()
        old = api.get(spreadsheetId=self.sheet_id, range=self.sheet_name).execute()
        old_rows = old.get("values", [])

        requests = [{"deleteTable": {"tableId": t["tableId"]}} for t in meta.get("tables", [])]
        grid = meta["properties"].get("gridProperties", {})
        if grid.get("columnCount", 0) < len(rows[0]) or grid.get("rowCount", 0) < len(rows):
            # Otherwise values.update fails outright with "exceeds grid limits".
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {
                    "rowCount": max(grid.get("rowCount", 0), len(rows)),
                    "columnCount": max(grid.get("columnCount", 0), len(rows[0]))}},
                "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
        # deleteTable wipes cell data, so both must precede the write.
        self._batch(requests)

        api.clear(spreadsheetId=self.sheet_id, range=self.sheet_name).execute()
        api.update(spreadsheetId=self.sheet_id, range=f"{self.sheet_name}!A1",
                   valueInputOption="RAW", body={"values": rows}).execute()

        self._batch([{"addTable": {"table": {
            "name": self.sheet_name,
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": len(rows),
                      "startColumnIndex": 0, "endColumnIndex": len(rows[0])}}}}])

        diff = diff_rows(old_rows, rows)
        logging.info("Updated sheet %r with %d data rows: %d added, %d removed",
                     self.sheet_name, diff["total"], diff["added"], diff["removed"])
        return diff

    def _batch(self, requests):
        if requests:
            self.sheet.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id, body={"requests": requests}
            ).execute()


def _build_sheets(sa_info):
    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=credentials)
