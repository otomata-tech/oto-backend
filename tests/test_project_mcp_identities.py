"""Un MCP de projet publié exécute sous les identités que le projet DÉCLARE (#276).

Le secret d'un endpoint publié authentifie le PROJET, pas une personne. Les outils
adossés à un COMPTE (LinkedIn/messagerie) n'avaient donc rien à quoi s'accrocher et
répondaient « Unauthenticated — no user identity on the request » — alors que le projet
porte l'information, dans son lien de connecteur :

    {"target_type": "connecteur", "target_ref": "unipile",
     "identity_ref": "acc_01ky…", "label": "LinkedIn — compte …"}

Conséquence mesurée : partager un projet coûtait les contacts LinkedIn (≈ un tiers d'une
mission d'enrichissement, et l'essentiel des fonctions RH/finance sur les grandes
entreprises). La seule alternative était de confier un jeton `oto_` NOMINAL, qui porte
l'organisation entière — indéfendable sous contrat de traitement.

Le risque à ne pas créer en le corrigeant : la clé Unipile de la plateforme adresse TOUT
l'abonnement. Une identité déclarée dans un lien de projet est une chaîne écrite par un
membre — la servir sans la recouper ferait agir un endpoint public sous le compte
LinkedIn d'un autre tenant. D'où la garde d'appartenance, que ces tests verrouillent.
"""
import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import access
from oto_mcp.subdomain_project import AnonContext
from oto_mcp.tools import unipile as U

PROJECT, ORG = 95, 2
ACC_LI = "acc_linkedin_du_projet"
ACC_WA = "acc_whatsapp_du_projet"
ACC_TIERS = "acc_dun_autre_tenant"


def _ctx(project_id=PROJECT, org_id=ORG):
    return AnonContext(project_id, org_id, frozenset({"linkedin_search"}))


@pytest.fixture
def links(monkeypatch):
    """Liens de projet pilotables + comptes réellement rattachés à l'org."""
    state = {"declared": [], "org_accounts": {}, "pin": None}

    monkeypatch.setattr(
        access, "project_declared_identities",
        lambda connector, project_id: list(state["declared"]))
    monkeypatch.setattr(
        U.db, "org_unipile_account_ids",
        # `org_id=None` (projet user-owned legacy) ⇒ aucun compte rattachable : le vrai
        # helper le fait, on le reproduit pour que le fail-closed soit exercé ici.
        lambda org_id, provider="LINKEDIN": (
            set() if org_id is None else set(state["org_accounts"].get(provider, ()))))
    monkeypatch.setattr(U.session_org, "current_call_account", lambda: state["pin"])
    return state


def test_the_declared_identity_is_used(links):
    """Le cas nominal : le projet déclare un compte, l'endpoint agit dessus."""
    links["declared"] = [ACC_LI]
    links["org_accounts"] = {"LINKEDIN": {ACC_LI}}
    assert U._project_operated_account(_ctx(), "LINKEDIN") == ACC_LI


def test_an_identity_outside_the_owning_org_is_refused(links):
    """LA garde. Un lien peut nommer n'importe quelle chaîne ; la clé partagée, elle,
    atteindrait vraiment ce compte. Sans recoupement = usurpation cross-tenant."""
    links["declared"] = [ACC_TIERS]
    links["org_accounts"] = {"LINKEDIN": {ACC_LI}}
    with pytest.raises(McpError) as e:
        U._project_operated_account(_ctx(), "LINKEDIN")
    # Et surtout : pas de repli sur le compte légitime de l'org.
    assert ACC_LI not in str(e.value)


def test_a_disconnected_account_stops_the_endpoint(links):
    """Le compte déclaré a été déconnecté : on refuse, on ne prend pas « un autre ».
    Un message parti sous la mauvaise identité est irréversible."""
    links["declared"] = [ACC_LI]
    links["org_accounts"] = {"LINKEDIN": set()}
    with pytest.raises(McpError, match="pas de repli|Pas de repli"):
        U._project_operated_account(_ctx(), "LINKEDIN")


def test_the_channel_disambiguates_two_declared_accounts(links):
    """Un projet qui déclare LinkedIn ET WhatsApp sous le même connecteur `unipile` n'est
    pas ambigu — il déclare deux canaux. La règle per-membre (« plusieurs bindings ⇒
    on abandonne ») aurait tout refusé ici."""
    links["declared"] = [ACC_LI, ACC_WA]
    links["org_accounts"] = {"LINKEDIN": {ACC_LI}, "WHATSAPP": {ACC_WA}}
    assert U._project_operated_account(_ctx(), "LINKEDIN") == ACC_LI
    assert U._project_operated_account(_ctx(), "WHATSAPP") == ACC_WA


def test_two_accounts_on_the_same_channel_refuse_rather_than_guess(links):
    """Là c'est vraiment ambigu, et un endpoint publié n'a personne à qui demander."""
    links["declared"] = [ACC_LI, "acc_second_linkedin"]
    links["org_accounts"] = {"LINKEDIN": {ACC_LI, "acc_second_linkedin"}}
    with pytest.raises(McpError, match="2 comptes"):
        U._project_operated_account(_ctx(), "LINKEDIN")


def test_a_project_without_declared_identity_says_what_to_do(links):
    links["declared"] = []
    links["org_accounts"] = {"LINKEDIN": {ACC_LI}}
    with pytest.raises(McpError, match="op=link"):
        U._project_operated_account(_ctx(), "LINKEDIN")


def test_the_caller_cannot_choose_the_identity(links):
    """`_account=` reste lisible sur cette surface (l'axe ne demande pas de `sub`). On
    refuse au lieu de l'avaler : un jeton de contexte silencieusement ignoré fait agir
    sous une autre identité que celle demandée — le mode de panne d'#250."""
    links["declared"] = [ACC_LI]
    links["org_accounts"] = {"LINKEDIN": {ACC_LI}}
    links["pin"] = ACC_TIERS
    with pytest.raises(McpError, match="pas recevable"):
        U._project_operated_account(_ctx(), "LINKEDIN")


def test_repeating_the_project_identity_is_tolerated(links):
    """Refuser un `_account=` qui redit exactement ce que le projet déclare serait une
    chicane : l'appelant n'obtient rien de plus que ce qu'il aurait eu."""
    links["declared"] = [ACC_LI]
    links["org_accounts"] = {"LINKEDIN": {ACC_LI}}
    links["pin"] = ACC_LI
    assert U._project_operated_account(_ctx(), "LINKEDIN") == ACC_LI


def test_a_project_without_owning_org_resolves_nothing(links):
    """Projet user-owned legacy : `org_id` est None, donc aucun compte n'est rattachable.
    Fail-closed, jamais « alors on prend le premier de l'abonnement »."""
    links["declared"] = [ACC_LI]
    links["org_accounts"] = {"LINKEDIN": {ACC_LI}}
    with pytest.raises(McpError):
        U._project_operated_account(_ctx(org_id=None), "LINKEDIN")


# ── La clé d'acteur (comptabilité locale, pas une autorisation) ───────────────

def test_the_rate_limit_key_falls_back_to_the_project(monkeypatch):
    """`_scrape` indexe un cooldown par acteur. Sans `sub`, exiger un `sub` refusait des
    lectures que le projet autorise — ce que la correction débloque."""
    monkeypatch.setattr(U, "access", access)
    monkeypatch.setattr("oto_mcp.subdomain_project.current_anon_context", lambda: _ctx())
    assert U._actor_key() == f"project:{PROJECT}"
