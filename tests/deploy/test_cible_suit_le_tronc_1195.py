"""Une instance cible suit le tronc (oto-backend#1195) : la montée joue les migrations, le
retour arrière vérifie la base.

Une cible doit pouvoir monter chaque tag sans geste humain. Jusqu'ici, une base en retard
sur le tag faisait refuser la montée, et la migration se jouait à la main en root : une
montée quotidienne se serait arrêtée à chaque tag porteur d'une migration. Désormais :

- `migrations_a_jour.py --migrer` migre une base EN RETARD sur une chaîne LINÉAIRE du
  registre du tag (`oto-mcp migrer upgrade head`, depuis l'arbre de la couleur inactive),
  la relit, et dit « de → vers » ; les autres refus restent (révision inconnue, plusieurs
  têtes, squash, base illisible, et désormais une fusion de files sur le chemin) ;
- `migrations_a_jour.py --retour` refuse de rebasculer sur un code qui ne connaît pas la
  révision de la base ;
- `deployer.sh` branche l'un avant le démarrage d'une montée, l'autre avant celui d'un
  retour.

Deux étages, comme #1163 : le script sur un VRAI registre Alembic jetable, l'état de la
base dicté ; la chaîne en root simulé (`_banc_cible.py`).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import textwrap

import pytest

from _banc_cible import DEPOT, Banc

_spec = importlib.util.spec_from_file_location(
    "migrations_a_jour_1195", DEPOT / "deploy" / "cible" / "migrations_a_jour.py")
garde = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(garde)

NEUVE, VERSIONNEE = garde.EtatRegistre.NEUVE, garde.EtatRegistre.VERSIONNEE


def _registre(tmp_path, revisions):
    """Un registre Alembic jetable : (révision, précédente(s)) par fichier."""
    registre = tmp_path / "migrations"
    (registre / "versions").mkdir(parents=True)
    (registre / "script.py.mako").write_text("")
    (registre / "env.py").write_text("")
    for rev, prec in revisions:
        (registre / "versions" / f"{rev}.py").write_text(textwrap.dedent(f'''\
            """{rev}"""
            revision = {rev!r}
            down_revision = {prec!r}
            branch_labels = None
            depends_on = None
            '''))
    return registre


@pytest.fixture
def lineaire(tmp_path):
    return _registre(tmp_path, [("0031", None), ("0035", "0031"), ("0037", "0035")])


@pytest.fixture
def fusion(tmp_path):
    """Deux files nées de 0031, réunies en 0037 : une seule tête, mais pas une chaîne."""
    return _registre(tmp_path, [("0031", None), ("0035a", "0031"), ("0035b", "0031"),
                                ("0037", ("0035a", "0035b"))])


class _Base:
    """La base dictée : ses révisions successives, une par lecture."""

    def __init__(self, *lectures):
        self.lectures = list(lectures)
        self.lues = 0

    def __call__(self, dsn):
        etat, versions = self.lectures[min(self.lues, len(self.lectures) - 1)]
        self.lues += 1
        if isinstance(etat, Exception):
            raise etat
        return etat, list(versions)


class _Migration:
    def __init__(self, echec: str | None = None):
        self.appels, self.echec = 0, echec

    def __call__(self):
        self.appels += 1
        if self.echec:
            raise garde.Refus(self.echec)


def _main(registre, monkeypatch, base, *argv, migration=None):
    monkeypatch.setenv("DATABASE_URL", "postgresql://exemple.invalid/base")
    migration = migration or _Migration()
    return garde.main(list(argv), lire=base, registre=registre, migrer=migration), migration


# --- la montée migre une chaîne linéaire ------------------------------------------------

def test_une_base_en_retard_lineaire_est_migree_puis_relue(lineaire, monkeypatch, capsys):
    base = _Base((VERSIONNEE, ["0031"]), (VERSIONNEE, ["0037"]))
    code, migration = _main(lineaire, monkeypatch, base, "--migrer")
    assert code == 0 and migration.appels == 1 and base.lues == 2
    sortie = capsys.readouterr()
    assert "chaîne linéaire : 0035 → 0037" in sortie.err
    assert ("on joue `migrer upgrade head` depuis l'arbre du tag (0031 → 0037), après "
            "`maintenance copies-du-coffre`") in sortie.out
    assert ("un index CONCURRENTLY se construit jusqu'à 10_000_000 lignes (défaut 100_000)"
            in sortie.out)
    assert "migrations : base migrée de 0031 vers 0037" in sortie.out


@pytest.mark.parametrize("etat, versions, motif", [
    (VERSIONNEE, ["0037"], "base à la tête du tag (0037)"),
    (NEUVE, [], "base neuve"),
])
def test_une_base_a_jour_ou_neuve_n_est_pas_migree(lineaire, monkeypatch, capsys,
                                                    etat, versions, motif):
    code, migration = _main(lineaire, monkeypatch, _Base((etat, versions)), "--migrer")
    assert code == 0 and migration.appels == 0
    assert motif in capsys.readouterr().out


def test_une_revision_inconnue_refuse_sans_migrer(lineaire, monkeypatch, capsys):
    code, migration = _main(lineaire, monkeypatch, _Base((VERSIONNEE, ["0042"])), "--migrer")
    assert code == 1 and migration.appels == 0
    assert "0042, révision inconnue du registre du tag" in capsys.readouterr().err


def test_un_squash_refuse_toujours_sans_migrer(lineaire, monkeypatch, capsys):
    retiree = garde.Refus("révision 0039_feed_synced_at_retiree antérieure à la référence "
                          "0041_recherche_valeurs_servies (squash du 06/10/2026)")
    code, migration = _main(lineaire, monkeypatch, _Base((retiree, [])), "--migrer")
    assert code == 1 and migration.appels == 0
    assert "REFUS — révision 0039_feed_synced_at_retiree antérieure" in capsys.readouterr().err


def test_un_squash_lu_en_base_refuse_sans_migrer(lineaire, monkeypatch, capsys):
    """Le même refus, par la vraie lecture : `constater` lève sur une révision retirée."""
    class _Dictee:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, *params):
            if "pg_class" in sql:
                lignes = [{"relname": "alembic_version"}, {"relname": "orgs"}]
            elif "FROM alembic_version" in sql:
                lignes = [{"version_num": "0039_feed_synced_at_retiree"}]
            else:
                lignes = []
            return type("Curseur", (), {"fetchall": lambda _: lignes})()

    monkeypatch.setattr(garde.psycopg, "connect", lambda *a, **k: _Dictee())
    code, migration = _main(lineaire, monkeypatch, garde.lire_base, "--migrer")
    assert code == 1 and migration.appels == 0
    assert "(squash du 06/10/2026)" in capsys.readouterr().err


def test_plusieurs_tetes_refusent_sans_lire_ni_migrer(tmp_path, monkeypatch, capsys):
    registre = _registre(tmp_path, [("0031", None), ("0036", "0031"), ("0037", "0031")])
    base = _Base((VERSIONNEE, ["0031"]))
    code, migration = _main(registre, monkeypatch, base, "--migrer")
    assert code == 1 and migration.appels == 0 and base.lues == 0
    assert "2 têtes (0036, 0037)" in capsys.readouterr().err


@pytest.mark.parametrize("courante", ["0031", "0035a"])
def test_une_fusion_sur_le_chemin_refuse_sans_migrer(fusion, monkeypatch, capsys, courante):
    code, migration = _main(fusion, monkeypatch, _Base((VERSIONNEE, [courante])), "--migrer")
    assert code == 1 and migration.appels == 0
    assert f"chaîne non linéaire entre {courante} et 0037" in capsys.readouterr().err


def test_le_constat_seul_ne_migre_jamais(lineaire, monkeypatch, capsys):
    code, migration = _main(lineaire, monkeypatch, _Base((VERSIONNEE, ["0031"])))
    assert code == garde.EN_RETARD == 3 and migration.appels == 0
    assert "chaîne linéaire : 0035 → 0037" in capsys.readouterr().err


def test_une_migration_en_echec_refuse_en_le_nommant(lineaire, monkeypatch, capsys):
    base = _Base((VERSIONNEE, ["0031"]))
    code, migration = _main(lineaire, monkeypatch, base, "--migrer",
                            migration=_Migration("`migrer upgrade head` a échoué (code 1)"))
    assert code == 1 and migration.appels == 1 and base.lues == 1
    assert "migrations : REFUS — `migrer upgrade head` a échoué (code 1)" in capsys.readouterr().err


def test_une_base_pas_a_la_tete_apres_migration_refuse(lineaire, monkeypatch, capsys):
    base = _Base((VERSIONNEE, ["0031"]), (VERSIONNEE, ["0035"]))
    code, _ = _main(lineaire, monkeypatch, base, "--migrer")
    assert code == 1
    assert ("après `migrer upgrade head`, la base n'est pas à la tête du tag : migrations : "
            "la base est en 0035, le tag attend 0037") in capsys.readouterr().err


def test_un_argument_inconnu_refuse(lineaire, monkeypatch, capsys):
    code, migration = _main(lineaire, monkeypatch, _Base((VERSIONNEE, ["0031"])), "--forcer")
    assert code == 1 and migration.appels == 0
    assert "usage" in capsys.readouterr().err


def test_la_migration_part_de_l_arbre_du_tag_apres_les_copies_du_coffre(monkeypatch):
    """Les copies du coffre des connecteurs renommés, PUIS la migration : une révision de
    renommage repointe vers ces copies, que notre instance fait au démarrage — ici, la
    migration précède le démarrage."""
    lances = []

    def lancer(commande, timeout, env):
        lances.append((commande, timeout, env))
        return subprocess.CompletedProcess(commande, 0)
    monkeypatch.setattr(garde.subprocess, "run", lancer)
    monkeypatch.setenv("DATABASE_URL", "postgresql://exemple.invalid/base")
    monkeypatch.delenv(garde.index_concurrent.PLAFOND_ENV, raising=False)
    garde.migrer_la_base()
    (copies, delai_copies, env_copies), (commande, delai, env) = lances
    oto_mcp = str(garde.ARBRE / ".venv" / "bin" / "oto-mcp")
    assert copies == [oto_mcp, "maintenance", "copies-du-coffre"]
    assert commande == [oto_mcp, "migrer", "upgrade", "head"]
    assert delai == delai_copies == garde.DELAI_MIGRATION_S
    # Le plafond relevé des index CONCURRENTLY : sur CETTE commande, par-dessus
    # l'environnement du lanceur (`DATABASE_URL` comprise) — jamais dans celui de la porte.
    assert env[garde.index_concurrent.PLAFOND_ENV] == str(garde.PLAFOND_INDEX_LIGNES)
    assert garde.index_concurrent.PLAFOND_ENV not in env_copies
    assert env["DATABASE_URL"] == env_copies["DATABASE_URL"] == "postgresql://exemple.invalid/base"
    assert garde.index_concurrent.PLAFOND_ENV not in os.environ


def test_des_copies_en_echec_refusent_sans_migrer(monkeypatch):
    lances = []

    def lancer(commande, timeout, env):
        lances.append(commande[1:])
        return subprocess.CompletedProcess(commande, 2)
    monkeypatch.setattr(garde.subprocess, "run", lancer)
    with pytest.raises(garde.Refus, match=r"`maintenance copies-du-coffre` a échoué "
                                          r"\(code 2.*rien n'est migré"):
        garde.migrer_la_base()
    assert lances == [["maintenance", "copies-du-coffre"]]


def test_le_plafond_releve_tient_dans_la_fenetre_de_migration():
    """172 s par index pour ~12 M lignes (prod, 04/10/2026), trois index pour 0049 :
    le plafond doit laisser la construction sous `DELAI_MIGRATION_S`."""
    secondes_par_ligne = 172 / 12_000_000
    assert 3 * garde.PLAFOND_INDEX_LIGNES * secondes_par_ligne < garde.DELAI_MIGRATION_S
    assert garde.PLAFOND_INDEX_LIGNES > garde.index_concurrent.CONSTRUCTION_MAX_LIGNES


@pytest.mark.parametrize("issue, motif", [
    (subprocess.CompletedProcess([], 2), r"a échoué \(code 2"),
    (subprocess.TimeoutExpired([], 1), "n'a pas fini en"),
])
def test_une_migration_qui_echoue_ou_pend_est_un_refus(monkeypatch, issue, motif):
    def lancer(commande, timeout, env):
        if isinstance(issue, Exception):
            raise issue
        return issue
    monkeypatch.setattr(garde.subprocess, "run", lancer)
    with pytest.raises(garde.Refus, match=motif):
        garde.migrer_la_base()


# --- le retour arrière vérifie la base --------------------------------------------------

def test_un_retour_sur_une_base_a_la_tete_passe(lineaire, monkeypatch, capsys):
    code, migration = _main(lineaire, monkeypatch, _Base((VERSIONNEE, ["0037"])), "--retour")
    assert code == 0 and migration.appels == 0
    assert "retour : base en 0037" in capsys.readouterr().out


def test_un_retour_sur_une_base_en_avance_refuse(lineaire, monkeypatch, capsys):
    """La montée a migré la base en 0041 ; le code d'avant s'arrête à 0037."""
    code, migration = _main(lineaire, monkeypatch, _Base((VERSIONNEE, ["0041"])), "--retour")
    assert code == 1 and migration.appels == 0
    err = capsys.readouterr().err
    assert "la base est en 0041, révision INCONNUE du code de la couleur précédente" in err
    assert "corriger vers l'avant" in err


@pytest.mark.parametrize("etat, versions, motif", [
    (VERSIONNEE, ["0035"], "EN RETARD sur le code de la couleur précédente"),
    (NEUVE, [], "la base est neuve"),
    (VERSIONNEE, ["0035", "0037"], "porte 2 révision(s)"),
])
def test_un_retour_refuse_tout_autre_etat(lineaire, monkeypatch, capsys, etat, versions, motif):
    code, _ = _main(lineaire, monkeypatch, _Base((etat, versions)), "--retour")
    assert code == 1
    assert motif in capsys.readouterr().err


def test_le_lanceur_accepte_les_deux_modes():
    spec = importlib.util.spec_from_file_location("lanceur_secrets_1195",
                                                  DEPOT / "deploy" / "lanceur_secrets.py")
    lanceur = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lanceur)
    for mode in ("--migrer", "--retour"):
        commande = lanceur.cible(["--script", "deploy/cible/migrations_a_jour.py", mode])
        assert commande[-2].endswith("/deploy/cible/migrations_a_jour.py")
        assert commande[-1] == mode


def test_le_registre_du_depot_est_lineaire_depuis_la_reference():
    """La référence du dernier squash est la plus vieille révision qu'une base acceptée
    puisse porter : de là à la tête, le registre du dépôt doit être une chaîne, sinon une
    montée automatique refuserait toute base en retard."""
    script, tete, _ = garde._registre(garde._REGISTRE)
    from oto_mcp.db._version_alembic import REFERENCE
    chemin = garde.chemin_lineaire(script, REFERENCE, tete)
    assert REFERENCE == tete or chemin[-1] == tete


# --- la chaîne ---------------------------------------------------------------------------

def _appels(banc, mode):
    return [c for c in banc.commandes() if c.startswith("systemd-run ")
            and f"--script deploy/cible/migrations_a_jour.py {mode}" in c]


@pytest.fixture
def banc(tmp_path):
    return Banc(tmp_path)


def test_la_montee_appelle_la_garde_qui_migre(banc):
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3",
                       BANC_MIGRATIONS_DIT="migrations : base migrée de 0031 vers 0037")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert "base migrée de 0031 vers 0037" in fini.stderr
    appel, = _appels(banc, "--migrer")
    cmds = banc.commandes()
    assert cmds.index(appel) < cmds.index("systemctl start exemple-prod@green")


def test_le_retour_verifie_la_base_dans_l_arbre_de_la_couleur_precedente(banc):
    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3").returncode == 0
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v1.2.3")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    appel, = _appels(banc, "--retour")
    assert "-p WorkingDirectory=/opt/exemple/prod-blue " in appel
    assert "/opt/exemple/prod-blue/deploy/lanceur_secrets.py --script" in appel
    cmds = banc.commandes()
    assert cmds.index(appel) < cmds.index("systemctl start exemple-prod@blue")
    assert not _appels(banc, "--migrer")
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"


def test_un_retour_sur_une_base_en_avance_ne_rebascule_pas(banc):
    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3").returncode == 0
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v1.2.3", BANC_RETOUR_CODE="1",
                       BANC_RETOUR_DIT="retour : REFUS — la base est en 0041, révision INCONNUE")
    assert fini.returncode == 1
    sortie = fini.stdout + fini.stderr
    assert "révision INCONNUE" in sortie and "REFUS du retour sur prod" in sortie
    assert "rien n'a basculé" in sortie
    cmds = banc.commandes()
    assert _appels(banc, "--retour")
    assert not [c for c in cmds if c.startswith("systemctl start ")]
    assert not [c for c in cmds if c.startswith("caddy ")]
    assert banc.lire("/etc/exemple/prod/active") == "green\n"
