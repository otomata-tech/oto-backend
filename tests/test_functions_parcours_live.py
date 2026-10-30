"""`oto_function`, parcours complet (ADR 0073) : publier sous la garde des tests, exécuter,
ranger les fichiers dans le projet, revenir en arrière — sur une vraie base et le vrai
bac à sable. Seul le stockage objet est doublé : il vit hors de la machine.

Ce que ces bancs tiennent :
- rien ne s'exécute avant d'être publié, et seule une version PUBLIÉE s'exécute ;
- une version ne se publie pas sans tests, ni avec un test rouge ;
- un fichier produit devient un fichier du projet, rendu avec une URL signée ;
- republier une version antérieure la remet en service (retour arrière) ;
- tester, publier, refuser : la plateforme seulement.
"""
import os

import pytest

from oto_mcp import access
from oto_mcp.capabilities import functions as F
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.db._conn import _connect
from oto_mcp.functions import executor

MOI = "sub-parcours-moi"
ORG = 4343
CTX = ResolvedCtx(sub=MOI, org_id=ORG)

CALCUL = ("import io, openpyxl\n\ndef executer(entree):\n"
          "    classeur = openpyxl.Workbook(); classeur.active['A1'] = entree['a'] * FACTEUR\n"
          "    tampon = io.BytesIO(); classeur.save(tampon)\n"
          "    return {'result': entree['a'] * FACTEUR, 'warnings': ['relu'],\n"
          "            'files': [{'name': 'devis.xlsx', 'content': tampon.getvalue()}]}\n")
TESTS = ("from calcul import executer\n\ndef test_double():\n"
         "    assert executer({'a': 2})['result'] == 4\n")


def _sources(facteur=2, tests=TESTS):
    sources = {"calcul.py": f"FACTEUR = {facteur}\n" + CALCUL}
    if tests is not None:
        sources["test_calcul.py"] = tests
    return sources


def _appel(**champs):
    return F._dispatch(CTX, F.FunctionInput(**champs))


def _refus(code, **champs):
    with pytest.raises(AuthzDenied) as refus:
        _appel(**champs)
    assert refus.value.code == code, refus.value.message
    return refus.value


def _creer(slug, **champs):
    return _appel(**dict({"op": "create", "slug": slug, "title": slug,
                          "sources": _sources(), "entrypoint": "calcul:executer",
                          "requirements": ["openpyxl"]}, **champs))


@pytest.fixture(scope="module")
def bac():
    if not os.environ.get(executor.ENV_DIR):
        message = f"{executor.ENV_DIR} absente : pas de bac à sable sur ce poste."
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture
def stockage(monkeypatch):
    poses = {}

    def _poser(prefixe, proprietaire, contenu, mime, nom, *, max_bytes=None):
        cle = f"{prefixe}/{proprietaire}/{nom}"
        poses[cle] = contenu
        return cle
    monkeypatch.setattr(F.media_store, "upload_object", _poser)
    monkeypatch.setattr(F.media_store, "presign_get", lambda cle, **kw: f"https://signe/{cle}")
    return poses


@pytest.fixture(scope="module")
def projet(live):
    with _connect() as conn:
        return conn.execute(
            "INSERT INTO projects (owner_type, owner_id, name) VALUES ('user', %s, 'Devis') "
            "RETURNING id", (MOI,)).fetchone()["id"]


def test_rien_ne_s_execute_avant_la_publication(live, bac):
    _creer("inedite")
    _refus("not_published", op="run", slug="inedite", input={"a": 1})


def test_une_version_au_test_rouge_ne_se_publie_pas(live, bac):
    _creer("rouge", sources=_sources(facteur=3))
    refus = _refus("tests_failed", op="publish", slug="rouge", version=1)
    assert refus.details["tests"][0]["test"] == "test_calcul.test_double"
    assert _appel(op="get", slug="rouge")["version"]["status"] == "proposee"


def test_une_version_sans_test_ne_se_publie_pas(live, bac):
    _creer("sans-test", sources=_sources(tests=None))
    _refus("no_tests", op="publish", slug="sans-test", version=1)


def test_publier_puis_executer_range_le_fichier_dans_le_projet(live, bac, stockage, projet):
    _creer("devis")
    publie = _appel(op="publish", slug="devis", version=1)
    assert publie["ok"] and publie["function"]["published_version"] == 1

    rendu = _appel(op="run", slug="devis", input={"a": 21}, project=projet)
    assert rendu["result"] == 42 and rendu["warnings"] == ["relu"]
    [fichier] = rendu["files"]
    assert fichier["name"] == "devis.xlsx"
    assert fichier["download_url"].startswith("https://signe/project-files/")
    assert list(stockage.values())[0][:2] == b"PK", "un .xlsx est une archive zip"
    with _connect() as conn:
        ligne = conn.execute("SELECT filename, created_by FROM project_files WHERE id = %s",
                             (fichier["file_id"],)).fetchone()
    assert (ligne["filename"], ligne["created_by"]) == ("devis.xlsx", MOI)


def test_un_fichier_produit_sans_projet_est_refuse_en_le_disant(live, bac, stockage):
    _creer("sans-projet")
    _appel(op="publish", slug="sans-projet", version=1)
    refus = _refus("missing_project", op="run", slug="sans-projet", input={"a": 1})
    assert "project=" in refus.message


def test_seule_une_version_publiee_s_execute_et_republier_revient_en_arriere(
        live, bac, stockage, projet):
    _creer("versions")
    _appel(op="publish", slug="versions", version=1)
    _appel(op="propose", slug="versions", expected_version=1,
           sources=_sources(tests=TESTS.replace("== 4", "== 4 or True")),
           entrypoint="calcul:executer", requirements=["openpyxl"])
    _refus("not_published", op="run", slug="versions", version=2, input={"a": 1},
           project=projet)
    _appel(op="publish", slug="versions", version=2)
    assert _appel(op="get", slug="versions")["version"]["version"] == 2
    _appel(op="publish", slug="versions", version=1)
    assert _appel(op="get", slug="versions")["function"]["published_version"] == 1


def test_une_version_refusee_garde_son_motif_et_ne_se_publie_plus(live, bac):
    _creer("refusee")
    _refus("missing_note", op="refuse", slug="refusee", version=1)
    _appel(op="refuse", slug="refusee", version=1, note="calcul faux sur les forfaits")
    version = _appel(op="get", slug="refusee", version=1)["version"]
    assert version["status"] == "refusee"
    assert version["test_report"] == {"motif": "calcul faux sur les forfaits"}
    _refus("refused_version", op="publish", slug="refusee", version=1)


@pytest.mark.parametrize("op", ["test", "publish", "refuse"])
def test_tester_publier_refuser_suivent_la_portee(op):
    """28/10/2026 : tester, publier et refuser une fonction suivent sa portée (membre de
    l'org, soi), comme la créer — plus de plancher plateforme. Une personne seulement
    (`code_human_only`, couvert à part)."""
    capacite = next(c for c in F.CAPABILITIES if c.key == "me.function")
    assert capacite.authz.platform_floor is None or \
        capacite.authz.platform_floor == F._PORTEE.platform_floor
    assert F._PORTEE in capacite.authz.autz_branches
    assert not hasattr(F, "_PLATEFORME")


