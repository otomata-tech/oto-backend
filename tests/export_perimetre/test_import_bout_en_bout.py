"""Bout en bout : exporter un propriétaire, faire naître sa base, l'y importer (#1088).

Deux vraies bases de test locales : une SOURCE à plusieurs tenants et orgs (`live`,
tenant primaire `oto`), et une CIBLE née par le démarrage normal (`init_db`) pour
une instance qui déclare le tenant du propriétaire comme le sien
(`OTO_TENANT_PRIMAIRE_SLUG`, `OTO_BRAND_NAME`). Chaque instance a SA clé maîtresse :
l'export tourne sous la clé source et reçoit la clé cible, l'import ne connaît que la
clé cible (décision du 28/09/2026).

Ce qui est vérifié sur la cible, par des chemins indépendants de l'outil :
- **rien d'autrui** : aucune ligne ne porte le marqueur du voisin ;
- **rien d'oublié** : par table, autant de lignes marquées que dans la source ;
- **le tenant est la ligne 1**, toutes les orgs y sont rattachées, et les comptes y
  sont NUS (le préfixe du tenant tiers est tombé, jusque dans les JSON) ;
- **les secrets se lisent avec la clé CIBLE**, sous l'AAD de leur ligne cible, et plus
  avec la clé source — qui n'a jamais quitté l'export ;
- **les lignes d'anciens comptes** sont rattachées au jumeau ou omises, comptées au
  manifeste, et l'import passe (dernière section).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp import credentials_store, runner_hook, transcription_worker  # noqa: E402
from oto_mcp.crypto import decrypt_with_key, encrypt_with_key  # noqa: E402
from oto_mcp.export_perimetre import commande  # noqa: E402
from oto_mcp.export_perimetre.classement import CLASSEMENT, EXPORTEES  # noqa: E402
from oto_mcp.export_perimetre.comptes import ComptesHorsRegle  # noqa: E402
from oto_mcp.export_perimetre.decouverte import (  # noqa: E402
    Cle, Schema, lire_schema, verifier_classement)
from oto_mcp.export_perimetre.extraction import exporter  # noqa: E402
from oto_mcp.export_perimetre.importation import (  # noqa: E402
    TAILLE_LOT, ImportRefuse, importer)
from oto_mcp.export_perimetre.rechiffrement import AAD, empreinte_cle  # noqa: E402
from oto_mcp.export_perimetre.transformation import Transformation  # noqa: E402
from oto_mcp.db.search import RANKED_SOURCES, _vec, rank_backfill_sql  # noqa: E402
from oto_mcp.datastore.recherche import recherche  # noqa: E402
from oto_mcp.db.search import lignes_trouvees_sql  # noqa: E402
from oto_mcp.export_perimetre.objets import StockageS3  # noqa: E402
from perimetre_banc import (  # noqa: E402
    A, B, BASE_CIBLE, BASE_SOURCE, SECRET, FauxS3, _credential, _sur, base_vide, demarrer,
    membre, naitre, org, semer, slug_de, tenant, url_cible)
from perimetre_banc import detruire as _detruire  # noqa: E402

CLE_SOURCE, CLE_CIBLE = os.urandom(32), os.urandom(32)
NOM_A = f"tenant {A}"


def _naitre(pg_dsn: str, slug: str, nom: str = NOM_A) -> str:
    return naitre(pg_dsn, slug, nom)


def _importer(dsn: str, chemin, cle: bytes | None = CLE_CIBLE,
              seau: FauxS3 | None = None) -> dict:
    """L'import, tel que l'instance CIBLE le joue : sous SA clé, et seulement la sienne,
    dans SON stockage, vers SA base publique."""
    with pytest.MonkeyPatch.context() as mp:
        if cle is None:
            mp.delenv("OTO_MCP_MASTER_KEY", raising=False)
        else:
            mp.setenv("OTO_MCP_MASTER_KEY", cle.hex())
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            return importer(c, chemin, stockage=StockageS3(seau or FauxS3(), "cible"),
                            base_publique=BASE_CIBLE)


def _marquees(dsn: str, marqueur: str) -> dict[str, int]:
    out = {}
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        for t, e in CLASSEMENT.items():
            if e.classe not in EXPORTEES:
                continue
            n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text "
                          "LIKE %s", (f"%{marqueur}%",)).fetchone()["n"]
            if n:
                out[t] = n
    return out


@pytest.fixture(scope="module")
def source(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        a, b = semer(c, A, cle=CLE_SOURCE), semer(c, B, cle=CLE_SOURCE)
        # Les vecteurs de classement remplis, comme la boucle de fond le fait sur toute
        # instance (`rank_backfill`, #318) : sans eux, l'export n'en transporterait aucun.
        for t in RANKED_SOURCES:
            while c.execute(rank_backfill_sql(t, 1000)).rowcount:
                pass
        # Un journal qui dépasse plusieurs lots d'écriture (`importation.TAILLE_LOT`).
        c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id, args) "
                  "SELECT 'oto', 'tool', %s, 'oto_doc', %s, "
                  "jsonb_build_object('_m', %s::text, 'n', g) FROM generate_series(1, 1234) g",
                  (a["alice"], a["org"], A))
        yield {"dsn": pg_module_dsn, A: a, B: b,
               "seau": FauxS3({**a["objets"], **b["objets"]})}


def _exporter(dsn: str, org: int, seau: FauxS3, chemin) -> dict:
    """L'export, tel que NOTRE instance le joue : sous notre clé, pour la clé cible."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OTO_MCP_MASTER_KEY", CLE_SOURCE.hex())
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            return exporter(c, [org], chemin, cle_cible=CLE_CIBLE,
                            stockage=StockageS3(seau, "source"), base_publique=BASE_SOURCE)


@pytest.fixture(scope="module")
def export_a(source, tmp_path_factory):
    chemin = tmp_path_factory.mktemp("bout") / "a.jsonl"
    return chemin, _exporter(source["dsn"], source[A]["org"], source["seau"], chemin)


@pytest.fixture(scope="module")
def cible(source, export_a, pg_dsn):
    dsn = _naitre(pg_dsn, slug_de(A))
    seau = FauxS3()
    try:
        yield {"dsn": dsn, "rapport": _importer(dsn, export_a[0], seau=seau), "seau": seau}
    finally:
        _detruire(pg_dsn, dsn)


def test_le_fichier_ne_porte_que_des_secrets_pour_la_cible(export_a):
    chemin, manifeste = export_a
    assert manifeste["secrets"] == {"connector_credentials": 4, "runner_triggers": 1,
                                    "transcription_jobs": 1}
    assert manifeste["cle_cible"] == empreinte_cle(CLE_CIBLE)
    texte = chemin.read_text(encoding="utf-8")
    assert "clair-" not in texte
    assert CLE_CIBLE.hex() not in texte and CLE_SOURCE.hex() not in texte
    transformation = Transformation.depuis(_schema_vide_de_tenant(), manifeste["comptes"])
    for x in map(json.loads, texte.splitlines()[:-1]):
        if x["t"] in AAD:
            colonne, aad = AAD[x["t"]]
            ligne = transformation.appliquer(x["t"], x["l"])
            decrypt_with_key(CLE_CIBLE, ligne[colonne], aad(ligne))
            with pytest.raises(RuntimeError):
                decrypt_with_key(CLE_SOURCE, ligne[colonne], aad(x["l"]))


def _schema_vide_de_tenant() -> Schema:
    return Schema({}, {}, {}, (), {}, {})


def test_le_manifeste_inscrit_l_archive_des_objets_du_perimetre(source, export_a):
    chemin, manifeste = export_a
    objets = manifeste["objets"]
    assert set(objets["liste"]) == set(source[A]["objets"])
    assert (chemin.parent / objets["archive"]).is_file()
    assert objets["base_publique"] == BASE_SOURCE


def test_les_objets_du_perimetre_sont_dans_le_stockage_cible_et_eux_seuls(source, cible):
    attendus = source[A]["objets"]
    assert {c: v[0] for c, v in cible["seau"].objets.items()} == attendus
    assert not set(cible["seau"].objets) & set(source[B]["objets"])


def test_aucune_url_de_notre_stockage_ne_subsiste_sur_la_cible(source, cible):
    """Décision du 28/09/2026 : tout est réécrit, colonnes comme contenus."""
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        for t in CLASSEMENT:
            n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text "
                          "LIKE %s", (f"%{BASE_SOURCE}%",)).fetchone()["n"]
            assert n == 0, f"{t} porte encore une URL de notre stockage"
        avatar = c.execute("SELECT avatar_url FROM users WHERE avatar_url IS NOT NULL"
                           ).fetchone()["avatar_url"]
        corps = c.execute("SELECT body_md FROM docs WHERE body_md LIKE '%%![%%'"
                          ).fetchone()["body_md"]
        ligne = c.execute("SELECT data FROM datastore_rows LIMIT 1").fetchone()["data"]
    cles = list(source[A]["objets"])      # avatar, logo, fichier, image de page, de ligne
    assert avatar == url_cible(cles[0])
    assert url_cible(cles[3]) in corps
    assert ligne["image"] == url_cible(cles[4])


def test_une_cle_qui_porte_un_caractere_encode_voyage_sous_sa_cle(source, export_a, cible):
    """Constaté sur une vraie copie : la clé `images/<slug>%3A<id>/…` (le sub
    `<slug>:<id>` encodé à l'écriture) est citée par `…/images/<slug>%253A<id>/…`. Le
    chemin de l'URL pris tel quel désignait un objet absent et l'export refusait. La clé
    se tire du chemin par l'inverse exact de `public_url`."""
    cle = next(k for k in source[A]["objets"] if k.endswith("-page.png"))
    assert "%3A" in cle
    assert cle in export_a[1]["objets"]["liste"]
    assert cible["seau"].objets[cle][0] == source[A]["objets"][cle]
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        corps = c.execute("SELECT body_md FROM docs WHERE body_md LIKE '%%![%%'"
                          ).fetchone()["body_md"]
    assert f"{BASE_CIBLE}/{cle.replace('%', '%25')}" in corps


def test_un_import_interrompu_reprend_ses_objets_sans_les_recopier(source, export_a, pg_dsn):
    seau = FauxS3()
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        from oto_mcp.export_perimetre import importation
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(importation, "recaler_sequences",
                       lambda conn, m: (_ for _ in ()).throw(RuntimeError("coupure")))
            with pytest.raises(RuntimeError, match="coupure"):
                _importer(dsn, export_a[0], seau=seau)
        assert seau.objets == {}          # rien n'est versé avant la relecture
        # Un objet déjà là (versé par un essai précédent, avec les en-têtes de l'écriture
        # native) : il est sauté, pas recopié.
        from oto_mcp import media_store
        liste = export_a[1]["objets"]["liste"]
        deja = sorted(liste)[0]
        donnees = source[A]["objets"][deja]
        StockageS3(seau, "cible").ecrire(deja, donnees, liste[deja]["sha256"],
                                         media_store.entetes_d_objet(
                                             deja, donnees, liste[deja]["content_type"],
                                             liste[deja]["public"]))
        _importer(dsn, export_a[0], seau=seau)
        assert seau.ecritures == len(liste)          # 1 (déjà là) + les autres, une fois
        assert {c: v[0] for c, v in seau.objets.items()} == source[A]["objets"]
    finally:
        _detruire(pg_dsn, dsn)


def test_une_archive_modifiee_ou_un_import_sans_stockage_refuse(export_a, tmp_path, pg_dsn):
    chemin, manifeste = export_a
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("OTO_MCP_MASTER_KEY", CLE_CIBLE.hex())
            with psycopg.connect(dsn, row_factory=dict_row) as c:
                with pytest.raises(ImportRefuse, match="stockage objet"):
                    importer(c, chemin)
        archive = chemin.parent / manifeste["objets"]["archive"]
        copie = tmp_path / chemin.name
        copie.write_bytes(chemin.read_bytes())
        brut = bytearray(archive.read_bytes())
        brut[-600] ^= 1
        (tmp_path / archive.name).write_bytes(bytes(brut))
        with pytest.raises(ImportRefuse, match="tronquée ou modifiée"):
            _importer(dsn, copie)
    finally:
        _detruire(pg_dsn, dsn)


def test_rien_d_autrui_et_rien_d_oublie_sur_la_cible(source, cible):
    assert _marquees(cible["dsn"], B) == {}
    assert _marquees(cible["dsn"], A) == _marquees(source["dsn"], A)


def test_le_rapport_d_import_suit_le_manifeste(cible, export_a):
    _, manifeste = export_a
    attendus = {t: v["lignes"] for t, v in manifeste["tables"].items() if v.get("lignes")}
    assert {t: v["lignes"] for t, v in cible["rapport"].items()} == attendus
    assert attendus["tool_calls"] > 2 * TAILLE_LOT   # plusieurs lots d'écriture


def test_le_tenant_du_proprietaire_est_la_ligne_1(cible):
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        tenants = c.execute("SELECT id, slug, name FROM tenants").fetchall()
        assert tenants == [{"id": 1, "slug": slug_de(A), "name": NOM_A}]
        assert {r["tenant_id"] for r in c.execute("SELECT tenant_id FROM orgs")} == {1}
        assert c.execute("SELECT slug FROM tenant_admins").fetchall() == [{"slug": slug_de(A)}]


def test_les_comptes_sont_nus_sur_la_cible(cible):
    prefixe = f"{slug_de(A)}:"
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        subs = sorted(r["sub"] for r in c.execute("SELECT sub FROM users"))
        assert subs == [f"{A}-alice", f"{A}-bob"]
        for t in CLASSEMENT:
            n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text "
                          "LIKE %s", (f"%{prefixe}%",)).fetchone()["n"]
            assert n == 0, f"{t} porte encore un sub qualifié"
        args = c.execute("SELECT args FROM tool_calls WHERE org_id IS NOT NULL").fetchone()
        assert args["args"]["pour"] == f"{A}-alice"


def test_les_secrets_se_lisent_avec_la_cle_cible(cible):
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        lus = {}
        for t, (colonne, aad) in AAD.items():
            for ligne in c.execute(f"SELECT * FROM {t} WHERE {colonne} IS NOT NULL"):
                lus[(t, ligne.get("connector"))] = decrypt_with_key(
                    CLE_CIBLE, ligne[colonne], aad(ligne))
                with pytest.raises(RuntimeError, match="indéchiffrable"):
                    decrypt_with_key(CLE_SOURCE, ligne[colonne], aad(ligne))
        membre = c.execute("SELECT entity_id FROM connector_credentials "
                           "WHERE entity_type = 'member'").fetchone()["entity_id"]
    assert membre.endswith(f":{A}-alice")
    assert lus == {
        ("connector_credentials", "serper"): SECRET.format(A, "serper"),
        ("connector_credentials", "apollo"): SECRET.format(A, "apollo"),
        ("connector_credentials", "tavily"): SECRET.format(A, "tavily"),
        ("connector_credentials", "hunter"): SECRET.format(A, "hunter"),
        ("runner_triggers", None): SECRET.format(A, "hook"),
        ("transcription_jobs", None): SECRET.format(A, "transcription"),
    }


def test_la_cle_du_tenant_devenu_primaire_est_une_instance_plateforme_ouverte(cible):
    """Le primaire ne porte pas de clé tenant : la sienne arrive en instance plateforme,
    ouverte à tous, sous son slug (`cles_du_primaire`)."""
    slug = slug_de(A)
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        assert c.execute("SELECT count(*) AS n FROM connector_credentials "
                         "WHERE entity_type = 'tenant'").fetchone()["n"] == 0
        cle = c.execute("SELECT entity_id, share_mode, share_down "
                        "FROM connector_credentials WHERE entity_type = 'platform' "
                        "AND connector = 'hunter'").fetchall()
        assert cle == [{"entity_id": slug, "share_mode": "open", "share_down": []}]
        assert c.execute("SELECT count(*) AS n FROM grants WHERE resource_id = %s AND "
                         "grantee_kind = 'platform' AND grantee_id = 'platform' AND "
                         "revoked_at IS NULL", (f"platform:hunter:{slug}",)
                         ).fetchone()["n"] == 1


def test_les_aad_viennent_du_code_qui_ecrit_les_secrets():
    assert set(AAD) == {t for t, e in CLASSEMENT.items() if e.secrets}
    assert all(AAD[t][0] in CLASSEMENT[t].secrets for t in AAD)
    assert AAD["connector_credentials"][1]({"entity_type": "user", "entity_id": "s",
                                             "connector": "c", "account": ""}) == \
        credentials_store._aad("user", "s", "c")
    assert AAD["runner_triggers"][1]({"id": 7}) == runner_hook._aad_du_secret(7)
    assert AAD["transcription_jobs"][1]({"audio_key": "k"}) == transcription_worker._aad("k")


def test_la_transformation_denude_et_rattache_au_tenant_primaire():
    schema = Schema({}, {}, {}, (Cle("orgs", ("tenant_id",), "tenants", ("id",)),), {}, {})
    t = Transformation.depuis(schema, {"t1:abc": "abc", "nu": "nu"})
    assert t.comptes == {"t1:abc": "abc"}
    assert t.appliquer("orgs", {"tenant_id": 7, "created_by": "t1:abc"}) == \
        {"tenant_id": 1, "created_by": "abc"}
    assert t.appliquer("x", {"e": "12:t1:abc", "j": {"l": ["t1:abc", "t1:abcd"]}}) == \
        {"e": "12:abc", "j": {"l": ["abc", "t1:abcd"]}}
    assert t.appliquer("tenants", {"id": 9})["id"] == 1


def test_les_suspensions_posees_par_le_tenant_ne_le_suivent_pas():
    """Comme sa désactivation (#1165) : une suspension que la désactivation du tenant a
    posée sur une org n'a plus de tenant à réactiver dans la cible. Celle posée sur
    l'org elle-même (un essai fini) la suit."""
    t = Transformation.depuis(Schema({}, {}, {}, (), {}, {}), {})
    par_tenant = {"id": 4, "suspended_at": "t", "suspended_by": "op",
                  "suspended_reason": "tenant x désactivé : fin", "suspended_tenant_id": 7}
    assert t.appliquer("orgs", par_tenant) == {
        "id": 4, "suspended_at": None, "suspended_by": None, "suspended_reason": None,
        "suspended_tenant_id": None}
    propre = {"id": 5, "suspended_at": "t", "suspended_by": "svc",
              "suspended_reason": "trial_ended", "suspended_tenant_id": None}
    assert t.appliquer("orgs", propre) == propre


def test_les_sequences_sont_recalees(cible, export_a):
    _, manifeste = export_a
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        for cle, maximum in manifeste["sequences"].items():
            sequence = c.execute("SELECT pg_get_serial_sequence(%s, %s) AS s",
                                 tuple(cle.split("."))).fetchone()["s"]
            dernier = c.execute(f"SELECT last_value FROM {sequence}").fetchone()["last_value"]
            assert dernier >= maximum, cle


def test_une_cible_deja_peuplee_refuse(cible, export_a):
    with pytest.raises(ImportRefuse, match="pas vierge"):
        _importer(cible["dsn"], export_a[0])


def _refus_avant_ecriture(dsn: str, chemin, monkeypatch) -> str:
    """L'import refusé par son contrôle PRÉALABLE : s'il atteignait l'écriture, le banc
    tomberait sur autre chose qu'un `ImportRefuse`."""
    from oto_mcp.export_perimetre import importation

    def _ecriture(*_):
        raise AssertionError("l'import a atteint l'écriture")
    monkeypatch.setattr(importation, "_verser", _ecriture)
    with pytest.raises(ImportRefuse) as refus:
        _importer(dsn, chemin)
    assert type(refus.value) is ImportRefuse
    return str(refus.value)


def _lignes(dsn: str, *tables: str) -> dict[str, int]:
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return {t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in tables}


def test_une_cible_ou_l_app_a_demarre_refuse_avant_d_ecrire_en_nommant_ses_tables(
        export_a, pg_dsn, monkeypatch):
    """#1161 : le premier démarrage de l'app sème ses guides plateforme (`nodes`,
    `blocks`), dont les identifiants heurtaient, tard et en `nodes_pkey`, ceux que
    l'import préserve. Le contrôle préalable refuse la cible, nomme chaque table semée et
    son nombre de lignes, et dit le geste ; les lignes de la naissance n'y sont pas."""
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        demarrer(dsn, slug_de(A), NOM_A)
        semees = _lignes(dsn, "nodes", "blocks", "orgs")
        assert semees["nodes"] and semees["blocks"], "rien de semé : le banc ne prouve rien"
        message = _refus_avant_ecriture(dsn, export_a[0], monkeypatch)
        assert "pas vierge" in message
        assert f"nodes ({semees['nodes']})" in message
        assert f"blocks ({semees['blocks']})" in message
        assert "connector_availability" not in message
        assert "connector_selection_seeded" not in message
        assert "tenants" not in message
        assert "base neuve" in message and "AVANT le premier démarrage" in message
        assert _lignes(dsn, "nodes", "blocks", "orgs") == semees
    finally:
        _detruire(pg_dsn, dsn)


def test_une_ligne_hors_naissance_d_une_table_semee_refuse(export_a, pg_dsn, monkeypatch):
    """Une table que la naissance sème n'est tolérée que pour CES lignes : une ligne
    d'org dans `connector_availability` se compte et se nomme."""
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with psycopg.connect(dsn) as c:
            c.execute("INSERT INTO connector_availability (scope_type, scope_id, connector, "
                      "enabled) VALUES ('org', '99', 'serper', false)")
        message = _refus_avant_ecriture(dsn, export_a[0], monkeypatch)
        assert "connector_availability (1)." in message
        assert "nodes" not in message
    finally:
        _detruire(pg_dsn, dsn)


def _naitre_par_la_commande(dsn: str, capsys) -> tuple[int, str, str]:
    """`oto-mcp perimetre naitre`, tel que l'opérateur le joue sur la base `dsn`."""
    with _sur(dsn, slug_de(A), NOM_A):
        code = commande.main(["naitre"])
    sortie = capsys.readouterr()
    return code, sortie.out, sortie.err


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return {r["t"] for r in c.execute(
            "SELECT relname AS t FROM pg_class WHERE relnamespace = "
            "current_schema()::regnamespace AND relkind IN ('r', 'p')")}


def _cles_exportees(dsn: str) -> dict[str, set[str]]:
    """Par table exportée à clé primaire, l'ensemble des clés de ses lignes."""
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        schema = lire_schema(c)
        cles = {}
        for t, e in verifier_classement(schema, CLASSEMENT).items():
            if e.classe in EXPORTEES and schema.primaires.get(t):
                colonnes = ", ".join(schema.primaires[t])
                cles[t] = {r["k"] for r in c.execute(
                    f"SELECT row_to_json(x)::text AS k FROM (SELECT {colonnes} FROM {t}) x")}
        return cles


def test_naitre_importer_puis_demarrer(source, export_a, pg_dsn, capsys, caplog,
                                      monkeypatch):
    """#1161, la séquence de l'opérateur : `naitre` fait naître le schéma d'une base vide
    à la tête du registre, sans rien semer de ce que sème le démarrage ; l'import y passe
    son contrôle préalable ; le premier démarrage de l'app, ENSUITE, sous la clé de
    l'instance, complète la base — aucune étape de sa préparation n'échoue (« at boot
    failed »), les guides plateforme sont semés à côté des nœuds importés sans heurter
    leurs identifiants, et aucune ligne importée ne disparaît. (Le démarrage peut en
    ajouter ou en déplacer — l'espace perso d'un compte qui n'en a pas, un credential
    passé du scope `user` au scope membre : ses backfills font sur la cible ce qu'ils
    font partout.)"""
    from oto_mcp.db._version_alembic import tete_du_registre
    dsn = base_vide(pg_dsn)
    try:
        code, sortie, erreur = _naitre_par_la_commande(dsn, capsys)
        assert code == 0, erreur
        assert json.loads(sortie)["version_schema"] == [tete_du_registre()]
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM nodes").fetchone()["n"] == 0
        _importer(dsn, export_a[0])
        assert _marquees(dsn, A) == _marquees(source["dsn"], A)
        importees = _cles_exportees(dsn)
        noeuds = _lignes(dsn, "nodes")["nodes"]
        monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_CIBLE.hex())
        with caplog.at_level(logging.WARNING):
            demarrer(dsn, slug_de(A), NOM_A)
        assert not [r.getMessage() for r in caplog.records
                    if "at boot failed" in r.getMessage()]
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            guides = c.execute("SELECT count(*) AS n FROM nodes WHERE owner_type = "
                               "'platform'").fetchone()["n"]
        assert guides, "le démarrage n'a semé aucun guide plateforme"
        assert _lignes(dsn, "nodes")["nodes"] == noeuds + guides
        apres = _cles_exportees(dsn)
        disparues = {t: [json.loads(x) for x in k - apres[t]]
                     for t, k in importees.items() if k - apres[t]}
        # La seule ligne qui change de clé : le credential de compte hors oauth que le
        # banc sème au scope `user`, passé au scope membre par `backfill_member_scope`.
        passees = disparues.pop("connector_credentials", [])
        assert disparues == {}
        assert all(x["entity_type"] == "user" for x in passees)
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            for x in passees:
                assert c.execute(
                    "SELECT count(*) AS n FROM connector_credentials WHERE entity_type = "
                    "'member' AND entity_id LIKE %s AND connector = %s",
                    (f"%:{x['entity_id']}", x["connector"])).fetchone()["n"] == 1, x
    finally:
        _detruire(pg_dsn, dsn)


def test_naitre_refuse_une_base_qui_n_est_pas_neuve(pg_dsn, capsys):
    """Une base née (elle a déjà `alembic_version`) ou qui porte une table sans version :
    `naitre` refuse en code 2, nommément, et n'y crée rien."""
    nee = naitre(pg_dsn, slug_de(A), NOM_A)
    autre = base_vide(pg_dsn)
    try:
        code, _, erreur = _naitre_par_la_commande(nee, capsys)
        assert code == 2 and "NaissanceRefusee" in erreur and "versionnée" in erreur
        with psycopg.connect(autre) as c:
            c.execute("CREATE TABLE deja_la (id INT)")
        code, _, erreur = _naitre_par_la_commande(autre, capsys)
        assert code == 2 and "sans version : 1 table(s)" in erreur
        assert _tables(autre) == {"deja_la"}
    finally:
        _detruire(pg_dsn, nee)
        _detruire(pg_dsn, autre)


def test_une_cible_d_un_autre_tenant_ou_d_un_autre_nom_refuse(export_a, pg_dsn):
    for slug, nom, motif in (("autre-instance", NOM_A, "tenant primaire"),
                             (slug_de(A), "autre marque", "'autre marque'.*'tenant A7d1e'")):
        dsn = _naitre(pg_dsn, slug, nom)
        try:
            with pytest.raises(ImportRefuse, match=motif):
                _importer(dsn, export_a[0])
            with psycopg.connect(dsn, row_factory=dict_row) as c:
                assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
        finally:
            _detruire(pg_dsn, dsn)


def test_une_instance_sans_la_cle_ou_avec_une_autre_refuse(export_a, pg_dsn):
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="pas de clé maîtresse"):
            _importer(dsn, export_a[0], cle=None)
        with pytest.raises(ImportRefuse, match="autre clé"):
            _importer(dsn, export_a[0], cle=os.urandom(32))
    finally:
        _detruire(pg_dsn, dsn)


def test_un_fichier_modifie_refuse(export_a, tmp_path, pg_dsn):
    chemin, _ = export_a
    lignes = chemin.read_text(encoding="utf-8").splitlines(keepends=True)
    i = next(i for i, x in enumerate(lignes) if A in x)
    lignes[i] = lignes[i].replace(A, "Z0000")
    abime = tmp_path / "abime.jsonl"
    abime.write_text("".join(lignes), encoding="utf-8")
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="empreinte"):
            _importer(dsn, abime)
    finally:
        _detruire(pg_dsn, dsn)


def test_une_cible_qui_ne_relit_pas_ce_qui_a_ete_ecrit_annule_tout(export_a, pg_dsn,
                                                                    monkeypatch):
    """La vérification finale mord : un tenant primaire mal repris fait tout annuler."""
    from oto_mcp.export_perimetre import importation
    monkeypatch.setattr(importation, "_ecrire_tenant_primaire", lambda conn, ligne: None)
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(importation.VerificationEchouee, match="tenants"):
            _importer(dsn, export_a[0])
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
    finally:
        _detruire(pg_dsn, dsn)


def test_la_commande_exporte_puis_importe(source, pg_dsn, tmp_path, monkeypatch, capsys):
    """`oto-mcp perimetre export|import` : la clé cible par l'environnement, pour cette
    seule exécution ; un refus sort en code 2, nommé, sans rien écrire."""
    chemin = tmp_path / "cli.jsonl"
    seau_cible = FauxS3()
    monkeypatch.setenv("DATABASE_URL", source["dsn"])
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_SOURCE.hex())
    monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_SOURCE)
    monkeypatch.setattr(commande, "_stockage", lambda: StockageS3(source["seau"], "source"))
    assert commande.main(["export", "--org", str(source[A]["org"]),
                          "--sortie", str(chemin)]) == 2
    assert "SecretsChiffres" in capsys.readouterr().err and not chemin.exists()
    monkeypatch.setenv("OTO_EXPORT_CLE_CIBLE", CLE_CIBLE.hex())
    assert commande.main(["export", "--org", str(source[A]["org"]),
                          "--sortie", str(chemin)]) == 0
    resume = json.loads(capsys.readouterr().out)
    assert resume["cle_cible"] == empreinte_cle(CLE_CIBLE)
    assert CLE_CIBLE.hex() not in json.dumps(resume)
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        monkeypatch.delenv("OTO_EXPORT_CLE_CIBLE")
        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_CIBLE.hex())
        monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_CIBLE)
        monkeypatch.setattr(commande, "_stockage", lambda: StockageS3(seau_cible, "cible"))
        assert commande.main(["import", str(chemin)]) == 0
        assert json.loads(capsys.readouterr().out) == resume["lignes"]
        assert {c: v[0] for c, v in seau_cible.objets.items()} == \
            {c: source["seau"].objets[c][0] for c in seau_cible.objets}
        assert len(seau_cible.objets) == resume["objets"]["nombre"]
    finally:
        _detruire(pg_dsn, dsn)


# --- Les colonnes se comparent par NOM (répétition à blanc sur une vraie base, #1088) --


def _colonnes_en_place(dsn: str, t: str) -> list[str]:
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return [r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (t,))]


def test_des_colonnes_dans_un_autre_ordre_s_importent(pg_dsn, tmp_path):
    """Une base servie porte en fin de table ce qu'`ALTER TABLE … ADD COLUMN` lui a
    ajouté ; une base née par le démarrage l'a à sa place de création. Mêmes colonnes,
    autre ordre : l'import passe, et la valeur arrive dans SA colonne."""
    source, cible = _naitre(pg_dsn, "oto", "oto"), _naitre(pg_dsn, slug_de(A))
    try:
        with psycopg.connect(source, autocommit=True, row_factory=dict_row) as c:
            a = semer(c, A, cle=CLE_SOURCE)
            # `label` repasse par ADD COLUMN, valeurs gardées : elle finit en fin de table.
            c.execute("ALTER TABLE tenant_legal_docs ADD COLUMN label_ TEXT")
            c.execute("UPDATE tenant_legal_docs SET label_ = label")
            c.execute("ALTER TABLE tenant_legal_docs DROP COLUMN label")
            c.execute("ALTER TABLE tenant_legal_docs RENAME COLUMN label_ TO label")
            c.execute("ALTER TABLE tenant_legal_docs ALTER COLUMN label SET NOT NULL")
        en_source = _colonnes_en_place(source, "tenant_legal_docs")
        en_cible = _colonnes_en_place(cible, "tenant_legal_docs")
        assert en_source != en_cible and sorted(en_source) == sorted(en_cible)
        chemin = tmp_path / "a.jsonl"
        _exporter(source, a["org"], FauxS3(a["objets"]), chemin)
        _importer(cible, chemin)
        with psycopg.connect(cible, row_factory=dict_row) as c:
            assert c.execute("SELECT label, url FROM tenant_legal_docs WHERE tenant_slug = %s",
                             (slug_de(A),)).fetchall() == \
                [{"label": f"CGU {A}", "url": "https://exemple.test/cgu"}]
    finally:
        _detruire(pg_dsn, source)
        _detruire(pg_dsn, cible)


def test_une_colonne_d_un_seul_cote_refuse_en_la_nommant(export_a, pg_dsn):
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("ALTER TABLE tenant_legal_docs DROP COLUMN url")
            c.execute("ALTER TABLE tenant_legal_docs ADD COLUMN en_trop TEXT")
        with pytest.raises(ImportRefuse) as refus:
            _importer(dsn, export_a[0])
        assert "tenant_legal_docs (source seule : url ; cible seule : en_trop)" in \
            str(refus.value)
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
    finally:
        _detruire(pg_dsn, dsn)


# --- Un seul chemin (décision du 28/09/2026) : le rechiffrement est À L'EXPORT --------


def test_l_import_n_a_aucun_chemin_de_rechiffrement():
    """L'ancien chemin (l'import recevait les deux clés et rechiffrait) est RETIRÉ, pas
    gardé en variante : l'import ne prend aucune clé, n'en chiffre aucune, et l'export
    n'a plus de mode qui transporterait un secret sous notre clé."""
    from oto_mcp.export_perimetre import importation, journal
    assert list(inspect.signature(importer).parameters) == ["conn", "chemin", "stockage",
                                                            "base_publique"]
    assert list(inspect.signature(journal.importer_tranche).parameters) == \
        ["conn", "chemin", "stockage", "base_publique"]
    assert set(inspect.signature(exporter).parameters) == {
        "conn", "orgs", "sortie", "base_publique", "cle_cible", "stockage", "classement",
        "journal"}
    for module in (importation, journal):
        source = inspect.getsource(module)
        assert "encrypt_with_key" not in source and "rechiffrer" not in source


def test_l_import_refuse_notre_cle(export_a, pg_dsn):
    """Une instance qui se présenterait avec NOTRE clé (celle de la source) n'importe rien."""
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="autre clé"):
            _importer(dsn, export_a[0], cle=CLE_SOURCE)
    finally:
        _detruire(pg_dsn, dsn)


def test_un_secret_chiffre_sous_une_autre_cle_que_la_cible_refuse(export_a, tmp_path,
                                                                   pg_dsn):
    """Un fichier dont UN secret n'est pas chiffré pour la cible (ici : sous notre clé),
    même au manifeste recalculé pour passer le contrôle d'empreinte, ne s'importe pas."""
    chemin, manifeste = export_a
    lignes = [json.loads(x) for x in chemin.read_text(encoding="utf-8").splitlines()[:-1]]
    transformation = Transformation.depuis(_schema_vide_de_tenant(), manifeste["comptes"])
    x = next(x for x in lignes if x["t"] == "connector_credentials")
    colonne, aad = AAD[x["t"]]
    x["l"][colonne] = encrypt_with_key(CLE_SOURCE, "forgé",
                                       aad(transformation.appliquer(x["t"], x["l"])))
    textes = [json.dumps(y, ensure_ascii=False) + "\n" for y in lignes]
    empreinte = hashlib.sha256("".join(textes).encode()).hexdigest()
    archive = manifeste["objets"]["archive"]
    (tmp_path / archive).write_bytes((chemin.parent / archive).read_bytes())
    forge = tmp_path / "forge.jsonl"
    forge.write_text("".join(textes) + json.dumps(
        {"manifeste": {**manifeste, "empreinte": empreinte}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        with pytest.raises(ImportRefuse, match="ne se déchiffre pas"):
            _importer(dsn, forge)
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            assert c.execute("SELECT count(*) AS n FROM orgs").fetchone()["n"] == 0
    finally:
        _detruire(pg_dsn, dsn)


# --- Les anciens comptes : rattacher, sinon omettre (répétition à blanc, #1088) --------
#
# Sur une vraie base, les tables possédées par org portaient des lignes d'anciens comptes
# de l'annuaire primaire (sub NU), qui ne sont plus membres. Certains ont un jumeau
# `<slug>:<même id>` dans le périmètre : dénudé sur la cible, il heurtait leurs lignes
# (violation d'unicité brute à l'import). La règle se tranche et se compte à l'export.

JUMEAU = f"{A}-alice"          # l'ancien compte nu dont `<slug>:A7d1e-alice` est le jumeau
ORPHELIN = "ancien-sans-jumeau"


@pytest.fixture(scope="module")
def anciens(pg_dsn):
    """Une source où l'org du périmètre porte des lignes de deux anciens comptes nus :
    `JUMEAU` (dont une en collision de clé avec une ligne de son jumeau) et `ORPHELIN`."""
    dsn = _naitre(pg_dsn, "oto", "oto")
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
            a = semer(c, A, cle=CLE_SOURCE)
            o, alice = a["org"], a["alice"]
            for ancien in (JUMEAU, ORPHELIN):
                c.execute("INSERT INTO users (sub, email) VALUES (%s, %s)",
                          (ancien, f"{ancien}@exemple.test"))
            c.execute("INSERT INTO connector_selection_seeded (sub, org_id) "
                      "VALUES (%s, %s), (%s, %s)", (alice, o, JUMEAU, o))       # doublon
            c.execute("INSERT INTO user_selected_connectors (sub, org_id, connector, origin) "
                      "VALUES (%s, %s, 'apollo', 'jumeau'), (%s, %s, 'apollo', 'ancien'), "
                      "(%s, %s, 'serper', 'ancien')", (alice, o, JUMEAU, o, JUMEAU, o))
            c.execute("INSERT INTO unipile_accounts (sub, org_id, provider, account_id) "
                      "VALUES (%s, %s, 'LINKEDIN', 'u-ancien')", (JUMEAU, o))  # FK → users
            for ancien in (JUMEAU, ORPHELIN):
                c.execute("INSERT INTO runs (run_id, sub, org_id, label) "
                          "VALUES (%s, %s, %s, 'ancien')", (f"run-{ancien}", ancien, o))
                c.execute("INSERT INTO run_messages (run_id, seq, role, content) "
                          "VALUES (%s, 1, 'user', '{}')", (f"run-{ancien}",))
                c.execute("INSERT INTO org_member_events (org_id, sub, action) "
                          "VALUES (%s, %s, 'removed')", (o, ancien))
            c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id) "
                      "SELECT 'oto', 'tool', %s, 'ancien', %s FROM generate_series(1, 3)",
                      (JUMEAU, o))
            c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id) "
                      "SELECT 'oto', 'tool', %s, 'ancien', %s FROM generate_series(1, 2)",
                      (ORPHELIN, o))
            # Le couple polymorphe : un credential de membre de l'ancien compte, l'un en
            # doublon de celui du jumeau (`apollo`), l'autre seul (`hunter`).
            _credential(c, CLE_SOURCE, "member", f"{o}:{JUMEAU}", "apollo", "ancien")
            _credential(c, CLE_SOURCE, "member", f"{o}:{JUMEAU}", "hunter", "ancien")
        yield {"dsn": dsn, A: a, "seau": FauxS3(a["objets"])}
    finally:
        _detruire(pg_dsn, dsn)


@pytest.fixture(scope="module")
def export_anciens(anciens, tmp_path_factory):
    chemin = tmp_path_factory.mktemp("anciens") / "a.jsonl"
    return chemin, _exporter(anciens["dsn"], anciens[A]["org"], anciens["seau"], chemin)


@pytest.fixture(scope="module")
def cible_anciens(export_anciens, pg_dsn):
    dsn = _naitre(pg_dsn, slug_de(A))
    try:
        yield {"dsn": dsn, "rapport": _importer(dsn, export_anciens[0])}
    finally:
        _detruire(pg_dsn, dsn)


def test_le_manifeste_compte_les_lignes_rattachees_et_omises(export_anciens):
    _, manifeste = export_anciens
    assert manifeste["comptes_hors_perimetre"] == {
        "tables": {
            "connector_selection_seeded": {"omises_doublon": 1},
            "user_selected_connectors": {"rattachees": 1, "omises_doublon": 1},
            "unipile_accounts": {"rattachees": 1},
            "runs": {"rattachees": 1, "omises_sans_jumeau": 1},
            "run_messages": {"omises_avec_leur_parent": 1},
            "org_member_events": {"rattachees": 1, "omises_sans_jumeau": 1},
            "tool_calls": {"rattachees": 3, "omises_sans_jumeau": 2},
            "connector_credentials": {"rattachees": 1, "omises_doublon": 1},
        },
        "comptes": {"rattaches": 1, "sans_jumeau": 1},
    }
    assert ORPHELIN not in json.dumps(manifeste)


def test_le_fichier_ne_porte_que_les_comptes_du_perimetre_ou_leurs_jumeaux(export_anciens):
    chemin, manifeste = export_anciens
    lignes = [json.loads(x) for x in chemin.read_text(encoding="utf-8").splitlines()[:-1]]
    assert not [x for x in lignes if ORPHELIN in json.dumps(x["l"])]
    seeded = [x["l"]["sub"] for x in lignes if x["t"] == "connector_selection_seeded"]
    assert seeded == [manifeste["tenant"]["slug"] + ":" + JUMEAU]     # le jumeau gagne


def test_l_import_passe_et_rattache_les_lignes_au_compte_nu_du_jumeau(
        anciens, cible_anciens, export_anciens):
    """La relecture est conforme (sinon `VerificationEchouee`), et sur la cible les
    lignes rattachées portent le compte nu du jumeau — celles de l'orphelin, aucune."""
    _, manifeste = export_anciens
    attendus = {t: v["lignes"] for t, v in manifeste["tables"].items() if v.get("lignes")}
    assert {t: v["lignes"] for t, v in cible_anciens["rapport"].items()} == attendus
    with psycopg.connect(cible_anciens["dsn"], row_factory=dict_row) as c:
        for t in CLASSEMENT:
            n = c.execute(f"SELECT count(*) AS n FROM {t} x WHERE row_to_json(x)::text "
                          "LIKE %s", (f"%{ORPHELIN}%",)).fetchone()["n"]
            assert n == 0, f"{t} porte une ligne de l'ancien compte sans jumeau"
        # (hors de l'org : les sentinelles que le démarrage sème, `org_id = 0`)
        assert c.execute("SELECT sub FROM connector_selection_seeded WHERE org_id = %s",
                         (anciens[A]["org"],)).fetchall() == [{"sub": JUMEAU}]
        assert c.execute("SELECT connector, origin FROM user_selected_connectors "
                         "WHERE sub = %s ORDER BY connector", (JUMEAU,)).fetchall() == \
            [{"connector": "apollo", "origin": "jumeau"},
             {"connector": "serper", "origin": "ancien"}]
        assert c.execute("SELECT sub FROM unipile_accounts").fetchall() == [{"sub": JUMEAU}]
        assert [r["run_id"] for r in c.execute(
            "SELECT run_id FROM runs WHERE sub = %s ORDER BY run_id", (JUMEAU,))] == \
            [f"run-{A}", f"run-{JUMEAU}"]
        assert c.execute("SELECT count(*) AS n FROM tool_calls WHERE tool = 'ancien'"
                         ).fetchone()["n"] == 3
        secrets = {r["connector"]: decrypt_with_key(CLE_CIBLE, r["secret_enc"], AAD[
            "connector_credentials"][1](r)) for r in c.execute(
                "SELECT * FROM connector_credentials WHERE entity_type = 'member'")}
    assert secrets == {"apollo": SECRET.format(A, "apollo"),              # le jumeau gagne
                       "hunter": SECRET.format("ancien", "hunter")}


def test_la_commande_affiche_les_comptes_hors_perimetre(anciens, export_anciens, tmp_path,
                                                       monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", anciens["dsn"])
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", CLE_SOURCE.hex())
    monkeypatch.setenv("OTO_EXPORT_CLE_CIBLE", CLE_CIBLE.hex())
    monkeypatch.setenv("OTO_MCP_S3_PUBLIC_BASE_URL", BASE_SOURCE)
    monkeypatch.setattr(commande, "_stockage", lambda: StockageS3(anciens["seau"], "source"))
    assert commande.main(["export", "--org", str(anciens[A]["org"]),
                          "--sortie", str(tmp_path / "cli.jsonl")]) == 0
    resume = json.loads(capsys.readouterr().out)
    assert resume["comptes_hors_perimetre"] == export_anciens[1]["comptes_hors_perimetre"]


def test_un_compte_d_un_autre_tenant_refuse_a_l_export_en_se_nommant(anciens, tmp_path):
    """Hors de la règle — le compte d'un AUTRE tenant, ou un `<slug>:` qui n'est pas du
    périmètre — : refus nommé (table, colonne, nombre) à l'export, rien d'écrit."""
    with psycopg.connect(anciens["dsn"], autocommit=True, row_factory=dict_row) as c:
        o = org(c, "org à part", tenant(c, "tautre", "tautre"))
        membre(c, o, "tautre:zoe")
        c.execute("INSERT INTO tool_calls (server, kind, sub, tool, org_id) "
                  "VALUES ('oto', 'tool', %s, 'x', %s)", (anciens[A]["alice"], o))
        c.execute("INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, %s)",
                  ("tautre:parti", o))
    chemin = tmp_path / "refus.jsonl"
    with pytest.raises(ComptesHorsRegle) as refus:
        _exporter(anciens["dsn"], o, anciens["seau"], chemin)
    assert refus.value.comptes == {"tool_calls.sub": 1, "connector_selection_seeded.sub": 1}
    assert "tool_calls.sub : 1 ligne(s)" in str(refus.value)
    assert not chemin.exists()


def test_aucun_vecteur_de_classement_ne_survit_au_contenu_reecrit(source, cible):
    """`search_vec` recopie le contenu d'une ligne — sub qualifié et URL de notre
    stockage compris — en un vecteur que les règles de l'export (valeur ÉGALE à un sub,
    URL dans un texte) ne réécrivent pas. Transporté tel quel, il garderait sur la cible
    la trace de l'ancien compte et décrirait un contenu qui n'y est plus. Il part vide,
    et la cible le reconstruit sur le contenu réécrit — par le calcul à la lecture,
    puis par la boucle qui tourne sur toute instance."""
    slug, hote = slug_de(A), BASE_SOURCE.split("//", 1)[1]
    trace = f"(search_vec @@ '{slug}'::tsquery OR search_vec @@ '{hote}'::tsquery)"
    with psycopg.connect(source["dsn"], row_factory=dict_row) as c:
        assert c.execute(f"SELECT count(*) AS n FROM datastore_rows WHERE {trace}"
                         ).fetchone()["n"], "le banc voit la trace à la source"
    with psycopg.connect(cible["dsn"], row_factory=dict_row) as c:
        for t, expr in RANKED_SOURCES.items():
            perimes = c.execute(
                f"SELECT count(*) AS n FROM {t} WHERE search_vec IS NOT NULL "
                f"AND search_vec IS DISTINCT FROM {_vec(expr)}").fetchone()["n"]
            assert perimes == 0, f"{t} : un vecteur décrit un contenu qui n'est plus là"
            assert c.execute(f"SELECT count(*) AS n FROM {t} WHERE search_vec IS NOT NULL"
                             ).fetchone()["n"] == 0, f"{t} : le vecteur a voyagé"
        assert not c.execute(f"SELECT count(*) AS n FROM datastore_rows WHERE {trace}"
                             ).fetchone()["n"]
        # ① Vide, la ligne se TROUVE et se classe quand même : le vecteur se calcule à
        # la lecture (`rank_expr`, `COALESCE(search_vec, …)`).
        clause, params = lignes_trouvees_sql(recherche("ligne", "all"))
        trouvees = c.execute(f"SELECT count(*) AS n FROM datastore_rows WHERE {clause}",
                             params).fetchone()["n"]
        assert trouvees == c.execute("SELECT count(*) AS n FROM datastore_rows"
                                     ).fetchone()["n"] > 0
        # ② La boucle de la cible (sa requête, `rank_backfill_sql`) le reconstruit, et
        # sur le contenu RÉÉCRIT : plus aucune trace de l'ancien compte ni de l'URL.
        for t in RANKED_SOURCES:
            while c.execute(rank_backfill_sql(t, 1000)).rowcount:
                pass
            assert c.execute(f"SELECT count(*) AS n FROM {t} WHERE search_vec IS NULL "
                             f"OR search_vec IS DISTINCT FROM {_vec(RANKED_SOURCES[t])}"
                             ).fetchone()["n"] == 0
        assert not c.execute(f"SELECT count(*) AS n FROM datastore_rows WHERE {trace}"
                             ).fetchone()["n"]
        c.rollback()
