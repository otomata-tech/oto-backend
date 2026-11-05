"""Axe d'appel `instance=` (ADR 0038 §C / B6) — pin gardé + résolution EN DUR.

Deux moitiés : (1) la POSE (`_pin_instance`) refuse ref malformé / platform /
mismatch connecteur-tool / instance d'un autre membre / groupe non-lu, et co-pose
l'org de l'instance ; (2) la RÉSOLUTION (`_resolve_credential_impl`) lit exactement
la ligne désignée — l'explicite bat la proximité (org gagne même si une clé membre
existe), ligne absente = McpError SANS fallback, ref d'un autre provider = ignoré.
"""
import asyncio

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import access, call_axes, credentials_store, group_store, instance_refs, roles, session_org


@pytest.fixture(autouse=True)
def _sub(monkeypatch):
    # `call_axes` et `access` importent/résolvent le sub différemment — les deux.
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: "u")
    yield


def _unpin(undo):
    for reset, tok in reversed(undo):
        reset(tok)


# ─── 1. Pose (_pin_instance) ─────────────────────────────────────────────────

def test_pin_instance_member_ok_copins_org(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.make_member_ref(8, "u", "zoho", "jane")

    async def _scenario():
        undo = await call_axes._pin_instance(ref, "zoho_record")
        try:
            pinned = session_org.current_call_instance()
            assert pinned.level == "member" and pinned.account == "jane"
            assert session_org.current_call_org() == 8       # org co-posée
        finally:
            _unpin(undo)
        assert session_org.current_call_instance() is None
        assert session_org.current_call_org() is None

    asyncio.run(_scenario())


def test_pin_instance_rejects_malformed():
    with pytest.raises(McpError, match="invalide"):
        asyncio.run(call_axes._pin_instance("n'importe:quoi", "zoho_record"))


def test_pin_instance_rejects_platform_ref():
    with pytest.raises(McpError, match="platform"):
        asyncio.run(call_axes._pin_instance("platform:11", "serpapi_search"))


def test_pin_instance_rejects_connector_mismatch():
    # Ref zoho passé sur un tool hunter → refus AVANT toute garde DB.
    ref = instance_refs.make_org_ref(8, "zoho")
    with pytest.raises(McpError, match="hunter"):
        asyncio.run(call_axes._pin_instance(ref, "hunter_domain_search"))


def _niveau_sans_base(monkeypatch):
    """Le niveau d'un compte épinglé se juge (#1499) dans l'org de l'appel (celle des
    refs, 8), sans base : chaque titulaire est membre de son org et éveillé, l'org a un
    nom."""
    from oto_mcp import account_suspension, org_store
    monkeypatch.setattr(access, "current_org", lambda sub: 8)
    monkeypatch.setattr(account_suspension, "etat", lambda sub: None)
    monkeypatch.setattr(org_store, "get_org", lambda oid: {"id": oid, "name": "Acme"})
    monkeypatch.setattr(org_store, "get_org_role", lambda org, sub: "member")


def test_pin_instance_rejects_other_members_instance(monkeypatch):
    _niveau_sans_base(monkeypatch)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.make_member_ref(8, "quelqu_un_d_autre", "zoho")
    with pytest.raises(McpError, match="another member"):
        asyncio.run(call_axes._pin_instance(ref, "zoho_record"))
    assert session_org.current_call_instance() is None


def test_pin_instance_rejects_group_non_reader(monkeypatch):
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: False)
    # Non lectrice, et aucune règle ne lui ouvre le compte d'équipe (#1499).
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    from oto_mcp import group_store
    monkeypatch.setattr(group_store, "is_group_member", lambda sub, gid: False)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: False)
    ref = instance_refs.make_group_ref(3, "zoho")
    with pytest.raises(McpError, match="group"):
        asyncio.run(call_axes._pin_instance(ref, "zoho_record"))


def test_pin_instance_group_ok_copins_parent_org(monkeypatch):
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: True)
    monkeypatch.setattr(group_store, "get_group", lambda gid: {"id": gid, "org_id": 42})
    ref = instance_refs.make_group_ref(3, "zoho")

    async def _scenario():
        undo = await call_axes._pin_instance(ref, "zoho_record")
        try:
            assert session_org.current_call_instance().group_id == 3
            assert session_org.current_call_org() == 42
        finally:
            _unpin(undo)

    asyncio.run(_scenario())


def test_pin_instance_none_is_inert():
    assert asyncio.run(call_axes._pin_instance(None, "zoho_record")) == []


# ─── 2. Résolution (_resolve_credential_impl, instance épinglée) ─────────────

class _Coffre(dict):
    """(etype, eid, provider, account) -> secret ; `regles` : celles de chaque compte."""
    regles: tuple = ()


@pytest.fixture()
def resolution(monkeypatch):
    """Neutralise les à-côtés de la résolution ; le coffre est un dict contrôlé. Un
    compte épinglé est jugé à son niveau (#1499) : l'appel se fait dans l'org de l'instance
    épinglée (5 sans épinglage, celle du binding de projet), dont "u" est membre ; chaque
    ligne du coffre a son instance, avec les règles `vault.regles`."""
    vault = _Coffre()
    monkeypatch.setattr(credentials_store, "get_credential",
                        lambda et, eid, prov, acc="": vault.get((et, eid, prov, acc)))
    _niveau_sans_base(monkeypatch)
    monkeypatch.setattr(access, "current_org", lambda sub: getattr(
        session_org.current_call_instance(), "org_id", None) or 5)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)

    def _comptes(porteur, cles):
        return {(et, eid, acct): access.Compte(
                    instance_id=7, owner_type=et, owner_id=eid, connector=porteur,
                    account=acct, org_id=int(eid.split(":")[0]), regles=vault.regles)
                for et, eid, acct in cles if (et, eid, porteur, acct) in vault}
    monkeypatch.setattr(access, "comptes_des_lignes", _comptes)
    return vault


def _pin(ref_str):
    return session_org.set_call_instance(instance_refs.parse_ref(ref_str))


def test_resolution_org_instance_beats_member_key(resolution, monkeypatch):
    # L'EXPLICITE bat la proximité : une instance org demandée est servie même si
    # une clé membre existe — et le palier membre n'est même pas consulté.
    resolution[("org", "5", "zoho", "")] = "SECRET-ORG"
    from oto_mcp import db
    monkeypatch.setattr(db, "get_member_api_key",
                        lambda *a, **k: pytest.fail("palier membre consulté"))
    tok = _pin("org:5:zoho")
    try:
        rc = access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "SECRET-ORG" and rc.mode == "org"
    assert (rc.entity_type, rc.entity_id) == ("org", "5")


def test_resolution_member_instance_with_account(resolution):
    eid = credentials_store.member_id(8, "u")
    resolution[(credentials_store.MEMBER, eid, "zoho", "jane")] = "SECRET-ALX"
    tok = _pin("member:8:u:zoho:jane")
    try:
        rc = access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "SECRET-ALX" and rc.mode == "user"
    assert rc.account == "jane"


def _regle_pour_u(resolution, niveau="read"):
    """Le compte de "boss" donne `niveau` à "u" par une règle (#1499)."""
    resolution.regles = (access.Regle(user="u", group=None, org=False, level=niveau),)


def test_resolution_member_instance_of_a_peer_reads_the_OWNERS_row(resolution,
                                                                     monkeypatch):
    """Régression (2026-09-02) : un ref `member` d'une autre personne porte `ref.sub` = le
    PROPRIÉTAIRE (ici "boss") ; le caller ("u") n'y a accès que par une règle (#1499). La
    ligne à lire est celle du propriétaire, jamais celle du caller — confondre les deux
    faisait échouer tout accès membre-à-membre avec un « l'instance ne résout plus »
    trompeur (la ligne existait, cherchée sous la mauvaise identité)."""
    _regle_pour_u(resolution)
    owner_eid = credentials_store.member_id(8, "boss")
    resolution[(credentials_store.MEMBER, owner_eid, "zoho", "")] = "SECRET-DU-BOSS"
    tok = _pin("member:8:boss:zoho")
    try:
        rc = access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "SECRET-DU-BOSS" and rc.mode == "user"
    assert (rc.entity_type, rc.entity_id) == (credentials_store.MEMBER, owner_eid)


def test_resolution_missing_instance_hard_error_no_fallback(resolution, monkeypatch):
    # La ligne n'existe plus : erreur actionnable, PAS de repli vers un autre palier
    # (l'org 5 a pourtant un secret… d'un autre compte — il ne doit PAS servir).
    resolution[("org", "5", "zoho", "")] = "SECRET-AUTRE"
    tok = _pin("org:5:zoho:parti")   # account `parti` retiré du coffre
    try:
        with pytest.raises(McpError, match="no longer resolves"):
            access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)


# ── Binding de projet (ADR 0038 B5) : le projet fournit le ref ──────────────

def _bind_project(monkeypatch, pid, links):
    monkeypatch.setattr(session_org, "current_call_project", lambda: pid)
    from oto_mcp import db
    monkeypatch.setattr(db, "list_project_links", lambda p: links)


def test_resolution_project_binding_resolves_hard(resolution, monkeypatch):
    # project=P porte un binding zoho → instance org:5 servie, re-gardée pour l'APPELANT.
    resolution[("org", "5", "zoho", "")] = "SECRET-BOUND"
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}}])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    rc = access._resolve_credential_impl("zoho", "auto", "u")
    assert rc.secret == "SECRET-BOUND" and rc.mode == "org"


def test_resolution_project_binding_reguards_caller(resolution, monkeypatch):
    # L'appelant du projet partagé n'est PAS membre de l'org de l'instance bindée →
    # refus actionnable, jamais le credential (le binding ne rouvre pas l'org).
    resolution[("org", "5", "zoho", "")] = "SECRET-BOUND"
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}}])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    with pytest.raises(McpError, match="not a member"):
        access._resolve_credential_impl("zoho", "auto", "u")


def test_resolution_project_binding_serves_a_caller_a_rule_names(resolution, monkeypatch):
    # Le cas symétrique : hors de l'org 5, une règle qui NOMME l'appelant lui ouvre le
    # compte d'org (D1, ou un héritage repris, #1499) — le binding le sert.
    resolution[("org", "5", "zoho", "")] = "SECRET-BOUND"
    resolution.regles = (access.Regle(user="u", group=None, org=False, level="sensitive"),)
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}}])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    rc = access._resolve_credential_impl("zoho", "auto", "u")
    assert rc.secret == "SECRET-BOUND" and (rc.entity_type, rc.entity_id) == ("org", "5")


def test_resolution_explicit_instance_beats_project_binding(resolution, monkeypatch):
    # `instance=` explicite (jeton le plus spécifique) prime sur le binding du projet.
    eid = credentials_store.member_id(8, "u")
    resolution[(credentials_store.MEMBER, eid, "zoho", "perso")] = "SECRET-EXPLICITE"
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}}])
    tok = _pin("member:8:u:zoho:perso")
    try:
        rc = access._resolve_credential_impl("zoho", "auto", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "SECRET-EXPLICITE"


def test_resolution_multiple_bindings_actionable_error(resolution, monkeypatch):
    # 2 bindings zoho dans le projet, pas d'instance= → erreur qui LISTE les choix.
    _bind_project(monkeypatch, 7, [
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "org:5:zoho"}},
        {"target_type": "connecteur", "target_ref": "zoho",
         "config": {"instance_ref": "member:5:u:zoho:alx"}}])
    with pytest.raises(McpError, match="SEVERAL"):
        access._resolve_credential_impl("zoho", "auto", "u")


def test_resolution_foreign_provider_ref_ignored(resolution, monkeypatch):
    # Ref zoho épinglé mais résolution du provider hunter (résolution auxiliaire) :
    # le ref est ignoré → cascade normale (ici : rien ne résout → erreur STANDARD,
    # pas l'erreur d'instance).
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    tok = _pin("org:5:zoho")
    try:
        with pytest.raises(McpError) as ei:
            access._resolve_credential_impl("hunter", "byo", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert "no longer resolves" not in str(ei.value)   # erreur cascade, pas instance


# ─── 3. Un porteur, une réf : la carte de service et le porteur (Microsoft, Google) ──
#
# Une ligne du coffre Microsoft vit sous le PORTEUR `microsoft` ; ses outils s'appellent
# `outlook_*`, `sharepoint_*`… La réf rendue par `oto_instance op=list` est celle du
# porteur : elle était REFUSÉE sur un outil `outlook_*` (comparaison des noms nus), et une
# réf écrite sous la carte (`…:outlook:…`) passait la pose puis était IGNORÉE à la
# résolution, qui compare au porteur — l'appel retombait sur la cascade, donc
# potentiellement sur un AUTRE compte que celui demandé.

def test_pin_instance_ref_du_porteur_sert_l_outil_du_service(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.make_member_ref(8, "u", "microsoft", "jane@contoso.example")

    async def _scenario():
        undo = await call_axes._pin_instance(ref, "outlook")
        try:
            pinned = session_org.current_call_instance()
            assert (pinned.connector, pinned.account) == ("microsoft", "jane@contoso.example")
        finally:
            _unpin(undo)

    asyncio.run(_scenario())


def test_pin_instance_ref_de_la_carte_est_ramenee_au_porteur(monkeypatch):
    """Même ligne, même réf : la réf écrite sous la carte est canonisée en celle du
    porteur AVANT la garde et la résolution (qui lisent le coffre sous lui)."""
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    vu = []
    real_guard = access.guard_instance_access
    monkeypatch.setattr(access, "guard_instance_access",
                        lambda sub, r: (vu.append(r.connector), real_guard(sub, r))[1])
    ref = instance_refs.make_member_ref(8, "u", "outlook", "jane@contoso.example")

    async def _scenario():
        undo = await call_axes._pin_instance(ref, "outlook")
        try:
            pinned = session_org.current_call_instance()
            assert instance_refs.format_ref(pinned) == instance_refs.make_member_ref(
                8, "u", "microsoft", "jane@contoso.example")
        finally:
            _unpin(undo)

    asyncio.run(_scenario())
    assert vu == ["microsoft"]


def test_pin_instance_google_ref_du_porteur_sert_gmail(monkeypatch):
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    ref = instance_refs.make_member_ref(8, "u", "google", "moi@exemple.test")

    async def _scenario():
        undo = await call_axes._pin_instance(ref, "gmail")
        _unpin(undo)

    asyncio.run(_scenario())


def test_pin_instance_porteur_etranger_reste_refuse():
    ref = instance_refs.make_org_ref(8, "microsoft", "jane@contoso.example")
    with pytest.raises(McpError, match="gmail"):
        asyncio.run(call_axes._pin_instance(ref, "gmail"))


@pytest.mark.parametrize("connecteur_du_ref", ["microsoft", "outlook"])
def test_resolution_instance_microsoft_par_la_carte_ou_le_porteur(resolution, monkeypatch,
                                                                   connecteur_du_ref):
    """La résolution compare les PORTEURS des deux côtés : la ligne `microsoft` est lue,
    jamais la cascade (le palier membre n'est pas consulté)."""
    _regle_pour_u(resolution)
    from oto_mcp import db
    monkeypatch.setattr(db, "get_member_api_key",
                        lambda *a, **k: pytest.fail("cascade consultée"))
    eid = credentials_store.member_id(8, "boss")
    resolution[(credentials_store.MEMBER, eid, "microsoft", "jane@contoso.example")] = "RT"
    tok = _pin(instance_refs.make_member_ref(8, "boss", connecteur_du_ref,
                                             "jane@contoso.example"))
    try:
        rc = access._resolve_credential_impl("outlook", "byo", "u")
    finally:
        session_org.reset_call_instance(tok)
    assert rc.secret == "RT" and rc.account == "jane@contoso.example"
    assert (rc.entity_type, rc.entity_id) == (credentials_store.MEMBER, eid)


def test_la_trace_ecrit_la_ref_du_porteur(monkeypatch):
    """Le journal (`instance`) nomme la ligne sous son porteur, qu'elle ait été atteinte
    par la carte du service (`outlook`) ou par le porteur : UNE réf par ligne, celle
    qu'`oto_instance op=list` rend et qu'`_instance=` accepte."""
    from oto_mcp.access import resolve
    from oto_mcp.access.resolved_credential import ResolvedCredential
    eid = credentials_store.member_id(8, "u")
    tok = session_org.set_call_trace({})
    try:
        resolve._note_resolved_instance(ResolvedCredential(
            "outlook", "RT", False, "user", credentials_store.MEMBER, eid,
            account="jane@contoso.example"))
        trace = session_org.current_call_trace()
    finally:
        session_org.reset_call_trace(tok)
    assert trace["instance"] == instance_refs.make_member_ref(
        8, "u", "microsoft", "jane@contoso.example")
    assert trace["resolved_connector"] == "outlook"


@pytest.mark.parametrize("carte", ["google", "gmail"])
def test_un_compte_google_d_autrui_epingle_par_la_carte_ou_le_porteur_designe_la_meme_ligne(
        resolution, monkeypatch, carte):
    """Le compte d'une autre personne se désigne par sa réf : écrite sous la carte
    (`gmail`) ou sous le porteur (`google`), c'est la MÊME ligne du coffre de sa
    titulaire, gardée (sa règle) puis lue."""
    _regle_pour_u(resolution)
    eid = credentials_store.member_id(8, "boss")
    resolution[(credentials_store.MEMBER, eid, "google", "jane@exemple.test")] = "RT"

    async def _scenario():
        undo = await call_axes._pin_instance(
            instance_refs.make_member_ref(8, "boss", carte, "jane@exemple.test"),
            "gmail")
        try:
            assert instance_refs.format_ref(session_org.current_call_instance()) == \
                instance_refs.make_member_ref(8, "boss", "google", "jane@exemple.test")
            return access._resolve_credential_impl("gmail", "byo", "u")
        finally:
            _unpin(undo)

    rc = asyncio.run(_scenario())
    assert rc.secret == "RT" and (rc.entity_type, rc.entity_id) == (credentials_store.MEMBER,
                                                                    eid)
