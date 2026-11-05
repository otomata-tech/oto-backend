"""Un compte en pause ne revient JAMAIS par un mécanisme automatique — sur SQL réel.

Les deux seuls chemins par lesquels une pause peut disparaître, et ils sont testés
ici parce qu'aucun garde-fou dérivé du DDL ne les voit :

  1. **la résurrection** — un ancien identifiant se présente, `upsert_user` ne trouve
     pas de ligne et en CRÉE une : la personne repart avec un compte neuf et un espace
     personnel neuf. Ce n'est pas une hypothèse, c'est arrivé avec la suppression, et
     le compte ressuscité a servi 884 appels sous une identité morte ;
  2. **la fusion** — `migrate_sub` est le seul `DELETE FROM users` du dépôt. Sur un
     compte en pause, il emporterait la marque ET repointerait tout le patrimoine vers
     un compte vivant. Le rapprochement automatique a été désarmé le 2026-09-03, mais
     la porte d'OPÉRATEUR reste ouverte — c'est celle qui reste, donc celle qu'il faut
     fermer.

⚠️ **Chaque refus est accompagné de son contrefactuel** : le même geste, sur un compte
qui n'est PAS en pause, doit réussir. Sans ça, une garde qui refuserait tout serait
verte ici et ne se remarquerait qu'en production.

Patron de base éphémère : `test_migrate_sub_group_grants_db.py::live`.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    """Le schéma réel sur la base neuve du module, plus la clé maîtresse du coffre.

    ⚠️ La création et la DESTRUCTION de la base vivent dans `pg_module_dsn`
    (`conftest` racine). Cette fixture-ci créait la sienne et ne la supprimait
    jamais : onze bases orphelines, 143 Mo, mesurés le 08/09/2026 — une fuite sans
    fin, parce qu'un `finally` qui ne remet que des variables d'environnement ne
    rend pas ce qu'il a pris au serveur.
    """
    avant = {cle: os.environ.get(cle)
             for cle in ("DATABASE_URL", "OTO_MCP_MASTER_KEY")}
    os.environ["DATABASE_URL"] = pg_module_dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        for cle, valeur in avant.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur


def _sql(requete, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(requete, params).fetchall()


def _neuf(prefixe: str) -> str:
    return f"{prefixe}-{uuid.uuid4().hex[:8]}"


def _alias(old: str, new: str):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO sub_aliases (old_sub, new_sub) VALUES (%s,%s)",
                     (old, new))


# ── 1. La résurrection ──────────────────────────────────────────────────────

def test_un_ancien_identifiant_ne_ressuscite_pas_un_compte_en_pause(live):
    from oto_mcp import db
    vivant, ancien = _neuf("canon"), _neuf("vieux")
    db.upsert_user(vivant, email=f"{vivant}@acme.test")
    db.suspend_account(vivant, by="op-1", reason="doublon du partenaire")
    _alias(ancien, vivant)

    with pytest.raises(db.CompteEnPause):
        db.upsert_user(ancien, email=f"{vivant}@acme.test")

    # Rien n'a été créé : ni la ligne, ni l'espace personnel qui l'aurait accompagnée.
    # C'est la moitié qui compte — un refus levé APRÈS la naissance laisserait derrière
    # lui un compte fantôme et une org de plus.
    assert _sql("SELECT 1 FROM users WHERE sub=%s", (ancien,)) == []
    assert _sql("SELECT 1 FROM orgs WHERE personal_of=%s", (ancien,)) == []


def test_une_CHAINE_dalias_qui_mene_a_un_compte_en_pause_est_refusee(live):
    """⚠️ Le cas que le saut simple ne voit pas — et il est le cas RÉEL.

    `migrate_sub` écrit `(old → new)` sans aplatir les alias qui pointaient déjà vers
    `old` : deux fusions successives laissent donc A→B→C, et une chaîne à trois
    maillons a été mesurée en production (28/07 → 03/08 → 13/08). Une garde qui ne
    regarde que le premier saut trouve B — un compte que la fusion a supprimé, donc
    ni vivant ni en pause — et laisse passer. Le porteur du jeton A repart avec un
    compte neuf, ce qui est exactement le geste qu'on interdit."""
    from oto_mcp import db
    a, b, c = _neuf("maillon-a"), _neuf("maillon-b"), _neuf("canon-c")
    db.upsert_user(c, email=f"{c}@acme.test")
    db.suspend_account(c, by="op-1", reason="doublon du partenaire")
    _alias(a, b)          # première fusion : A a été absorbé par B…
    _alias(b, c)          # …puis B par C, qui est aujourd'hui en pause

    with pytest.raises(db.CompteEnPause):
        db.upsert_user(a, email=f"{c}@acme.test")
    assert _sql("SELECT 1 FROM users WHERE sub=%s", (a,)) == []


def test_une_chaine_dalias_qui_mene_a_un_compte_VIVANT_recree_bien_la_ligne(live):
    """Le contrefactuel de la chaîne : suivre les maillons ne doit pas se transformer
    en refus généralisé."""
    from oto_mcp import db
    a, b, c = _neuf("maillon-a"), _neuf("maillon-b"), _neuf("canon-c")
    db.upsert_user(c, email=f"{c}@acme.test")
    _alias(a, b)
    _alias(b, c)
    db.upsert_user(a, email=f"{a}@acme.test")
    assert _sql("SELECT 1 FROM users WHERE sub=%s", (a,)) != []


def test_un_cycle_dalias_ne_fait_pas_tourner_la_garde_sans_fin(live):
    """Rien n'interdit structurellement A→B→A. Une remontée non bornée y tournerait
    jusqu'au timeout, sur le chemin de naissance d'un compte.

    ⚠️ Ce que ce test prouve exactement : que la remontée S'ARRÊTE. C'est la borne de
    profondeur qui le garantit, pas la clause `NOT (… = ANY(chemin))` — celle-ci reste
    verte quand on la retire, parce qu'un cycle ne fait apparaître aucun compte en
    pause de plus. Elle borne un coût, et le commentaire de la requête le dit."""
    from oto_mcp import db
    a, b = _neuf("cycle-a"), _neuf("cycle-b")
    _alias(a, b)
    _alias(b, a)
    db.upsert_user(a, email=f"{a}@acme.test")     # ne doit ni pendre ni lever
    assert _sql("SELECT 1 FROM users WHERE sub=%s", (a,)) != []


def test_un_ancien_identifiant_dun_compte_VIVANT_recree_bien_la_ligne(live):
    """Le contrefactuel. Sans lui, une garde qui refuserait TOUTE naissance passerait
    le test ci-dessus sans qu'on le sache — et casserait chaque inscription."""
    from oto_mcp import db
    vivant, ancien = _neuf("canon"), _neuf("vieux")
    db.upsert_user(vivant, email=f"{vivant}@acme.test")
    _alias(ancien, vivant)
    db.upsert_user(ancien, email=f"{ancien}@acme.test")
    assert _sql("SELECT 1 FROM users WHERE sub=%s", (ancien,)) != []


def test_une_inscription_ordinaire_nest_pas_touchee(live):
    """L'autre contrefactuel : un sub sans aucun alias, le cas de 100 % des comptes."""
    from oto_mcp import db
    neuf = _neuf("neuf")
    db.upsert_user(neuf, email=f"{neuf}@acme.test")
    assert _sql("SELECT 1 FROM users WHERE sub=%s", (neuf,)) != []


# ── 2. La fusion ────────────────────────────────────────────────────────────

def test_la_fusion_refuse_une_SOURCE_en_pause(live):
    from oto_mcp import db
    source, cible = _neuf("src"), _neuf("dst")
    db.upsert_user(source)
    db.upsert_user(cible)
    db.suspend_account(source, by="op-1", reason="identité disparue de l'annuaire")

    with pytest.raises(db.CompteEnPause):
        db.migrate_sub(source, cible, operator_source="test")

    # La ligne est toujours là : le refus a bien empêché le `DELETE` de l'étape 4,
    # et pas seulement produit un message.
    assert _sql("SELECT suspended_at FROM users WHERE sub=%s", (source,))[0]["suspended_at"]


def test_la_fusion_refuse_une_CIBLE_en_pause(live):
    """L'autre sens, et il n'est pas symétrique par accident : verser le patrimoine
    d'un compte vivant dans un compte neutralisé le rendrait inatteignable sans que
    personne ne l'ait voulu."""
    from oto_mcp import db
    source, cible = _neuf("src"), _neuf("dst")
    db.upsert_user(source)
    db.upsert_user(cible)
    db.suspend_account(cible, by="op-1", reason="sortie définitive")

    with pytest.raises(db.CompteEnPause):
        db.migrate_sub(source, cible, operator_source="test")

    assert _sql("SELECT 1 FROM users WHERE sub=%s", (source,)) != []


def test_la_fusion_de_deux_comptes_vivants_passe_toujours(live):
    """Le contrefactuel de la fusion : la garde ne doit pas avoir fermé le merge."""
    from oto_mcp import db
    source, cible = _neuf("src"), _neuf("dst")
    db.upsert_user(source)
    db.upsert_user(cible)
    assert db.migrate_sub(source, cible, operator_source="test") is True
    assert _sql("SELECT 1 FROM users WHERE sub=%s", (source,)) == []


def test_lauteur_dune_pause_est_repointe_par_la_fusion(live):
    """`users.suspended_by` porte un sub et n'a pas de FK : sans repointage, la ligne
    survivrait au `DELETE` de l'étape 4 en désignant un identifiant disparu — la
    signature ne deviendrait pas historique, elle deviendrait illisible."""
    from oto_mcp import db
    operateur, canonique, dormeur = _neuf("op"), _neuf("op-canon"), _neuf("dort")
    db.upsert_user(operateur)
    db.upsert_user(canonique)
    db.upsert_user(dormeur)
    db.suspend_account(dormeur, by=operateur, reason="posée avant la fusion")

    assert db.migrate_sub(operateur, canonique, operator_source="test") is True
    assert db.get_suspension(dormeur)["suspended_by"] == canonique


# ── 3. Les deux verbes ──────────────────────────────────────────────────────

def test_reposer_une_pause_ne_reecrit_ni_lauteur_ni_le_motif(live):
    """Une pause est un fait daté. L'écraser ferait perdre qui l'a décidée et quand,
    c'est-à-dire la seule chose que ces colonnes existent pour retenir."""
    from oto_mcp import db
    sub = _neuf("dort")
    db.upsert_user(sub)
    premier = db.suspend_account(sub, by="op-1", reason="motif d'origine")
    second = db.suspend_account(sub, by="op-2", reason="motif réécrit")
    assert second["suspended_by"] == "op-1"
    assert second["suspended_reason"] == "motif d'origine"
    assert second["suspended_at"] == premier["suspended_at"]


def test_le_reveil_dit_sil_a_reveille_quelque_chose(live):
    from oto_mcp import db
    sub = _neuf("dort")
    db.upsert_user(sub)
    db.suspend_account(sub, by="op-1", reason="essai")
    assert db.resume_account(sub) is True
    assert db.get_suspension(sub) is None
    # Deuxième réveil : il ne dormait plus. « Réveillé » et « il ne dormait pas » ne
    # sont pas la même réponse, et une console qui affiche « fait » dans les deux cas
    # ment une fois sur deux.
    assert db.resume_account(sub) is False


def test_une_pause_ne_detruit_ni_ne_detache_rien(live):
    """Le cœur du geste, vérifié sur ce qui pend réellement d'un compte : son espace
    personnel, son appartenance, son profil. Tout doit être là APRÈS la pause, et
    continuer de désigner ce compte."""
    from oto_mcp import db, org_store
    sub = _neuf("membre")
    db.upsert_user(sub, email=f"{sub}@acme.test")
    perso = org_store.home_org(sub)
    db.update_account_profile(sub, {"metier": "chef de projet"})
    assert perso

    db.suspend_account(sub, by="op-1", reason="parti chez le partenaire")

    assert org_store.home_org(sub) == perso
    assert _sql("SELECT 1 FROM org_members WHERE sub=%s", (sub,)) != []
    assert db.get_account_profile(sub)["profile"]["metier"] == "chef de projet"
    # …et le compte est bien lisible comme membre, marqué en pause plutôt que retiré.
    from oto_mcp.capabilities.orgs import reads
    lignes = reads._members(perso["id"] if isinstance(perso, dict) else perso,
                            exposer_operateur=False)
    moi = [m for m in lignes if m["sub"] == sub]
    assert moi and moi[0]["suspended"] is True
