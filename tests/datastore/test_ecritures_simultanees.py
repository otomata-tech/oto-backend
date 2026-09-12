"""Deux écritures simultanées sur UNE ligne — contre un vrai PostgreSQL (12/09/2026).

**Le défaut fermé.** Le patch par `id` (REST `PATCH …/rows/{id}`, `data_write(id=…)`,
`append_row` avec `_id`) lisait la ligne dans une connexion, fusionnait en Python et
réécrivait le JSON ENTIER dans une autre. Mesuré sur l'arbre servi (v1.274.0) : deux
patchs partis ensemble sur deux colonnes DIFFÉRENTES en perdaient un dans 100 % des cas,
quand la voie par clé métier, déjà verrouillée, n'en perdait aucun. Il passe désormais par
`datastore_merge_row_locked` — `FOR UPDATE`, bail vérifié sous le même verrou.

**Ce que le verrou ne ferme pas.** Un écrivain qui LIT, recalcule et renvoie la valeur
entière écraserait l'écriture faite entre sa lecture et la sienne : le verrou ne protège
pas une lecture faite par le client. D'où `expected_revision`, et ce banc tient que deux
écrivains qui recalculent sous précondition, avec trois essais, ne perdent rien — et
qu'un refus ne touche à RIEN.

Chutes jouées en mémoire (greffon, arbre intact), rouge constaté :
- lecture hors verrou → (a) sur les trois portes, et la course réservation/patch ;
- comparaison retirée → (b) perd des entrées, et « un refus ne touche à rien » ;
- comparaison placée après l'UPDATE → « un refus ne touche à rien ».
"""
from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from _datastore_rest import call, stub_authz

SUB = "sub-concurrence"
# L'ancien chemin perdait à CHAQUE départ simultané. Un défaut qui ne perdrait qu'un tour
# sur deux passerait trente tours sans perte une fois sur un milliard.
TOURS = 30


class Conflit(Exception):
    """La précondition refusée, quelle que soit la porte."""

    def __init__(self, texte: str, courante=None):
        super().__init__(texte)
        self.texte, self.courante = texte, courante


class Verrouillee(Exception):
    """La ligne réservée par un autre, quelle que soit la porte."""


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


def _table(ligne: dict | None = None, rid: str = "r1") -> tuple[str, int]:
    from oto_mcp import db
    ns = "conc-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    db.datastore_insert_row(ns_id, rid, dict(ligne or {}))
    return ns, ns_id


def _etat(ns_id: int, rid: str = "r1") -> dict:
    """Ce que porte la BASE, par une connexion fraîche. Les dates en texte PostgreSQL :
    le formateur de lignes les coupe à la seconde, un refus « sans effet » s'y cacherait."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT data, rev, updated_at::text AS updated_at, claims, claimed_by, "
            "       claimed_until::text AS claimed_until, claimed_run "
            "FROM datastore_rows WHERE ns_id = %s AND row_id = %s", (ns_id, rid)).fetchone())


_OUTILS: dict = {}


def _outil(nom: str):
    if nom not in _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-concurrence")
        register_all(m)
        _OUTILS[nom] = asyncio.run(m.get_tool(nom))
    return _OUTILS[nom]


class _Store:
    def lire(self, ns, rid):
        return _store().get_row(ns, rid)

    def ecrire(self, ns, rid, patch, revision=None):
        from oto_mcp.datastore.errors import RevisionConflict, RowLocked
        try:
            return _store().update_row(ns, rid, patch, expected_revision=revision)
        except RevisionConflict as e:
            raise Conflit(str(e), e.current_revision) from None
        except RowLocked as e:
            raise Verrouillee(str(e)) from None


class _Rest:
    def lire(self, ns, rid):
        code, corps = call("me.datastore.get_row",
                           path_params={"datastore": ns, "row_id": rid}, sub=SUB)
        assert code == 200, corps
        return corps

    def ecrire(self, ns, rid, patch, revision=None):
        query = b"" if revision is None else f"expected_revision={revision}".encode()
        code, corps = call("me.datastore.update_row",
                           path_params={"datastore": ns, "row_id": rid},
                           body=patch, query=query, sub=SUB)
        if (code, corps.get("error")) == (409, "revision_conflict"):
            raise Conflit(corps["detail"], corps["details"]["current_revision"])
        if (code, corps.get("error")) == (409, "row_locked"):
            raise Verrouillee(corps["detail"])
        assert code == 200, (code, corps)
        return corps


class _Mcp:
    @staticmethod
    def _appel(nom, **arguments):
        return asyncio.run(_outil(nom).run(arguments)).structured_content

    def lire(self, ns, rid):
        return self._appel("data_rows", datastore=ns, id=rid)

    def ecrire(self, ns, rid, patch, revision=None):
        arguments = {"datastore": ns, "id": rid, "row": patch}
        if revision is not None:
            arguments["expected_revision"] = revision
        try:
            return self._appel("data_write", **arguments)
        except Exception as e:  # noqa: BLE001 — traduit en refus de banc, ou relevé
            if "revision_conflict" in str(e):
                raise Conflit(str(e)) from None
            raise


@pytest.fixture
def portes(live, monkeypatch):
    from oto_mcp.tools import datastore as T
    stub_authz(monkeypatch)
    monkeypatch.setattr(T, "_acting_store", _store)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    _outil("data_write"), _outil("data_rows")      # montés AVANT les fils
    return {"store": _Store(), "rest": _Rest(), "mcp": _Mcp()}


def _ensemble(*gestes) -> list:
    """Lance les gestes au MÊME instant (barrière) ; rend les exceptions, jamais avalées."""
    barriere = threading.Barrier(len(gestes))
    erreurs: list = []

    def courir(geste):
        barriere.wait()
        try:
            geste()
        except BaseException as e:  # noqa: BLE001 — rendue au test qui l'examine
            erreurs.append(e)

    fils = [threading.Thread(target=courir, args=(g,)) for g in gestes]
    for f in fils:
        f.start()
    for f in fils:
        f.join(timeout=60)
    assert not any(f.is_alive() for f in fils), "un geste n'a pas rendu la main en 60 s"
    return erreurs


# ── (a) le verrou : deux colonnes, deux écrivains, aucune perte ─────────────────

@pytest.mark.parametrize("porte", ["store", "rest", "mcp"])
def test_deux_patchs_simultanes_sur_deux_colonnes_ne_se_perdent_pas(portes, porte):
    p = portes[porte]
    ns, ns_id = _table({"a": "-", "b": "-"})
    pertes = []
    for i in range(TOURS):
        erreurs = _ensemble(lambda: p.ecrire(ns, "r1", {"a": f"a{i}"}),
                            lambda: p.ecrire(ns, "r1", {"b": f"b{i}"}))
        assert not erreurs, erreurs
        data = _etat(ns_id)["data"]
        if (data.get("a"), data.get("b")) != (f"a{i}", f"b{i}"):
            pertes.append((i, data))
    assert not pertes, (f"{porte} : {len(pertes)}/{TOURS} tours ont perdu une écriture "
                        f"— {pertes[:3]}")


def test_une_reservation_et_un_patch_simultanes_ne_passent_jamais_par_dessus(live):
    """Si le patch a écrit ET que la réservation a réussi, le patch est passé AVANT : la
    ligne réservée porte donc sa valeur. Sinon le patch a écrit sur une ligne réservée
    par un autre sans l'avoir vue — le défaut d'un bail lu hors du verrou."""
    ns, ns_id = _table({"c": "-"})
    fautes, issues = [], {"ecrit": 0, "refuse": 0}
    for i in range(TOURS):
        res: dict = {}

        def reserver():
            res["bail"] = _store().claim_row(ns, "r1", worker="poste-1", lease_s=600)

        def patcher():
            try:
                _Store().ecrire(ns, "r1", {"c": f"c{i}"})
                res["patch"] = "ecrit"
            except Verrouillee:
                res["patch"] = "refuse"

        assert not _ensemble(reserver, patcher)
        issues[res["patch"]] += 1
        if res["patch"] == "ecrit" and res["bail"].get("c") != f"c{i}":
            fautes.append(i)
        _store().release_claim(ns, "r1", worker="poste-1")
    assert not fautes, (f"{len(fautes)}/{TOURS} patchs ont écrit par-dessus une "
                        f"réservation qu'ils n'avaient pas vue (issues : {issues})")


# ── (b) la précondition : recalculer sans rien perdre ───────────────────────────

@pytest.mark.parametrize("porte", ["store", "rest", "mcp"])
def test_deux_ecrivains_qui_recalculent_sous_precondition_ne_perdent_rien(portes, porte):
    from oto_mcp import db
    p = portes[porte]
    ns, ns_id = _table({"journal": ["depart"]}, rid="j0")
    pertes = []
    for i in range(TOURS):
        rid = f"j{i + 1}"
        db.datastore_insert_row(ns_id, rid, {"journal": ["depart"]})
        essais: dict = {}

        def ecrivain(nom):
            for essai in range(1, 4):
                lu = p.lire(ns, rid)
                try:
                    p.ecrire(ns, rid, {"journal": list(lu["journal"]) + [nom]},
                             revision=lu["_revision"])
                    essais[nom] = essai
                    return
                except Conflit:
                    continue
            essais[nom] = None

        erreurs = _ensemble(lambda: ecrivain("A"), lambda: ecrivain("B"))
        assert not erreurs, erreurs
        journal = _etat(ns_id, rid)["data"].get("journal") or []
        if sorted(journal) != ["A", "B", "depart"] or None in essais.values():
            pertes.append((rid, journal, essais))
    assert not pertes, f"{porte} : {len(pertes)}/{TOURS} tours perdus — {pertes[:3]}"


@pytest.mark.parametrize("porte", ["store", "rest", "mcp"])
def test_un_refus_de_revision_ne_touche_a_rien(portes, porte):
    from oto_mcp.db._conn import _connect
    p = portes[porte]
    ns, ns_id = _table({"statut": "a_faire"})
    lue = p.lire(ns, "r1")["_revision"]
    _store().update_row(ns, "r1", {"note": "entre-temps"})
    with _connect() as conn:   # `claims` n'avance pas la révision : il doit rester à 2
        conn.execute("UPDATE datastore_rows SET claims = 2 "
                     "WHERE ns_id = %s AND row_id = 'r1'", (ns_id,))
    avant = _etat(ns_id)
    with pytest.raises(Conflit) as refus:
        p.ecrire(ns, "r1", {"statut": "fait"}, revision=lue)
    assert _etat(ns_id) == avant, "un refus a modifié la ligne (donnée, rev, horodatage, claims)"
    assert "revision_conflict" in refus.value.texte
    assert f"révision actuelle {avant['rev']}" in refus.value.texte
    if refus.value.courante is not None:
        assert refus.value.courante == str(avant["rev"])


@pytest.mark.parametrize("porte", ["store", "rest"])
def test_le_bail_se_juge_AVANT_la_revision(portes, porte):
    """Sous le verrou, dans cet ordre : le bail, puis la révision. Une ligne réservée par
    un autre reste `row_locked`, même quand la précondition est périmée aussi."""
    p = portes[porte]
    ns, ns_id = _table({"statut": "a_faire"})
    _store().update_row(ns, "r1", {"note": "entre-temps"})
    _store().claim_row(ns, "r1", worker="poste-autre", lease_s=600)
    avant = _etat(ns_id)
    with pytest.raises(Verrouillee):
        p.ecrire(ns, "r1", {"statut": "fait"}, revision="0")
    assert _etat(ns_id) == avant
