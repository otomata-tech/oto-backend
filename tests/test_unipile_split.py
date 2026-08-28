"""Le split `unipile` → le COMPTE + ses six CONNEXIONS (2026-08-28).

Un fournisseur dont UNE clé ouvre N connexions posait un problème que le modèle de
connecteur ne savait pas dire : `unipile` portait sept namespaces, donc UNE
activation, UNE ACL, UNE ligne de sélection et UNE carte pour LinkedIn *et*
WhatsApp *et* quatre autres. Réserver la messagerie WhatsApp à un département sans
lui ouvrir LinkedIn était impossible ; installer WhatsApp montait 40 outils LinkedIn.

Le split donne à chaque canal son connecteur. Ce qui se gouverne PAR CANAL —
activation, ACL, sélection, visibilité, connexion hébergée — lui appartient ; ce qui
appartient à la CLÉ — coffre, cascade, quota, clé plateforme, option couche-3 — reste
au compte, via `Connector.credential_of`.

Ce fichier tient la frontière entre ces deux questions. Elle est le seul endroit où
ce changement peut mal tourner en silence : les confondre, c'est refaire la
divergence du 2026-07-07 (carte « clé d'org » verte à côté d'un « Bloqué » rouge),
où le statut et la résolution répondaient à deux questions différentes en croyant
répondre à la même.
"""
from __future__ import annotations

import pytest
from fastmcp import FastMCP

from oto_mcp import providers
from oto_mcp.access import quotas
from oto_mcp.connectors import flow as connector_flow
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import register_all

CANAUX = ("linkedin", "whatsapp", "telegram",
          "instagram", "messenger", "twitter")


@pytest.fixture(scope="module", autouse=True)
def _declarations():
    """Les flux et les hooks de statut se déclarent à l'IMPORT du module d'outils —
    donc ce banc charge ce que charge le boot (`register_all`), pas les modules qui
    l'arrangent. Un banc qui importe de complaisance certifie une couverture qui
    n'existe pas : c'est comme ça que le flux hébergé a vécu un mois déclaré nulle
    part en production (12/08)."""
    register_all(FastMCP("unipile-split-probe"))


# --- 1. la forme du registre --------------------------------------------------

def test_les_six_canaux_sont_des_connecteurs_et_le_compte_reste():
    """Sept entrées là où il y en avait une — le compte n'est PAS remplacé.

    C'est ce qui distingue un split d'un renommage, et pourquoi la migration de
    sélection est un fan-out (`fanout_selection`) et non un `rename_selection` :
    les lignes `unipile` restent valides, elles ne se déplacent pas."""
    for nom in CANAUX:
        assert nom in providers.REGISTRY, nom
    assert "unipile" in providers.REGISTRY


@pytest.mark.parametrize("canal", CANAUX)
def test_un_canal_ne_porte_pas_sa_clef(canal):
    """La clé vit sur le compte, et un canal ne peut pas en détenir une.

    Trois surfaces d'écriture, trois refus cohérents — un seul qui manquerait
    laisserait entrer un credential fantôme : accepté au coffre, jamais relu (la
    cascade normalise vers le porteur), et contredisant la vraie clé."""
    assert providers.credential_provider(canal) == "unipile"
    assert canal not in providers.CREDENTIAL_PROVIDERS
    with pytest.raises(ValueError, match="unipile"):
        providers.require_credential("user", canal)
    assert providers.org_secret_meta(canal, None)[1] == "provider_not_shareable"
    # Et rien à saisir : la carte d'un canal n'a pas de formulaire de clé.
    assert providers.REGISTRY[canal].secret_fields == ()


def test_le_compte_lui_porte_bien_sa_clef():
    """Le pendant : le porteur, lui, garde tout ce que les canaux perdent. Sans
    cette moitié, le test ci-dessus passerait aussi sur un registre où PLUS
    PERSONNE ne peut poser de clé Unipile."""
    assert providers.credential_provider("unipile") == "unipile"
    assert "unipile" in providers.CREDENTIAL_PROVIDERS
    assert providers.REGISTRY["unipile"].secret_fields  # un champ à coller
    providers.require_credential("user", "unipile")     # ne lève pas
    assert providers.org_secret_meta("unipile", None) == (None, None)


# --- 2. ce qui reste COMMUN : l'option, le quota, la clé plateforme ------------

@pytest.mark.parametrize("canal", CANAUX)
def test_loption_reste_celle_du_compte(canal):
    """UNE option pour les sept, sinon un client abonné perd WhatsApp le jour du
    split. L'option paie des SIÈGES sur la clé plateforme, et un siège est un
    compte chez le fournisseur — pas un canal."""
    assert quotas.paid_option_for(canal) == "unipile"


@pytest.mark.parametrize("canal", CANAUX)
def test_le_quota_est_celui_de_la_clef(canal):
    """Six compteurs indépendants laisseraient consommer 6× le quota d'une seule
    clé. `quota_for` normalise donc vers le porteur, comme `usage_today`."""
    assert quotas.quota_for(canal) == quotas.quota_for("unipile")


def test_le_flag_de_cle_ouverte_ne_se_recopie_pas_sur_les_canaux():
    """`platform_key_open` gouverne le PARTAGE d'une clé plateforme : il se lit sur
    le connecteur qui la porte, la cascade ayant déjà normalisé quand on l'atteint.
    Le recopier serait de la configuration morte que le prochain lecteur croirait
    vivante — et qu'il pourrait faire diverger de celle du compte. C'est la forme
    exacte de la panne all-users de #245, où un réglage de clé posé au mauvais
    endroit a coupé tout le monde."""
    assert providers.REGISTRY["unipile"].platform_key_open is True
    for canal in CANAUX:
        assert providers.REGISTRY[canal].platform_key_open is False, canal


# --- 3. ce qui devient PROPRE à chaque canal ----------------------------------

@pytest.mark.parametrize("canal", CANAUX)
def test_chaque_canal_a_son_namespace_et_son_flux(canal):
    """Un namespace par canal = un gate par canal (activation, ACL, sélection,
    visibilité passent tous par `connector_for_namespace`). Un flux par canal, SANS
    paramètre : le canal est dérivé du connecteur, donc on ne peut plus démarrer
    une connexion WhatsApp depuis la carte Telegram."""
    con = providers.REGISTRY[canal]
    assert con.namespaces == (canal,)
    assert providers.connector_for_namespace(canal) is con
    assert connector_flow.supports(canal)
    assert connector_flow.describe(canal)["params"] == []


def test_le_compte_na_plus_de_flux_hebergé():
    """Sa carte pose une CLÉ. Y laisser un flux « connecter un compte » afficherait
    un bouton qui ne peut plus rien connecter — la carte ne représente plus aucun
    canal."""
    assert not connector_flow.supports("unipile")
    assert providers.REGISTRY["unipile"].auth_method == "secret"
    assert providers.REGISTRY["unipile"].namespaces == ("unipile",)


def test_le_canal_se_retrouve_depuis_son_code_fournisseur():
    """Réciproque de `hosted_channel` : le code qui ne connaît que le canal (les
    tools de messagerie, la résolution du compte opéré, le picker d'identités)
    retrouve le connecteur à GATER — sinon il retomberait sur le porteur et
    gaterait les six ensemble."""
    for canal in CANAUX:
        code = providers.REGISTRY[canal].hosted_channel
        assert code and code.isupper()
        assert providers.connector_for_hosted_channel(code).name == canal
        assert providers.connector_for_hosted_channel(code.lower()).name == canal
    assert providers.REGISTRY["unipile"].hosted_channel is None


def test_le_partage_dorg_appartient_au_compte_pas_aux_canaux():
    """Ce qui se partage au niveau org, c'est la CLÉ, sur la carte du compte. Les
    lecteurs qui interrogent le nom nu — `org_secret_meta`, les hints « une équipe
    a la clé » — proposeraient sinon d'atteindre un secret d'org sous `whatsapp`,
    que la cascade (normalisée) n'irait jamais lire."""
    assert providers.is_org_shareable("unipile")
    for canal in CANAUX:
        assert not providers.is_org_shareable(canal), canal


# --- 4. le namespace multi-token, sous trois `linkedin` -----------------------

def test_chaque_namespace_va_a_son_connecteur():
    """Le gate d'un tool suit son NAMESPACE — c'est ce qui donne à chaque canal un
    credential, une activation et une sélection distincts.

    Le compte garde `unipile` : `unipile_connect_start` est multi-canal (il
    n'appartient à aucune capacité), il ne peut donc tomber sous aucune carte de
    canal. Le détail LinkedIn — le nom nu, et pourquoi `aiark` ne le porte pas — est
    tenu par `tests/test_linkedin.py`, avec son histoire."""
    for canal in CANAUX:
        assert namespace_of(f"{canal}_chat") == canal
    assert namespace_of("unipile_connect_start") == "unipile"


# --- 5. ce que la délégation ne doit PAS coûter, ni faire perdre --------------

def test_api_me_ne_marche_pas_six_fois_la_meme_cascade(monkeypatch):
    """`/api/me` est le chemin chaud (à chaque chargement du dashboard) : c'est pour
    lui que `status_for` précharge l'inventaire du coffre et la carte des quotas.

    Six canaux `keyed` de plus, c'est six marches de cascade qui rendraient — par
    construction, `walk_cascade` normalisant avant le premier barreau — EXACTEMENT
    celle du porteur. On marche donc une fois et on recopie. Le test compte les
    marches parce que la sortie, elle, est identique dans les deux cas : une
    régression de coût ici serait invisible à l'œil."""
    from oto_mcp.access import status as st

    marches = []
    monkeypatch.setattr(st.cascade, "walk_cascade",
                        lambda sub, provider, **kw: (marches.append(provider), iter(()))[1])
    monkeypatch.setattr(st.scope, "get_user_role", lambda s: "user")
    monkeypatch.setattr(st.group_store, "list_groups_for_user", lambda s, o: [])
    monkeypatch.setattr(st.cascade, "preloaded_presence_probe",
                        lambda *a, **k: st.cascade.PRESENCE_PROBE)
    monkeypatch.setattr(st.db, "usage_today_map", lambda s: {})
    monkeypatch.setattr(st.db, "get_usage_today", lambda s, p: 0)
    try:
        st.status_for("u1", org=1, group=None)
    except Exception:
        pass   # la suite de `status_for` touche le coffre — hors périmètre ici

    assert [m for m in marches if m in CANAUX] == []
    assert marches.count("unipile") == 1


def test_les_instances_a_portee_du_compte_apparaissent_sur_la_carte_du_canal(monkeypatch):
    """La carte d'un canal doit montrer les instances de SON compte.

    Le catalogue annote par NOM de connecteur : sans alias, la ligne `whatsapp`
    reste muette pendant que `unipile` affiche la clé d'équipe qui la ferait
    marcher. Pour un connecteur PAR-PERSONNE, c'est le signal qui évite de
    reconnecter un compte déjà lié ailleurs — le doublon d'`account_id` de #172,
    que le 409 au connect ne fait que rattraper après coup."""
    from oto_mcp.access import rbac

    monkeypatch.setattr(rbac.group_store, "list_groups_for_user",
                        lambda s, o: [{"group_id": 3, "name": "sales"}])
    monkeypatch.setattr(rbac.group_store, "list_group_secrets",
                        lambda gid: [{"provider": "unipile"}])
    monkeypatch.setattr(rbac.org_store, "list_orgs_for_user", lambda s: [])
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda s, o: False)

    reach = rbac.reachable_instances_map("u1", 1)
    assert reach["unipile"], "l'instance du porteur devrait être à portée"
    for canal in CANAUX:
        assert reach.get(canal) == reach["unipile"], canal


def test_laxe_account_survit_sur_les_six_canaux():
    """`_account=` est le pin d'identité opérée — c'est lui qui permet d'agir sous un
    compte ACCORDÉ par son propriétaire (#55). Il est annoncé et accepté d'après le
    NAMESPACE du tool, donc d'après le connecteur du CANAL : c'est ce qui rend
    `personal_cross_org` load-bearing sur les canaux, là où `platform_key_open` ne
    l'est pas. Les « uniformiser » par symétrie couperait les comptes partagés sur
    les six canaux d'un coup."""
    from oto_mcp import call_axes
    for tool in ("whatsapp_chat", "telegram_chat", "instagram_chat",
                 "messenger_chat", "twitter_chat", "linkedin_chat"):
        assert call_axes.accepts_account_axis(tool), tool
        assert call_axes._has_account_axis(tool), tool


def test_un_lien_de_projet_se_lit_sous_les_deux_noms(monkeypatch):
    """Les liens de projet ne sont PAS migrés — et ils ne doivent pas l'être : ils
    portent une identité choisie par une personne, et les deux noms restent vrais.
    Un lien d'AVANT le split nomme `unipile`, un lien posé DEPUIS la carte du canal
    nomme le canal. Ne lire que l'un des deux casserait la moitié des projets, en
    silence : l'absence de lien se rend « ce projet ne déclare aucun compte ».

    Et la même identité déclarée sous les DEUX noms reste UN compte : sinon le
    garde-fou « plusieurs comptes ⟹ je ne devine pas » se déclencherait sur un
    projet parfaitement univoque, simplement parce qu'il a été relié après le split.
    """
    from oto_mcp.tools import unipile as tu

    class _Anon:
        project_id, org_id = 42, 7

    for liens in (
        {"unipile": ["acc_1"]},                        # lien d'avant le split
        {"whatsapp": ["acc_1"]},                       # lien posé depuis la carte
        {"unipile": ["acc_1"], "whatsapp": ["acc_1"]},  # les deux, même compte
    ):
        monkeypatch.setattr(tu.access, "project_declared_identities",
                            lambda nom, pid, _l=liens: _l.get(nom, []))
        monkeypatch.setattr(tu.db, "org_unipile_account_ids",
                            lambda org, prov: ["acc_1", "acc_2"])
        monkeypatch.setattr(tu.session_org, "current_call_account", lambda: None)
        assert tu._project_operated_account(_Anon(), "WHATSAPP") == "acc_1", liens


def test_le_gate_dacces_porte_le_nom_NU_pas_celui_du_porteur(monkeypatch):
    """LA frontière du split, en un test.

    `require_connector_access` est le backstop DUR de l'ACL d'org (ADR 0025) — il
    mord même sur une clé BYO. Il doit recevoir le connecteur APPELÉ, sinon une org
    qui réserve WhatsApp à un département verrait le gate évalué sur `unipile` :
    les six canaux redeviendraient indivisibles, ce que le split existe pour défaire.

    La clé, elle, part bien sous le porteur — l'autre moitié de la frontière. Les
    deux assertions doivent tenir ENSEMBLE : chacune seule passerait sur une
    implémentation qui a tout normalisé, ou sur une qui n'a rien normalisé."""
    from oto_mcp.access import resolve as res

    gates, marches = [], []
    monkeypatch.setattr(res.rbac, "require_connector_access",
                        lambda p, sub=None: gates.append(p))
    monkeypatch.setattr(res.session_org, "current_call_instance", lambda: None)
    monkeypatch.setattr(res.session_org, "current_call_account", lambda: None)
    monkeypatch.setattr(res.scope, "project_pinned_instance", lambda p: None)
    monkeypatch.setattr(res.scope, "current_org", lambda s: 1)
    monkeypatch.setattr(res.scope, "current_group", lambda s: None)
    monkeypatch.setattr(res.cascade, "walk_cascade",
                        lambda sub, provider, **kw: (marches.append(provider), iter(()))[1])
    with pytest.raises(Exception):
        res._resolve_credential_impl("whatsapp", "auto", "u1")

    assert gates == ["whatsapp"], "le gate d'accès doit voir le canal APPELÉ"
    assert marches == ["whatsapp"], "la cascade reçoit le nom nu…"
    # …et c'est ELLE qui normalise, une fois, en son sein (cf. walk_cascade).
    assert providers.credential_provider(marches[0]) == "unipile"
