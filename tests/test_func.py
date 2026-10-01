"""Tests for the Badnet -> Google Sheet function.

Grouped by the thing under test rather than by pipeline stage. Every case here
pins behaviour that a live run actually exhibited; the comments say which.
"""

import email
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (
    FakeClient,
    FakeIMAP,
    FakeResponse,
    fixture_bytes,
    fixture_text,
    make_email,
    make_multi_sheet_xlsx,
    make_sheets_service,
    make_xlsx,
)
from function import func
from function.func import EXPORT_LABEL, BadnetUpdate, new

CONNEXION = fixture_text("connexion.html")
VALIDATION = fixture_text("validation_code.html")
DASHBOARD = fixture_text("tableau_de_bord.html")
STATISTIQUES = fixture_text("statistiques.html")
REDIRECT_2FA = fixture_text("redirect_validation.html")
REDIRECT_SHELL = fixture_text("redirect_dashboard.html")
REAL_EMAIL = fixture_bytes("badnet_2fa_email.eml")

NOW = datetime(2026, 8, 12, 9, 5, 16, tzinfo=timezone.utc)
CODE_BODY = "Votre code d'authentification est : 321265"


async def _nosleep(seconds):
    pass


def make_updater(**kwargs):
    """A started-enough BadnetUpdate: config set, no I/O performed."""
    kwargs.setdefault("sleep", _nosleep)
    kwargs.setdefault("now", lambda: NOW)
    updater = BadnetUpdate(**kwargs)
    updater.username = "club@example.com"
    updater.password = "s3cret"
    updater.gmail_address = "club@example.com"
    updater.gmail_app_password = "app-pw"
    updater.sheet_id = "sheet-abc"
    updater.sheet_name = "Tournois"
    updater.portefeuille_sheet_name = "Portefeuille Badnet"
    updater.timeout = 120
    return updater


async def drive_handle(updater):
    sent = []

    async def send(message):
        sent.append(message)

    await updater.handle({"type": "http", "path": "/"}, None, send)
    return sent


# --------------------------------------------------------------------------
# Knative contract
# --------------------------------------------------------------------------


def test_new_returns_an_instance_that_reports_not_ready_until_started():
    updater = new()
    assert isinstance(updater, BadnetUpdate)
    assert updater.alive() == (True, "Alive")
    assert updater.ready() == (False, "Not initialized")


def test_start_reads_config_and_builds_the_sheets_client():
    service = make_sheets_service()
    built = []
    updater = BadnetUpdate(sheets_factory=lambda info: built.append(info) or service)

    updater.start({
        "BADNET_USERNAME": "club@example.com", "BADNET_PASSWORD": "s3cret",
        "GMAIL_ADDRESS": "club@example.com", "GMAIL_APP_PASSWORD": "app-pw",
        "GOOGLE_SHEETS_ID": "sheet-abc", "GOOGLE_SHEET_NAME": "Tournois",
        "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps({"client_email": "sa@example.com"}),
    })

    assert (updater.username, updater.sheet_id, updater.sheet_name) == (
        "club@example.com", "sheet-abc", "Tournois")
    assert updater.sheet is service
    assert built == [{"client_email": "sa@example.com"}]
    assert updater.ready() == (True, "Ready")


def test_start_rejects_missing_required_config():
    updater = BadnetUpdate(sheets_factory=lambda info: make_sheets_service())
    with pytest.raises(KeyError):
        updater.start({"BADNET_USERNAME": "club@example.com"})


@pytest.mark.asyncio
async def test_handle_returns_a_diff_per_sheet_as_json():
    updater = make_updater()
    diff = {"total": 2, "added": 1, "removed": 0, "columns": 14}

    async def fetch():
        return {"tournoi": make_xlsx([["Nom"], ["Dupont"]]),
                "portefeuille": wallet_workbook([ENGAGEMENT])}

    updater._fetch_all = fetch
    updater._update_sheet = lambda rows, sheet_name, **kw: diff

    sent = await drive_handle(updater)

    assert sent[0]["status"] == 200
    assert [b"content-type", b"application/json"] in sent[0]["headers"]
    assert json.loads(sent[1]["body"]) == {"tournoi": diff, "portefeuille": diff}


@pytest.mark.asyncio
async def test_handle_reports_a_login_failure_as_500_with_the_message():
    updater = make_updater()

    async def boom():
        raise ValueError("Badnet authentication failed")

    updater._fetch_all = boom

    sent = await drive_handle(updater)

    assert sent[0]["status"] == 500
    assert b"Badnet authentication failed" in sent[1]["body"]


# --------------------------------------------------------------------------
# Page parsing
# --------------------------------------------------------------------------


def test_parse_form_extracts_hidden_fields_and_resolves_the_action():
    action, data = func.parse_form(CONNEXION)
    assert action == "https://badnet.fr/index.php"
    assert data["ic_a"] == "a" * 32
    assert data["ic_ajax"] == "1"
    assert {"login", "pwd"} <= set(data)


def test_parse_form_raises_when_there_is_no_form():
    with pytest.raises(ValueError, match="form"):
        func.parse_form("<html><body>nothing</body></html>")


def test_page_predicates_distinguish_the_three_states():
    assert func.is_login_page(CONNEXION) and not func.is_2fa_page(CONNEXION)
    assert func.is_2fa_page(VALIDATION) and not func.is_login_page(VALIDATION)
    assert not func.is_login_page(DASHBOARD) and not func.is_2fa_page(DASHBOARD)


# --------------------------------------------------------------------------
# Script redirects
#
# Live: a successful POST returns 68 bytes of JavaScript, not a page.
# --------------------------------------------------------------------------


def test_redirect_target_reads_the_real_login_responses():
    assert func.redirect_target(REDIRECT_2FA) == "/validation-code"
    assert func.redirect_target(REDIRECT_SHELL) == "/tableau-de-bord"


@pytest.mark.parametrize("body", [
    '<script>location.href="/statistiques"</script>',
    "<script type='text/javascript'>location = '/statistiques';</script>",
    '<script>location="/statistiques"</script>\n<script>var x=1;</script>',
])
def test_redirect_target_accepts_the_common_shapes(body):
    assert func.redirect_target(body) == "/statistiques"


@pytest.mark.parametrize("body", [CONNEXION, VALIDATION, DASHBOARD, "", "<div>hi</div>"])
def test_redirect_target_ignores_real_pages(body):
    # Real pages carry analytics JS; only a script-only body is a redirect.
    assert func.redirect_target(body) is None


# --------------------------------------------------------------------------
# ic-click navigation
# --------------------------------------------------------------------------


def test_find_action_resolves_a_nav_link_from_its_data_ic_url():
    assert func.find_action(DASHBOARD, "Statistiques") == (
        "8a500ade7445616e671408c7af41031f", {})


def test_find_action_picks_the_right_export_button_by_label():
    # Two buttons share id="btnXls"; only the label distinguishes them, and the
    # parameters live in flat data-* attributes, not a data-ic_url blob.
    assert func.find_action(STATISTIQUES, EXPORT_LABEL) == (
        "dd9a33fae336d129eb0d2b36a3dcc116",
        {"season": "20", "assoid": "564", "popup": "1"})


def test_find_action_tolerates_a_list_shaped_data_ic_url():
    # Wallet tabs carry data-ic_url='[]' (a JSON list, not an object) plus their
    # params as flat data-* attributes. The list must contribute nothing, not crash.
    html = ('<a data-ic_a="abc" data-ic_url="[]" data-toggle="tab" '
            'data-user_id="U">Mon portefeuille BadNet</a>')
    assert func.find_action(html, "Mon portefeuille BadNet") == ("abc", {"user_id": "U"})


def test_find_action_ignores_accents_and_case():
    ic_a, _ = func.find_action(STATISTIQUES, "excel du detail des TOURNOIS des licencies")
    assert ic_a == "dd9a33fae336d129eb0d2b36a3dcc116"


def test_find_action_error_names_the_labels_it_did_see():
    with pytest.raises(ValueError) as excinfo:
        func.find_action(STATISTIQUES, "Bilan financier")
    message = str(excinfo.value)
    assert "Bilan financier" in message and "Excel des licenciés" in message


def test_find_action_error_quotes_the_body_when_the_page_is_not_html():
    # The failure mode when a POST response was parsed as a page.
    with pytest.raises(ValueError) as excinfo:
        func.find_action("<script>location='/x'</script>", "Statistiques")
    assert "0 element(s)" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ic_click_merges_params_and_sends_no_intercooler_headers():
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse(STATISTIQUES,
                                                    headers={"content-type": "text/html"})])

    await updater._ic_click(client, DASHBOARD, "Statistiques")

    call = client.get_calls[0]
    assert call["url"] == "https://badnet.fr/index.php"
    assert call["params"] == {"ic_ajax": "1", "ic_a": "8a500ade7445616e671408c7af41031f"}
    # This is a bespoke iclick library, not Intercooler.js.
    assert not any(k.lower().startswith("x-ic-") for k in (call["headers"] or {}))


@pytest.mark.asyncio
async def test_ic_click_rejects_an_expired_session():
    # An expired session answers with the login page, not a 401.
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse(CONNEXION,
                                                    headers={"content-type": "text/html"})])
    with pytest.raises(ValueError, match="authentication failed"):
        await updater._ic_click(client, DASHBOARD, "Statistiques")


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_posts_the_scraped_token_with_the_credentials():
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse(CONNEXION), FakeResponse(DASHBOARD)],
                        post_responses=[FakeResponse(DASHBOARD)])

    assert await updater._login(client) == DASHBOARD

    data = client.post_calls[0]["data"]
    assert client.post_calls[0]["url"] == "https://badnet.fr/index.php"
    assert data["ic_a"] == "a" * 32
    assert (data["login"], data["pwd"], data["remember"]) == (
        "club@example.com", "s3cret", "1")


@pytest.mark.asyncio
async def test_login_fails_when_the_shell_still_shows_the_login_page():
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse(CONNEXION), FakeResponse(CONNEXION)],
                        post_responses=[FakeResponse(CONNEXION)])

    with pytest.raises(ValueError) as excinfo:
        await updater._login(client)
    # The diagnostic must say what came back, not just that it failed.
    assert "status=" in str(excinfo.value)


@pytest.mark.asyncio
async def test_login_skips_imap_entirely_when_no_challenge_is_raised():
    imap = FakeIMAP()
    updater = make_updater(imap_factory=lambda: imap)
    client = FakeClient(get_responses=[FakeResponse(CONNEXION), FakeResponse(DASHBOARD)],
                        post_responses=[FakeResponse(DASHBOARD)])

    await updater._login(client)

    assert imap.logged_in is False


@pytest.mark.asyncio
async def test_login_follows_the_script_redirect_and_clears_the_challenge():
    # The real sequence: POST -> location='/validation-code' -> code ->
    # location='/tableau-de-bord'.
    imap = FakeIMAP(messages=[(NOW + timedelta(seconds=5), make_email(CODE_BODY))])
    updater = make_updater(imap_factory=lambda: imap)
    client = FakeClient(
        get_responses=[FakeResponse(CONNEXION), FakeResponse(VALIDATION),
                       FakeResponse(DASHBOARD), FakeResponse(DASHBOARD)],
        post_responses=[FakeResponse(REDIRECT_2FA), FakeResponse(REDIRECT_SHELL)])

    assert await updater._login(client) == DASHBOARD
    assert client.get_calls[1]["url"] == "https://badnet.fr/validation-code"
    assert client.post_calls[1]["data"]["code"] == "321265"
    assert client.post_calls[1]["data"]["ic_a"] == "b" * 32
    assert imap.trashed == ["1"]


@pytest.mark.asyncio
async def test_login_stops_following_a_redirect_loop():
    updater = make_updater()
    client = FakeClient(
        get_responses=([FakeResponse(CONNEXION)] + [FakeResponse(REDIRECT_2FA)] * 5
                       + [FakeResponse(CONNEXION)]),
        post_responses=[FakeResponse(REDIRECT_2FA)])

    with pytest.raises(ValueError, match="authentication failed"):
        await updater._login(client)
    assert len(client.get_calls) == 7  # login page + 5 capped hops + shell


@pytest.mark.asyncio
async def test_a_rejected_code_raises_and_keeps_the_email():
    imap = FakeIMAP(messages=[(NOW + timedelta(seconds=5), make_email(CODE_BODY))])
    updater = make_updater(imap_factory=lambda: imap)
    client = FakeClient(post_responses=[FakeResponse(VALIDATION)])

    with pytest.raises(ValueError, match="2FA"):
        await updater._submit_2fa(client, VALIDATION, submitted_at=NOW)
    # Unconsumed code: keep it for a retry or for diagnosis.
    assert imap.trashed == []


@pytest.mark.asyncio
async def test_a_failed_deletion_does_not_fail_the_run():
    imap = FakeIMAP(messages=[(NOW + timedelta(seconds=5), make_email(CODE_BODY))],
                    store_error=True)
    updater = make_updater(imap_factory=lambda: imap)
    client = FakeClient(post_responses=[FakeResponse(REDIRECT_SHELL)],
                        get_responses=[FakeResponse(DASHBOARD)])

    await updater._submit_2fa(client, VALIDATION, submitted_at=NOW)
    assert imap.trashed == []


@pytest.mark.asyncio
async def test_deletion_falls_back_to_the_standard_imap_delete():
    imap = FakeIMAP(messages=[(NOW + timedelta(seconds=5), make_email(CODE_BODY))],
                    gmail_labels=False)
    updater = make_updater(imap_factory=lambda: imap)
    client = FakeClient(post_responses=[FakeResponse(REDIRECT_SHELL)],
                        get_responses=[FakeResponse(DASHBOARD)])

    await updater._submit_2fa(client, VALIDATION, submitted_at=NOW)
    assert imap.deleted_flagged == ["1"] and imap.expunged is True


# --------------------------------------------------------------------------
# Reading the code out of the mail
# --------------------------------------------------------------------------


def test_extract_code_reads_the_real_badnet_template():
    text = func.message_text(email.message_from_bytes(REAL_EMAIL))
    assert func.extract_code(text) == "321265"


def test_extract_code_ignores_css_hex_colours():
    # Badnet mislabels its HTML body as text/plain and the <style> block holds
    # `color: #222222` *before* the code. A naive \d{6} returns 222222.
    body = """<style>h1 { color: #222222; } a { color: #333333; }</style>
              <p>Votre code d'authentification est : 321265</p>"""
    assert func.extract_code(body) == "321265"


@pytest.mark.parametrize("text, expected", [
    ("<script>var t = 999999;</script><p>code d'authentification est : 123456</p>", "123456"),
    ("code d’authentification est : 654321", "654321"),   # typographic apostrophe
    ("Votre code est 482913", "482913"),                  # unanchored fallback
    ("12345", None),
    ("reference 1234567 here", None),
    ("", None),
])
def test_extract_code_edge_cases(text, expected):
    assert func.extract_code(text) == expected


def test_message_date_uses_the_date_header():
    # Gmail's INTERNALDATE does not survive Internaldate2tuple; the Date header
    # is what actually resolves, and dropping undated mail is what made a
    # delivered code look like it never arrived.
    sent = NOW + timedelta(seconds=5)
    message = email.message_from_bytes(make_email(CODE_BODY, date=sent))
    assert func.message_date(message) == sent


def test_message_date_returns_none_without_a_usable_header():
    assert func.message_date(email.message_from_bytes(b"Subject: x\r\n\r\nbody")) is None


# --------------------------------------------------------------------------
# Polling the mailbox
# --------------------------------------------------------------------------


def _poll(updater, since=NOW):
    return updater._poll_inbox(since)


def test_poll_runs_one_gmail_query_scoped_to_recent_badnet_mail():
    imap = FakeIMAP(messages=[(NOW + timedelta(seconds=5), make_email(CODE_BODY))])
    updater = make_updater(imap_factory=lambda: imap)

    assert _poll(updater) == ("321265", "1")
    assert len(imap.searches) == 1
    query = " ".join(str(c) for c in imap.searches[0])
    assert "X-GM-RAW" in query and "from:badnet.fr" in query and "newer_than" in query
    assert imap.logged_out is True


def test_poll_ignores_a_code_from_an_earlier_session():
    imap = FakeIMAP(messages=[(NOW - timedelta(minutes=10), make_email(CODE_BODY))])
    updater = make_updater(imap_factory=lambda: imap)

    assert _poll(updater) is None
    assert "older than cutoff" in updater.poll_report


def test_poll_takes_the_most_recent_of_several_candidates():
    imap = FakeIMAP(messages=[
        (NOW + timedelta(seconds=10), make_email("code d'authentification est : 111111")),
        (NOW + timedelta(seconds=90), make_email("code d'authentification est : 222222")),
    ])
    assert _poll(make_updater(imap_factory=lambda: imap)) == ("222222", "2")


@pytest.mark.parametrize("imap, expected_note", [
    (FakeIMAP(messages=[]), "0 hit"),
    (FakeIMAP(search_error=True), "0 hit"),
    (FakeIMAP(login_error="AUTHENTICATIONFAILED bad app password"), "AUTHENTICATIONFAILED"),
])
def test_poll_report_explains_why_nothing_was_found(imap, expected_note):
    updater = make_updater(imap_factory=lambda: imap)
    assert _poll(updater) is None
    assert expected_note in updater.poll_report


def test_poll_report_flags_mail_without_a_code():
    imap = FakeIMAP(messages=[(NOW + timedelta(seconds=5), make_email("rien ici"))])
    updater = make_updater(imap_factory=lambda: imap)

    assert _poll(updater) is None
    assert "no 6-digit code" in updater.poll_report


@pytest.mark.asyncio
async def test_await_code_retries_then_succeeds():
    attempts = []
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    updater = make_updater(sleep=sleep)
    updater._poll_inbox = lambda since: (
        attempts.append(since) or (("321265", "1") if len(attempts) == 3 else None))

    assert await updater._await_code(since=NOW) == ("321265", "1")
    assert len(attempts) == 3
    assert slept == sorted(slept), "backoff must not shrink"


@pytest.mark.asyncio
async def test_await_code_times_out_carrying_the_poll_diagnostics():
    clock = {"t": NOW}

    async def sleep(seconds):
        clock["t"] += timedelta(seconds=seconds)

    imap = FakeIMAP(messages=[(NOW - timedelta(minutes=30), make_email(CODE_BODY))])
    updater = make_updater(imap_factory=lambda: imap, sleep=sleep,
                           now=lambda: clock["t"])
    updater.timeout = 30

    with pytest.raises(TimeoutError) as excinfo:
        await updater._await_code(since=NOW)
    # A bare "timed out" tells you nothing.
    assert "older than cutoff" in str(excinfo.value)


# --------------------------------------------------------------------------
# Reading the export
#
# Live shape: XLSX served as application/xls, a merged title banner on row 0,
# and every row padded out to 100 columns.
# --------------------------------------------------------------------------

REAL_HEADER = ["Nom prénom", "Licence", "Catégorie", "Tournoi", "Lieu", "Date", "Matchs",
               "Vainqueur", "Finaliste", "Troisième", "Montant dû", "Paiement joueur",
               "Paiement club", "Rbs joueur"]


def _pad(row, width=100):
    return list(row) + [""] * (width - len(row))


def real_shaped_export():
    return make_xlsx([
        _pad(["Compétitions des licenciés de Club Exemple — saison 2025-2026"]),
        _pad(REAL_HEADER),
        _pad(["DUPONT Marie", "07000001", "+35-V1", "Tournoi Exemple", "Ville Exemple",
              "Le 26 décembre 2025", "4", "1", "0", "0", "10", "0", "10", "0"]),
        _pad(["MARTIN Lucas", "07000002", "Sénior", "Tournoi Beta", "Ville Beta",
              "Les 4 et 5 juillet", "9", "1", "1", "0", "14", "0", "14", "0"]),
    ])


def test_read_export_strips_the_banner_and_the_column_padding():
    rows = func.read_export(real_shaped_export())
    assert rows[0] == REAL_HEADER
    assert all(len(row) == 14 for row in rows)
    assert rows[1][0] == "DUPONT Marie"
    assert len(rows) == 3


def test_read_export_stringifies_cells_and_blanks_nones():
    rows = func.read_export(make_xlsx([["Nom", "Licence"], ["Dupont", 7123456], ["X", None]]))
    assert rows == [["Nom", "Licence"], ["Dupont", "7123456"], ["X", ""]]


def test_read_export_drops_blank_rows():
    # openpyxl reports the workbook's trailing and interleaved blank rows; they
    # would otherwise land in the sheet as empty lines.
    rows = func.read_export(make_xlsx([
        ["Nom", "Licence"],
        ["Dupont", "1"],
        [None, None],
        ["Martin", "2"],
        ["", ""],
        [None, None],
    ]))
    assert rows == [["Nom", "Licence"], ["Dupont", "1"], ["Martin", "2"]]


def test_read_export_keeps_a_single_column_export_intact():
    # The banner heuristic must not eat a legitimately narrow export.
    rows = func.read_export(make_xlsx([["Licence"], ["07000001"]]))
    assert rows == [["Licence"], ["07000001"]]


@pytest.mark.parametrize("payload, message", [
    (b"", "empty export"),
    (b"   \r\n ", "empty export"),
    (b"\xd0\xcf\x11\xe0" + b"\x00" * 32, "legacy .xls"),
    (b"Nom;Licence\r\nDupont;07000001\r\n", "not an XLSX"),
])
def test_read_export_rejects_what_it_cannot_trust(payload, message):
    # Refusing beats wiping the sheet with garbage.
    with pytest.raises(ValueError, match=message):
        func.read_export(payload)


# --------------------------------------------------------------------------
# Reading the wallet (portefeuille) ledger
#
# Live shape: the "Excel financier du club" button downloads a multi-sheet
# workbook; only "Manipulations du portefeuille" is read. Its columns are
# Date | Auteur | Manipulation | Montant | Frais | Total, with a club-name
# banner above the header. Type and Tournoi are derived from the prose.
# --------------------------------------------------------------------------

SEASON_START = func.season_start(NOW)  # NOW is 2026-08-12 -> 2025-09-01

# One row of each kind, modelled on the real export wordings with fake names.
ENGAGEMENT = ("29-09-2026", "Club Exemple",
              "Paiements pour les joueurs DUPONT Marie, MARTIN Lucas "
              "pour le tournoi Tournoi Exemple #3", -22.0, 0.86, -22.86)
FRAIS_RBS = ("29-09-2026", "Club Exemple",
             "Tournoi Exemple #3 : remboursement des frais de service "
             "grâce à l'utilisation du portefeuille", 0.86, 0.0, 0.86)
ENG_RBS = ("15-09-2026", "Club Exemple", "Remboursement paiement en ligne - DUPONT Marie",
           11.0, 0.0, 11.0)
INSCRIPTION = ("20-09-2026", "Club Exemple",
               "Tournoi Exemple #15 : versement sur le portefeuille du club",
               340.0, 0.0, 340.0)
VIREMENT = ("10-09-2026", "Club Exemple", "Virement bancaire", -500.0, 0.0, -500.0)


def wallet_workbook(data_rows, with_other_sheets=True):
    manip = ([["Club Exemple"],
              ["Date", "Auteur", "Manipulation", "Montant", "Frais", "Total"]]
             + [list(r) for r in data_rows] + [[]])
    sheets = {func.MANIP_SHEET: manip}
    if with_other_sheets:
        # Prove the reader picks the named tab, not the first/active one.
        sheets = {"Paiement par des joueurs": [["x"]], func.MANIP_SHEET: manip}
    return make_multi_sheet_xlsx(sheets)


@pytest.mark.parametrize("manipulation, expected", [
    ("Virement bancaire", "virement"),
    ("Remboursement paiement en ligne - DUPONT Marie", "engagement-rembourse"),
    ("Paiements pour les joueurs X, Y pour le tournoi Z", "engagement"),
    ("Z : remboursement des frais de service grâce à l'utilisation du portefeuille",
     "frais-rembourse"),
    ("Tournoi Exemple #15 : versement sur le portefeuille du club",
     "inscription-tournoi"),
])
def test_classify_manipulation_maps_the_real_wordings(manipulation, expected):
    assert func.classify_manipulation(manipulation) == expected


def test_classify_manipulation_raises_on_an_unknown_wording():
    # A new Badnet label must surface loudly, never be silently mis-booked.
    with pytest.raises(ValueError, match="Unrecognised"):
        func.classify_manipulation("Quelque chose de totalement nouveau")


@pytest.mark.parametrize("text, kind, expected", [
    ("Paiements pour les joueurs X, Y pour le tournoi Tournoi Exemple #3", "engagement",
     "Tournoi Exemple #3"),
    ("Tournoi Exemple #3 : remboursement des frais de service grâce à l'utilisation du portefeuille",
     "frais-rembourse", "Tournoi Exemple #3"),
    ("Tournoi Exemple #15 : versement sur le portefeuille du club",
     "inscription-tournoi", "Tournoi Exemple #15"),
    ("Virement bancaire", "virement", ""),
    ("Remboursement paiement en ligne - DUPONT Marie", "engagement-rembourse", ""),
])
def test_extract_tournoi_mines_only_the_keyed_shapes(text, kind, expected):
    assert func.extract_tournoi(text, kind) == expected


def test_portefeuille_rows_reshapes_into_the_cashier_schema():
    rows = func.portefeuille_rows(wallet_workbook([ENGAGEMENT]), SEASON_START)
    assert rows[0] == ["Date", "Type", "Libellé", "Montant", "Frais", "Total", "Tournoi"]
    assert rows[1] == [
        "2026-09-29", "engagement", ENGAGEMENT[2], -22.0, 0.86, -22.86,
        "Tournoi Exemple #3"]


def test_portefeuille_rows_keeps_amounts_as_native_numbers():
    rows = func.portefeuille_rows(wallet_workbook([VIREMENT]), SEASON_START)
    assert rows[1][3:6] == [-500.0, 0.0, -500.0]
    assert all(isinstance(v, (int, float)) for v in rows[1][3:6])


def test_portefeuille_rows_filters_to_the_current_season():
    old = ("15-08-2025", "Club", "Virement bancaire", -1.0, 0.0, -1.0)  # before 1 Sep 2025
    rows = func.portefeuille_rows(wallet_workbook([old, VIREMENT]), SEASON_START)
    dates = [r[0] for r in rows[1:]]
    assert dates == ["2026-09-10"]  # the August 2025 movement is dropped


def test_portefeuille_rows_drops_the_footer_and_blank_rows():
    data = [ENGAGEMENT, ["Total", 100.0, 5.0], []]
    rows = func.portefeuille_rows(wallet_workbook(data), SEASON_START)
    assert len(rows) == 2  # header + the single real movement


def test_portefeuille_rows_picks_the_named_sheet_not_the_first():
    # with_other_sheets puts another tab first; the reader must still find ours.
    rows = func.portefeuille_rows(wallet_workbook([INSCRIPTION]), SEASON_START)
    assert rows[1][1] == "inscription-tournoi"


def test_portefeuille_rows_raises_when_the_sheet_is_absent():
    only_other = make_multi_sheet_xlsx({"Autre chose": [["Date"]]})
    with pytest.raises(ValueError, match="Manipulations du portefeuille"):
        func.portefeuille_rows(only_other, SEASON_START)


def test_portefeuille_rows_raises_on_an_empty_ledger():
    # No movements at all signals a broken export; refuse rather than wipe.
    with pytest.raises(ValueError, match="no data rows"):
        func.portefeuille_rows(wallet_workbook([]), SEASON_START)


def test_portefeuille_rows_allows_an_empty_in_season_result():
    # A season with movements only before 1 Sep is legitimate: header only, no raise.
    old = ("15-08-2025", "Club", "Virement bancaire", -1.0, 0.0, -1.0)
    rows = func.portefeuille_rows(wallet_workbook([old]), SEASON_START)
    assert rows == [func.PORTEFEUILLE_HEADER]


def test_portefeuille_rows_rejects_a_non_xlsx_payload():
    with pytest.raises(ValueError, match="not an XLSX"):
        func.portefeuille_rows(b"Date;Manipulation\r\n", SEASON_START)


def test_diff_rows_counts_added_and_removed():
    old = [["Nom", "Licence"], ["Dupont", "1"], ["Ancien", "2"]]
    new = [["Nom", "Licence"], ["Dupont", "1"], ["Nouveau", "3"]]
    assert func.diff_rows(old, new) == {
        "total": 2, "added": 1, "removed": 1, "columns": 2}


def test_diff_rows_treats_an_empty_sheet_as_all_added():
    new = [["Nom", "Licence"], ["Dupont", "1"]]
    assert func.diff_rows([], new) == {
        "total": 1, "added": 1, "removed": 0, "columns": 2}


# --------------------------------------------------------------------------
# Google Sheet sync
# --------------------------------------------------------------------------

ROWS = [["Nom", "Licence"], ["Dupont", "1"], ["Martin", "2"]]


def sync(service, rows=ROWS, sheet_name="Tournois", **kwargs):
    updater = make_updater()
    updater.sheet = service
    return updater._update_sheet(rows, sheet_name, **kwargs)


def call_names(service):
    return [name for name, _ in service.recorded_calls]


def test_update_sheet_writes_every_column_verbatim():
    service = make_sheets_service()
    sync(service)

    update = next(kw for name, kw in service.recorded_calls if name == "values.update")
    assert update["body"]["values"] == ROWS
    assert update["valueInputOption"] == "RAW"
    assert update["range"] == "Tournois!A1"


def test_update_sheet_deletes_tables_before_clearing_and_writing():
    service = make_sheets_service(tables=[{"tableId": "t1"}])
    sync(service)

    names = call_names(service)
    # deleteTable wipes cell data, so it has to precede the write.
    assert names.index("batchUpdate.deleteTable") < names.index("values.clear")
    assert names.index("values.clear") < names.index("values.update")
    assert names.index("values.update") < names.index("batchUpdate.addTable")


def test_update_sheet_reads_the_old_rows_before_clearing():
    service = make_sheets_service(existing_rows=[["Nom"], ["Dupont"]])
    sync(service)
    names = call_names(service)
    assert names.index("values.get") < names.index("values.clear")


def test_update_sheet_grows_a_grid_that_is_too_narrow():
    # Otherwise values.update fails with "exceeds grid limits".
    service = make_sheets_service(column_count=1)
    sync(service)

    resize = next(r for name, r in service.recorded_calls
                  if name == "batchUpdate.updateSheetProperties")
    assert resize["updateSheetProperties"]["properties"]["gridProperties"]["columnCount"] >= 2
    names = call_names(service)
    assert names.index("batchUpdate.updateSheetProperties") < names.index("values.update")


def test_update_sheet_leaves_a_big_enough_grid_alone():
    service = make_sheets_service(column_count=26)
    sync(service)
    assert "batchUpdate.updateSheetProperties" not in call_names(service)


def test_added_table_spans_the_real_extent():
    service = make_sheets_service()
    sync(service)

    add = next(r for name, r in service.recorded_calls if name == "batchUpdate.addTable")
    assert add["addTable"]["table"]["range"] == {
        "sheetId": 1234, "startRowIndex": 0, "endRowIndex": 3,
        "startColumnIndex": 0, "endColumnIndex": 2}


def test_update_sheet_names_the_available_tabs_when_the_target_is_missing():
    with pytest.raises(ValueError, match="Tournois"):
        sync(make_sheets_service(sheet_title="Autre"))


def test_update_sheet_creates_the_tab_when_asked_and_missing():
    # The wallet tab need not be set up by hand on the first run.
    service = make_sheets_service(sheet_title="Autre")
    sync(service, sheet_name="Portefeuille Badnet", create_missing=True)

    names = call_names(service)
    assert "batchUpdate.addSheet" in names
    # The tab is created, then written to.
    assert names.index("batchUpdate.addSheet") < names.index("values.update")
    update = next(kw for name, kw in service.recorded_calls if name == "values.update")
    assert update["range"] == "Portefeuille Badnet!A1"


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


# The three wallet navigation hops after the shell, each a minimal page whose
# only job is to carry the next hop's label. The shell (DASHBOARD) already holds
# the "Portefeuille" nav link.
HTML = {"content-type": "text/html"}
PORTE_PAGE = ('<html><body><a data-ic_a="p1" data-ic_ajax="1" '
              'data-user_id="U">Mon portefeuille BadNet</a></body></html>')
PORTE_TAB = ('<html><body><a data-ic_a="p2" data-ic_ajax="1" '
             "data-user_id=\"U\">Consulter l'historique du portefeuille</a></body></html>")
PORTE_HIST = ('<html><body><a data-ic_a="p3" data-ic_ajax="1" data-popup="1" '
              'data-user_id="U">Excel financier du club</a></body></html>')


def wallet_download(data_rows=(ENGAGEMENT, VIREMENT)):
    return FakeResponse(content=wallet_workbook(list(data_rows)),
                        headers={"content-type": "application/xls"})


def pipeline(tournoi_download, service, imap=None, porte_download=None):
    imap = imap or FakeIMAP(messages=[(NOW + timedelta(seconds=5), make_email(CODE_BODY))])
    if porte_download is None:
        porte_download = wallet_download()
    client = FakeClient(
        get_responses=[
            FakeResponse(CONNEXION), FakeResponse(VALIDATION),
            FakeResponse(DASHBOARD), FakeResponse(DASHBOARD),
            # tournoi chain
            FakeResponse(STATISTIQUES, headers=HTML), tournoi_download,
            # portefeuille chain
            FakeResponse(PORTE_PAGE, headers=HTML), FakeResponse(PORTE_TAB, headers=HTML),
            FakeResponse(PORTE_HIST, headers=HTML), porte_download,
        ],
        post_responses=[FakeResponse(REDIRECT_2FA), FakeResponse(REDIRECT_SHELL)])
    updater = make_updater(client_factory=lambda: client, imap_factory=lambda: imap)
    updater.sheet = service
    return updater, client


def written_ranges(service):
    return [kw["range"] for name, kw in service.recorded_calls if name == "values.update"]


@pytest.mark.asyncio
async def test_full_pipeline_logs_in_once_and_writes_both_sheets():
    tournoi = FakeResponse(content=real_shaped_export(),
                           headers={"content-type": "application/xls"})
    service = make_sheets_service()
    updater, client = pipeline(tournoi, service)

    sent = await drive_handle(updater)

    assert sent[0]["status"] == 200
    body = json.loads(sent[1]["body"])
    assert body["tournoi"] == {"total": 2, "added": 2, "removed": 0, "columns": 14}
    assert body["portefeuille"] == {"total": 2, "added": 2, "removed": 0, "columns": 7}

    # One login (one 2FA mail), both tabs written.
    assert client.post_calls[0]["data"]["login"] == "club@example.com"
    assert "Tournois!A1" in written_ranges(service)
    assert "Portefeuille Badnet!A1" in written_ranges(service)

    # The tournoi export request carries the button's own season and club id.
    assert client.get_calls[5]["params"] == {
        "season": "20", "assoid": "564", "popup": "1",
        "ic_ajax": "1", "ic_a": "dd9a33fae336d129eb0d2b36a3dcc116"}
    # The wallet export request carries the button's user id.
    assert client.get_calls[9]["params"] == {
        "user_id": "U", "popup": "1", "ic_ajax": "1", "ic_a": "p3"}


@pytest.mark.asyncio
async def test_wallet_rows_land_in_the_portefeuille_tab():
    service = make_sheets_service()
    tournoi = FakeResponse(content=real_shaped_export(),
                           headers={"content-type": "application/xls"})
    updater, _ = pipeline(tournoi, service,
                          porte_download=wallet_download([ENGAGEMENT, INSCRIPTION]))

    await drive_handle(updater)

    update = next(kw for name, kw in service.recorded_calls
                  if name == "values.update" and kw["range"] == "Portefeuille Badnet!A1")
    rows = update["body"]["values"]
    assert rows[0] == func.PORTEFEUILLE_HEADER
    assert [r[1] for r in rows[1:]] == ["engagement", "inscription-tournoi"]


@pytest.mark.asyncio
async def test_a_broken_wallet_export_still_syncs_the_tournoi_sheet():
    tournoi = FakeResponse(content=real_shaped_export(),
                           headers={"content-type": "application/xls"})
    service = make_sheets_service()
    # An empty wallet download: its sync fails, the tournoi sync must not.
    broken = FakeResponse(content=b"", headers={"content-type": "application/xls"})
    updater, _ = pipeline(tournoi, service, porte_download=broken)

    sent = await drive_handle(updater)

    assert sent[0]["status"] == 207
    body = json.loads(sent[1]["body"])
    assert body["tournoi"]["total"] == 2
    assert "empty export" in body["portefeuille"]["error"]
    assert "Tournois!A1" in written_ranges(service)
    assert "Portefeuille Badnet!A1" not in written_ranges(service)


@pytest.mark.asyncio
async def test_a_broken_tournoi_export_still_syncs_the_wallet_sheet():
    service = make_sheets_service()
    empty_tournoi = FakeResponse(content=b"", headers={"content-type": "application/xls"})
    updater, _ = pipeline(empty_tournoi, service)

    sent = await drive_handle(updater)

    assert sent[0]["status"] == 207
    body = json.loads(sent[1]["body"])
    assert "empty export" in body["tournoi"]["error"]
    assert body["portefeuille"]["total"] == 2
    assert "Tournois!A1" not in written_ranges(service)
    assert "Portefeuille Badnet!A1" in written_ranges(service)


@pytest.mark.asyncio
async def test_a_login_failure_returns_500_and_writes_nothing():
    service = make_sheets_service()
    client = FakeClient(
        get_responses=[FakeResponse(CONNEXION), FakeResponse(CONNEXION)],
        post_responses=[FakeResponse(CONNEXION)])
    updater = make_updater(client_factory=lambda: client)
    updater.sheet = service

    sent = await drive_handle(updater)

    assert sent[0]["status"] == 500
    assert b"authentication failed" in sent[1]["body"]
    assert service.recorded_calls == []


# --------------------------------------------------------------------------
# Season override
#
# The export button advertises the current season (data-season="20" =
# 2025-2026). BADNET_SEASON refines that so a past season can be pulled.
# --------------------------------------------------------------------------


def test_start_reads_the_optional_season():
    config = {
        "BADNET_USERNAME": "u", "BADNET_PASSWORD": "p",
        "GMAIL_ADDRESS": "g", "GMAIL_APP_PASSWORD": "gp",
        "GOOGLE_SHEETS_ID": "s", "GOOGLE_SHEET_NAME": "n",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
    }
    updater = BadnetUpdate(sheets_factory=lambda info: make_sheets_service())
    updater.start(config)
    assert updater.season is None

    updater = BadnetUpdate(sheets_factory=lambda info: make_sheets_service())
    updater.start(config | {"BADNET_SEASON": "19"})
    assert updater.season == "19"


@pytest.mark.asyncio
async def test_ic_click_substitutes_the_configured_season():
    updater = make_updater()
    updater.season = "19"
    client = FakeClient(get_responses=[FakeResponse("<html></html>",
                                                    headers={"content-type": "text/html"})])

    await updater._ic_click(client, STATISTIQUES, EXPORT_LABEL)

    params = client.get_calls[0]["params"]
    assert params["season"] == "19"
    # Everything else still comes from the button itself.
    assert params["assoid"] == "564"
    assert params["popup"] == "1"
    assert params["ic_a"] == "dd9a33fae336d129eb0d2b36a3dcc116"


@pytest.mark.asyncio
async def test_the_button_season_is_used_when_no_override_is_set():
    updater = make_updater()
    client = FakeClient(get_responses=[FakeResponse("<html></html>",
                                                    headers={"content-type": "text/html"})])

    await updater._ic_click(client, STATISTIQUES, EXPORT_LABEL)

    assert client.get_calls[0]["params"]["season"] == "20"


@pytest.mark.asyncio
async def test_a_season_override_never_invents_the_parameter(caplog):
    # The Statistiques nav link takes no season; silently adding one would send
    # a parameter the action does not understand.
    updater = make_updater()
    updater.season = "19"
    client = FakeClient(get_responses=[FakeResponse(STATISTIQUES,
                                                    headers={"content-type": "text/html"})])

    await updater._ic_click(client, DASHBOARD, "Statistiques")

    assert "season" not in client.get_calls[0]["params"]
    assert "BADNET_SEASON" in caplog.text, "a no-op override must not be silent"
