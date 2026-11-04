"""Le compte Google d'un appel se choisit par le mécanisme COMMUN des connecteurs
multi-compte (oto-backend#1160).

Les outils Google (gmail, drive, sheets, calendar, tasks, chat, bigquery) annonçaient
l'axe d'appel `_account=` dans leur schéma, mais `credentials_for` ne lisait que leur
paramètre `account` ou le compte épinglé par le projet : un agent qui suivait le
schéma avec deux comptes liés était servi par le compte PAR DÉFAUT — lire ou envoyer
depuis la mauvaise boîte.

Le choix passe désormais par `access.resolve_credential`, comme `sharepoint` (#23) :
`account` (paramètre de l'outil) ou `_account=` (axe), puis le compte épinglé par le
projet (sur la carte du service, sinon sur celle du compte Google), puis le compte
unique, puis le défaut, sinon un refus qui nomme les comptes. Un compte inconnu est
refusé, jamais remplacé par un autre. Les deux noms d'un même choix qui divergent :
refus nommé (`account_conflict`).

Le coffre est un faux en mémoire, le client Google est moqué à sa frontière : aucun
appel réseau ni base. La RÉSOLUTION, elle, est la vraie.
"""
from __future__ import annotations

import sys
import types

import pytest

from _coffre_google import installer
from oto_mcp.mcp_errors import McpError

ORG, GROUP = 42, 9
SUB = "user-de-test"
MEMBRE = f"{ORG}:{SUB}"
A, B = "alice@exemple.test", "bob@exemple.test"
PARTAGE = "hello@exemple.test"
SERVICES = ("gmail", "drive", "sheets", "calendar", "tasks", "chat", "bigquery")
# Le module d'outils de chaque service et la classe cliente qu'il instancie.
CLIENTS = {
    "gmail": "oto.tools.google.gmail.lib.gmail_client:GmailClient",
    "drive": "oto.tools.google.drive.lib.drive_client:DriveClient",
    "sheets": "oto.tools.google.sheets.lib.sheets_client:SheetsClient",
    "calendar": "oto.tools.google.calendar.lib.calendar_client:CalendarClient",
    "tasks": "oto.tools.google.tasks.lib.tasks_client:TasksClient",
    "chat": "oto.tools.google.chat.lib.chat_client:ChatClient",
    "bigquery": "oto.tools.google.bigquery.lib.bigquery_client:BigQueryClient",
}


@pytest.fixture
def env(monkeypatch):
    return installer(monkeypatch, org=ORG, sub=SUB)


def _deux_comptes(env, defaut=A):
    env.coffre.poser(A, "RT-A", defaut=defaut == A, access_token="AT-A")
    env.coffre.poser(B, "RT-B", defaut=defaut == B, access_token="AT-B")


def _creds(service, account=None, **k):
    from oto_mcp.auth import google as G
    return G.credentials_for(SUB, account=account, service=service, **k)


# ── 1. Le banc rouge de #1160 : `_account=` désigne le compte qui sert ─────────

def _outil(monkeypatch, service):
    """`_client_for_user` du module d'outils du service, sa classe cliente moquée à sa
    frontière : rend les credentials qu'elle a reçus."""
    import importlib

    chemin, classe = CLIENTS[service].split(":")
    faux = types.ModuleType(chemin)
    setattr(faux, classe, lambda credentials, **k: credentials)
    monkeypatch.setitem(sys.modules, chemin, faux)
    module = importlib.import_module(f"oto_mcp.tools.{service}")
    monkeypatch.setattr(module.access, "current_user_sub_or_raise", lambda: SUB)
    return module._client_for_user


@pytest.mark.parametrize("service", SERVICES)
def test_l_axe_account_designe_le_compte_qui_sert(env, monkeypatch, service):
    """Deux comptes liés, `_account=` sur le compte NON défaut : c'est lui qui sert,
    pour chaque famille d'outils Google — jamais le défaut."""
    _deux_comptes(env)
    client_for_user = _outil(monkeypatch, service)
    creds = env.sous_compte(B, client_for_user)
    assert (creds.token, creds.refresh_token) == ("AT-B", "RT-B")


# ── 2. Le choix du compte, famille par famille ────────────────────────────────

@pytest.mark.parametrize("service", SERVICES)
def test_sans_rien_nommer_le_compte_par_defaut(env, service):
    _deux_comptes(env, defaut=B)
    assert _creds(service).refresh_token == "RT-B"


@pytest.mark.parametrize("service", SERVICES)
def test_un_seul_compte_sert_sans_defaut(env, service):
    env.coffre.poser(A, "RT-A")
    assert _creds(service).refresh_token == "RT-A"


@pytest.mark.parametrize("service", SERVICES)
def test_plusieurs_comptes_sans_defaut_refus_qui_les_nomme(env, service):
    _deux_comptes(env, defaut=None)
    with pytest.raises(McpError) as e:
        _creds(service)
    message = str(e.value)
    assert A in message and B in message and "_account" in message


@pytest.mark.parametrize("service", SERVICES)
def test_compte_inconnu_refuse_jamais_un_autre(env, service):
    """Par l'axe comme par le paramètre : un refus qui nomme les comptes liés et la
    forme attendue (une adresse), jamais le défaut à la place."""
    _deux_comptes(env)
    for appel in (lambda: env.sous_compte("inconnu@exemple.test", _creds, service),
                  lambda: _creds(service, account="inconnu@exemple.test")):
        with pytest.raises(RuntimeError) as e:
            appel()
        message = str(e.value)
        assert "inconnu@exemple.test" in message and A in message and B in message


@pytest.mark.parametrize("service", SERVICES)
def test_sans_compte_le_refus_dit_de_connecter(env, monkeypatch, service):
    from oto_mcp import access
    from oto_mcp.auth import google as G

    # Les indices du refus générique lisent la base (révocations, instances à
    # portée) : hors sujet ici, c'est le refus de Google qu'on vérifie.
    for indice in ("_revoked_hint", "_reachable_hint", "_poser_ou_accorder"):
        monkeypatch.setattr(access, indice, lambda *a, **k: "")
    monkeypatch.setattr(G, "_reconnecter", lambda sub: "https://app.exemple.test/c")
    with pytest.raises(RuntimeError, match="No Google account connected"):
        _creds(service)


# ── 3. Le paramètre `account` et l'axe `_account=` : un seul choix ────────────

@pytest.mark.parametrize("service", SERVICES)
def test_le_parametre_account_sert_comme_avant(env, service):
    _deux_comptes(env)
    assert _creds(service, account=B).refresh_token == "RT-B"


@pytest.mark.parametrize("service", SERVICES)
def test_les_deux_noms_concordants_servent(env, service):
    _deux_comptes(env)
    assert env.sous_compte(B, _creds, service, account=B).refresh_token == "RT-B"


@pytest.mark.parametrize("service", SERVICES)
def test_les_deux_noms_divergents_sont_un_refus_nomme(env, service):
    _deux_comptes(env)
    with pytest.raises(McpError) as e:
        env.sous_compte(B, _creds, service, account=A)
    assert e.value.error.data == {"code": "account_conflict", "retryable": False}
    assert A in str(e.value) and B in str(e.value)


def test_le_compte_d_une_piece_jointe_peut_differer_de_celui_de_l_appel(env):
    """`gmail_act(op="send", _account=B, attachments=[{kind: drive, account: A}])` : envoyer
    depuis B un fichier du Drive de A. Le compte d'une SOURCE n'est pas le paramètre
    de l'outil — aucun conflit."""
    _deux_comptes(env)
    creds = env.sous_compte(B, _creds, "drive", account=A, source_account=True)
    assert creds.refresh_token == "RT-A"


# ── 4. L'épinglage du projet ──────────────────────────────────────────────────

@pytest.mark.parametrize("service", SERVICES)
def test_le_projet_epingle_le_compte(env, service):
    """Un lien posé sur la carte du compte Google (`google`, tous les liens d'avant
    le split) épingle le compte de tous ses services ; celui de la carte du service,
    plus précis, l'emporte."""
    _deux_comptes(env)
    env.epingles["google"] = B
    assert _creds(service).refresh_token == "RT-B"
    env.epingles[service] = A
    assert _creds(service).refresh_token == "RT-A"


@pytest.mark.parametrize("service", SERVICES)
def test_l_appel_l_emporte_sur_l_epinglage(env, service):
    _deux_comptes(env)
    env.epingles["google"] = A
    assert env.sous_compte(B, _creds, service).refresh_token == "RT-B"
    assert _creds(service, account=B).refresh_token == "RT-B"


def test_un_compte_epingle_inconnu_est_refuse(env):
    _deux_comptes(env)
    env.epingles["google"] = "parti@exemple.test"
    with pytest.raises(RuntimeError, match="parti@exemple.test"):
        _creds("gmail")


# ── 5. Comptes partagés, renouvellement, écho ─────────────────────────────────

def test_un_compte_partage_se_nomme_par_l_axe(env, monkeypatch):
    """Le membre a son compte ; l'org en partage un autre. `_account=` sur le
    partagé : c'est lui qui sert, lu sur l'entité de l'org."""
    from oto_mcp import access

    monkeypatch.setattr(access, "current_group", lambda s: GROUP)
    env.coffre.poser(A, "RT-A", defaut=True)
    env.coffre.poser(PARTAGE, "RT-ORG", entite=("org", str(ORG)), defaut=True)
    assert _creds("gmail").refresh_token == "RT-A"
    assert env.sous_compte(PARTAGE, _creds, "gmail").refresh_token == "RT-ORG"


def test_le_renouvellement_s_ecrit_sur_la_ligne_du_compte_qui_sert(env, monkeypatch):
    import requests
    from oto_mcp.connectors import health

    _deux_comptes(env)
    env.coffre.meta(B)["access_token"] = None
    sante = []
    monkeypatch.setattr(health, "record_health",
                        lambda prov, scope, ok, err: sante.append(scope))

    class _OK:
        status_code, text = 200, ""

        def json(self):
            return {"access_token": "AT-NEUF", "expires_in": 3600}

        def raise_for_status(self):
            pass
    monkeypatch.setattr(requests, "post", lambda *a, **k: _OK())
    creds = env.sous_compte(B, _creds, "gmail")
    assert creds.token == "AT-NEUF"
    assert env.coffre.meta(B)["access_token"] == "AT-NEUF"
    assert env.coffre.meta(A)["access_token"] == "AT-A"
    assert sante == [("member", MEMBRE, B)]


@pytest.mark.parametrize("service", SERVICES)
def test_la_reponse_dit_quel_compte_a_servi(env, service):
    """Le relevé de l'appel porte le compte résolu et le service appelé : c'est ce
    que l'écho `_account` des réponses (`middleware/call_context._echo_account`) lit."""
    from oto_mcp import session_org

    _deux_comptes(env)
    jeton = session_org._CALL_TRACE.set({})
    try:
        env.sous_compte(B, _creds, service)
        trace = session_org.current_call_trace()
    finally:
        session_org._CALL_TRACE.reset(jeton)
    assert (trace["resolved_connector"], trace["resolved_account"]) == (service, B)


def test_l_echo_nomme_le_compte_sur_la_reponse_d_un_outil_gmail():
    from oto_mcp import session_org
    from oto_mcp.middleware import call_context

    resultat = types.SimpleNamespace(is_error=False, structured_content={"messages": []},
                                     content=[])
    jeton = session_org._CALL_TRACE.set({"resolved_connector": "gmail",
                                         "resolved_account": B})
    try:
        vu = {}
        orig_extract, orig_rebuild = (call_context.redaction.extract_payload,
                                      call_context.redaction.rebuild_result)
        call_context.redaction.extract_payload = lambda r: r.structured_content
        call_context.redaction.rebuild_result = lambda r, p: vu.setdefault("payload", p)
        try:
            call_context._echo_account(resultat, "gmail")
        finally:
            call_context.redaction.extract_payload = orig_extract
            call_context.redaction.rebuild_result = orig_rebuild
    finally:
        session_org._CALL_TRACE.reset(jeton)
    assert vu["payload"]["_account"] == B


# ── 6. L'axe est annoncé ET lu sur chaque outil Google ────────────────────────

def test_l_axe_account_est_annonce_et_lu_sur_chaque_outil_google():
    from oto_mcp import call_axes, providers

    outils = [t for svc in SERVICES for t in _outils_du_service(svc)]
    assert outils
    for outil in outils:
        assert "_account" in {a.param for a in call_axes.axes_for(outil)}, outil
        assert "_account" in {a.param for a in call_axes.axes_for_call(outil)}, outil
    assert providers.REGISTRY["google"].account_axis_static


def _outils_du_service(service):
    import importlib

    module = importlib.import_module(f"oto_mcp.tools.{service}")
    noms = []

    class _Faux:
        def tool(self, *a, **k):
            def deco(fn):
                noms.append(k.get("name") or fn.__name__)
                return fn
            return deco(a[0]) if a and callable(a[0]) else deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)
    module.register(_Faux())
    return [n for n in noms if n.startswith(f"{service}_")]
