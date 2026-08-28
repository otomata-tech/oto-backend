"""La carte connecteur dit ce qu'elle SAIT — pas ce qu'elle suppose (signaux #476, #577).

Deux mensonges par raccourci, tous deux vécus en prod, tous deux du même genre :
une surface qui rend un état RASSURANT alors qu'elle n'a vérifié qu'une des trois
couches de `docs/connector-model.md`.

- **#476** (org 196, 16/08) : `state:"active"` + `recommended:true`, la sonde
  `verify` à `ok:true` — et pourtant rien ne pouvait partir, aucun compte hébergé
  n'était lié. `state` ne dit QUE « le membre a coché la case » ; il ne dit rien de
  la clé (couche 2), de l'option (couche 3), ni de l'étape qui reste. Trois lectures
  vertes, capacité absente, cinq jours de recherche au mauvais endroit.
- **#577** (org 196) : les outils d'un connecteur d'org ne remontaient pas dans une
  session planifiée, alors que le MÊME connecteur y est `active`. Cause prouvée par
  différentiel sur la prod le 28/08 : la boîte à outils d'une session MCP est calculée
  AU HANDSHAKE contre l'org MAISON (`current_org` sans jeton d'appel), pendant que le
  run épingle `_org=` à chaque appel. Le sub qui fait tourner la procédure a pour
  maison l'org **42** (`folk`, `grain` sélectionnés) et travaille sur l'org **196**
  (treize connecteurs, dont `granola`, `slack`, `linear`, `folk`). Les sept outils
  cités par le signal existaient, résolvaient, marchaient du premier coup par
  `oto_call` : défaut de VISIBILITÉ, pas de credential. La carte ne le disait nulle part.

Le verdict lui-même vit dans `oto_mcp/connectors/readiness.py` (seam partagé avec la
liste d'identités, cf. `test_connector_identities_empty_reason.py`) — ici on teste ce
que la CARTE en publie, et ce qu'elle avoue ne pas avoir calculé.
"""
import pytest

from oto_mcp.capabilities.connectors import selection as CS
import oto_mcp.connectors.readiness as RD
from oto_mcp.capabilities._types import ResolvedCtx

_ROW = {"name": "unipile", "label": "Unipile", "help": "messagerie hébergée",
        "family": "api", "category": "Comms", "availability": "self_serve",
        "logo_url": None, "namespaces": ["linkedin"]}


def _wire(monkeypatch, *, catalog=(_ROW,), selection=None, option_ok=True,
          mode="platform", pending=None, home_org=42):
    """Seams de domaine stubés — aucun accès DB (convention du repo)."""
    monkeypatch.setattr(CS, "_visible_catalog", lambda ctx: [dict(c) for c in catalog])
    monkeypatch.setattr(CS.connector_selection, "list_selection",
                        lambda sub, org: dict(selection or {}))
    monkeypatch.setattr(CS.org_store, "get_org_default_connectors", lambda org: [])
    monkeypatch.setattr(CS, "_doctrine_refs_by_ns", lambda org: {})
    monkeypatch.setattr(CS.access, "reachable_instances_map", lambda sub, org: {})
    monkeypatch.setattr(CS.access, "option_open",
                        lambda sub, name, org=None: option_ok)
    monkeypatch.setattr(CS.access, "paid_option_for",
                        lambda name: "unipile" if name == "unipile" else None)
    monkeypatch.setattr(CS.access, "credential_mode_for",
                        lambda sub, name, org=None, group=None, probe=None: mode)
    # Le hook d'étape manquante est lu par le seam PARTAGÉ, pas par la carte.
    monkeypatch.setattr(RD.status_hints, "pending_action",
                        lambda name, sub, org, group, entry: pending)
    # Le groupe est résolu UNE fois et passé explicitement aux seams (le contexte doit
    # être celui du SUJET, et `credential_mode_for` le re-dériverait sinon).
    monkeypatch.setattr(CS.access, "current_group", lambda sub: None)
    monkeypatch.setattr(CS.org_store, "get_active_org", lambda sub: home_org)


# ── #476 : l'aptitude se dit, couche par couche ──────────────────────────────

def test_lookup_par_nom_rend_un_verdict_daptitude(monkeypatch):
    """Le cœur de #476 : demander UN connecteur doit rendre « est-ce que ça marche ? »,
    pas seulement « l'ai-je installé ? »."""
    _wire(monkeypatch, selection={"unipile": "active"})
    row = CS._me(ResolvedCtx(sub="u1", org_id=42),
                 CS.MyConnectorsInput(name="unipile"))["connectors"][0]
    assert row["state"] == "active"
    assert row["ready"] is True
    assert "not_ready" not in row and "next_step" not in row


def test_active_mais_aucun_compte_lie_nest_pas_pret(monkeypatch):
    """Le cas EXACT de #476 : `state:"active"`, option levée, clé plateforme qui
    résout — et pourtant rien ne peut partir, faute de canal lié. L'étape manquante
    vient du seam générique `status_hints` (déjà déclaré par le module unipile) :
    on la RELAIE, on ne la reformule pas."""
    _wire(monkeypatch, selection={"unipile": "active"}, pending="Connecte un canal")
    row = CS._me(ResolvedCtx(sub="u1", org_id=42),
                 CS.MyConnectorsInput(name="unipile"))["connectors"][0]
    assert row["state"] == "active"          # le membre l'a bien installé…
    assert row["ready"] is False             # …et pourtant il ne peut rien faire
    assert row["not_ready"] == "pending_step"
    assert row["next_step"] == "Connecte un canal"


def test_option_fermee_nomme_la_couche_3(monkeypatch):
    _wire(monkeypatch, selection={"unipile": "active"}, option_ok=False)
    row = CS._me(ResolvedCtx(sub="u1", org_id=42),
                 CS.MyConnectorsInput(name="unipile"))["connectors"][0]
    assert row["ready"] is False and row["not_ready"] == "paid_option_off"
    assert row["next_step"]


def test_aucune_cle_ne_resout_nomme_la_couche_2(monkeypatch):
    _wire(monkeypatch, selection={"unipile": "active"}, mode="forbidden")
    row = CS._me(ResolvedCtx(sub="u1", org_id=42),
                 CS.MyConnectorsInput(name="unipile"))["connectors"][0]
    assert row["ready"] is False and row["not_ready"] == "no_credential"
    assert row["next_step"]


def test_quota_epuise_nest_pas_un_credential_manquant(monkeypatch):
    """`over_quota` = la clé résout, la journée est finie. Le confondre avec
    « aucune clé » envoie reconfigurer un credential qui va très bien."""
    _wire(monkeypatch, selection={"unipile": "active"}, mode="over_quota")
    row = CS._me(ResolvedCtx(sub="u1", org_id=42),
                 CS.MyConnectorsInput(name="unipile"))["connectors"][0]
    assert row["ready"] is False and row["not_ready"] == "over_quota"


def test_catalogue_complet_declare_quil_na_pas_calcule(monkeypatch):
    """Mesuré sur la prod le 28/08 (catalogue de 90, compte réel, org 196) : rendre le
    verdict sur TOUT le catalogue coûte **1 993 ms** de cascade de credentials, sur un
    serveur MONO-LOOP (un connecteur seul : ~244 ms). On ne le calcule donc pas — mais
    on le DIT, au lieu de laisser croire que l'absence de `ready` vaut « rien à
    signaler ». C'est la règle du lot : dire « je ne sais pas » coûte moins cher que
    rassurer à tort."""
    _wire(monkeypatch, catalog=(_ROW, {**_ROW, "name": "slack"}),
          selection={"unipile": "active", "slack": "active"})
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput())
    assert out["readiness"] == "not_computed"
    for row in out["connectors"]:
        assert "ready" not in row            # pas de verdict muet
    assert out["readiness_hint"]             # …et le geste pour l'obtenir


def test_lookup_par_nom_declare_avoir_calcule(monkeypatch):
    _wire(monkeypatch, selection={"unipile": "active"})
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput(name="unipile"))
    assert out["readiness"] == "computed" and "readiness_hint" not in out


# ── #577 : la boîte à outils de la session n'est pas celle de l'org épinglée ──

def test_org_epinglee_differente_de_lorg_de_la_boite_a_outils(monkeypatch):
    """Reproduit #577 : le run épingle `_org=196`, la session a été montée pour
    l'org maison. `state:"active"` est VRAI pour 196 et pourtant les outils ne
    sont pas montés. La carte doit nommer l'écart — sinon l'agent conclut « le
    connecteur est en panne » (trois matinées de faux rapports, 20-22/08)."""
    _wire(monkeypatch, selection={"unipile": "active"}, home_org=42)
    monkeypatch.setattr(CS.session_org, "current_call_org", lambda: 196)
    out = CS._me(ResolvedCtx(sub="u1", org_id=196), CS.MyConnectorsInput(name="unipile"))
    tb = out["toolbox_scope"]
    assert tb["mounted_for_org"] == 42 and tb["listing_for_org"] == 196
    assert "oto_call" in tb["note"]


def test_pas_decart_pas_de_bruit(monkeypatch):
    """Le cas nominal ne paie pas la remarque : un champ toujours présent devient
    du bruit qu'on cesse de lire."""
    _wire(monkeypatch, selection={"unipile": "active"}, home_org=196)
    monkeypatch.setattr(CS.session_org, "current_call_org", lambda: 196)
    out = CS._me(ResolvedCtx(sub="u1", org_id=196), CS.MyConnectorsInput(name="unipile"))
    assert "toolbox_scope" not in out


def test_sans_jeton_dappel_aucun_ecart_annonce(monkeypatch):
    """Face REST (dashboard) : pas de jeton `_org=`, donc pas de session MCP dont la
    boîte à outils pourrait diverger. Ne rien annoncer."""
    _wire(monkeypatch, selection={"unipile": "active"}, home_org=42)
    monkeypatch.setattr(CS.session_org, "current_call_org", lambda: None)
    out = CS._me(ResolvedCtx(sub="u1", org_id=196), CS.MyConnectorsInput(name="unipile"))
    assert "toolbox_scope" not in out
