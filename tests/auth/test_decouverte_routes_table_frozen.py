"""CLIQUET de la face OAuth : la table de routes SERVIE par la découverte et l'inscription
dynamique == la table attendue.

Pourquoi. La table `tests/api/api_routes_table.txt` ne couvre que `/api/*`. Les routes de
la découverte (RFC 9728) sont un contrat avec des clients qui gardent ce qu'ils ont lu : un
chemin annoncé puis retiré casse un client installé, sans qu'aucun autre test ne rougisse. Même discipline que la
table REST : un chemin retiré, une méthode perdue, un ordre changé ou un chemin AJOUTÉ sans
régénérer le fichier attendu font rouge.

Régénérer après un changement VOULU :

    python - <<'PY'
    import pathlib
    from oto_mcp.auth import decouverte, inscription_dynamique
    lignes = [f"{','.join(sorted(r.methods or []))} {r.path} -> {r.name}"
              for r in [*decouverte.make_routes("https://mcp.example.test"),
                        *inscription_dynamique.make_routes()]]
    pathlib.Path("tests/auth/decouverte_routes_table.txt").write_text("\\n".join(lignes) + "\\n")
    PY
"""
from __future__ import annotations

import pathlib

from oto_mcp.auth import decouverte, inscription_dynamique

ATTENDU = pathlib.Path(__file__).resolve().parent / "decouverte_routes_table.txt"


def _servie() -> list[str]:
    return [f"{','.join(sorted(getattr(r, 'methods', None) or []))} {r.path} -> {r.name}"
            for r in [*decouverte.make_routes("https://mcp.example.test"),
                      *inscription_dynamique.make_routes()]]


def test_table_des_routes_de_la_decouverte_figee():
    servie = _servie()
    attendue = ATTENDU.read_text(encoding="utf-8").splitlines()
    manquantes = [l for l in attendue if l not in servie]
    ajoutees = [l for l in servie if l not in attendue]
    assert not manquantes, (
        f"{len(manquantes)} route(s) de la découverte ne sont PLUS servies : {manquantes}\n"
        "Un client OAuth garde la métadonnée qu'il a lue : retirer un chemin qu'elle "
        "annonçait le casse. Si le retrait est voulu, retire la ligne de "
        "`tests/auth/decouverte_routes_table.txt` DANS LE MÊME commit et dis-le dans la PR.")
    assert not ajoutees, (
        f"{len(ajoutees)} route(s) NEUVES non déclarées : {ajoutees}\n"
        "Régénère `tests/auth/decouverte_routes_table.txt` (recette dans le docstring) : le diff "
        "nomme le chemin, la revue le voit.")
    assert servie == attendue, "L'ORDRE de la table a changé (Starlette prend le PREMIER match)."
