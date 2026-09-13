"""Écrire dans le NOUVEL univers — et n'y écrire que ce qui lui appartient.

Arbitrage d'Alexis (31/08/2026) : on ne migre pas, on arrête la recopie, la surface
nœud vit **à côté** de l'ancienne et part de vide. Ce qui s'écrit ici est donc NATIF —
aucune source dans l'ancien monde, rien qui le rafraîchisse, et les anciennes surfaces
ne le voient pas.

Trois invariants que ces tests tiennent, chacun payé ailleurs :

1. **Aucun nœud natif ne porte `delivery`** — la recherche discrimine une couche de
   contexte par cette propriété, PAS par le genre. En poser une sur une page ordinaire
   la ferait remonter dans le périmètre injecté au handshake.
2. **Trois genres, jamais un genre pour un rôle** (ADR 0054-D5). Une page reste une
   page, même quand elle joue autre chose.
3. **Un nœud COPIÉ de l'ancien monde ne s'écrit pas ici** — sa source y est la vérité,
   et l'écrire des deux côtés ferait diverger les deux. **Une couche de contexte non
   plus** (oto#198) : elle s'écrit par `oto_guide`, et la garde lit la MÊME surface que
   la fiche (`node_keys.edit_surface_de`).
4. **On n'écrit que ce dont on est PROPRIÉTAIRE**, et ça se compare (2026-09-01). Le
   palier des guides résout une identité, il n'autorise pas : l'appeler et jeter sa
   réponse n'était pas une garde. Le refus est indistinct de l'introuvable.

Le REJEU de bout en bout sur la route servie vit dans
`tests/api/test_node_edit_proprietaire.py` : ici on tient les invariants au grain de
la fonction, là-bas on prouve les codes de retour réellement servis.
"""
from __future__ import annotations

import inspect

import pytest

from oto_mcp.capabilities import node_edit
from oto_mcp.db import nodes as db_nodes


# --- ce que la couche d'écriture refuse d'introduire ---------------------------

def test_une_page_native_ne_porte_JAMAIS_delivery():
    """L'invariant qui protège le périmètre des couches de contexte : `delivery` est
    ce qui distingue un guide, et il n'appartient qu'à eux."""
    src = inspect.getsource(db_nodes.create_page)
    assert "delivery" not in src, (
        "la création pose `delivery` : la page remonterait comme une couche de "
        "contexte dans la recherche, donc dans ce qu'on injecte au handshake")


def test_la_creation_nintroduit_aucun_genre_neuf():
    """ADR 0054-D5 : le genre dit ce que l'objet EST, le rôle est porté en propriété.
    Un `kind='projet'` ou `kind='procédure'` réintroduirait l'objet que le modèle
    retire."""
    src = inspect.getsource(db_nodes.create_page)
    assert "_KIND" in src, "la création doit poser le genre canonique, pas un littéral"
    assert db_nodes._KIND == "page"
    for role in ("project", "projet", "guide", "procedure", "procédure", "agent"):
        assert f"'{role}'" not in src and f'"{role}"' not in src, (
            f"« {role} » apparaît comme genre : c'est un RÔLE, jamais un `kind`")


def test_une_page_native_na_pas_de_source_dans_lancien_monde():
    """C'est ce qui la distingue d'une copie : rien ne la rafraîchit, et la purge des
    copies (qui cible `props.legacy`) ne doit jamais l'emporter."""
    src = inspect.getsource(db_nodes.create_page)
    assert "legacy" not in src, (
        "la création pose une clé d'origine : la page serait prise pour une copie, "
        "donc écrasée par une re-projection ou emportée par la purge")


# --- l'identité, et pourquoi elle est tirée ------------------------------------

def test_lidentifiant_est_TIRÉ_jamais_calculé():
    """Une identité dérivée du contenu ou du rang se recalcule — donc elle casse au
    premier renommage ou réordonnancement, et toute référence externe part avec.
    Les nœuds CONVERTIS dérivent la leur (c'est ce qui rend leur conversion
    idempotente) ; un natif n'a pas de clé naturelle."""
    a, b = db_nodes._new_node_id(), db_nodes._new_node_id()
    assert a != b, "deux créations produisent le même identifiant"
    assert a.startswith("nod_") and len(a) > 20
    src = inspect.getsource(db_nodes._new_node_id)
    assert "md5" not in src and "sha" not in src, "l'identité est dérivée du contenu"


def test_le_corps_nest_reecrit_que_sil_change():
    """Les blocs portent des identifiants stables qu'une prose peut citer. Les
    réécrire à chaque édition de titre les ferait tous changer, et les citations
    tomberaient."""
    src = inspect.getsource(db_nodes.update_page)
    assert "if body_md is not None" in src, (
        "le corps est réécrit inconditionnellement : éditer un titre ré-identifierait "
        "tous les paragraphes")


def test_la_suppression_ramasse_la_descendance():
    """L'arbre n'a PAS de clé étrangère (arbitrage M-e, ouvert) : sans ramassage, un
    parent supprimé laisse des enfants rattachés à un identifiant disparu — des
    orphelins qu'aucun lecteur ne trouve et qu'aucune purge ne voit.

    ⚠️ Le SQL a quitté le corps de `delete_page` pour la constante `_DESCENDANCE`
    (2026-09-01) : chercher « RECURSIVE » dans la source de la fonction ne voyait plus
    rien. Le relevé suit donc l'objet, et il exige les DEUX propriétés — récursive
    (elle ramasse) et bornée (elle termine sur un cycle déjà en base).

    La preuve de comportement, elle, est dans `test_node_parent_cycle.py`, sur une
    vraie base : quarante niveaux ramassés, et une boucle qui ne fait plus tourner
    la requête à vide."""
    assert "_DESCENDANCE" in inspect.getsource(db_nodes.delete_page), (
        "la descendance n'est pas ramassée")
    assert "WITH RECURSIVE" in db_nodes._DESCENDANCE
    assert "CYCLE " in db_nodes._DESCENDANCE, (
        "descente non bornée : un cycle déjà en base remplit `pgsql_tmp`")


# --- l'écriture ne franchit pas la frontière des deux univers ------------------

class _Ctx:
    """Le porteur minimal — `_owner_for_write` ne lit que ça au cran personne."""
    sub = "usr_moi"
    org_id = None
    group_id = None


def _fiche(**kw) -> dict:
    base = {"id": 1, "public_id": "nod_x", "owner_type": "user",
            "owner_id": "usr_moi", "props": {}}
    base.update(kw)
    return base


def test_un_noeud_COPIÉ_ne_sécrit_pas_ici():
    """LE point de couture entre les deux mondes. Sa source est la vérité dans
    l'ancien ; l'écrire des deux côtés ferait diverger les deux, et une
    re-projection écraserait silencieusement ce qu'on vient d'écrire."""
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as err:
        node_edit._mien(_Ctx(), _fiche(props={"legacy": "doc", "legacy_id": 42}))
    assert err.value.status == 409
    assert err.value.code == "node_projete"


def test_le_palier_est_appelé_avec_le_propriétaire_RÉEL_du_noeud(monkeypatch):
    """L'autorisation n'est pas réécrite : c'est `guides._owner_for_write`, qui porte
    déjà plateforme / org / chef d'équipe / soi. Une seconde version divergerait de la
    première au premier changement — c'est la faute qu'on répare ailleurs."""
    vus: list = []
    from oto_mcp.capabilities import guides
    monkeypatch.setattr(
        guides, "_owner_for_write",
        lambda ctx, scope, owner=None: vus.append((scope, owner)) or owner)
    node_edit._mien(_Ctx(), _fiche(owner_type="group", owner_id="3"))
    assert vus == [("group", "3")], "le palier réel du nœud n'est pas celui qu'on teste"


def test_un_palier_qui_résout_un_AUTRE_propriétaire_est_REFUSÉ(monkeypatch):
    """⚠️ **Le test qui vivait ici ne voyait pas le défaut, et c'est pour ça qu'il a
    vécu.** Il bouchonnait le palier par une lambda qui rendait « 7 » pour un nœud
    possédé par « 3 », puis n'assertait que l'APPEL — donc il restait vert alors que la
    valeur rendue était jetée sans comparaison. Un appel n'est pas un contrôle.

    C'est exactement la faute du code qu'il couvrait : `_owner_for_write` RÉSOUT une
    identité (au cran personne, `return ctx.sub`, sans regarder la cible), il
    n'autorise pas. La garde est la comparaison, et c'est elle qu'on prouve ici.
    """
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.capabilities import guides
    monkeypatch.setattr(guides, "_owner_for_write", lambda ctx, scope, owner=None: "7")
    with pytest.raises(AuthzDenied) as err:
        node_edit._mien(_Ctx(), _fiche(owner_type="group", owner_id="3"))
    assert (err.value.status, err.value.code) == (404, "not_found")


def test_le_refus_de_propriété_PRÉCÈDE_celui_de_la_copie():
    """L'ordre est une garde à lui seul : un tiers qui sonde un identifiant ne doit
    pas apprendre par un 409 que le nœud existe ET qu'il est une copie. Inversé,
    l'ordre rend l'oracle que le 404 indistinct retire."""
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as err:
        node_edit._mien(_Ctx(), _fiche(owner_id="usr_quelquun_dautre",
                                       props={"legacy": "doc", "legacy_id": 42}))
    assert (err.value.status, err.value.code) == (404, "not_found")


def test_le_refus_décriture_est_le_MÊME_OBJET_que_celui_de_lintrouvable():
    """Même statut, même code, même message — au caractère près. Deux messages
    différents referaient du CORPS de la réponse l'oracle que le code d'état n'est
    plus."""
    from oto_mcp.capabilities._types import AuthzDenied
    inconnu = node_edit._introuvable()
    with pytest.raises(AuthzDenied) as err:
        node_edit._mien(_Ctx(), _fiche(owner_id="usr_quelquun_dautre"))
    assert (err.value.status, err.value.code, str(err.value)) == \
           (inconnu.status, inconnu.code, str(inconnu))


# --- la couture : la capacité est-elle réellement montée ? ---------------------

def test_le_HUB_declare_le_module_dedition():
    """⚠️ LE test de couture, et ma première version ne mordait pas.

    Elle lisait le registre après que CE fichier ait importé `node_edit` — donc elle
    prouvait son propre import, pas celui du démarrage. En retirant la ligne du hub,
    elle restait verte. C'est la troisième fois dans la journée que le même piège se
    referme : un banc qui charge plus que le boot certifie une couverture qui n'existe
    pas.

    On lit donc le HUB, qui est ce que le serveur importe. Une capacité qu'aucun
    module chargé au boot ne déclare n'existe pas pour le produit, aussi juste soit
    son code."""
    import pathlib as _p
    hub = (_p.Path(__file__).resolve().parent.parent
           / "oto_mcp" / "capabilities" / "__init__.py").read_text(encoding="utf-8")
    assert "import node_edit" in hub, (
        "`node_edit` n'est déclaré par aucun module chargé au démarrage : la surface "
        "d'écriture n'est pas servie, quels que soient ses tests")


def test_la_capacite_declare_ses_deux_faces():
    from oto_mcp.capabilities import registry
    cap = next((c for c in registry.CAPABILITIES if c.key == "me.node.edit"), None)
    assert cap is not None, "la capacité d'écriture n'est pas déclarée au registre"
    assert cap.mcp == "oto_node_edit"
    assert cap.rest is not None and cap.rest.verb == "POST"
    assert cap.authz is not None, "une capacité sans autz est refusée par le moule"


def test_les_quatre_verbes_sont_joignables():
    """Un `op` déclaré mais non routé rend une erreur de validation brute au lieu d'un
    refus métier — la scène qu'on répare justement ailleurs ce soir."""
    declares = set(NodeEditInput_ops())
    assert declares == set(node_edit._OPS), (
        "un verbe est déclaré au schéma sans être routé, ou l'inverse")


def NodeEditInput_ops() -> list:
    champ = node_edit.NodeEditInput.model_fields["op"]
    return list(getattr(champ.annotation, "__args__", ()))


# --- ADR 0068 : un nœud créé sans scope naît à SOI ----------------------------

def test_le_scope_par_defaut_dune_creation_est_la_PERSONNE(monkeypatch):
    """Le défaut était `org` — un nœud créé sans rien préciser naissait lisible de
    tous les membres. Aucun banc ne le défendait, et c'est bien le problème : il
    n'était protégé par rien, donc personne n'avait décidé qu'il devait en être ainsi.

    ⚠️ Le palier exigeait déjà `org_admin` (`_owner_for_write`), donc personne
    n'élargissait sans en avoir le DROIT. Mais « en avoir le droit » n'est pas « l'avoir
    voulu », et l'appelant est un agent qui ne lit que le nom du verbe : un org_admin
    qui demande une page à son assistant en obtenait une que toute l'org peut lire.

    On lit ce que le code fait du scope, pas ce qu'on croit qu'il en fait : le palier
    est bouchonné pour observer l'argument qui lui parvient réellement."""
    vus: list = []
    from oto_mcp.capabilities import guides
    monkeypatch.setattr(guides, "_owner_for_write",
                        lambda ctx, scope, owner=None: vus.append(scope) or "usr_moi")
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied):        # `title` manquant : on s'arrête juste après
        node_edit._create(_Ctx(), node_edit.NodeEditInput(op="create", kind="page"))
    assert vus == ["user"], "un nœud sans scope demandé ne naît plus au palier de l'org"


def test_le_scope_org_reste_possible_en_le_DISANT(monkeypatch):
    """Privé par défaut n'est pas fermé : l'org reste une cible, elle se nomme."""
    vus: list = []
    from oto_mcp.capabilities import guides
    monkeypatch.setattr(guides, "_owner_for_write",
                        lambda ctx, scope, owner=None: vus.append(scope) or "7")
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied):
        node_edit._create(_Ctx(), node_edit.NodeEditInput(op="create", kind="page",
                                                            scope="org"))
    assert vus == ["org"]


def test_un_noeud_PERSONNEL_n_est_lisible_que_de_son_proprietaire(monkeypatch):
    """La vérification demandée par Alexis (04/09) : « privé par défaut » ne vaut que
    si la LECTURE le respecte aussi. Un défaut de création privé au-dessus d'une
    lecture permissive ne protégerait rien — il donnerait seulement l'impression.

    `ownership.owner_in_scope` est la règle de portée UNIQUE de la plateforme, et sa
    branche `user` exige l'égalité stricte avec l'appelant. Aucune escalade
    d'administrateur n'y touche — contrairement au palier ÉQUIPE, qui en a une
    (`roles.can_read_group`, ADR 0049) et que ce banc mesure aussi, pour que l'écart
    entre les deux paliers soit un fait écrit et non une surprise."""
    from oto_mcp import ownership

    monkeypatch.setattr(ownership, "active_owner", lambda org: ("org", str(org)))
    # Un nœud à MOI : lisible par moi, dans n'importe quel contexte d'org.
    assert ownership.owner_in_scope("usr_moi", 7, ("user", "usr_moi")) is True
    assert ownership.owner_in_scope("usr_moi", 99, ("user", "usr_moi")) is True
    # Le même nœud, lu par quelqu'un d'autre de la MÊME org — y compris un admin :
    # la branche `user` ne regarde ni le rôle ni l'org, seulement l'égalité.
    assert ownership.owner_in_scope("usr_admin", 7, ("user", "usr_moi")) is False
    # Et un nœud d'ORG reste, lui, visible de son org active — le contraste est le
    # sens même du défaut : c'est le choix du scope qui décide, pas le hasard.
    assert ownership.owner_in_scope("usr_admin", 7, ("org", "7")) is True


# --- oto#198 : la garde d'écriture lit la MÊME surface que la fiche -----------

def test_U6_la_surface_se_derive_en_UN_seul_point():
    """La fiche annonce, la garde refuse. Si l'une relit le stockage à sa façon, la fiche
    finit par dire `node` d'un nœud que l'écriture refuse — et le client croit la fiche.
    `props.get("legacy")` était ce second lecteur : un `legacy` vide y passait pour natif."""
    from oto_mcp.capabilities import node_view
    assert "edit_surface_de(" in inspect.getsource(node_view._compose)
    assert "edit_surface_de(" in inspect.getsource(node_edit._s_ecrit_ici)
    src = inspect.getsource(node_edit)
    assert 'get("legacy")' not in src and "get('legacy')" not in src, (
        "la garde d'écriture relit `props.legacy` à côté de la dérivation partagée")


_GUIDE = {"delivery": "on-demand", "slug": "prospection"}


def test_U8_un_guide_POSSEDE_est_refuse_en_nommant_oto_guide():
    """Il s'écrivait ici en 200, en contournant la borne de `oto_guide`. Le refus nomme la
    destination : un « non » seul fait tourner un agent en rond."""
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as err:
        node_edit._mien(_Ctx(), _fiche(props=dict(_GUIDE)))
    assert (err.value.status, err.value.code) == (409, "node_guide")
    assert "`oto_guide`" in err.value.message and "slug=prospection" in err.value.message
    assert err.value.details == {"edit_surface": "guide", "scope": "user",
                                 "slug": "prospection", "delivery": "on-demand"}


def test_U8_le_guide_d_un_TIERS_rend_le_meme_404_que_l_inconnu():
    """`node_guide` se juge APRÈS la propriété : avant, un 409 dirait à un tiers qu'un
    guide existe sous cet identifiant — dérivé, donc devinable depuis un `sub`."""
    from oto_mcp.capabilities._types import AuthzDenied
    inconnu = node_edit._introuvable()
    with pytest.raises(AuthzDenied) as err:
        node_edit._mien(_Ctx(), _fiche(owner_id="usr_quelquun_dautre", props=dict(_GUIDE)))
    assert (err.value.status, err.value.code, err.value.message) == \
           (inconnu.status, inconnu.code, inconnu.message)
    assert err.value.details is None


def test_U8_le_readme_injecte_d_une_org_s_adresse_par_son_org_sans_slug(monkeypatch):
    from oto_mcp.capabilities import guides
    from oto_mcp.capabilities._types import AuthzDenied
    monkeypatch.setattr(guides, "_owner_for_write", lambda ctx, scope, owner=None: owner)
    with pytest.raises(AuthzDenied) as err:
        node_edit._mien(_Ctx(), _fiche(owner_type="org", owner_id="42",
                                       props={"delivery": "init", "slug": "readme"}))
    assert (err.value.status, err.value.code) == (409, "node_guide")
    assert "owner_id=42" in err.value.message and "sans slug" in err.value.message


def test_un_noeud_INCOHERENT_leve_500_au_lieu_de_passer_pour_natif(caplog):
    """`legacy` vide : l'ancien test de vérité le laissait écrire comme un nœud né ici."""
    from oto_mcp.capabilities._types import AuthzDenied
    with caplog.at_level("ERROR", logger=node_edit.__name__):
        with pytest.raises(AuthzDenied) as err:
            node_edit._mien(_Ctx(), _fiche(props={"legacy": ""}))
    assert (err.value.status, err.value.code) == (500, "noeud_incoherent")
    assert "nod_x" in err.value.message
    assert any("nod_x" in r.getMessage() for r in caplog.records), "rien n'est journalisé"


@pytest.fixture
def ecriture(monkeypatch):
    """Le nœud natif possédé, et ce qui parvient réellement à `update_page`."""
    appels: list = []
    monkeypatch.setattr(node_edit.db_node, "node_by_public_id",
                        lambda pid: _fiche(public_id=pid, kind="page"))
    monkeypatch.setattr(node_edit.db_nodes, "update_page",
                        lambda nid, **kw: appels.append(kw) or True)
    return appels


@pytest.mark.parametrize("titre", ["", "   "])
def test_U7_un_titre_VIDE_ou_BLANC_est_refuse_sans_rien_ecrire(ecriture, titre):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as err:
        node_edit._update(_Ctx(), node_edit.NodeEditInput(op="update", node_id="nod_x",
                                                          title=titre))
    assert (err.value.status, err.value.code) == (400, "missing_title")
    assert ecriture == [], "le refus est tombé après l'écriture"


def test_U7_un_titre_est_stocke_TAILLE(ecriture):
    out = node_edit._update(_Ctx(), node_edit.NodeEditInput(op="update", node_id="nod_x",
                                                            title="  Neuf  "))
    assert out == {"ok": True, "id": "nod_x", "op": "update"}
    assert [a["title"] for a in ecriture] == ["Neuf"]


def test_U7_un_update_SANS_titre_n_exige_rien(ecriture):
    node_edit._update(_Ctx(), node_edit.NodeEditInput(op="update", node_id="nod_x",
                                                      body_md="corps"))
    assert ecriture == [{"title": None, "description": None, "body_md": "corps"}]
