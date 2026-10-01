"""Shared test doubles and fixture loaders.

Nothing here touches the network: the badnet session, the Gmail inbox and the
Google Sheets API are all reached through factories injected into
``BadnetUpdate.__init__``, and this module supplies the fakes for each.
"""

import imaplib
import io
import sys
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    """Stand-in for httpx.Response covering only what the function reads."""

    def __init__(self, text="", content=None, headers=None, status_code=200, url=""):
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")
        return self


class FakeClient:
    """Async stub for httpx.AsyncClient; responses are consumed in order."""

    def __init__(self, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        if not self.get_responses:
            raise AssertionError(f"Unexpected GET call to {url} params={params}")
        return self.get_responses.pop(0)

    async def post(self, url, data=None, headers=None):
        self.post_calls.append({"url": url, "data": data, "headers": headers})
        if not self.post_responses:
            raise AssertionError(f"Unexpected POST call to {url}")
        return self.post_responses.pop(0)

    async def aclose(self):
        pass


def make_email(body, sender="BadNet <contact@badnet.fr>",
               subject="[BadNet] Code authentification BadNet", date=None):
    """Build a raw RFC822 message as imaplib would hand it back."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "club@example.com"
    msg["Subject"] = subject
    if date is not None:
        msg["Date"] = format_datetime(date)
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    """imaplib.IMAP4_SSL stub driven through the UID command interface.

    ``messages`` is a list of ``(internaldate, raw_bytes)`` pairs; the fake
    reports every one as a search hit and lets the production code do the
    date filtering, so the staleness guard is genuinely exercised.

    UIDs are 1-based indices into ``messages``. State is shared across
    connections — the production code reconnects to delete — so ``trashed``
    survives the logout of the polling connection.
    """

    def __init__(self, messages=(), search_error=False, store_error=False,
                 gmail_labels=True, broken_internaldate=False, login_error=None):
        self.messages = list(messages)
        self.search_error = search_error
        self.store_error = store_error
        self.gmail_labels = gmail_labels
        self.broken_internaldate = broken_internaldate
        self.login_error = login_error
        self.logged_in = False
        self.logged_out = False
        self.selected = None
        self.searches = []
        self.trashed = []
        self.deleted_flagged = []
        self.expunged = False

    def login(self, user, password):
        if self.login_error:
            raise RuntimeError(self.login_error)
        self.logged_in = True
        self.credentials = (user, password)
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox="INBOX", readonly=False):
        self.selected = mailbox
        return ("OK", [str(len(self.messages)).encode()])

    def uid(self, command, *args):
        handler = getattr(self, f"_uid_{command.lower()}", None)
        if handler is None:
            return ("NO", [f"unsupported: {command}".encode()])
        return handler(*args)

    def _uid_search(self, charset, *criteria):
        self.searches.append(criteria)
        if self.search_error:
            return ("NO", [b"search failed"])
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return ("OK", [ids])

    def _uid_fetch(self, uid, parts):
        when, raw = self.messages[int(uid) - 1]
        # Real mail always carries a Date header; add one from the message's
        # nominal arrival time so call sites don't have to repeat themselves.
        if b"\nDate:" not in raw and not raw.startswith(b"Date:"):
            raw = f"Date: {format_datetime(when)}\r\n".encode() + raw
        if self.broken_internaldate:
            # Some servers order the FETCH items differently; the parser must
            # not silently drop the message when it cannot read INTERNALDATE.
            envelope = f"{uid} (UID {uid} RFC822 {{{len(raw)}}}".encode()
            return ("OK", [(envelope, raw), b")"])
        # Time2Internaldate emits the real IMAP wire format, quotes included,
        # so the production parser is exercised rather than a convenient stub.
        internaldate = imaplib.Time2Internaldate(when.timestamp())
        envelope = f"{uid} (UID {uid} INTERNALDATE {internaldate} RFC822 {{{len(raw)}}}".encode()
        return ("OK", [(envelope, raw), b")"])

    def _uid_store(self, uid, flag_command, value):
        if self.store_error:
            return ("NO", [b"store failed"])
        if value == "\\Trash":
            if not self.gmail_labels:
                return ("NO", [b"X-GM-LABELS not supported"])
            self.trashed.append(str(uid))
        elif value == "\\Deleted":
            self.deleted_flagged.append(str(uid))
        return ("OK", [b"STORE completed"])

    def expunge(self):
        self.expunged = True
        return ("OK", [b"EXPUNGE completed"])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"logout"])

    def close(self):
        pass


def make_sheets_service(existing_rows=None, sheet_title="Tournois", column_count=26, tables=()):
    """MagicMock shaped like the Sheets v4 discovery client.

    Records the order of the calls the sync makes so tests can assert that
    tables are deleted before the clear/write (deleting a table wipes cells).
    """
    calls = []
    service = MagicMock()
    spreadsheets = service.spreadsheets.return_value
    values = spreadsheets.values.return_value

    meta = {
        "sheets": [
            {
                "properties": {
                    "title": sheet_title,
                    "sheetId": 1234,
                    "gridProperties": {"rowCount": 1000, "columnCount": column_count},
                },
                "tables": list(tables),
            }
        ]
    }

    def _get(**kwargs):
        calls.append(("spreadsheets.get", kwargs))
        return MagicMock(execute=MagicMock(return_value=meta))

    def _values_get(**kwargs):
        calls.append(("values.get", kwargs))
        return MagicMock(
            execute=MagicMock(return_value={"values": list(existing_rows or [])})
        )

    def _values_clear(**kwargs):
        calls.append(("values.clear", kwargs))
        return MagicMock(execute=MagicMock(return_value={}))

    def _values_update(**kwargs):
        calls.append(("values.update", kwargs))
        return MagicMock(execute=MagicMock(return_value={}))

    def _batch_update(**kwargs):
        for request in kwargs.get("body", {}).get("requests", []):
            kind = next(iter(request))
            calls.append((f"batchUpdate.{kind}", request))
            if kind == "addSheet":
                # Reflect the new tab so the re-fetch in _update_sheet finds it,
                # exactly as the real API would.
                props = request["addSheet"]["properties"]
                meta["sheets"].append({
                    "properties": {
                        "title": props["title"],
                        "sheetId": 9999,
                        "gridProperties": {"rowCount": 1000, "columnCount": 26},
                    },
                    "tables": [],
                })
        return MagicMock(execute=MagicMock(return_value={}))

    spreadsheets.get.side_effect = _get
    spreadsheets.batchUpdate.side_effect = _batch_update
    values.get.side_effect = _values_get
    values.clear.side_effect = _values_clear
    values.update.side_effect = _values_update

    service.recorded_calls = calls
    return service


def make_xlsx(rows):
    """Build an in-memory .xlsx so no binary fixture has to be checked in."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_multi_sheet_xlsx(sheets):
    """An in-memory workbook with several named tabs, in insertion order.

    ``sheets`` maps a tab title to its rows. Used to exercise the wallet export,
    which is a multi-sheet workbook the reader must pick a named tab out of.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title=title)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def sheets_service():
    return make_sheets_service()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test opens a real socket.

    The suite is meant to be hermetic; a missing injected factory would
    otherwise quietly reach badnet.fr, Gmail or the Sheets API.
    """
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError(
            "Test attempted a real network connection — inject a fake factory instead"
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
