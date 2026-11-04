"""Un guide servi à un compte de tenant parle de SON produit, pas du nôtre.

Le socle d'accueil suit déjà le tenant (`test_socle_tenant.py`, 13/08). Les guides à la
demande, non — et ce sont les textes les plus lus après le socle : `oto_guide op=read`
est prescrit par le socle lui-même. Un partenaire sert donc aujourd'hui NOS guides à SES
agents.

**Deux défauts distincts, et le second est le plus large.**

1. *Quel* guide est servi. Le magasin cherche plateforme → org → user ; il n'a pas
   d'étage tenant à la lecture, alors que la colonne l'accepte déjà pour la prose
   d'initialisation. Un partenaire qui rédige les siens ne peut pas les faire servir.
2. *De qui* ce guide parle. Le corps n'est jamais réécrit aux noms d'outils du produit,
   alors que l'artefact de session, les descriptions d'outils et les messages d'erreur le
   sont. Le guide prescrit donc `oto_doc` à un agent dont l'outil s'appelle `acme_doc` —
   exactement ce que `tool_alias.rewrite_prose` existe pour empêcher : « traduire la
   liste sans traduire la consigne ne corrige rien ». Celui-là touche le partenaire qui
   n'a RIEN rédigé, c'est-à-dire le cas courant.

Invariant tenu de bout en bout : **sans déclaration de tenant, l'octet servi est celui
d'avant.** Un compte de la plateforme ne voit aucune différence.
"""
from __future__ import annotations

import pytest

from oto_mcp import guide_store, tenancy


@pytest.fixture
def registre():
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme", "issuer": "https://auth.acme.test/oidc",
                  "tool_prefix": "acme"},
                 {"slug": "beta", "name": "Beta", "issuer": "https://auth.beta.test/oidc",
                  "tool_prefix": "beta"}])))
    yield
    tenancy.install(avant)


@pytest.fixture
def magasin(registre, monkeypatch):
    """La base simulée : un guide `notice` chez nous, et un chez `acme`.

    On simule le MAGASIN, pas la réponse : ce qu'on éprouve est la cascade de lecture,
    pas la capacité à rendre ce qu'on lui a soufflé."""
    lignes = {
        ("platform", guide_store.PLATFORM_OWNER, "notice"): {
            "slug": "notice", "title": "Notice", "description": "Le mode d'emploi",
            "body_md": "Écris avec `oto_doc`, puis publie avec `oto_project`."},
        ("tenant", "acme", "notice"): {
            "slug": "notice", "title": "Notice Acme", "description": "Le mode d'emploi Acme",
            "body_md": "Chez Acme, on écrit avec `acme_doc`."},
        ("tenant", "acme", "demarrage"): {
            "slug": "demarrage", "title": "Démarrer", "description": "Premiers pas",
            "body_md": "Bienvenue chez Acme."},
    }
    from oto_mcp import db
    monkeypatch.setattr(db, "get_guide_db",
                        lambda scope, owner, slug: lignes.get((scope, owner, slug)))
    monkeypatch.setattr(db, "list_guides_db",
                        lambda scope, owner: [v for (sc, ow, _), v in lignes.items()
                                              if (sc, ow) == (scope, owner)])
    monkeypatch.setattr(guide_store, "list_file_guides", lambda: [])
    monkeypatch.setattr(guide_store, "file_guide", lambda slug: None)
    return lignes


# ── 1. Quel guide est servi ──────────────────────────────────────────────────

def test_le_guide_du_TENANT_remplace_le_notre(magasin):
    g = guide_store.read_guide_scoped("notice", sub="acme:u-1")
    assert g["scope"] == "tenant"
    assert "Acme" in g["body_md"], "c'est SA notice, pas la nôtre"


def test_un_guide_que_seul_le_tenant_possede_est_servi(magasin):
    g = guide_store.read_guide_scoped("demarrage", sub="acme:u-1")
    assert g is not None and g["scope"] == "tenant"


def test_un_tenant_SANS_guide_propre_retombe_sur_le_notre(magasin, monkeypatch):
    """Le garde-fou d'inertie : déclarer un tenant ne change rien tant que personne n'a
    rédigé. C'est ce qui rend ce lot livrable sans coordination."""
    from oto_mcp import db
    vrai = db.get_guide_db
    monkeypatch.setattr(db, "get_guide_db",
                        lambda sc, ow, sl: None if sc == "tenant" else vrai(sc, ow, sl))
    g = guide_store.read_guide_scoped("notice", sub="acme:u-1")
    assert g["scope"] == "platform" and "oto_doc" in g["body_md"]


def test_un_compte_de_la_plateforme_ne_voit_aucune_difference(magasin):
    g = guide_store.read_guide_scoped("notice", sub="bn01jfy76a5n")
    assert g["scope"] == "platform"
    assert g["body_md"] == magasin[("platform", guide_store.PLATFORM_OWNER, "notice")]["body_md"]


def test_le_catalogue_ne_montre_pas_DEUX_fois_le_meme_slug(magasin):
    """Un slug surchargé est servi une seule fois — celui du tenant. Deux entrées de
    même slug feraient choisir l'agent entre deux guides dont un seul lui sera rendu."""
    vus = guide_store.list_guides_for(sub="acme:u-1")
    slugs = [g["slug"] for g in vus]
    assert len(slugs) == len(set(slugs)), f"doublon dans le catalogue : {slugs}"
    par_slug = {g["slug"]: g for g in vus}
    assert par_slug["notice"]["scope"] == "tenant"
    assert "demarrage" in par_slug


# ── 2. De qui ce guide parle ─────────────────────────────────────────────────

def test_le_corps_servi_cite_les_outils_DU_PRODUIT(magasin, monkeypatch):
    """Le cas courant, et le plus large : un partenaire qui n'a rien rédigé reçoit NOTRE
    guide — il doit au moins prescrire des outils qui existent chez lui."""
    from oto_mcp import db
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import ResolvedCtx
    vrai = db.get_guide_db
    monkeypatch.setattr(db, "get_guide_db",
                        lambda sc, ow, sl: None if sc == "tenant" else vrai(sc, ow, sl))

    g = G._get(ResolvedCtx(sub="acme:u-1", org_id=None),
               G.GuideRefInput(scope="", slug="notice"))
    assert "acme_doc" in g["body_md"] and "acme_project" in g["body_md"]
    assert "oto_doc" not in g["body_md"], "la consigne nommerait un outil qu'il n'a pas"


def test_sans_prefixe_declare_la_prose_est_INCHANGEE(magasin, monkeypatch):
    """Rien n'est renommé par défaut — même parti pris que le préfixe, les liens et
    l'identité annoncée au handshake."""
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp import tool_alias
    monkeypatch.setattr(tool_alias, "prefix_for", lambda sub: "")

    g = G._get(ResolvedCtx(sub="acme:u-1", org_id=None),
               G.GuideRefInput(scope="", slug="notice"))
    assert "Acme" in g["body_md"]


def test_un_compte_de_la_plateforme_lit_la_prose_a_l_octet(magasin):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import ResolvedCtx
    g = G._get(ResolvedCtx(sub="bn01jfy76a5n", org_id=None),
               G.GuideRefInput(scope="", slug="notice"))
    assert g["body_md"] == magasin[("platform", guide_store.PLATFORM_OWNER, "notice")]["body_md"]


# ── 3. Qui l'écrit ───────────────────────────────────────────────────────────
# La lecture servait le guide du tenant ; l'écriture le refusait (« scope éditable =
# platform | org | user ») — un étage qu'on lit sans pouvoir y écrire. Même palier que
# le socle de tenant : l'admin plateforme, sur un tenant connu du registre.

@pytest.fixture
def ecrit(registre, monkeypatch):
    from oto_mcp import db
    faits = []

    def _set(scope, owner, slug, body, title, description, *, expected_rev=None, **_):
        faits.append((scope, owner, slug, body))
        return {"title": title, "description": description}

    monkeypatch.setattr(db, "set_guide_db", _set)
    monkeypatch.setattr(db, "delete_guide_db",
                        lambda scope, owner, slug, *, expected_rev=None, **_:
                        bool(faits.append(("del", scope, owner, slug))) or True)
    return faits


def _admin(monkeypatch, oui: bool, admins_de_tenant=()):
    """`oui` : admin plateforme. `admins_de_tenant` : les couples (slug, sub) déclarés
    admins d'un tenant — la table `tenant_admins`, lue par `db.is_tenant_admin`."""
    from oto_mcp import db, roles
    monkeypatch.setattr(roles, "is_platform_admin", lambda sub: oui)
    monkeypatch.setattr(db, "is_tenant_admin",
                        lambda slug, sub: (slug, sub) in set(admins_de_tenant))


def test_l_admin_plateforme_ecrit_le_guide_d_un_tenant(ecrit, monkeypatch):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import ResolvedCtx
    _admin(monkeypatch, True)
    G._set(ResolvedCtx(sub="bn01jfy76a5n", org_id=None),
           G.GuideSetInput(scope="tenant", owner_id="acme", slug="dessin", body_md="Dessine."))
    assert ecrit == [("tenant", "acme", "dessin", "Dessine.")]


def test_un_admin_d_org_n_ecrit_pas_le_guide_du_tenant(ecrit, monkeypatch):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    _admin(monkeypatch, False)
    with pytest.raises(AuthzDenied) as e:
        G._set(ResolvedCtx(sub="acme:u-1", org_id=None),
               G.GuideSetInput(scope="tenant", owner_id="acme", slug="dessin", body_md="x"))
    assert e.value.status == 403 and ecrit == []


def test_un_tenant_inconnu_est_refuse_plutot_qu_ecrit_pour_personne(ecrit, monkeypatch):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    _admin(monkeypatch, True)
    with pytest.raises(AuthzDenied) as e:
        G._set(ResolvedCtx(sub="bn01jfy76a5n", org_id=None),
               G.GuideSetInput(scope="tenant", owner_id="acmee", slug="dessin", body_md="x"))
    assert e.value.status == 404 and ecrit == []


def test_le_guide_d_un_tenant_se_retire(ecrit, monkeypatch):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import ResolvedCtx
    _admin(monkeypatch, True)
    G._delete(ResolvedCtx(sub="bn01jfy76a5n", org_id=None),
              G.GuideDeleteInput(scope="tenant", owner_id="acme", slug="dessin"))
    assert ecrit == [("del", "tenant", "acme", "dessin")]


# ── 4. L'admin du TENANT écrit ses guides à la demande (décision du 30/09/2026) ──
# Le contenu d'un tenant est le sien : son admin l'écrit sur SON tenant ; la plateforme
# garde la main (mandat). Le socle injecté du tenant reste au palier plateforme.

def test_l_admin_du_tenant_ecrit_le_guide_de_SON_tenant(ecrit, monkeypatch):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import ResolvedCtx
    _admin(monkeypatch, False, admins_de_tenant=[("acme", "acme:u-9")])
    G._set(ResolvedCtx(sub="acme:u-9", org_id=None),
           G.GuideSetInput(scope="tenant", owner_id="acme", slug="dessin", body_md="Dessine."))
    G._delete(ResolvedCtx(sub="acme:u-9", org_id=None),
              G.GuideDeleteInput(scope="tenant", owner_id="acme", slug="dessin"))
    assert ecrit == [("tenant", "acme", "dessin", "Dessine."),
                     ("del", "tenant", "acme", "dessin")]


def test_l_admin_d_un_AUTRE_tenant_est_refuse(ecrit, monkeypatch):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    _admin(monkeypatch, False, admins_de_tenant=[("acme", "acme:u-9")])
    with pytest.raises(AuthzDenied) as e:
        G._set(ResolvedCtx(sub="acme:u-9", org_id=None),
               G.GuideSetInput(scope="tenant", owner_id="beta", slug="dessin", body_md="x"))
    assert e.value.status == 403 and ecrit == []


def test_un_admin_de_tenant_n_ecrit_pas_le_SOCLE_injecte(ecrit, monkeypatch):
    from oto_mcp import guide_store as GS
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    _admin(monkeypatch, False, admins_de_tenant=[("acme", "acme:u-9")])
    monkeypatch.setattr(GS, "set_init_guide", lambda *a: pytest.fail("écrit"))
    with pytest.raises(AuthzDenied) as e:
        G._set(ResolvedCtx(sub="acme:u-9", org_id=None),
               G.GuideSetInput(scope="tenant", owner_id="acme", delivery="init",
                               body_md="Acme — autre socle"))
    assert e.value.status == 403


def test_l_admin_plateforme_RELIT_le_guide_qu_il_vient_d_ecrire(magasin, monkeypatch):
    """Défaut de la revue : la lecture ne suivait que le tenant de l'appelant — l'admin
    plateforme écrivait pour un partenaire sans pouvoir se relire (404)."""
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import ResolvedCtx
    _admin(monkeypatch, True)
    g = G._get(ResolvedCtx(sub="bn01jfy76a5n", org_id=None),
               G.GuideRefInput(scope="tenant", owner_id="acme", slug="demarrage"))
    assert g["scope"] == "tenant" and g["body_md"] == "Bienvenue chez Acme."


def test_un_compte_d_un_autre_tenant_ne_relit_pas_ce_guide(magasin, monkeypatch):
    from oto_mcp.capabilities import guides as G
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    _admin(monkeypatch, False)
    with pytest.raises(AuthzDenied) as e:
        G._get(ResolvedCtx(sub="beta:u-1", org_id=None),
               G.GuideRefInput(scope="tenant", owner_id="acme", slug="demarrage"))
    assert e.value.status == 403
