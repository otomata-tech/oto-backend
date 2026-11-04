"""Le scope LEGACY `("user", sub)` dans le walker de cascade (oto-backend#876).

`atlassian` et `folkmcp` n'ont jamais migré au scope membre (ADR 0033 — Google l'a
fait en B3, commit 79759702 ; ces deux-là ont été laissés de côté, DÉLIBÉRÉMENT,
au même moment). Leur credential vivait à `("user", sub)`, un scope que le walker ne
consultait pas du tout : une résolution générique (`connectors.verify` en
`level=auto`) rendait « aucune clé configurée » alors que la clé existe et
authentifie — vécu le 04/09/2026, script direct sur `access.resolve_credential`.

⚠️ **Les deux occupants sont partis le 2026-09-09** avec la fédération MCP (ADR
0069), et `LEGACY_USER_SCOPE_PROVIDERS` est désormais **vide** — mais le barreau,
lui, est GARDÉ : le scope `("user", sub)` existe toujours en base, des lignes y
dorment, et c'est sa seule marche de lecture. Un barreau gardé doit rester
verrouillé, sinon il pourrit sans que rien ne le dise. Ce banc injecte donc un
occupant **SYNTHÉTIQUE** dans la liste fermée (`zoho`, un connecteur réel qui n'y
est pas) : ce qui est exercé est bien la mécanique du barreau, pas un connecteur
du catalogue. Même parti que l'ancien banc du mount no-auth, pour la même raison.

Ce fichier fige trois choses : le gate (liste FERMÉE, mono-compte, jamais
cross-sub), le comportement par l'entrée PUBLIQUE réelle (`resolve_credential` —
la même que `connectors.verify` emprunte, pas un raccourci de test), et le
plafond qui empêche la liste fermée de grandir en silence.
"""
import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp import access, credentials_store
from oto_mcp.connectors import link as connector_link

# L'import est CE qui déclare (même patron que test_connector_link_status.py) :
# sans lui, `connector_link.entries()` est vide dans un process qui n'a chargé
# aucun autre test important `auth.google` avant celui-ci.
from oto_mcp.auth import google as google_oauth  # noqa: F401,E402


# Le connecteur SYNTHÉTIQUE injecté dans la liste fermée (cf. docstring) : réel au
# registre, mais rangé au scope MEMBRE dans la vraie vie — il n'est là que pour
# donner un occupant au barreau, jamais pour décrire le catalogue.
_LEGACY_SYNTH = "planity"
_HORS_LISTE = "slack"


@pytest.fixture(autouse=True)
def _occupant_synthetique(monkeypatch):
    """La liste fermée est VIDE en vrai (ADR 0069) ; on lui donne un occupant pour
    que la mécanique du barreau reste exercée. `access` propage l'écriture jusqu'à
    `cascade` (façade `_Facade.__setattr__`), c'est ce qui rend l'injection réelle."""
    monkeypatch.setattr(access, "LEGACY_USER_SCOPE_PROVIDERS", (_LEGACY_SYNTH,))


# --- le barreau, en isolation (sondes injectées, pas de coffre) --------------

def _probe(*, legacy=False):
    return access.CascadeProbe(
        member=lambda s, o, p: None,
        legacy_user=lambda s, p: ("LK" if legacy else None),
        group=lambda g, p: None, org=lambda o, p: None, tenant=lambda t, p: None,
        platform=lambda s, p, o: None,
    )


def test_legacy_rung_fires_for_a_closed_list_provider():
    win = access.cascade_winner("u1", _LEGACY_SYNTH, org=1, group=None,
                                probe=_probe(legacy=True))
    assert win is not None
    assert win.mode == "user" and win.entity_type == credentials_store.USER
    assert win.entity_id == "u1" and win.payload == "LK"


def test_legacy_rung_never_fires_off_the_closed_list():
    """Une sonde qui RENDRAIT une ligne ne suffit pas — seule la liste fermée
    décide. Sinon un provider qui n'a jamais écrit à ce scope se mettrait à y
    être lu parce qu'une sonde répond `True` (bug de sonde, ou provider mal
    câblé) : le gate doit couper AVANT que la sonde ne soit même consultée."""
    win = access.cascade_winner("u1", _HORS_LISTE, org=1, group=None,
                                probe=_probe(legacy=True))
    assert win is None


def test_legacy_rung_needs_an_identity():
    win = access.cascade_winner(None, _LEGACY_SYNTH, org=1, group=None,
                                probe=_probe(legacy=True))
    assert win is None


def test_legacy_rung_is_org_independent():
    """Ni `org=None` ni un changement d'org n'affectent ce barreau — c'est
    précisément le point du scope legacy : il n'a jamais porté de dimension org."""
    win = access.cascade_winner("u1", _LEGACY_SYNTH, org=None, group=None,
                                probe=_probe(legacy=True))
    assert win is not None and win.mode == "user"


# --- par l'entrée publique RÉELLE (resolve_credential), pas le walker seul --

def _wire(monkeypatch, vault: dict, *, current_org=1):
    # Mono-compte, comme l'étaient les deux occupants historiques du barreau : la
    # cardinalité se lit en base (`connectors.cardinality`), et ce banc n'en a pas —
    # le sujet ici est le barreau legacy, pas la sélection de compte.
    monkeypatch.setattr(access, "_is_multi_account", lambda p, o=None: False)
    monkeypatch.setattr(access, "current_org", lambda sub: current_org)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access.db, "get_member_api_key",
                        lambda sub, org, prov, account="": None)
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, con, account="": vault.get((et, eid, con)))
    monkeypatch.setattr(access.db, "insert_tool_call", lambda row: None)


def test_un_membre_avec_une_ligne_legacy_se_resout_par_un_appel_reel(monkeypatch):
    """Le banc précis demandé sur #876 : membre A a une ligne `("user", "A",
    "atlassian")` posée — `resolve_credential` (l'entrée RÉELLE, celle que
    `connectors.verify` en `level=auto` emprunte) la trouve et la rend."""
    vault = {("user", "A", _LEGACY_SYNTH): "REFRESH-A"}
    _wire(monkeypatch, vault)
    rc = access.resolve_credential(_LEGACY_SYNTH, sub="A", want="auto",
                                   emit_on_failure=False)
    assert rc.key == "REFRESH-A"
    assert rc.mode == "user" and rc.entity_type == credentials_store.USER
    assert rc.entity_id == "A"


def test_un_autre_membre_de_la_meme_org_ne_la_voit_pas(monkeypatch):
    vault = {("user", "A", _LEGACY_SYNTH): "REFRESH-A"}
    _wire(monkeypatch, vault, current_org=1)   # même org que A
    with pytest.raises(McpError):
        access.resolve_credential(_LEGACY_SYNTH, sub="B", want="auto",
                                   emit_on_failure=False)


def test_un_org_admin_ne_la_voit_pas_non_plus(monkeypatch):
    """`resolve_credential` n'a pas de chemin dédié « pour un admin » — il résout
    toujours SOUS LE SUB de l'appel. Ce test fige que ce chemin ne rend jamais
    la ligne d'un membre à quelqu'un d'autre, admin compris."""
    vault = {("user", "A", _LEGACY_SYNTH): "REFRESH-A"}
    _wire(monkeypatch, vault, current_org=1)
    with pytest.raises(McpError):
        access.resolve_credential(_LEGACY_SYNTH, sub="admin-sub", want="auto",
                                   emit_on_failure=False)


# --- le plafond : grandir la liste fermée est une décision, pas un incident --

# oto-backend#876, 2026-09-05 : atlassian, folkmcp — les deux mounts oauth
# fédérés per-user jamais migrés au scope membre (ADR 0033). Un troisième nom
# (« memento ») est déjà cité par `tests/test_member_credential_scope.py` comme
# futur candidat au même scope — vérifié ce jour : ni `auth/memento.py` ni
# d'entrée `providers/memento.py` n'existent encore, donc rien n'écrit à ce
# scope sous ce nom aujourd'hui. Ce plafond ne le compte pas tant qu'il ne code
# rien ; le lot qui l'implémentera l'ajoutera ici, EN CONNAISSANCE DE CAUSE.
_PLAFOND = 2


def test_la_liste_fermee_reelle_est_vide(monkeypatch):
    """⚠️ Lit la liste RÉELLE, hors injection (`undo` de la fixture autouse) : depuis
    le 2026-09-09 elle est VIDE, ses deux occupants étant partis avec la fédération
    MCP (ADR 0069). Un nom qui y réapparaîtrait rendrait le scope legacy lisible pour
    un connecteur qui n'y écrit pas — décision à relire (#876), jamais un ajout
    silencieux. Si voulu, c'est ICI qu'on l'inscrit, dans le même commit."""
    monkeypatch.undo()
    entries = access.LEGACY_USER_SCOPE_PROVIDERS
    assert len(entries) <= _PLAFOND, (
        f"{entries!r} dépasse le plafond {_PLAFOND}.")
    assert set(entries) == set(), (
        f"provider inattendu dans la liste fermée : {entries!r}")


def test_liste_fermee_nest_pas_celle_de_connector_link(monkeypatch):
    """Ce n'est PAS `connectors.link.entries()` : `google` y est AUSSI (lecteur
    de lien pour `/api/me`), migré au scope membre depuis longtemps. Les
    confondre relirait le scope legacy pour un connecteur déjà migré, sur la
    foi d'une ligne pré-migration qui aurait pu ne pas être purgée."""
    monkeypatch.undo()
    assert "google" in connector_link.entries()
    assert "google" not in access.LEGACY_USER_SCOPE_PROVIDERS
