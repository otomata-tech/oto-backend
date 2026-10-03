"""Fixtures partagées.

`pg_dsn` — un PostgreSQL RÉEL, pour les rares tests qui n'ont de valeur que là :
une contrainte (la PK que viole un renommage naïf, #295) ou un opérateur JSONB
(`data - key`, qui efface là où `null` conserve, #296) ne s'exerce pas contre un
stub. Le reste de la suite reste sans base — la convention du repo est de tester
la logique pure et les gardes par stub, le chemin SQL étant vérifié au déploiement.

Source, dans l'ordre : `OTO_TEST_PG_DSN`, sinon un conteneur jetable si `docker`
répond, sinon `skip`. Session-scopé : un seul conteneur pour toute la suite.

Le conteneur ne doit rien laisser derrière lui (#640, `_pg_hygiene.py`) : il est
étiqueté et daté, son `PGDATA` est un tmpfs (aucun volume), sa sortie est couverte
par `atexit` + SIGTERM/SIGINT en plus du finalizer, et chaque session commence par
balayer ce qu'une session tuée a laissé (`pytest_sessionstart`).

Ce fichier porte aussi le **forçage du pin oto-core** (`_oto_core_pin.py`) : quand
le venv n'exécute pas le tag qu'épingle le manifeste, la suite le DIT en bannière
— aux deux bouts du run — au lieu de laisser des rouges fidèles au venv passer
pour des rouges du dépôt. ⚠️ **La bannière ne survit pas à un `| grep passed`**
(#790, elle est écrite à côté de la ligne qui contient ce mot) : c'est pourquoi
`pytest_report_teststatus` ci-dessous range EN PLUS ces skips sous une catégorie
parlante, directement dans le résumé final de pytest — la ligne qui, elle,
survit au filtre parce qu'elle contient déjà « passed ».
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from functools import lru_cache
from typing import Iterator, NamedTuple, Optional

import pytest

from _oto_core_pin import (MARQUEUR, categorie_non_concluante, ecart,
                           lignes_de_banniere, skips_autorises)
import _groupes_xdist
import _jeton_de_suite as jeton
from _pg_hygiene import Guard, docker_available, run_args, sweep_orphans


# --------------------------------------------------------------------------- #
# Garde-fou réseau : aucune connexion sortante réelle dans la suite
# --------------------------------------------------------------------------- #
#
# Mesure du 05/09/2026 (serper flaky, otomata-tech/oto#69) : sockets sortants
# bloqués pour toute la suite (10881 tests), UN SEUL a réellement dialé —
# `test_une_url_ordinaire_n_est_PAS_refusee` visait `serper._client`, qui
# n'existe pas au niveau module (fermeture locale de `register()`) ; le
# monkeypatch posait un attribut mort, `_Faux` n'était jamais exercé, et le
# `except Exception: pass` du test avalait l'appel réseau réel qui suivait
# (vers `exemple.invalid`, RFC 2606, pourtant résolu vers une IP live).
#
# Un test qui ouvre une connexion réelle est non déterministe PAR
# CONSTRUCTION : son issue dépend de l'horloge, du réseau et des conditions du
# moment — et un rouge intermittent se fait accuser au dernier commit poussé,
# jamais à sa vraie cause. Fermé structurellement plutôt que corrigé au cas
# par cas : le loopback (tests DB réels, `pg_dsn`) reste libre, tout le reste
# est bloqué par défaut. Le besoin légitime existe : il se déclare, avec sa
# raison, à l'endroit où il se présente — jamais une exemption muette.
_MARQUEUR_RESEAU = "reseau_reel"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_reseau_autorise_pour: list[str] = []  # pile de raisons — le test courant, s'il a le droit


def _connexion_gardee(self: socket.socket, address):
    host = address[0] if isinstance(address, tuple) else address
    if host in _LOOPBACK or _reseau_autorise_pour:
        return _SOCKET_CONNECT_ORIGINAL(self, address)
    raise AssertionError(
        f"connexion sortante bloquée vers {address!r} — cette suite n'ouvre "
        f"aucune connexion réseau réelle (otomata-tech/oto#69, 05/09/2026). "
        f"Un test qui en a légitimement besoin le déclare avec "
        f"@pytest.mark.{_MARQUEUR_RESEAU}(\"pourquoi un stub ne suffit pas ici\").")


_SOCKET_CONNECT_ORIGINAL = socket.socket.connect
socket.socket.connect = _connexion_gardee


# ── L'adresse publique de l'instance, gréée pour toute la suite ──────────────
# Le code refuse de fabriquer une adresse qu'il n'a pas : `config.public_base_url()`
# lève au lieu de retomber sur un domaine (redirection OAuth, rappel de paiement, jeton
# de téléversement, lien de désinscription). Soixante-dix bancs n'ont pas à connaître
# les entrailles de ce qu'ils appellent pour autant — un banc de TVA ne devrait pas
# savoir qu'un prélèvement porte une adresse de rappel.
#
# ⚠️ Elle était posée par `os.environ.setdefault` au niveau module dans un fichier
# d'authentification, donc dès la COLLECTE et pour tout ce que pytest importait ensuite.
# Mesuré le 15/09/2026 : des bancs qui ne la déclaraient pas passaient au vert en suite
# complète et rougissaient lancés seuls — un vert FABRIQUÉ par un voisin, la forme la
# plus trompeuse, puisqu'elle ne cache pas un échec mais en invente un succès. La
# différence ici est qu'une fixture se déclare, se voit et s'annule (`delenv`), là où
# un effet de bord d'import dépendait de l'ordre alphabétique des fichiers.
#
# L'adresse posée n'est PAS l'une des nôtres, et c'est délibéré : un banc qui affirme
# quelque chose sur `mcp.oto.ninja` sans l'avoir déclaré doit rougir ici, pas en
# production sur l'instance d'un partenaire.
_ADRESSE_DE_GREEMENT = "https://mcp.exemple.test"


@pytest.fixture(autouse=True)
def _adresse_publique_de_l_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", _ADRESSE_DE_GREEMENT)


# ── Le domaine des endpoints de projet, gréé lui aussi (`config.project_domain()`,
# devenue `require_env` le 15/09/2026) ──────────────────────────────────────────────
#
# Décoy délibérément ÉVITÉ ici, contrairement à l'adresse publique juste au-dessus :
# des dizaines de bancs (`test_subdomain_project.py` en tête) affirment sur la forme
# littérale `*.mcp.oto.cx` / `*.share.oto.cx` — c'est le comportement de PROD qu'ils
# vérifient, pas une fuite vers notre domaine. Un décoy ici rendrait ces bancs
# aveugles à une vraie régression au lieu de les réparer. La valeur gréée reste donc
# la nôtre ; un banc qui teste explicitement PREPROD (`oto.ninja`) ou l'absence de
# déclaration continue de le poser lui-même via `monkeypatch`.
@pytest.fixture(autouse=True)
def _domaine_des_projets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTO_PROJECT_DOMAIN", "oto.cx")


# ── L'adresse du tableau de bord (`config.dashboard_url()`, devenue `require_env`-
# like le 16/09/2026, #968) — même raison que le domaine des projets ci-dessus : des
# dizaines de bancs (`test_dashboard_url_par_tenant.py` en tête, 9 fichiers au total)
# affirment sur la forme littérale `manage.oto.cx` — le comportement de PROD qu'ils
# vérifient. La valeur gréée reste donc la nôtre.
@pytest.fixture(autouse=True)
def _adresse_du_tableau_de_bord(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTO_APP_URL", "https://manage.oto.cx")


# ── Les défauts des droits déclarés (`access.entitlements`, #1066) — REQUISE : sans
# déclaration, la lecture d'un droit lève. La valeur gréée est celle de l'instance
# historique (`scripts/defauts_des_droits.py`) pour les clés fixes, et `0` pour toute
# clé de plateforme : ce que le code lisait avant le lot (aucun droit = non).
DEFAUTS_DES_DROITS = ('{"unipile": 0, "platform_unmetered": 0, "unipile_seats": 5, '
                      '"members_max": "unlimited", "platform_key:*": 0}')


@pytest.fixture(autouse=True)
def _defauts_des_droits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTO_ENTITLEMENT_DEFAULTS", DEFAUTS_DES_DROITS)


@pytest.fixture(autouse=True)
def _org_active_sans_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans base, toute org est ACTIVE. La garde de suspension d'org
    (`org_suspension`) lit la liste des orgs suspendues (en mémoire, relue
    périodiquement) à chaque capacité et à chaque outil de connecteur ; un banc qui
    double l'autz sans base n'a rien à y lire. Un banc sur base réelle
    (`DATABASE_URL` posé par sa fixture, de portée module, donc AVANT celle-ci) lit la
    vraie colonne ; un banc qui teste la garde double lui-même la liste.

    La liste en mémoire repart VIDE et non lue à chaque banc : sans ça, un banc
    hériterait de la suspension posée par le précédent."""
    from oto_mcp import org_store, org_suspension
    monkeypatch.setattr(org_suspension, "_cache", {"ids": frozenset(), "lu_a": None})
    if os.environ.get("DATABASE_URL"):
        return
    monkeypatch.setattr(org_store, "suspended_org_ids", lambda: [])
    monkeypatch.setattr(org_store, "get_org_suspension", lambda org_id: None)


@pytest.fixture
def sans_droit_declare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans base : aucune ligne de droit déclaré posée — `value_for` rend le défaut
    d'instance. Pour les bancs SANS base qui traversent un point d'usage des droits :
    depuis #1090, le palier plateforme lit les lignes de la PERSONNE même hors org
    (levée `platform_unmetered`, option payante). À poser par
    `pytestmark = pytest.mark.usefixtures("sans_droit_declare")`."""
    from oto_mcp.db import entitlements as db_entitlements
    monkeypatch.setattr(db_entitlements, "valeurs_posees",
                        lambda org_id, sub, cle, now=None: db_entitlements.Posees(None, None))


# ── Le tenant primaire déclaré (`tenancy.primary_slug`, #969) — identité : sans
# déclaration, le démarrage et toute classification d'un sub lèvent. La valeur gréée
# est celle de l'instance historique, comme les droits ci-dessus : des dizaines de bancs
# affirment sur le slug `oto` d'un sub nu, c'est le comportement de PROD qu'ils
# vérifient. Un banc qui joue une AUTRE instance pose la sienne par `monkeypatch`
# (`tests/test_naissance_d_une_base.py`).
# ⚠️ Portée SESSION, pas fonction : des fixtures de MODULE jouent le démarrage
# (`init_db`, qui sème le tenant sous ce slug et le nom de la marque) avant toute
# fixture de fonction — d'où aussi `OTO_BRAND_NAME` ici, à la même valeur que
# `_identite_de_l_instance` plus bas. Elle se déclare, se voit et s'annule en fin de
# session ; un `monkeypatch` de banc la surcharge.
@pytest.fixture(autouse=True, scope="session")
def _tenant_primaire_declare() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OTO_TENANT_PRIMAIRE_SLUG", "oto")
        mp.setenv("OTO_BRAND_NAME", "oto")
        yield


# ── Email transactionnel (`oto_mcp/email.py`, devenues REQUISE le 16/09/2026, #968) —
# aucun banc n'affirme sur leur valeur littérale (les envois réels sont mockés/
# court-circuités par l'absence d'`OTO_MAILER_SEND_BEARER` en test) : les anciens
# défauts servent de valeur gréée, sans risque de masquer une régression.
# ── Les bascules DATÉES de l'écriture (oto#140 J2 et J3, oto#141) ──────────────────
#
# Elles tombent à leur date, dans le code, sans déploiement : sans ce gréement, la suite
# changerait de verdict le 6, le 8 puis le 21 octobre 2026, sans qu'une ligne ait bougé. Le jour
# qui les juge est fixé à la VEILLE de leur date par défaut — le comportement d'avant,
# que la plupart des bancs décrivent. Un banc qui veut l'APRÈS déplace la date par son
# réglage (`OTO_VIDE_REMPLACE_LE`, `OTO_MOTS_DEPRECIES_REFUSES_LE`,
# `OTO_UPSERT_IMPLICITE_REFUSE_LE`), jamais l'horloge.
@pytest.fixture(autouse=True)
def _bascules_datees_a_la_veille(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from oto_mcp.datastore import mots_deprecies as mdp
    from oto_mcp.datastore import upsert_implicite as upi
    from oto_mcp.datastore import vide_remplace as vr

    veille = timedelta(days=1)
    monkeypatch.setattr(vr, "_aujourdhui", lambda: vr.VIDE_REMPLACE_LE - veille)
    monkeypatch.setattr(mdp, "_aujourdhui",
                        lambda: mdp.MOTS_DEPRECIES_REFUSES_LE - veille)
    monkeypatch.setattr(upi, "_aujourdhui",
                        lambda: upi.UPSERT_IMPLICITE_REFUSE_LE - veille)


@pytest.fixture(autouse=True)
def _email_transactionnel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTO_MAILER_URL", "https://mailer.oto.zone/api/send")
    monkeypatch.setenv("OTO_MAIL_FROM", "Oto <oto@otomata.tech>")
    monkeypatch.setenv("OTO_CONTACT_TO", "alexis@otomata.tech")


# ── Le reste de l'identité de l'instance (décision du 28/09/2026, #968 : refus partout,
# `identite_instance.verifier`) — invitations, CORS, contrats, marque. Valeurs gréées =
# celles de notre production, même raison que le domaine des projets ci-dessus : des
# bancs affirment sur leur forme littérale (origine `manage.oto.cx`, signature
# `oto · oto.cx`, pied des pages publiques). Un banc qui bumpe une version de document
# repose `OTO_LEGAL_DOCS` : `legal_docs.current_docs()` la relit à chaque appel.
DOCUMENTS_LEGAUX = {
    "terms": {"version": "3.1", "label": "CGU", "url": "https://oto.cx/terms"},
    "cgv": {"version": "2.1", "label": "CGV", "url": "https://oto.cx/cgv"},
    "dpa": {"version": "2.1", "label": "DPA", "url": "https://oto.cx/dpa"},
}
ORIGINES_CORS = ("https://oto.cx", "https://manage.oto.cx", "https://app.oto.ninja",
                 "https://dashboard.oto.ninja", "http://localhost:5192")


@pytest.fixture(autouse=True)
def _identite_de_l_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTO_INVITE_BASE_URL", "https://oto.cx")
    monkeypatch.setenv("OTO_MCP_CORS_ORIGINS", ",".join(ORIGINES_CORS))
    monkeypatch.setenv("OTO_LEGAL_DOCS", json.dumps(DOCUMENTS_LEGAUX))
    monkeypatch.setenv("OTO_BRAND_NAME", "oto")
    monkeypatch.setenv("OTO_BRAND_SITE", "oto.cx")


# **Comment vérifier que ce gréement ne CACHE rien** — à refaire après tout changement
# qui touche la fabrication des liens publics.
#
# Gréer une adresse répare le montage des bancs qui en fabriquent une sans le savoir.
# Ça ne dit rien de ceux qui affirmaient quelque chose SUR elle : ceux-là ne seraient
# pas réparés, ils seraient RENDUS VIDES — ils continueraient de passer en affirmant sur
# l'adresse de gréement ce qu'ils croyaient affirmer sur la production.
#
# La mesure : jouer la suite DEUX fois, avec deux adresses gréées différentes, et
# comparer les verdicts. Pas de substitution en cours de route — la valeur change ici,
# à la racine, donc tout ce qui en dérive change aussi, y compris ce qu'une fixture a
# construit à son setup.
#
#     vert dans les deux passes → l'adresse n'est qu'un décor pour ce banc
#     vert puis ROUGE           → ce banc AFFIRME sur l'adresse : qu'il déclare la sienne
#
# Mesuré le 15/09/2026, les deux passes rendant le même unique rouge (sans rapport) :
# AUCUN banc n'affirme sur l'adresse gréée. Le gréement ne masque rien.
#
# ⚠️ Deux pièges, tous deux rencontrés en construisant cette mesure. La passe qui
# substitue doit PROUVER qu'elle a substitué : une passe qui ne change rien rend un vert
# parfait et se lit comme « aucun banc ne dépend de l'adresse ». Et la preuve ne peut pas
# porter sur le premier test venu — un banc qui déclare sa propre adresse la surcharge
# légitimement, et l'exiger de lui fabriquerait un rouge dans la passe même qui compte
# les rouges. Elle porte sur la session : la valeur substituée doit avoir été vue
# quelque part.


@pytest.fixture(autouse=True)
def _garde_reseau_sortant(request: pytest.FixtureRequest) -> Iterator[None]:
    marker = request.node.get_closest_marker(_MARQUEUR_RESEAU)
    if marker is None:
        yield
        return
    if not marker.args or not str(marker.args[0]).strip():
        raise TypeError(
            f"@pytest.mark.{_MARQUEUR_RESEAU} exige une raison : "
            f'@pytest.mark.{_MARQUEUR_RESEAU}("pourquoi un stub ne suffit pas ici")')
    _reseau_autorise_pour.append(marker.args[0])
    try:
        yield
    finally:
        _reseau_autorise_pour.pop()


# --------------------------------------------------------------------------- #
# Garde d'exécution « pas de SQL dans la boucle » (oto_mcp/db/_hors_boucle.py)
# --------------------------------------------------------------------------- #
#
# En production la garde AVERTIT ; ici elle LÈVE pour tout site `async def` hors du stock
# gelé. La suite n'a presque pas de base (les bancs doublent `db.*`) : c'est donc surtout
# le balayage statique (`test_db_hors_boucle.py`) qui garde, et cette levée qui rattrape
# ce qu'un banc à base RÉELLE ferait passer par un chemin indirect.
@pytest.fixture(scope="session", autouse=True)
def _garde_sql_hors_boucle() -> Iterator[None]:
    from oto_mcp.db import _hors_boucle
    from _stock_db_hors_boucle import STOCK
    _hors_boucle.configurer(strict=True, tolere=STOCK)
    yield
    _hors_boucle.configurer(strict=False)
    # La levée a pu être avalée par un `except Exception` fail-soft : le relevé, lui, ne
    # l'est pas. Une violation notée fait échouer la session, sites nommés.
    _hors_boucle.exiger_aucune_violation()


# --------------------------------------------------------------------------- #
# Pin oto-core : le venv exécute-t-il ce que le tronc épingle ?
# --------------------------------------------------------------------------- #
#
# Sept sessions ont enquêté sur le même faux rouge le 01/09/2026, dont une qui a
# conclu « le tronc est rouge, plus aucune PR ne peut entrer » pendant que la CI
# était verte. La doc décrivait déjà le piège — donc ce n'est pas la doc qui
# manquait, c'est le forçage. Le voici.


@lru_cache(maxsize=1)
def _ecart_de_session():
    """Mesuré une fois par run. `.cache_clear()` pour les tests du garde-fou."""
    return ecart()


class _ConftestSansTests(pytest.File):
    """Le nœud d'un `conftest.py` donné en argument : présent (pytest exige que tout
    argument désigne un nœud, sinon `ERROR: not found`), mais qui ne collecte RIEN et
    n'importe RIEN."""

    def collect(self):
        return []


@pytest.hookimpl(wrapper=True)
def pytest_collect_file(file_path, parent):
    """Un `conftest.py` n'est jamais un module de test (#508).

    `pytest tests/*.py tests/*/` glob AUSSI `tests/conftest.py` et le passe en argument
    explicite : pytest l'importe alors une seconde fois comme MODULE de test. Or trois
    `conftest.py` (racine, `datastore/`, `db/`) partagent le basename `conftest` — pas de
    `__init__.py`, import `prepend` — donc dès que celui d'un sous-dossier est déjà chargé,
    la collecte s'interrompt sur `import file mismatch` et TOUTE la suite tombe avec.
    La CI lance `pytest` sans chemin et ne le voyait pas ; le rouge n'existait que pour
    quelqu'un qui globbe.

    ⚠️ `pytest_ignore_collect` ne peut PAS le faire : pytest ne le consulte pas pour un
    fichier passé en argument (`isinitpath`). D'où ce wrapper sur `pytest_collect_file`,
    qui remplace le module de test construit pour un `conftest.py` par un nœud vide — celui-ci reste chargé
    comme plugin de dossier, ses fixtures et ses hooks s'appliquent comme avant.
    """
    collectes = yield
    if file_path.name == "conftest.py":
        return [_ConftestSansTests.from_parent(parent, path=file_path)]
    return collectes


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{MARQUEUR}: ce test n'a de SENS que face à l'oto-core épinglé — il est "
        "passé (non concluant) en local quand le venv est en retard sur le pin, "
        "et reste mordant en CI.")
    config.addinivalue_line(
        "markers",
        f"{_MARQUEUR_RESEAU}(raison): autorise CE test à ouvrir une connexion "
        "réseau sortante réelle (non-loopback) — la raison est OBLIGATOIRE, "
        "elle documente pourquoi un stub ne suffit pas ici.")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items) -> None:
    """Un rouge qui ne prouve rien vaut moins qu'un test explicitement non
    concluant — mais SEULEMENT en local : en CI la garde version-skew doit mordre,
    c'est tout son objet (cf. `skips_autorises`).

    Pose aussi, avant tout, le groupement xdist des modules à fixture module-scopée
    (`_groupes_xdist`, #963). ⚠️ `tryfirst` n'est pas cosmétique : xdist lit les marqueurs
    `xdist_group` dans SON `pytest_collection_modifyitems` (il en suffixe le nodeid) — un
    marqueur posé après lui est ignoré, sans erreur, et le flake revient."""
    _groupes_xdist.regrouper(items)
    config.stash_oto_core_skips = 0            # type: ignore[attr-defined]
    e = _ecart_de_session()
    if e is None or not skips_autorises():
        return
    marque = pytest.mark.skip(
        reason=f"oto-core installé ({e.installe or 'aucun'}) ≠ épinglé "
               f"({e.epingle}) — non concluant dans cet environnement")
    vises = [item for item in items if item.get_closest_marker(MARQUEUR)]
    for item in vises:
        item.add_marker(marque)
    config.stash_oto_core_skips = len(vises)   # type: ignore[attr-defined]


def pytest_report_teststatus(report: pytest.TestReport, config: pytest.Config
                              ) -> tuple[str, str, str] | None:
    """#790 — le nombre survit déjà à `| grep passed` (il vit dans la MÊME ligne
    que ce mot) ; ce qui lui manquait, c'est une phrase. On ne rajoute pas une
    ligne (filtrable, comme la bannière) : on renomme la CATÉGORIE sous laquelle
    pytest compte ces skips précis, donc le nom change directement dans le
    résumé final que pytest imprime de toute façon.

    Seuls les skips posés par `pytest_collection_modifyitems` ci-dessus
    (marqueur `MARQUEUR`, phase setup) migrent vers cette catégorie — un skip
    ORDINAIRE (docker absent, etc.) reste compté sous « skipped » : le but est
    de séparer les deux effectifs, pas de maquiller l'un en l'autre.
    """
    if report.when != "setup" or not report.skipped:
        return None
    if MARQUEUR not in report.keywords:
        return None
    e = _ecart_de_session()
    if e is None:
        return None
    return categorie_non_concluante(e), "s", "NON CONCLUANT"


def _ecrire_banniere(reporter, config) -> None:
    e = _ecart_de_session()
    if e is None or reporter is None:
        return
    skips = getattr(config, "stash_oto_core_skips", 0)
    reporter.write_sep("=", "PIN oto-core", red=True, bold=True)
    for ligne in lignes_de_banniere(e, skips=skips):
        reporter.write_line(ligne, red=True, bold=ligne.startswith("oto-core"))


class PgBox(NamedTuple):
    dsn: str
    container: Optional[str]   # None quand la base vient d'`OTO_TEST_PG_DSN`


def pytest_sessionstart(session: pytest.Session) -> None:
    """Le balai (#640) : un conteneur `oto-test=1` de plus de deux heures est un orphelin
    d'une session morte sans finalizer. On le dit, une ligne par conteneur.

    Et la bannière du pin : la voir AVANT le run évite d'attendre la fin pour
    apprendre qu'on mesurait le mauvais oto-core."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    _ecrire_banniere(reporter, session.config)
    lines = sweep_orphans(time.time())
    if not lines:
        return
    for line in lines:
        if reporter is not None:
            reporter.write_line(line)
        else:
            print(line)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """La MÊME bannière en fin de run — et c'est celle-ci qui compte.

    Une ligne juste écrite là où personne ne regarde est exactement le mode de
    panne qu'on ferme : au démarrage, la bannière a défilé depuis longtemps quand
    les `FAILED` s'affichent. Ici elle atterrit contre eux, au moment précis où on
    se demande à qui sont ces rouges."""
    _ecrire_banniere(terminalreporter, config)


@pytest.fixture(scope="session")
def pg_box() -> Iterator[PgBox]:
    # Le JETON d'abord, avant de toucher au serveur : au-delà de deux suites en
    # parallèle sur ce poste, elles se fabriquent mutuellement de faux échecs (neuf
    # simultanées le 07/09/2026). Pris ICI et pas au démarrage de la session — une
    # exécution ciblée qui n'ouvre aucune base ne consomme aucune place, sinon la
    # garde punirait le geste qu'elle veut encourager. Détail : `_jeton_de_suite`.
    jeton.prendre()
    dsn = os.environ.get("OTO_TEST_PG_DSN")
    if dsn:
        yield PgBox(dsn, None)
        return
    if not docker_available():
        pytest.skip("aucun PostgreSQL joignable (ni OTO_TEST_PG_DSN, ni docker)")
    name = f"oto-test-pg-{uuid.uuid4().hex[:8]}"
    subprocess.run(run_args(name), capture_output=True, check=True)
    guard = Guard(name)
    guard.install()
    try:
        port = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            capture_output=True, text=True, check=True).stdout.strip().rsplit(":", 1)[1]
        dsn = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
        # L'attente se fait avec L'INSTRUMENT DU TEST — une vraie connexion depuis
        # l'hôte. `pg_isready` dans le conteneur répond OK pendant la phase d'INIT
        # de l'image postgres (serveur temporaire, socket locale), puis le serveur
        # redémarre : les premiers tests tombaient alors sur « server closed the
        # connection unexpectedly ». Un sondage qui n'emprunte pas le chemin du test
        # ne prouve pas que le chemin du test est prêt.
        psycopg = pytest.importorskip("psycopg")
        deadline = time.time() + 60
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=3) as c:
                    c.execute("SELECT 1")
                break
            except Exception:
                if time.time() > deadline:
                    pytest.skip("le PostgreSQL jetable n'est pas devenu prêt")
                time.sleep(1)
        yield PgBox(dsn, name)
    finally:
        guard.remove()
        guard.uninstall()


@pytest.fixture(scope="session")
def pg_dsn(pg_box: PgBox) -> str:
    return pg_box.dsn


@pytest.fixture(scope="module")
def pg_module_dsn(pg_dsn: str) -> Iterator[str]:
    """Une base neuve POUR CE MODULE, détruite à sa sortie — rend son DSN.

    ⚠️ **`pg_dsn` est le SERVEUR, pas un bac à sable.** Sur ce poste il vient de
    `OTO_TEST_PG_DSN`, qui désigne une base fixe (`postgres`) partagée par toutes
    les sessions : un module qui y pose son DDL écrit chez tout le monde. Mesuré
    le 08/09/2026 — quatre-vingts tables de familles étrangères y coexistaient,
    un `DROP TABLE` sans `CASCADE` échouait dès qu'un voisin avait laissé une
    dépendance, **y compris pour un fichier lancé seul** (le résidu survit à la
    fin du run), et un module effaçait EN COURS DE VOL les lignes qu'un autre
    venait d'écrire. Ces rouges-là ne ressemblent pas à un problème de base : ils
    ressemblent à une régression métier, et c'est ce qui coûte cher.

    Le mécanisme est celui, éprouvé, de la fixture `live` des bancs du datastore —
    remonté ici pour que les modules hors `tests/datastore/` en disposent, et pour
    qu'il n'existe qu'un seul exemplaire à corriger. `live` s'appuie désormais
    dessus.

    Le pool de `oto_mcp.db._conn` est **mémoïsé au module Python** : un module qui
    détourne `_database_url` sans neutraliser le pool parlerait à la base d'un
    voisin. Il est donc mis à neuf ici, et l'ancien remis à la sortie — sans quoi
    l'isolement de la base serait vrai et celui du pool faux.
    """
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_test_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    pool_avant = dbconn._pool
    dbconn._pool = None
    try:
        yield pg_dsn.rsplit("/", 1)[0] + "/" + nom
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    """Le schéma réel sur la base neuve du module (`pg_module_dsn`), pointé par
    `DATABASE_URL`.

    Une vraie base plutôt qu'un double : ce qu'on vérifie ici, c'est ce que le
    STOCKAGE porte — un simulacre rendrait ce qu'on lui a appris à rendre.

    ⚠️ **Ce harnais existait en 106 exemplaires recopiés à la main** (mesuré le
    15/09/2026 : 120 fixtures `live` dans la suite, dont 14 seulement s'appuyaient
    sur `pg_module_dsn`), sous 37 formes qui avaient divergé. Chacune refaisait le
    `CREATE DATABASE`, le détournement de `DATABASE_URL` et la remise à neuf du pool
    — soit, à chaque fois, les trois endroits où se tromper. `pg_module_dsn` avait
    justement été remonté ici « pour qu'il n'existe qu'un seul exemplaire à
    corriger » ; le travail s'était arrêté à mi-chemin. Il n'y a plus qu'un
    exemplaire : celui-ci.

    La création et la destruction de la base vivent une étape plus haut, dans
    `pg_module_dsn` : ce harnais-ci n'est plus que le branchement du code sur elle.
    """
    pytest.importorskip("psycopg")

    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
