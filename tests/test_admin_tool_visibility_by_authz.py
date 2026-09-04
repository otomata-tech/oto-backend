"""Ce qui masque un outil admin, c'est son AUTORISATION — pas son nom (#471).

`oto_admin_org_member` porte depuis juin `op=remove` avec l'autz `ORG_ADMIN_OF("org_id")`
et le dashboard s'en sert. Mais la visibilité de session masquait toute la famille
`oto_admin_*` à quiconque n'était pas **super** admin de la plateforme, sur le seul
préfixe du nom — et fastmcp filtre aussi `get_tool`, donc le masquage bloque l'APPEL, pas
seulement la liste. Un responsable d'organisation était donc renvoyé au dashboard pour un
geste que son autorisation lui accorde.

Un nom ne porte pas un droit. La couche capacité DÉCLARE l'autz (ADR 0009 §7,
combinateurs fermés de `_authz.py`) : c'est elle qui doit décider ce qu'on montre, et
c'est la seule source qui ne peut pas dériver de ce qui est réellement appliqué à l'appel.

⚠️ Ce masquage est de la **gouvernance, pas une barrière** (ADR 0031) : la vraie barrière
est l'autz de la capacité, appliquée de toute façon. Le montrer de trop ne donne aucun
droit ; le masquer de trop rend un geste légitime introuvable. Le risque est donc
asymétrique, et c'est ce qui autorise à ne masquer QUE l'inatteignable par construction.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, db, session_visibility


class _Tool:
    def __init__(self, name):
        self.name = name


class _FastMCP:
    def __init__(self, noms):
        self._noms = noms

    async def list_tools(self, run_middleware=False):
        return [_Tool(n) for n in self._noms]


class _Ctx:
    def __init__(self, noms):
        self.fastmcp = _FastMCP(noms)


# Un échantillon qui couvre les trois planchers + le cas « pas une capacité ».
OUTILS = [
    "oto_admin_org",              # ADMIN_BY_OP : create/archive SUPER, list/get PLATFORM
    "oto_admin_org_member",       # ADMIN_BY_OP : 4 ops ORG_ADMIN_OF, list PLATFORM
    "oto_admin_guide",         # ADMIN_BY_OP : ORG_MEMBER_OF / ORG_ADMIN_OF
    "oto_admin_signal",           # PLATFORM_ADMIN
    "oto_admin_monitoring",       # PLATFORM_ADMIN
    # ADMIN_BY_OP : lectures PLATFORM, test/send/optout_clear SUPER. Il est ici parce
    # que son namespace (`oto`) ne résout AUCUN connecteur : aucun bloc de gating par
    # connecteur ne le touche, et seule la dérivation du plancher depuis l'autz
    # déclarée le masque. Le vérifier en EXÉCUTANT le filtre, pas en lisant `PLANCHERS`.
    "oto_admin_outreach",
    "oto_admin_unipile_seat",     # SUPER_ADMIN
    "oto_admin_set_option",       # SUPER_ADMIN
    "oto_admin_refresh_mount",    # écrit à la main (tools/mount.py) : autz NON déclarée
    "oto_search",                 # SUB_ONLY : jamais concerné
]


@pytest.fixture
def toolbox(monkeypatch):
    """Rend `compute_hidden_tools(role)` — tout le reste du calcul neutralisé.

    Seul le premier bloc est stubé (il lit la DB) : les blocs connecteur/RBAC/sélection
    sont fail-open par construction et n'ont aucun connecteur à résoudre sur des noms
    du namespace `oto`, donc ils ne retirent rien ici."""
    monkeypatch.setattr(db, "list_user_disabled_tools", lambda sub, org: [])
    monkeypatch.setattr(db, "list_user_enabled_tools", lambda sub, org: [])
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "org_admin_hidden_tools", lambda org: set())
    monkeypatch.setattr(access, "group_admin_hidden_tools", lambda g: set())

    async def _pour(role: str) -> set[str]:
        monkeypatch.setattr(access, "get_user_role", lambda sub: role)
        return await session_visibility.compute_hidden_tools(_Ctx(OUTILS), "sub-x")
    return _pour


@pytest.mark.asyncio
async def test_un_org_admin_voit_le_geste_que_son_autz_lui_accorde(toolbox):
    """Le trou de #471 : retirer un membre existe, l'autz l'accorde, l'agent ne l'atteint
    pas. `oto_admin_org_member` réunit quatre ops `ORG_ADMIN_OF` et un `list` plateforme —
    il reste donc UTILE hors de la plateforme, et se montre. Son `op=list` continuera de
    répondre 403 : c'est l'autz qui tranche à l'appel, comme toujours."""
    caches = await toolbox("member")
    assert "oto_admin_org_member" not in caches
    assert "oto_admin_guide" not in caches      # ORG_MEMBER_OF / ORG_ADMIN_OF


@pytest.mark.asyncio
async def test_ce_qui_est_reserve_a_la_plateforme_reste_cache(toolbox):
    """Le pendant, non négociable : un outil dont TOUTES les branches exigent la
    plateforme n'est utile à personne d'autre — il ne fait qu'alourdir le contexte."""
    caches = await toolbox("member")
    for n in ("oto_admin_org", "oto_admin_signal", "oto_admin_monitoring",
              "oto_admin_unipile_seat", "oto_admin_set_option",
              # Une campagne de relance n'a rien à faire dans la toolbox de qui la
              # subirait : 82 des 87 comptes de la prod sont `member` (2026-09-02).
              "oto_admin_outreach"):
        assert n in caches, n


@pytest.mark.asyncio
async def test_un_admin_plateforme_non_super_voit_enfin_ses_outils(toolbox):
    """Le second défaut, plus discret : le masquage testait `is_super_admin`, alors que
    `PLATFORM_ADMIN` accepte l'`admin` de supervision. Un opérateur plateforme non-super
    ne voyait donc AUCUN outil admin, y compris ceux écrits pour lui."""
    caches = await toolbox("admin")
    for n in ("oto_admin_signal", "oto_admin_monitoring", "oto_admin_org",
              "oto_admin_outreach"):
        assert n not in caches, n
    # …mais l'escalade s'arrête là : le super reste le super.
    for n in ("oto_admin_unipile_seat", "oto_admin_set_option"):
        assert n in caches, n


@pytest.mark.asyncio
async def test_le_super_admin_ne_perd_rien(toolbox):
    caches = await toolbox("super_admin")
    assert not (caches & set(OUTILS))


@pytest.mark.asyncio
async def test_un_outil_sans_autz_declaree_garde_le_repli_par_le_nom(toolbox):
    """`oto_admin_refresh_mount` est écrit à la main (`tools/mount.py`) : sa garde vit
    DANS son handler, invisible d'ici. On ne peut donc rien dériver — le préfixe reste
    son seul indice, et il vaut mieux le masquer de trop que le montrer à tort. Le jour
    où il devient une capacité, il tombera sous la règle générale sans qu'on y pense."""
    assert "oto_admin_refresh_mount" in await toolbox("member")
    assert "oto_admin_refresh_mount" not in await toolbox("admin")


@pytest.mark.asyncio
async def test_un_outil_ordinaire_n_est_jamais_concerne(toolbox):
    assert "oto_search" not in await toolbox("member")


# ── L'inventaire : ajouter un outil admin est une DÉCISION ────────────────────

# Le plancher effectif de chaque `oto_admin_*`, tel que son autz le déclare. Figé
# exprès : la règle générale est bonne, mais elle rend maintenant visible tout outil
# admin dont une seule op est org-scopée. Poser un `oto_admin_*` neuf doit donc passer
# par ici — c'est le moment où l'on regarde à qui il apparaîtra, plutôt que de le
# découvrir dans la toolbox de tout le monde.
PLANCHERS = {
    # Lecture seule de la fenêtre de double lecture L7 : une lentille de supervision,
    # comme le monitoring — elle ne montre aucun secret et n'écrit rien (le compteur
    # est alimenté par le chemin de résolution, et s'éteint par l'environnement).
    "oto_admin_access_shadow": "operator",
    # Surcharger une propriété de connecteur en base fait primer la donnée sur le code
    # servi : c'est un acte de plateforme, jamais délégable à un opérateur.
    "oto_admin_connector_setting": "super",
    "oto_admin_guide": None,            # ORG_MEMBER_OF / ORG_ADMIN_OF
    "oto_admin_invite": "operator",
    "oto_admin_key_grant": "operator",     # list PLATFORM, grant/revoke SUPER
    "oto_admin_monitoring": "operator",
    "oto_admin_org": "operator",           # list/get PLATFORM, create/archive SUPER
    # `admin.account` (pause d'un compte) : plancher `None`, donc l'outil entre
    # dans la boîte de CHAQUE compte — 837 caractères de description servis à
    # tout le monde. Assumé, et ce n'est pas un oubli : ses deux ops passent par
    # `TENANT_ADMIN_OF_TARGET`, dont l'accès dépend d'une cible que le handshake
    # ne connaît pas. Le masquer ne protégerait rien (ADR 0031/0066-R4) et
    # rendrait le geste INAPPELABLE pour un admin de tenant — fastmcp refuse
    # aussi le `tools/call` d'un outil masqué (#471). Or c'est précisément lui
    # que ce verbe existe pour servir, et il travaille par MCP.
    "oto_admin_account": None,
    "oto_admin_org_member": None,          # 4 ops ORG_ADMIN_OF, list PLATFORM
    "oto_admin_platform_instructions": "operator",
    "oto_admin_set_option": "super",
    "oto_admin_set_plan": "super",
    # #863 — sonder la session d'un TIERS. `operator` (PLATFORM_ADMIN) et pas
    # `org_admin` : sonder l'accès de quelqu'un n'est pas un geste d'org, sinon un
    # admin d'org sonderait les instances de ses membres. Ce qui rend le régime
    # tenable (pas de consentement préalable, décision du 04/09) n'est pas la trace,
    # c'est l'étroitesse de ce qui est lisible par là — un seul verdict.
    "oto_admin_instance_health": "operator",
    "oto_admin_signal": "operator",
    "oto_admin_tenant": "operator",        # list/get PLATFORM, reload SUPER
    "oto_admin_unipile_seat": "super",
    "oto_admin_user": "operator",          # list/get PLATFORM, set_role SUPER
    "oto_admin_vault_health": "operator",
}


def test_inventaire_des_planchers_admin():
    from oto_mcp.capabilities._authz import platform_floor
    from oto_mcp.capabilities.registry import CAPABILITIES

    reel = {c.mcp: platform_floor(c.authz) for c in CAPABILITIES
            if c.mcp and c.mcp.startswith("oto_admin_")}
    assert reel == PLANCHERS, (
        "l'inventaire des planchers admin a bougé. Un plancher `None` rend l'outil "
        "VISIBLE à tout le monde (son autz refusera les ops hors de portée) : c'est "
        "voulu quand une op sert un org_admin, à regarder deux fois sinon. Mets la "
        "table à jour en connaissance de cause.")


def test_le_plancher_d_un_combinateur_est_le_plus_BAS_de_ses_branches():
    """Le plus haut re-créerait le défaut : `oto_admin_org_member` serait masqué à un
    org_admin à cause de son seul `op=list`, réservé à la plateforme."""
    from oto_mcp.capabilities._authz import (ADMIN_BY_OP, ORG_ADMIN_OF, PLATFORM_ADMIN,
                                             SUPER_ADMIN, platform_floor)

    assert platform_floor(ADMIN_BY_OP({"a": ORG_ADMIN_OF("org_id"),
                                       "b": PLATFORM_ADMIN})) is None
    assert platform_floor(ADMIN_BY_OP({"a": PLATFORM_ADMIN,
                                       "b": SUPER_ADMIN})) == "operator"
    assert platform_floor(ADMIN_BY_OP({"a": SUPER_ADMIN})) == "super"


def test_montrer_l_outil_ne_donne_aucun_droit(monkeypatch):
    """La contrepartie de tout ce fichier, prouvée et non affirmée : rendre
    `oto_admin_org_member` visible à un membre ordinaire ne lui accorde rien. L'autz
    reste appliquée à l'appel, et c'est elle la barrière (ADR 0031)."""
    from oto_mcp.capabilities import _authz
    from oto_mcp.capabilities.admin_console import OrgMemberAdminInput
    from oto_mcp.capabilities._types import AuthzDenied, RawCtx
    from oto_mcp.capabilities.registry import CAPABILITIES

    capa = next(c for c in CAPABILITIES if c.mcp == "oto_admin_org_member")
    monkeypatch.setattr(_authz.roles, "is_org_admin", lambda sub, org: False)

    with pytest.raises(AuthzDenied) as refus:
        capa.authz(RawCtx(sub="sub-membre"),
                   OrgMemberAdminInput(op="remove", org_id=246, target="qqn@x.fr"))
    assert refus.value.status == 403
