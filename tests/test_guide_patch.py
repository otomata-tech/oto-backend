"""`oto_guide op=patch` — éditer UNE région d'un guide sans en réémettre le corps.

**Le manque.** `oto_guide` n'avait que `write`, qui REMPLACE le corps. Un guide tenu en
journal (une entrée datée par passage) grossit : à ~60 000 caractères, y ajouter une
ligne voulait dire relire le corps entier et le réémettre à l'identique — cher, et avec
le risque d'en abîmer le reste sans que rien ne le signale. Le geste existait déjà pour
les pages (`oto_doc op=patch`) ; ce fichier fige son pendant pour les guides.

**Ce que ce fichier tient :**

1. le MOTEUR est celui des pages (`doc_patch`) : mêmes paramètres (`mode`, `section`,
   `region`, `expected_rev`), mêmes codes de refus ;
2. le DROIT est celui d'`op=write`, à l'identique, et il passe avant toute lecture ;
3. un patch porte sur un guide EXISTANT — il ne crée rien ;
4. la réponse est un ACCUSÉ : jamais le corps ;
5. un lire-modifier-écrire ne perd pas une écriture concurrente : `expected_rev` refuse
   un guide qui a changé, et sans lui deux patchs s'enchaînent sous le verrou de la
   ligne — éprouvé contre un vrai PostgreSQL, sur le chemin servi.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest

from oto_mcp import db as vraie_db
from oto_mcp.capabilities import guides as C
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

_JOURNAL = """Journal des passages — une entrée par jour.

## 2026-01-05

- import terminé
- deux doublons écartés

## 2026-01-06

- rien à signaler

## Règles

Une entrée par passage, jamais de réécriture.

### Format

Date en titre, puces dessous."""


# ── Banc sans base : le store en mémoire, qui JOUE le callable ─────────────
# `patch_guide_db` est le seul point où le corps est lu puis réécrit : le double
# applique donc vraiment le moteur `doc_patch`, et compare la rev comme la vraie.

class _FausseBase:
    def __init__(self):
        self.guides: dict = {}     # (scope, owner, slug) -> {title, description, body_md}
        self.readmes: set = set()  # (scope, owner, slug) des couches `init`
        self.appels = 0

    def poser(self, scope, owner, slug, body_md, title="Journal", description="Le fil."):
        self.guides[(scope, str(owner), slug)] = {
            "title": title, "description": description, "body_md": body_md}

    def corps(self, scope, owner, slug):
        return self.guides[(scope, str(owner), slug)]["body_md"]

    def get_guide_db(self, scope, owner_id, slug):
        return self.guides.get((scope, str(owner_id), slug))

    def get_init_guide_db(self, scope, owner_id, slug):
        return {"body_md": ""} if (scope, str(owner_id), slug) in self.readmes else None

    def patch_guide_db(self, scope, owner_id, slug, appliquer, *, expected_rev=None, **_):
        self.appels += 1
        row = self.guides.get((scope, str(owner_id), slug))
        if row is None:
            return None
        rev = vraie_db.guide_rev(row["title"], row["description"], row["body_md"])
        if expected_rev is not None and expected_rev != rev:
            raise vraie_db.GuideConflict(rev)
        row["body_md"] = appliquer(row["body_md"])
        return {**row, "updated_at": "2026-01-07 09:00:00"}


@pytest.fixture
def base(monkeypatch):
    fausse = _FausseBase()
    for nom in ("get_guide_db", "get_init_guide_db", "patch_guide_db"):
        monkeypatch.setattr(vraie_db, nom, getattr(fausse, nom))
    # La lecture par l'agent sonde une procédure homonyme (oto#100) : aucune ici.
    monkeypatch.setattr(vraie_db, "procedure_du_slug", lambda *a, **k: None)
    import oto_mcp.roles as roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: sub == "u-admin")
    monkeypatch.setattr(roles, "is_platform_admin", lambda sub: False)
    fausse.poser("user", "u1", "journal", _JOURNAL)
    return fausse


def _op(sub="u1", org=None, **args):
    """UN appel d'`oto_guide`, contexte déjà résolu — comme le ferait l'adaptateur MCP."""
    return C._guide_op(ResolvedCtx(sub=sub, org_id=org), C.GuideOpInput(**args))


def _refus(**args) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        _op(**args)
    return e.value


# ── 1. Les quatre modes, sur une section ────────────────────────────────────

def test_append_ajoute_en_fin_de_section_et_ne_touche_a_rien_d_autre(base):
    _op(op="patch", slug="journal", section="2026-01-06", mode="append",
        body_md="- relance envoyée")
    assert base.corps("user", "u1", "journal") == _JOURNAL.replace(
        "- rien à signaler", "- rien à signaler\n\n- relance envoyée")


def test_prepend_insere_juste_sous_le_titre(base):
    _op(op="patch", slug="journal", section="2026-01-05", mode="prepend",
        body_md="- passage du matin")
    corps = base.corps("user", "u1", "journal")
    assert "## 2026-01-05\n\n- passage du matin\n" in corps
    assert corps.index("- passage du matin") < corps.index("- import terminé")
    # Le reste du guide est celui d'avant, à l'octet.
    suite = _JOURNAL[_JOURNAL.index("- import terminé"):]
    assert corps.endswith(suite) and corps.startswith(_JOURNAL[:_JOURNAL.index("- import")])


def test_replace_remplace_le_corps_de_la_section_et_garde_son_titre(base):
    out = _op(op="patch", slug="journal", section="2026-01-05", body_md="- repris à zéro")
    corps = base.corps("user", "u1", "journal")
    assert "## 2026-01-05\n\n- repris à zéro\n\n## 2026-01-06" in corps
    assert "import terminé" not in corps and "rien à signaler" in corps
    # `mode` omis = replace, et l'accusé dit ce qui est parti (lignes du corps d'avant).
    assert out["removed"]["line_count"] == 4 and out["removed"]["subsections"] == []


def test_delete_retire_la_section_titre_compris(base):
    out = _op(op="patch", slug="journal", section="2026-01-05", mode="delete")
    corps = base.corps("user", "u1", "journal")
    assert "2026-01-05" not in corps and "import terminé" not in corps
    assert corps.startswith("Journal des passages") and "## 2026-01-06" in corps
    assert out["removed"]["from_line"] == 3


def test_le_titre_se_vise_sans_son_niveau_ni_sa_casse(base):
    _op(op="patch", slug="journal", section="règles", mode="append", body_md="Un ajout.")
    assert "Un ajout." in base.corps("user", "u1", "journal")


def test_remplacer_un_parent_annonce_les_sous_sections_emportees(base):
    out = _op(op="patch", slug="journal", section="Règles", body_md="Tout est dit ici.")
    assert out["removed_subsections"] == ["Format"] and "warning" in out
    assert "### Format" not in base.corps("user", "u1", "journal")


# ── 2. Le préambule : un autre AXE, jamais un nom de section ───────────────

def test_le_preambule_se_vise_par_region(base):
    _op(op="patch", slug="journal", region="preamble", body_md="Dernier passage : 6 janvier.")
    corps = base.corps("user", "u1", "journal")
    assert corps.startswith("Dernier passage : 6 janvier.\n\n## 2026-01-05")
    _op(op="patch", slug="journal", region="preamble", mode="append", body_md="Tenu par le run.")
    assert base.corps("user", "u1", "journal").startswith(
        "Dernier passage : 6 janvier.\n\nTenu par le run.\n\n## 2026-01-05")
    _op(op="patch", slug="journal", region="preamble", mode="delete")
    assert base.corps("user", "u1", "journal").startswith("## 2026-01-05")


@pytest.mark.parametrize("args, code", [
    (dict(region="preamble", mode="delete"), "empty_preamble"),
    (dict(region="preamble", body_md="## Un titre"), "heading_in_preamble"),
])
def test_les_refus_du_preambule_sont_ceux_des_pages(base, args, code):
    base.poser("user", "u1", "sans-bandeau", "## Seule section\n\nDu texte.")
    assert _refus(op="patch", slug="sans-bandeau", **args).code == code
    assert base.corps("user", "u1", "sans-bandeau") == "## Seule section\n\nDu texte."


def test_un_guide_sans_titre_ne_se_vide_pas_par_son_preambule(base):
    base.poser("user", "u1", "plat", "Trois lignes,\nsans aucun titre.")
    assert _refus(op="patch", slug="plat", region="preamble",
                  mode="delete").code == "preamble_is_whole_page"


# ── 3. Une cible qui ne résout pas est refusée, et rien n'est écrit ────────

def test_une_section_introuvable_nomme_celles_qui_existent(base):
    refus = _refus(op="patch", slug="journal", section="2026-02-01", mode="append",
                   body_md="- x")
    assert (refus.status, refus.code) == (404, "unknown_section")
    # C'est la PHRASE qu'on éprouve ici : sans la liste, l'agent ne peut que deviner —
    # et sans la poignée du préambule, il tente `section="preamble"`.
    for titre in ("2026-01-05", "2026-01-06", "Règles", "Format"):
        assert titre in str(refus)
    assert 'region="preamble"' in str(refus)
    assert base.corps("user", "u1", "journal") == _JOURNAL


def test_deux_titres_homonymes_ne_se_departagent_pas_au_hasard(base):
    double = "## Notes\n\nA\n\n## Autre\n\n### notes\n\nB"
    base.poser("user", "u1", "double", double)
    refus = _refus(op="patch", slug="double", section="Notes", body_md="écrasé")
    assert (refus.status, refus.code) == (409, "ambiguous_section")
    assert base.corps("user", "u1", "double") == double


@pytest.mark.parametrize("args, code", [
    (dict(section="Règles", region="preamble", body_md="x"), "ambiguous_target"),
    (dict(body_md="x"), "missing_target"),
    (dict(section="   ", body_md="x"), "missing_target"),
    (dict(section="Règles", mode="delete", body_md="x"), "unexpected_body"),
    (dict(section="Règles", mode="append"), "missing_body"),
    # Le patch n'écrit que le corps : un titre passé ici serait avalé.
    (dict(section="Règles", mode="append", body_md="x", title="Nouveau"),
     "unsupported_on_patch"),
])
def test_une_forme_bancale_est_refusee_avant_la_base(base, args, code):
    refus = _refus(op="patch", slug="journal", **args)
    assert (refus.status, refus.code) == (400, code)
    assert base.appels == 0


# ── 4. Un patch ne crée rien ───────────────────────────────────────────────

def test_patcher_un_guide_absent_est_refuse_et_ne_le_cree_pas(base):
    refus = _refus(op="patch", slug="inconnu", section="A", mode="append", body_md="x")
    assert (refus.status, refus.code) == (404, "not_found")
    assert ("user", "u1", "inconnu") not in base.guides


def test_le_readme_injecte_ne_se_patche_pas(base):
    """Ni par `delivery='init'`, ni en le visant comme un guide à charger : dans les
    deux cas le refus est nommé, et il renvoie à l'écriture entière."""
    refus = _refus(op="patch", delivery="init", region="preamble", body_md="x")
    assert (refus.status, refus.code) == (400, "unsupported_delivery")
    assert base.appels == 0

    base.readmes.add(("user", "u1", "readme"))
    refus = _refus(op="patch", slug="readme", region="preamble", body_md="x")
    assert (refus.status, refus.code) == (409, "delivery_conflict")


# ── 5. Le droit d'écrire est celui d'`op=write`, avant toute lecture ───────

@pytest.mark.parametrize("sub, org, scope, attendu", [
    ("u-membre", 42, "org", (403, "forbidden")),          # membre, pas admin de l'org
    ("u-membre", None, "org", (400, "no_active_org")),
    ("u-membre", 42, "platform", (403, "forbidden")),
    ("u-membre", 42, "group", (403, "forbidden")),        # pas chef de l'équipe
    ("u-membre", 42, "planete", (400, "bad_scope")),
])
def test_qui_ne_peut_pas_ecrire_ne_peut_pas_patcher(base, monkeypatch, sub, org, scope,
                                                    attendu):
    import oto_mcp.access as access
    import oto_mcp.roles as roles
    monkeypatch.setattr(access, "current_group", lambda sub: 7)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: False)
    # Le guide EXISTE à la clé visée : seul le droit explique le refus.
    for owner in ("42", "platform", "7"):
        base.poser(scope, owner, "journal", _JOURNAL)

    ecrire = _refus(sub=sub, org=org, op="write", scope=scope, slug="journal", body_md="x")
    patcher = _refus(sub=sub, org=org, op="patch", scope=scope, slug="journal",
                     section="2026-02-01", mode="append", body_md="x")
    assert (patcher.status, patcher.code) == (ecrire.status, ecrire.code) == attendu
    # Le refus tombe AVANT la lecture : la section visée n'existe pas, et ce n'est pas
    # ce qu'on apprend à qui n'écrit pas ce guide.
    assert base.appels == 0


def test_le_scope_omis_vise_son_propre_guide_comme_a_l_ecriture(base):
    base.poser("org", "42", "journal", _JOURNAL)
    out = _op(sub="u-admin", org=42, op="patch", scope="org", slug="journal",
              section="2026-01-06", mode="append", body_md="- vu par l'admin")
    assert out["scope"] == "org" and "vu par l'admin" in base.corps("org", "42", "journal")
    # Scope omis = `user` : l'admin n'a pas de guide `journal` à lui, et celui de l'org
    # n'est pas pris à sa place.
    refus = _refus(sub="u-admin", org=42, op="patch", slug="journal",
                   section="2026-01-06", mode="append", body_md="- hors cible")
    assert refus.code == "not_found"
    assert "hors cible" not in base.corps("org", "42", "journal")


def test_un_scope_sans_guide_a_charger_refuse_comme_a_l_ecriture(base, monkeypatch):
    """L'étage équipe n'a que de la prose `init` : le chef passe l'autz, et c'est le
    store qui refuse — même code que `op=write`."""
    import oto_mcp.access as access
    import oto_mcp.roles as roles
    monkeypatch.setattr(access, "current_group", lambda sub: 7)
    monkeypatch.setattr(roles, "can_admin_group", lambda sub, gid: True)
    ecrire = _refus(op="write", scope="group", slug="journal", body_md="x")
    patcher = _refus(op="patch", scope="group", slug="journal", section="A", body_md="x")
    assert patcher.code == ecrire.code == "invalid_guide"


# ── 6. La réponse est un accusé, jamais le corps ───────────────────────────

def test_l_accuse_ne_rejoue_pas_le_corps(base):
    """Le cas qui a fait naître l'op : ~60 000 caractères, une ligne à ajouter."""
    entrees = "\n\n".join(f"## 2025-{m:02d}-{j:02d}\n\n- passage n°{m * 31 + j} : "
                          + "rien à signaler, file vide, aucun rejet. " * 3
                          for m in range(1, 13) for j in range(1, 29))
    assert 50_000 < len(entrees) and len(entrees.encode()) < C._MAX_BODY_BYTES
    base.poser("user", "u1", "gros-journal", entrees)

    out = _op(op="patch", slug="gros-journal", section="2025-12-28", mode="append",
              body_md="- TEMOIN-DU-PATCH")

    assert base.corps("user", "u1", "gros-journal").endswith("- TEMOIN-DU-PATCH")
    assert "body_md" not in out
    rendu = json.dumps(out, ensure_ascii=False)
    assert "TEMOIN-DU-PATCH" not in rendu and "passage n°" not in rendu
    assert len(rendu) < 400
    # De quoi enchaîner sans relire : la taille, et la rev de CE qui est stocké.
    assert out["body_md_length"] == len(base.corps("user", "u1", "gros-journal"))
    assert out["rev"] == _op(op="read", slug="gros-journal", scope="user")["rev"]
    assert (out["slug"], out["scope"], out["title"]) == ("gros-journal", "user", "Journal")


def test_le_patch_ne_touche_ni_au_titre_ni_a_la_description(base):
    _op(op="patch", slug="journal", section="Format", mode="append", body_md="Une ligne.")
    lu = _op(op="read", slug="journal", scope="user")
    assert (lu["title"], lu["description"]) == ("Journal", "Le fil.")


# ── 7. Les deux bornes de l'écriture valent pour le RÉSULTAT du patch ──────

def test_un_patch_qui_depasse_la_borne_est_refuse_avec_la_mesure(base):
    plein = "## Entrées\n\n" + "x" * (C._MAX_BODY_BYTES - 40)
    base.poser("user", "u1", "plein", plein)
    refus = _refus(op="patch", slug="plein", section="Entrées", mode="append",
                   body_md="é" * 30)
    assert (refus.status, refus.code) == (400, "body_too_large")
    # La mesure est dans la phrase, en OCTETS (un « é » en pèse deux) — sans elle
    # l'auteur raccourcit à l'aveugle.
    attendu = len(plein.encode()) + 2 + 60
    assert str(attendu) in str(refus) and str(attendu - C._MAX_BODY_BYTES) in str(refus)
    assert base.corps("user", "u1", "plein") == plein


def test_un_patch_ne_laisse_pas_un_guide_vide(base):
    base.poser("user", "u1", "mince", "## Seule section\n\nDu texte.")
    refus = _refus(op="patch", slug="mince", section="Seule section", mode="delete")
    assert (refus.status, refus.code) == (400, "empty_guide")
    assert base.corps("user", "u1", "mince") == "## Seule section\n\nDu texte."


# ── 8. `expected_rev` : le guide a-t-il changé depuis ma lecture ? ─────────

def test_expected_rev_refuse_un_guide_qui_a_change(base):
    rev = _op(op="read", slug="journal", scope="user")["rev"]
    # Quelqu'un d'autre écrit entre la lecture et le patch.
    base.guides[("user", "u1", "journal")]["body_md"] = _JOURNAL + "\n\n## 2026-01-07\n\n- a"

    refus = _refus(op="patch", slug="journal", section="Règles", body_md="écrasé",
                   expected_rev=rev)
    assert (refus.status, refus.code) == (409, "conflict")
    assert "écrasé" not in base.corps("user", "u1", "journal")

    # Avec la rev à jour, il passe — et l'accusé rend la suivante.
    frais = _op(op="read", slug="journal", scope="user")["rev"]
    out = _op(op="patch", slug="journal", section="Règles", mode="append", body_md="ok",
              expected_rev=frais)
    assert out["rev"] != frais
    assert out["rev"] == _op(op="read", slug="journal", scope="user")["rev"]


def test_la_rev_suit_le_titre_et_la_description_aussi(base):
    rev = _op(op="read", slug="journal", scope="user")["rev"]
    base.guides[("user", "u1", "journal")]["title"] = "Renommé"
    assert _op(op="read", slug="journal", scope="user")["rev"] != rev


# `expected_rev` n'est plus propre au patch depuis le signal #1203 : `op=write` et
# `op=delete` l'HONORENT (test_guide_ecriture_gardee_1203) ; il reste refusé sur les
# lectures, où il ferait croire à un contrôle qui n'a pas lieu.
@pytest.mark.parametrize("op, arg, valeur", [
    (op, arg, valeur)
    for op in ("list", "read", "write", "delete")
    for arg, valeur in (("expected_rev", "0" * 16), ("section", "Règles"),
                        ("region", "preamble"), ("mode", "append"))
    if not (arg == "expected_rev" and op in ("write", "delete"))
])
def test_un_argument_de_patch_n_est_pas_avale_par_une_autre_op(base, op, arg, valeur):
    """Un argument de patch accepté-et-ignoré par une autre op ferait croire à un
    geste qui n'a pas lieu."""
    refus = _refus(op=op, slug="journal", scope="user", body_md="x", **{arg: valeur})
    assert (refus.status, refus.code) == (400, "unsupported_patch_arg")
    assert base.corps("user", "u1", "journal") == _JOURNAL


def test_la_description_servie_annonce_le_patch():
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.guide")
    assert "op=patch" in cap.description and "expected_rev" in cap.description


# ── 9. Contre un vrai PostgreSQL, sur le CHEMIN SERVI ──────────────────────
# Le verrou de ligne, le contrôle de rev et le refus d'une couche `init` sont portés
# par les instructions SQL : aucun double ne les prouve.

@pytest.fixture(scope="module")
def monde(pg_module_dsn):
    """Une base neuve pour ce module (cf. `conftest.pg_module_dsn`), bootée par le vrai
    `init_db`, avec une org, son admin et un membre simple."""
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp import org_store
        from oto_mcp.db import init_db
        init_db()
        org = org_store.create_org("Acme", created_by="u-admin")
        org_store.add_org_member(org, "u-admin", "org_admin")
        org_store.add_org_member(org, "u-membre", "org_member")
        org_store.set_active_org("u-admin", org)
        org_store.set_active_org("u-membre", org)
        yield {"org": org}
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


def _guide(sub: str, **args):
    """UN appel d'`oto_guide` par le chemin servi : autz DÉCLARÉE puis handler."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.guide")
    inp = cap.Input(**args)
    out = cap.handler(cap.authz(RawCtx(sub=sub), inp), inp)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def test_en_base_un_patch_s_applique_et_la_rev_garde_le_conflit(monde):
    org = str(monde["org"])
    _guide("u-admin", op="write", scope="org", owner_id=org, slug="journal",
           body_md=_JOURNAL, title="Journal", description="Le fil des passages.")
    lu = _guide("u-admin", op="read", scope="org", slug="journal")
    assert lu["body_md"] == _JOURNAL and lu["rev"]

    out = _guide("u-admin", op="patch", scope="org", owner_id=org, slug="journal",
                 section="2026-01-06", mode="append", body_md="- relance envoyée",
                 expected_rev=lu["rev"])
    assert "body_md" not in out and out["body_md_length"] > len(_JOURNAL)

    relu = _guide("u-admin", op="read", scope="org", slug="journal")
    assert relu["body_md"] == _JOURNAL.replace(
        "- rien à signaler", "- rien à signaler\n\n- relance envoyée")
    assert relu["rev"] == out["rev"] != lu["rev"]
    assert (relu["title"], relu["description"]) == ("Journal", "Le fil des passages.")

    # La rev d'AVANT ne passe plus, et rien ne bouge.
    with pytest.raises(AuthzDenied) as refus:
        _guide("u-admin", op="patch", scope="org", owner_id=org, slug="journal",
               section="Règles", body_md="écrasé", expected_rev=lu["rev"])
    assert (refus.value.status, refus.value.code) == (409, "conflict")
    assert _guide("u-admin", op="read", scope="org",
                  slug="journal")["body_md"] == relu["body_md"]


def test_en_base_un_membre_simple_est_refuse_comme_a_l_ecriture(monde):
    org = str(monde["org"])
    _guide("u-admin", op="write", scope="org", owner_id=org, slug="regles",
           body_md="## Règles\n\nUne seule.", title="Règles")
    refus = {}
    for op, args in (("write", dict(body_md="pris")),
                     ("patch", dict(section="Règles", mode="append", body_md="pris"))):
        with pytest.raises(AuthzDenied) as e:
            _guide("u-membre", op=op, scope="org", owner_id=org, slug="regles", **args)
        refus[op] = (e.value.status, e.value.code)
    assert refus["patch"] == refus["write"] == (403, "forbidden")
    # Le membre LIT ce guide — et il le lit intact.
    assert _guide("u-membre", op="read", scope="org",
                  slug="regles")["body_md"] == "## Règles\n\nUne seule."


def test_en_base_un_patch_ne_cree_rien_et_ne_touche_pas_au_readme(monde):
    with pytest.raises(AuthzDenied) as absent:
        _guide("u-seul", op="patch", slug="fantome", section="A", mode="append",
               body_md="x")
    assert (absent.value.status, absent.value.code) == (404, "not_found")
    with pytest.raises(AuthzDenied) as toujours_absent:
        _guide("u-seul", op="read", scope="user", slug="fantome")
    assert toujours_absent.value.status == 404

    # La clé `readme` porte la couche INJECTÉE : la viser comme un guide à charger ne
    # la modifie pas (même clé dérivée, autre livraison).
    _guide("u-seul", op="write", scope="user", delivery="init", body_md="## Moi\n\nBref.")
    with pytest.raises(AuthzDenied) as refus:
        _guide("u-seul", op="patch", scope="user", slug="readme", section="Moi",
               mode="append", body_md="glissé")
    assert (refus.value.status, refus.value.code) == (409, "delivery_conflict")
    assert _guide("u-seul", op="read", scope="user",
                  delivery="init")["body_md"] == "## Moi\n\nBref."


def test_en_base_deux_patchs_simultanes_s_enchainent(monde):
    """Sans `expected_rev`, rien ne se perd pour autant : le second patch attend le
    verrou du premier et s'applique à ce qu'il a écrit. Le premier est tenu DANS sa
    transaction le temps de lancer le second — s'il n'y avait pas de verrou, le second
    finirait tout de suite, et sur le corps d'avant."""
    from oto_mcp import guide_store
    _guide("u-duo", op="write", scope="user", slug="journal", body_md="## Fil\n\n- départ")
    dedans, lacher, fini = threading.Event(), threading.Event(), threading.Event()

    def premier(corps: str) -> str:
        dedans.set()
        assert lacher.wait(20), "le banc n'a jamais relâché le premier patch"
        return corps + "\n- premier"

    def second():
        _guide("u-duo", op="patch", slug="journal", section="Fil", mode="append",
               body_md="- second")
        fini.set()

    t1 = threading.Thread(target=guide_store.patch_guide,
                          args=("user", "u-duo", "journal", premier),
                          kwargs={"edited_by": "u-duo"})
    t2 = threading.Thread(target=second)
    t1.start()
    try:
        assert dedans.wait(20)
        t2.start()
        a_attendu = not fini.wait(0.5)
    finally:
        lacher.set()               # jamais un premier patch laissé dans sa transaction
    t1.join(20)
    t2.join(20)
    assert a_attendu, "le second patch n'a pas attendu le verrou du premier"
    assert fini.is_set()

    corps = _guide("u-duo", op="read", scope="user", slug="journal")["body_md"]
    assert "- premier" in corps and "- second" in corps
    assert corps.index("- premier") < corps.index("- second")
