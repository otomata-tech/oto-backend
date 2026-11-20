"""Banc des GESTES du déploiement bleu/vert de NOTRE box (#967).

Ce que ce banc prouve : rendre la bibliothèque bleu/vert générique ne change RIEN à ce
que notre déploiement (`deploy/oto-backend.sh`, la production) fait sur la machine. Il
rejoue chaque scénario réel — bascule dans les deux sens, retour arrière, et
les chemins d'échec (couleur morte au démarrage, `caddy validate` qui refuse, trafic
public en échec, certificat pas encore prêt puis jamais prêt, lanceur figé sur un
chemin) — dans une racine jetable, avec des
doublures qui JOURNALISENT chaque commande au lieu de l'exécuter, puis compare la
trace à une référence enregistrée depuis les scripts d'avant.

Retrait de la préproduction (19/11/2026) : le tronc ne déploie plus, le wrapper de
préproduction `oto-backend-canari.sh` et ses quatre scénarios `canari-*` sont retirés ;
aucun geste de production n'a bougé.

Lot 5 (30/09/2026) : nos deux wrappers déclarent `BG_LANCEUR=versionne`, et les
références de leurs scénarios ont été régénérées — seule la propagation du lanceur
(`cp -a start-encrypted.sh` + `chmod`) a disparu des gestes. Le mode historique
`propage`, gardé jusqu'au lot 5b pour le retour arrière, est rejoué par les scénarios
`prod-lanceur-propage` et `prod-lanceur-fige`, qui réécrivent le wrapper dans ce mode.

oto-backend#932 : l'installation passe par le verrou. Les deux lignes `[pip] … install -e .`
et `… --force-reinstall oto-core…`, et le log « oto-core requis par le manifeste »,
sont remplacés par la ligne `uv sync --frozen --quiet` (doublure `uv`) et le log
« installé par le verrou : uv.lock <sha12> » ; aucun autre geste n'a bougé. Le scénario
`prod-tag-sans-verrou` rejoue un tag antérieur au verrou : refus nommé, rien d'installé.

Vidange sans coupure (19/11/2026) : une montée ne coupe plus une vidange en cours, elle
l'ATTEND (`systemctl is-active --quiet <vidange>`, au lieu de `systemctl stop` de la
vidange) — `prod-montee-pendant-vidange`, et `prod-vidange-sans-fin` pour la borne ;
une couleur qui a servi avant une rebascule est VIDÉE, plus arrêtée net
(`prod-public-ko-rebascule`) ; le plafond passe de 120 à 240 s.

Bac à sable des fonctions (#1202) : après `uv sync`, `bg_install` pose le bac à sable
dans l'arbre de la couleur (`.venv/bin/python scripts/installer_bac_a_sable.py
<arbre>/.bac-a-sable`, doublure `PYTHON_DU_VENV`) ; chaque scénario qui installe gagne
cet appel et le log « bac à sable posé », et l'état final les deux installateurs. Deux
scénarios neufs : `prod-bac-a-sable-ko` (la pose échoue : rien ne démarre, l'ancienne
couleur reste) et `prod-tag-sans-bac-a-sable` (un tag sans installateur : les gestes
d'avant, à l'identique).

Retrait de la façade d'enregistrement (inscription des clients MCP par CIMD, chez Logto) :
la santé lit `/.well-known/oauth-protected-resource`, que le serveur monte sans
interrupteur, au lieu de la métadonnée d'autorisation que seule la façade servait. Les
références n'ont changé que de ce chemin, dans le `curl` local et dans `BG_PUBLIC`.

La trace contient : chaque commande externe et ses arguments, dans l'ordre ; ce que
le script écrit sur sa sortie ; son code de sortie ; et l'état final des fichiers
qu'il écrit (amont Caddy, pointeur de couleur, coordonnée de version, unités posées).
Deux traces égales = les mêmes gestes, au caractère près.

Les chemins absolus des scripts (`/opt/`, `/etc/`, `/var/lock/`, `/run/lock/`) sont réécrits vers
la racine jetable AVANT exécution, puis la racine est retirée de la trace : les
chemins y apparaissent donc tels qu'ils sont sur la box.

Régénérer la référence (seulement quand un geste de notre box DOIT changer — c'est
alors une décision, qui se relit dans le diff des fichiers de référence) :

    python tests/deploy/_banc_bleu_vert.py --ecrire <dossier deploy/ de référence>
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ICI = pathlib.Path(__file__).resolve().parent
REFERENCES = ICI / "gestes_bleu_vert"
DEPLOY_DU_DEPOT = ICI.parents[1] / "deploy"

SHA = "0123456789abcdef0123456789abcdef01234567"
TAG = "v1.2.3"

# Scripts de notre box, sources de la réécriture de chemins.
SCRIPTS = ("oto-mcp-bluegreen.sh", "oto-backend.sh", "oto-mcp-drain.sh")

# nom -> (script, argument, environnement (prod), couleur active, variantes)
SCENARIOS: dict[str, tuple[str, list[str], str, str, dict[str, str]]] = {
    "prod-bleu-vers-vert": ("oto-backend.sh", [TAG], "prod", "blue", {}),
    "prod-vert-vers-bleu": ("oto-backend.sh", [TAG], "prod", "green", {}),
    "prod-retour-arriere": ("oto-backend.sh", ["--rollback"], "prod", "green", {}),
    "prod-sans-argument": ("oto-backend.sh", [], "prod", "blue", {}),
    "prod-public-ko-rebascule": ("oto-backend.sh", [TAG], "prod", "blue",
                                 {"BANC_PUBLIC_KO": "1"}),
    # Nom d'hôte neuf : Caddy obtient le certificat après le reload, le public ne
    # répond pas (000) aux deux premiers essais puis répond — la bascule tient.
    "prod-certificat-pas-pret": ("oto-backend.sh", [TAG], "prod", "blue",
                                 {"BANC_PUBLIC_SANS_REPONSE": "2"}),
    # Toujours aucune réponse au plafond : échec net, rebascule comme avant.
    "prod-certificat-jamais-pret": ("oto-backend.sh", [TAG], "prod", "blue",
                                    {"BANC_PUBLIC_SANS_REPONSE": "99"}),
    "prod-couleur-morte": ("oto-backend.sh", [TAG], "prod", "blue",
                           {"BANC_DEMARRAGE_KO": "1"}),
    "prod-caddy-refuse": ("oto-backend.sh", [TAG], "prod", "blue",
                          {"BANC_CADDY_KO": "1"}),
    "prod-lanceur-fige": ("oto-backend.sh", [TAG], "prod", "blue",
                          {"BANC_LANCEUR_PROPAGE": "1", "BANC_LANCEUR_FIGE": "1"}),
    "prod-tag-sans-maintenance": ("oto-backend.sh", [TAG], "prod", "blue",
                                  {"BANC_SANS_MAINTENANCE": "1"}),
    # Un tag antérieur à #932 ne porte pas de `uv.lock` : refusé, rien n'est installé.
    "prod-tag-sans-verrou": ("oto-backend.sh", [TAG], "prod", "blue",
                             {"BANC_SANS_VERROU": "1"}),
    # Le mode historique `BG_LANCEUR=propage` (retour arrière du lot 5, retiré au 5b) :
    # le lanceur hors git passe de la couleur en service à la nouvelle.
    "prod-lanceur-propage": ("oto-backend.sh", [TAG], "prod", "blue",
                             {"BANC_LANCEUR_PROPAGE": "1"}),
    # La pose du bac à sable des fonctions échoue (réseau, empreinte, contrôle) : la
    # couleur n'est pas démarrée, l'ancienne reste en service.
    "prod-bac-a-sable-ko": ("oto-backend.sh", [TAG], "prod", "blue",
                            {"BANC_BAC_A_SABLE_KO": "1"}),
    # Un tag antérieur à la pose du bac à sable ne porte pas son installateur : rien
    # n'est posé, l'installation continue comme avant.
    "prod-tag-sans-bac-a-sable": ("oto-backend.sh", [TAG], "prod", "blue",
                                  {"BANC_SANS_BAC_A_SABLE": "1"}),
    # Deux montées rapprochées : la vidange de la précédente tourne encore. La montée
    # attend qu'elle finisse, sans arrêter ni la vidange ni sa couleur, puis continue.
    "prod-montee-pendant-vidange": ("oto-backend.sh", [TAG], "prod", "blue",
                                    {"BANC_VIDANGE_EN_COURS": "3"}),
    # La vidange dure au-delà de sa plus longue vie possible : refus nommé, rien n'est
    # arrêté ni installé, la couleur en service n'a pas bougé.
    "prod-vidange-sans-fin": ("oto-backend.sh", [TAG], "prod", "blue",
                              {"BANC_VIDANGE_EN_COURS": "sans-fin"}),
    "vidange": ("oto-mcp-drain.sh", ["oto-mcp", "blue", "9103", "120"], "prod", "green", {}),
}

ARBRES = {"prod": "/opt/oto-mcp"}

# Doublures : chacune journalise `nom args…` dans $BANC_TRACE, puis se comporte comme
# le scénario le demande. Les commandes de fichiers (cp, chmod, install) s'exécutent
# réellement après journalisation : leur effet fait partie de l'état final comparé.
DOUBLURES = {
    # Une vidange (unité `*-drain-*`) est active aux BANC_VIDANGE_EN_COURS premières
    # questions (« sans-fin » : toujours), puis finie — par défaut, aucune ne tourne.
    "systemctl": r'''
case "$1 $3" in
  "is-active "*-drain-*)
    [ "${BANC_VIDANGE_EN_COURS:-0}" = sans-fin ] && exit 0
    [ "$(grep -c '^systemctl is-active --quiet .*-drain-' "$BANC_TRACE")" -le "${BANC_VIDANGE_EN_COURS:-0}" ] && exit 0
    exit 3 ;;
esac
case "$1" in
  is-active) [ -n "${BANC_DEMARRAGE_KO:-}" ] && exit 3; exit 0 ;;
  show) echo "Sun 2026-10-04 03:20:00 CEST" ;;
esac
exit 0''',
    "curl": r'''
url=""; for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
case "$url" in
  *tls-check*) printf 404 ;;
  http://127.0.0.1:*) : ;;
  https://*)
    if [ -n "${BANC_PUBLIC_KO:-}" ]; then printf 503; exit 22; fi
    # Les BANC_PUBLIC_SANS_REPONSE premiers appels publics (celui-ci compris, déjà
    # journalisé) n'obtiennent aucune réponse HTTP : poignée TLS refusée, code 000.
    if [ "$(grep -c '^curl .*https://' "$BANC_TRACE")" -le "${BANC_PUBLIC_SANS_REPONSE:-0}" ]; then
      printf 000; exit 35
    fi
    printf 200 ;;
esac
exit 0''',
    "git": r'''
echo "  (dans $(pwd))" >> "$BANC_TRACE"
for a in "$@"; do
  case "$a" in
    --short) echo "${BANC_SHA:0:7}"; exit 0 ;;
    HEAD) echo "$BANC_SHA"; exit 0 ;;
  esac
done
exit 0''',
    "caddy": r'''[ -n "${BANC_CADDY_KO:-}" ] && exit 1; exit 0''',
    # Comme le vrai `uv sync --frozen`, refuse un arbre sans verrou.
    "uv": r'''
echo "  (dans $(pwd) ; UV_PYTHON_DOWNLOADS=${UV_PYTHON_DOWNLOADS-<absent>})" >> "$BANC_TRACE"
[ -f uv.lock ] || { echo "uv : pas de uv.lock dans $(pwd)" >&2; exit 2; }
exit 0''',
    "ss": "exit 0",
    "systemd-run": "exit 0",
    "journalctl": "exit 0",
    "flock": "exit 0",
    "sleep": "exit 0",
    "date": r'''
case "$1" in
  -Is) echo "2026-09-29T12:00:00+02:00" ;;
  -u) echo "10:00:00Z" ;;
  *) echo "12:00:00" ;;
esac''',
    "cp": 'exec /bin/cp "$@"',
    "chmod": 'exec /bin/chmod "$@"',
    "install": 'exec /usr/bin/install "$@"',
}

# Le verrou du tag : lu par `uv sync`, et attesté dans la sortie par son empreinte.
VERROU_NOM = "uv.lock"
VERROU = "# uv.lock du banc\nversion = 1\n"

# L'interpréteur du venv de la couleur : appelé par son chemin (jamais par le PATH),
# il est posé dans chaque arbre et journalise comme une doublure.
PYTHON_DU_VENV = ('#!/bin/bash\necho "python $*" >> "$BANC_TRACE"\n'
                  'echo "  (dans $(pwd))" >> "$BANC_TRACE"\n'
                  '[ -n "${BANC_BAC_A_SABLE_KO:-}" ] && exit 1\nexit 0\n')

LANCEUR = '#!/bin/bash\nexec "$(cd "$(dirname "$0")" && pwd)/.venv/bin/oto-mcp"\n'
LANCEUR_FIGE = "#!/bin/bash\nexec /opt/oto-mcp-blue/.venv/bin/oto-mcp\n"

PYPROJECT = ('dependencies = [\n'
             '    "oto-core[anonymize] @ git+https://github.com/otomata-tech/oto-core.git@v9.9.9",\n'
             ']\n')


def _reecrire(texte: str, racine: str, variantes: dict[str, str] | None = None) -> str:
    for prefixe in ("/opt/", "/etc/", "/var/lock/", "/run/lock/"):
        texte = texte.replace(prefixe, racine + prefixe)
    if (variantes or {}).get("BANC_LANCEUR_PROPAGE"):
        texte = re.sub(r"(?m)^BG_LANCEUR=versionne$", "BG_LANCEUR=propage", texte)
    return texte


def _preparer(racine: pathlib.Path, deploy: pathlib.Path, env: str, active: str,
              variantes: dict[str, str]) -> pathlib.Path:
    r = str(racine)
    (racine / "opt/deploy").mkdir(parents=True)
    for nom in SCRIPTS:
        cible = racine / "opt/deploy" / nom
        cible.write_text(_reecrire((deploy / nom).read_text(encoding="utf-8"), r, variantes),
                         encoding="utf-8")
        cible.chmod(0o755)
    for d in ("etc/oto-mcp", "etc/caddy", "etc/systemd/system", "var/lock", "run/lock"):
        (racine / d).mkdir(parents=True)
    (racine / "etc/caddy/Caddyfile").write_text("# Caddyfile du banc\n")
    (racine / f"etc/oto-mcp/active-{env}").write_text(active + "\n")
    (racine / f"etc/caddy/upstream-oto-{env}.conf").write_text(
        "# amont d'avant la bascule\n")
    for couleur in ("blue", "green"):
        arbre = pathlib.Path(r + ARBRES[env] + "-" + couleur)
        (arbre / ".venv/bin").mkdir(parents=True)
        python = arbre / ".venv/bin/python"
        python.write_text(PYTHON_DU_VENV)
        python.chmod(0o755)
        if not variantes.get("BANC_SANS_BAC_A_SABLE"):
            (arbre / "scripts").mkdir()
            (arbre / "scripts/installer_bac_a_sable.py").write_text("# installateur du tag\n")
        (arbre / "pyproject.toml").write_text(PYPROJECT)
        if not variantes.get("BANC_SANS_VERROU"):
            (arbre / VERROU_NOM).write_text(VERROU)
        (arbre / "deploy").mkdir()
        if variantes.get("BANC_LANCEUR_PROPAGE"):
            lanceur = arbre / "start-encrypted.sh"
            lanceur.write_text(LANCEUR_FIGE if variantes.get("BANC_LANCEUR_FIGE") else LANCEUR)
            lanceur.chmod(0o755)
        else:
            # Le lanceur est DANS le tag ; rien n'est propagé d'une couleur à l'autre.
            (arbre / "deploy" / "lanceur_secrets.py").write_text("# lanceur du tag\n")
        if not variantes.get("BANC_SANS_MAINTENANCE"):
            for u in ("oto-mcp-maintenance.service", "oto-mcp-maintenance.timer",
                      "oto-journal-archive.service", "oto-journal-archive.timer"):
                (arbre / "deploy" / u).write_text(f"# {u} de la couleur {couleur}\n")
    doublures = racine / "banc-bin"
    doublures.mkdir()
    for nom, corps in DOUBLURES.items():
        f = doublures / nom
        f.write_text('#!/bin/bash\necho "' + nom + ' $*" >> "$BANC_TRACE"\n' + corps + "\n")
        f.chmod(0o755)
    return doublures


def _etat(racine: pathlib.Path) -> list[str]:
    lignes = []
    # Le verrou est une ENTRÉE du tag que le script ne fait que lire : son empreinte
    # est déjà dans la sortie (« installé par le verrou : uv.lock <sha12> »).
    for rel in sorted(p.relative_to(racine).as_posix() for p in racine.rglob("*")
                      if p.is_file() and not p.relative_to(racine).as_posix()
                      .startswith(("banc-bin/", "opt/deploy/", "trace"))
                      and "/.venv/" not in "/" + p.relative_to(racine).as_posix()
                      and p.name != VERROU_NOM):
        lignes.append(f"--- /{rel}")
        lignes.extend((racine / rel).read_text(encoding="utf-8").splitlines())
    return lignes


def rejouer(deploy: pathlib.Path, scenario: str) -> str:
    script, args, env, active, variantes = SCENARIOS[scenario]
    with tempfile.TemporaryDirectory(prefix="banc-bv-") as tmp:
        racine = pathlib.Path(tmp)
        doublures = _preparer(racine, deploy, env, active, variantes)
        trace = racine / "trace"
        trace.write_text("")
        environnement = {
            "PATH": f"{doublures}:/usr/bin:/bin",
            "BANC_TRACE": str(trace),
            "BANC_SHA": SHA,
            "LANG": "C.UTF-8",
            **variantes,
        }
        fini = subprocess.run(["/bin/bash", str(racine / "opt/deploy" / script), *args],
                              env=environnement, cwd=str(racine),
                              capture_output=True, text=True, timeout=60)
        sortie = [f"### scénario {scenario} : {script} {' '.join(args)}",
                  f"### code de sortie : {fini.returncode}",
                  "### commandes", *trace.read_text().splitlines(),
                  "### sortie", *fini.stdout.splitlines(),
                  "### erreurs", *fini.stderr.splitlines(),
                  "### état final", *_etat(racine)]
        texte = "\n".join(sortie).replace(str(racine), "") + "\n"
    # Les durées mesurées (`SECONDS`) dépendent de la machine ; rien d'autre ne varie.
    return re.sub(r"(?<=en )\d+s\b|(?<=démarrage )\d+s\b|(?<=après )\d+s\b", "Ns", texte)


def reference(scenario: str) -> str:
    return (REFERENCES / f"{scenario}.txt").read_text(encoding="utf-8")


def _ecrire(deploy: pathlib.Path) -> None:
    if REFERENCES.exists():
        shutil.rmtree(REFERENCES)
    REFERENCES.mkdir()
    for scenario in SCENARIOS:
        (REFERENCES / f"{scenario}.txt").write_text(rejouer(deploy, scenario),
                                                     encoding="utf-8")
    print(f"{len(SCENARIOS)} références écrites depuis {deploy}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--ecrire":
        sys.exit("usage : _banc_bleu_vert.py --ecrire <dossier deploy/>")
    _ecrire(pathlib.Path(sys.argv[2]).resolve())
