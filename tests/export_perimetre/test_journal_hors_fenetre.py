"""Le journal hors fenêtre : pousser le journal la veille, importer le reste, puis le diff (#1088).

Mesuré sur une vraie copie : le journal d'appels fait l'essentiel des lignes et du temps
d'un export de périmètre. Il voyage donc à part, par tranches de dates, dans l'ordre du
jour J :

1. **la veille** : la cible naît par le démarrage (tenant primaire, ni orgs ni comptes) ;
   on y POUSSE le journal des derniers jours, faits de run complets ;
2. **le jour J** : export principal `--sans-journal`, import principal dans CETTE base
   (vierge en orgs et comptes, mais qui porte déjà des appels) ;
3. puis le **diff** : la dernière tranche, de la borne du push (avec un chevauchement de
   sécurité) jusqu'à l'instant du gel.

Ce qui est vérifié sur la cible, par des chemins indépendants de l'outil : chaque appel
attendu du périmètre y est, une fois, et rien d'autrui ; une tranche rejouée n'insère
rien ; les anciens comptes suivent la règle (rattachés, sinon omis, comptés) ; la
séquence du journal ne recule jamais ; un tenant qui n'est pas le bon refuse.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp.export_perimetre import commande  # noqa: E402
from oto_mcp.export_perimetre.classement import (  # noqa: E402
    CLASSEMENT, EXCLUE, EXPORTEES, FAITS_DE_RUN, JOURNAL, sans_journal)
from oto_mcp.export_perimetre.decouverte import (  # noqa: E402
    Cle, JournalNonDetachable, Schema, lire_schema, verifier_classement, verifier_journal)
from oto_mcp.export_perimetre.extraction import exporter  # noqa: E402
from oto_mcp.export_perimetre.importation import (  # noqa: E402
    ImportRefuse, VerificationEchouee, importer)
from oto_mcp.export_perimetre.journal import (  # noqa: E402
    FORMAT_TRANCHE, TrancheRefusee, exporter_tranche, importer_tranche, instant)
from oto_mcp.export_perimetre.objets import StockageS3  # noqa: E402
from oto_mcp.export_perimetre.regles import Via  # noqa: E402
from perimetre_banc import (  # noqa: E402
    A, B, BASE_CIBLE, BASE_SOURCE, FauxS3, detruire, naitre, semer, slug_de, url_cible,
    url_source)

CLE_SOURCE, CLE_CIBLE = os.urandom(32), os.urandom(32)
NOM_A = f"tenant {A}"
JUMEAU = f"{A}-alice"            # ancien compte nu dont `<slug>:A7d1e-alice` est le jumeau
ORPHELIN = "ancien-sans-jumeau"
FAITS = FAITS_DE_RUN["tool_calls"][1]

MAINTENANT = datetime.now(timezone.utc).replace(microsecond=0)
GEL = MAINTENANT + timedelta(hours=1)          # l'instant du gel du jour J
PUSH = (MAINTENANT - timedelta(days=30), MAINTENANT - timedelta(days=1))
DIFF = (PUSH[1] - timedelta(hours=1), GEL)     # une heure de chevauchement de sécurité
QUAND_ANCIENS = MAINTENANT - timedelta(days=15)


def _appel(c, m: str, quand: datetime, *, sub, org_id, tool: str = "oto_doc",
           n: int = 1, extra: dict | None = None) -> None:
    c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id, args, created_at) "
              "SELECT 'oto', 'tool', %s, %s, %s, %s::jsonb || jsonb_build_object('n', g), %s "
              "FROM generate_series(1, %s) g",
              (sub, tool, org_id, json.dumps({"_m": m, **(extra or {})}), quand, n))


@pytest.fixture(scope="module")
def source(pg_dsn):
    """Une source au tenant primaire `oto` : le propriétaire A (tenant tiers) et son voisin
    B, et un journal daté — avant la fenêtre du push, dedans, dans le chevauchement, dans
    le diff, après le gel — dont des lignes de deux anciens comptes de A."""
    dsn = naitre(pg_dsn, "oto", "oto")
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
            a, b = semer(c, A, cle=CLE_SOURCE), semer(c, B, cle=CLE_SOURCE)
            o, alice, bob = a["org"], a["alice"], a["bob"]
            for ancien in (JUMEAU, ORPHELIN):
                c.execute("INSERT INTO users (sub, email) VALUES (%s, %s)",
                          (ancien, f"{ancien}@exemple.test"))
            vieux = MAINTENANT - timedelta(days=60)
            _appel(c, A, vieux, sub=alice, org_id=o, n=2)                  # hors des 30 jours
            for fait in FAITS:                                             # … sauf les faits
                _appel(c, A, vieux, sub=alice, org_id=o, tool=fait)
            _appel(c, A, MAINTENANT - timedelta(days=20), sub=alice, org_id=o, n=3)
            _appel(c, A, MAINTENANT - timedelta(days=10), sub=bob, org_id=None)
            _appel(c, A, MAINTENANT - timedelta(days=5), sub=alice, org_id=o, n=1234)
            _appel(c, A, QUAND_ANCIENS, sub=JUMEAU, org_id=o, n=2)
            _appel(c, A, QUAND_ANCIENS, sub=ORPHELIN, org_id=o)
            cle_image = next(k for k in a["objets"] if k.endswith("-page.png"))
            _appel(c, A, MAINTENANT - timedelta(days=3), sub=alice, org_id=o,
                   extra={"image": url_source(cle_image)})
            _appel(c, A, PUSH[1] - timedelta(minutes=30), sub=alice, org_id=o, n=2)  # recouvert
            _appel(c, A, MAINTENANT - timedelta(hours=2), sub=alice, org_id=o, n=2)
            _appel(c, A, GEL + timedelta(hours=1), sub=alice, org_id=o)    # après le gel
            for quand in (vieux, MAINTENANT - timedelta(days=20), MAINTENANT):
                _appel(c, B, quand, sub=b["alice"], org_id=b["org"])
            _appel(c, B, vieux, sub=b["alice"], org_id=b["org"], tool="run_start")
        yield {"dsn": dsn, A: a, B: b, "seau": FauxS3({**a["objets"], **b["objets"]})}
    finally:
        detruire(pg_dsn, dsn)


def _attendus(source) -> set[int]:
    """Les appels que la cible doit porter, lus à la source par un chemin indépendant de
    l'outil : ceux des orgs de A (ou sans org, d'un compte de A), de la fenêtre
    `[début du push, gel)` ou faits de run, hors l'ancien compte sans jumeau."""
    a = source[A]
    with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
        orgs = [r["id"] for r in c.execute(
            "SELECT id FROM orgs WHERE id = %s OR (auto_created AND id IN "
            "(SELECT org_id FROM org_members WHERE sub = %s))", (a["org"], a["alice"]))]
        return {r["id"] for r in c.execute(
            "SELECT id FROM tool_calls WHERE (org_id = ANY(%(o)s) OR (org_id IS NULL AND "
            "sub = ANY(%(s)s))) AND sub IS DISTINCT FROM %(orph)s AND created_at < %(gel)s "
            "AND (created_at >= %(debut)s OR tool = ANY(%(faits)s))",
            {"o": orgs, "s": [a["alice"], a["bob"]], "orph": ORPHELIN, "gel": GEL,
             "debut": PUSH[0], "faits": list(FAITS)})}


def _sous_nos_cles(env_cle: bytes):
    mp = pytest.MonkeyPatch()
    mp.setenv("OTO_MCP_MASTER_KEY", env_cle.hex())
    return mp


def _tranche(source, chemin, bornes, *, faits_de_run=False) -> dict:
    mp = _sous_nos_cles(CLE_SOURCE)
    try:
        with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
            return exporter_tranche(c, [source[A]["org"]], chemin, depuis=bornes[0],
                                    jusqu_a=bornes[1], base_publique=BASE_SOURCE,
                                    cle_cible=CLE_CIBLE,
                                    stockage=StockageS3(source["seau"], "source"),
                                    faits_de_run=faits_de_run)
    finally:
        mp.undo()


def _verser(dsn: str, chemin, seau: FauxS3) -> dict:
    mp = _sous_nos_cles(CLE_CIBLE)
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            return importer_tranche(c, chemin, stockage=StockageS3(seau, "cible"),
                                    base_publique=BASE_CIBLE)
    finally:
        mp.undo()


def _importer(dsn: str, chemin, seau: FauxS3) -> dict:
    mp = _sous_nos_cles(CLE_CIBLE)
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            return importer(c, chemin, stockage=StockageS3(seau, "cible"),
                            base_publique=BASE_CIBLE)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def principal(source, tmp_path_factory):
    """L'export principal SANS journal, fait le jour J."""
    chemin = tmp_path_factory.mktemp("principal") / "a.jsonl"
    mp = _sous_nos_cles(CLE_SOURCE)
    try:
        with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
            manifeste = exporter(c, [source[A]["org"]], chemin, base_publique=BASE_SOURCE,
                                 cle_cible=CLE_CIBLE,
                                 stockage=StockageS3(source["seau"], "source"),
                                 journal=False)
    finally:
        mp.undo()
    return chemin, manifeste


@pytest.fixture(scope="module")
def jour_j(source, principal, tmp_path_factory, pg_dsn):
    """L'enchaînement complet : push la veille, import principal, diff."""
    dossier = tmp_path_factory.mktemp("journal")
    dsn = naitre(pg_dsn, slug_de(A), NOM_A)
    seau = FauxS3()
    try:
        push = _tranche(source, dossier / "push.jsonl", PUSH, faits_de_run=True)
        etapes = {"push": push, "versement_push": _verser(dsn, dossier / "push.jsonl", seau)}
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            etapes["apres_push"] = {
                t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
                for t in ("orgs", "users", "tool_calls")}
        etapes["rejeu_push"] = _verser(dsn, dossier / "push.jsonl", seau)
        # La cible a déjà servi quelques appels à elle : sa séquence est loin devant.
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("SELECT setval(pg_get_serial_sequence('tool_calls', 'id'), %s)",
                      (10 ** 9,))
        etapes["import"] = _importer(dsn, principal[0], seau)
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            etapes["sequence_apres_import"] = c.execute(
                "SELECT last_value AS v FROM tool_calls_id_seq").fetchone()["v"]
        etapes["diff"] = _tranche(source, dossier / "diff.jsonl", DIFF)
        etapes["versement_diff"] = _verser(dsn, dossier / "diff.jsonl", seau)
        etapes["rejeu_diff"] = _verser(dsn, dossier / "diff.jsonl", seau)
        yield {"dsn": dsn, "seau": seau, "dossier": dossier, **etapes}
    finally:
        detruire(pg_dsn, dsn)


# --- L'export principal sans journal ------------------------------------------------


def test_l_export_sans_journal_compte_ce_qu_il_laisse(source, principal):
    chemin, manifeste = principal
    assert '"t": "tool_calls"' not in chemin.read_text(encoding="utf-8")
    assert "tool_calls" not in manifeste["ordre"]
    a = source[A]
    with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
        brut = c.execute(
            "SELECT count(*) AS n, min(created_at) AS premier, max(created_at) AS dernier, "
            "max(id) AS id FROM tool_calls WHERE org_id IN (SELECT id FROM orgs WHERE id = "
            "%(o)s OR (auto_created AND id IN (SELECT org_id FROM org_members WHERE sub = %(a)s))) "
            "OR (org_id IS NULL AND sub IN (%(a)s, %(b)s))",
            {"o": a["org"], "a": a["alice"], "b": a["bob"]}).fetchone()
    laisse = manifeste["journal"]["tables"]["tool_calls"]
    assert manifeste["journal"]["inclus"] is False
    assert laisse["lignes"] == brut["n"] and laisse["horodatage"] == "created_at"
    assert datetime.fromisoformat(laisse["premier"]) == brut["premier"]
    assert datetime.fromisoformat(laisse["dernier"]) == brut["dernier"]
    assert manifeste["tables"]["tool_calls"]["classe"] == EXCLUE
    assert manifeste["tables"]["tool_calls"]["omises"] == brut["n"]
    # La séquence du journal est posée sur la cible : ses propres appels ne prendront
    # jamais l'id d'un appel encore à verser.
    assert manifeste["sequences"]["tool_calls.id"] == brut["id"]


def test_l_import_sans_journal_donne_une_instance_complete_sans_journal(
        source, principal, pg_dsn):
    """Sur une cible sans push : tout est là, relu conforme, sauf le journal."""
    dsn = naitre(pg_dsn, slug_de(A), NOM_A)
    try:
        rapport = _importer(dsn, principal[0], FauxS3())
        attendus = {t: v["lignes"] for t, v in principal[1]["tables"].items()
                    if v.get("lignes")}
        assert {t: v["lignes"] for t, v in rapport.items()} == attendus
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM tool_calls").fetchone()["n"] == 0
            for t, e in CLASSEMENT.items():
                if e.classe in EXPORTEES and t not in JOURNAL:
                    n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text "
                                  "LIKE %s", (f"%{B}%",)).fetchone()["n"]
                    assert n == 0, t
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 2
    finally:
        detruire(pg_dsn, dsn)


def test_l_export_par_defaut_emporte_toujours_le_journal(source, tmp_path):
    mp = _sous_nos_cles(CLE_SOURCE)
    try:
        with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
            manifeste = exporter(c, [source[A]["org"]], tmp_path / "a.jsonl",
                                 base_publique=BASE_SOURCE, cle_cible=CLE_CIBLE,
                                 stockage=StockageS3(source["seau"], "source"))
    finally:
        mp.undo()
    assert manifeste["journal"] == {"inclus": True}
    assert manifeste["tables"]["tool_calls"]["lignes"] > 1234


# --- Push, import principal, diff -----------------------------------------------------


def test_le_push_se_verse_dans_une_base_sans_orgs_ni_comptes(jour_j):
    assert jour_j["apres_push"]["orgs"] == 0 and jour_j["apres_push"]["users"] == 0
    versement = jour_j["versement_push"]["tool_calls"]
    assert jour_j["apres_push"]["tool_calls"] == versement["lignes"] == versement["inserees"]
    assert jour_j["push"]["format"] == FORMAT_TRANCHE
    assert jour_j["push"]["tranche"]["faits_de_run_complets"] is True


def test_toutes_les_lignes_du_perimetre_une_fois_et_rien_d_autrui(source, jour_j):
    with psycopg.connect(jour_j["dsn"], row_factory=dict_row) as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM tool_calls")]
        assert c.execute("SELECT count(*) AS n FROM tool_calls WHERE args::text LIKE %s",
                         (f"%{B}%",)).fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM tool_calls WHERE row_to_json(tool_calls)"
                         "::text LIKE %s", (f"%{slug_de(A)}:%",)).fetchone()["n"] == 0
    assert len(ids) == len(set(ids))
    assert set(ids) == _attendus(source)


def test_rejouer_une_tranche_n_insere_rien(jour_j):
    for rejeu, versement in (("rejeu_push", "versement_push"), ("rejeu_diff", "versement_diff")):
        r, v = jour_j[rejeu]["tool_calls"], jour_j[versement]["tool_calls"]
        assert r["inserees"] == 0 and r["deja_presentes"] == r["lignes"] == v["lignes"]
        assert r["empreinte"] == v["empreinte"]


def test_le_diff_recouvre_le_push_sans_doublon(jour_j):
    """Le chevauchement de sécurité : les appels de la dernière heure du push sont dans
    le diff, déjà présents, pas réinsérés."""
    diff = jour_j["versement_diff"]["tool_calls"]
    assert diff["deja_presentes"] == 2
    assert diff["inserees"] == diff["lignes"] - 2 > 0


def test_les_faits_de_run_partent_en_entier_le_reste_sur_sa_fenetre(jour_j):
    with psycopg.connect(jour_j["dsn"], row_factory=dict_row) as c:
        avant = c.execute("SELECT tool, count(*) AS n FROM tool_calls WHERE created_at < %s "
                          "GROUP BY tool ORDER BY tool", (PUSH[0],)).fetchall()
    assert avant == [{"tool": f, "n": 1} for f in sorted(FAITS)]


def test_sans_l_option_les_faits_de_run_anciens_ne_partent_pas(source, tmp_path):
    manifeste = _tranche(source, tmp_path / "p.jsonl", PUSH)
    lignes = [json.loads(x) for x in (tmp_path / "p.jsonl").read_text().splitlines()[:-1]]
    assert all(datetime.fromisoformat(x["l"]["created_at"]) >= PUSH[0] for x in lignes)
    assert manifeste["tranche"]["faits_de_run_complets"] is False


def test_la_regle_des_anciens_comptes_s_applique_au_journal(jour_j):
    """Rattachées au jumeau, sinon omises — et comptées au manifeste de la tranche."""
    assert jour_j["push"]["comptes_hors_perimetre"] == {
        "tables": {"tool_calls": {"rattachees": 2, "omises_sans_jumeau": 1}},
        "comptes": {"rattaches": 1, "sans_jumeau": 1}}
    with psycopg.connect(jour_j["dsn"], row_factory=dict_row) as c:
        # Sur la cible, le jumeau est nu : ses lignes rattachées portent son compte.
        assert c.execute("SELECT sub, count(*) AS n FROM tool_calls WHERE created_at = %s "
                         "GROUP BY sub", (QUAND_ANCIENS,)).fetchall() == \
            [{"sub": JUMEAU, "n": 2}]
        assert c.execute("SELECT count(*) AS n FROM tool_calls WHERE sub = %s",
                         (ORPHELIN,)).fetchone()["n"] == 0


def test_le_diff_dit_ce_qui_reste_apres_le_gel(jour_j):
    assert jour_j["diff"]["tranche"]["apres"] == {"tool_calls": 1}


def test_la_sequence_du_journal_ne_recule_jamais(jour_j):
    assert jour_j["sequence_apres_import"] == 10 ** 9
    with psycopg.connect(jour_j["dsn"], row_factory=dict_row) as c:
        assert c.execute("SELECT last_value AS v FROM tool_calls_id_seq").fetchone()["v"] \
            == 10 ** 9


def test_une_tranche_poussee_seule_avance_la_sequence(source, jour_j, pg_dsn):
    dsn = naitre(pg_dsn, slug_de(A), NOM_A)
    try:
        _verser(dsn, jour_j["dossier"] / "push.jsonl", FauxS3())
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT last_value AS v FROM tool_calls_id_seq").fetchone()["v"] \
                == c.execute("SELECT max(id) AS m FROM tool_calls").fetchone()["m"]
    finally:
        detruire(pg_dsn, dsn)


def test_les_objets_cites_par_le_journal_suivent_et_leurs_url_sont_reecrites(source, jour_j):
    liste = jour_j["push"]["objets"]["liste"]
    assert len(liste) == 1
    cle = next(iter(liste))
    assert cle in jour_j["seau"].objets
    with psycopg.connect(jour_j["dsn"], row_factory=dict_row) as c:
        image = c.execute("SELECT args->>'image' AS i FROM tool_calls WHERE args ? 'image'"
                          ).fetchone()["i"]
    assert image == url_cible(cle)


# --- Les refus ------------------------------------------------------------------------


def test_une_cible_d_un_autre_tenant_ou_d_un_autre_nom_refuse(jour_j, pg_dsn):
    for slug, nom, motif in (("autre-instance", NOM_A, "tenant primaire"),
                             (slug_de(A), "autre marque", "autre marque")):
        dsn = naitre(pg_dsn, slug, nom)
        try:
            with pytest.raises(ImportRefuse, match=motif):
                _verser(dsn, jour_j["dossier"] / "push.jsonl", FauxS3())
            with psycopg.connect(dsn, row_factory=dict_row) as c:
                assert c.execute("SELECT count(*) AS n FROM tool_calls").fetchone()["n"] == 0
        finally:
            detruire(pg_dsn, dsn)


def test_une_cle_deja_prise_par_une_autre_ligne_annule_tout(source, jour_j, pg_dsn):
    """`ON CONFLICT DO NOTHING` ne doit pas taire une collision : la relecture la voit."""
    dsn = naitre(pg_dsn, slug_de(A), NOM_A)
    try:
        premier = min(_attendus(source) & {
            json.loads(x)["l"]["id"] for x in
            (jour_j["dossier"] / "push.jsonl").read_text().splitlines()[:-1]})
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("INSERT INTO tool_calls (id, tool, created_at) VALUES (%s, 'autre', %s)",
                      (premier, PUSH[0] + timedelta(days=1)))
        with pytest.raises(VerificationEchouee, match="tool_calls"):
            _verser(dsn, jour_j["dossier"] / "push.jsonl", FauxS3())
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM tool_calls").fetchone()["n"] == 1
    finally:
        detruire(pg_dsn, dsn)


def test_un_export_principal_ne_s_importe_pas_comme_une_tranche(principal, pg_dsn):
    dsn = naitre(pg_dsn, slug_de(A), NOM_A)
    try:
        with pytest.raises(ImportRefuse, match=FORMAT_TRANCHE):
            _verser(dsn, principal[0], FauxS3())
    finally:
        detruire(pg_dsn, dsn)


def test_une_borne_en_z_se_lit_en_utc():
    """La doc donne ses bornes en `…Z` ; `fromisoformat` ne le lit qu'à partir de 3.11,
    et la box tourne en 3.10. Sans fuseau, la borne reste en UTC."""
    utc = datetime(2026, 8, 31, 16, 15, 56, tzinfo=timezone.utc)
    assert instant("2026-08-31T16:15:56Z") == utc
    assert instant("2026-08-31T16:15:56") == utc
    assert instant("2026-08-31T18:15:56+02:00") == utc
    with pytest.raises(TrancheRefusee, match="pas une date ISO 8601"):
        instant("31/08/2026")


def test_une_tranche_vide_ou_renversee_refuse(source, tmp_path):
    with pytest.raises(TrancheRefusee, match="renversée"):
        _tranche(source, tmp_path / "x.jsonl", (PUSH[1], PUSH[0]))
    assert not (tmp_path / "x.jsonl").exists()


def test_le_journal_se_detache_du_schema_reel(source):
    with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
        schema = lire_schema(c)
    verifier_journal(schema, verifier_classement(schema, CLASSEMENT))


def test_ce_qui_empeche_le_journal_de_voyager_a_part_refuse_en_se_nommant(source):
    """Une table qui renvoie au journal (clé ou `Via`), un journal qui renvoie à une table
    exportée (il ne pourrait plus être poussé avant l'import principal)."""
    with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
        schema = lire_schema(c)
    cles = (*schema.cles, Cle("runs", ("appel_id",), "tool_calls", ("id",)),
            Cle("tool_calls", ("sub",), "users", ("sub",)))
    classement = {**CLASSEMENT, "run_messages": CLASSEMENT["run_messages"].__class__(
        "indirecte", Via("tool_calls", ("run_id",), ("run_id",), fk=False))}
    with pytest.raises(JournalNonDetachable) as refus:
        verifier_journal(Schema(schema.colonnes, schema.generees, schema.sequences, cles,
                                schema.primaires, schema.vues, schema.uniques),
                         verifier_classement(schema, classement))
    texte = str(refus.value)
    assert "`runs(appel_id)` → `tool_calls` : une table renvoie au journal" in texte
    assert "`tool_calls(sub)` → `users`" in texte and "poussé avant" in texte
    assert "`run_messages` hérite du journal `tool_calls`" in texte


def test_sans_journal_une_reference_au_journal_refuse_a_l_export(source, tmp_path):
    """Un enfant du journal : l'export sans journal refuse avant la première ligne."""
    classement = {**CLASSEMENT, "run_messages": CLASSEMENT["run_messages"].__class__(
        "indirecte", Via("tool_calls", ("run_id",), ("run_id",), fk=False))}
    assert sans_journal(classement)["tool_calls"].classe == EXCLUE
    mp = _sous_nos_cles(CLE_SOURCE)
    try:
        with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
            with pytest.raises(JournalNonDetachable, match="hérite du journal"):
                exporter(c, [source[A]["org"]], tmp_path / "a.jsonl",
                         base_publique=BASE_SOURCE, cle_cible=CLE_CIBLE,
                         classement=classement, journal=False)
    finally:
        mp.undo()
    assert not (tmp_path / "a.jsonl").exists()


def test_les_faits_de_run_sont_ceux_que_l_archive_ne_supprime_jamais():
    chemin = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "archive_tool_calls.py"
    spec = importlib.util.spec_from_file_location("archive_tool_calls_journal", chemin)
    archive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive)
    assert tuple(FAITS) == archive.RUN_FACTS
    assert set(JOURNAL) == set(FAITS_DE_RUN)


# --- La commande ----------------------------------------------------------------------


def test_la_commande_journal_exporte_puis_verse(source, jour_j, tmp_path, monkeypatch,
                                               capsys):
    chemin = tmp_path / "cli.jsonl"
    monkeypatch.setenv("DATABASE_URL", source["dsn"])
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_SOURCE.hex())
    monkeypatch.setenv("OTO_EXPORT_CLE_CIBLE", CLE_CIBLE.hex())
    monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_SOURCE)
    monkeypatch.setattr(commande, "_stockage", lambda: StockageS3(source["seau"], "source"))
    org = str(source[A]["org"])
    assert commande.main(["journal", "export", "--org", org, "--depuis", "pas-une-date",
                          "--jusqu-a", GEL.isoformat(), "--sortie", str(chemin)]) == 2
    assert "TrancheRefusee" in capsys.readouterr().err
    assert commande.main(["journal", "export", "--org", org, "--depuis",
                          DIFF[0].isoformat(), "--jusqu-a", DIFF[1].isoformat(),
                          "--sortie", str(chemin)]) == 0
    resume = json.loads(capsys.readouterr().out)
    assert resume["tranche"]["apres"] == {"tool_calls": 1}
    assert CLE_CIBLE.hex() not in json.dumps(resume)
    monkeypatch.delenv("OTO_EXPORT_CLE_CIBLE")
    monkeypatch.setenv("DATABASE_URL", jour_j["dsn"])
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_CIBLE.hex())
    monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_CIBLE)
    monkeypatch.setattr(commande, "_stockage", lambda: StockageS3(jour_j["seau"], "cible"))
    assert commande.main(["journal", "import", str(chemin)]) == 0
    verse = json.loads(capsys.readouterr().out)["tool_calls"]
    assert verse["inserees"] == 0 and verse["deja_presentes"] == verse["lignes"] == \
        resume["lignes"]["tool_calls"]


def test_la_commande_exporte_sans_journal(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", source["dsn"])
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_SOURCE.hex())
    monkeypatch.setenv("OTO_EXPORT_CLE_CIBLE", CLE_CIBLE.hex())
    monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_SOURCE)
    monkeypatch.setattr(commande, "_stockage", lambda: StockageS3(source["seau"], "source"))
    assert commande.main(["export", "--org", str(source[A]["org"]), "--sans-journal",
                          "--sortie", str(tmp_path / "a.jsonl")]) == 0
    resume = json.loads(capsys.readouterr().out)
    assert resume["journal"]["inclus"] is False
    assert "tool_calls" not in resume["lignes"]
    assert resume["journal"]["tables"]["tool_calls"]["lignes"] > 1234
