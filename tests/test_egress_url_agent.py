"""Une URL choisie par l'agent sort par la MÊME garde que les connecteurs (oto#180).

Le fait, mesuré le 12/09/2026 : depuis un run, la seule façon d'atteindre une API
ouverte sans instance déclarée est `web_read` (le connecteur `api` exige une
`base_url` posée par un administrateur ; `api_get` refuse sans elle). Ce chemin
sortait par une garde À PART, écrite avant la garde d'egress du 06/09
(`oto_mcp/egress.py`) : `not is_global` seul, sans le mot de la plage refusée, et
qui laissait passer une plage que l'autre refuse (`is_reserved`, dont le préfixe
NAT64). `file_source` (une source `url` de fichier) portait une troisième copie.

Décision d'Alexis (12/09/2026) : passer par la garde, toujours — un hôte interdit
est refusé en le nommant. Une seule couture, deux politiques NOMMÉES : la
destination d'un credential (posée par un admin) peut être une exception déclarée
par l'opérateur ; une URL tapée dans une conversation, jamais — sinon déclarer un
pont pour une organisation l'ouvrirait à toute lecture de page de n'importe quelle
autre.

⚠️ La règle qui classe une adresse (`internal_reason`) n'est PAS touchée ici :
sur un poste en DNS64 elle refuse le préfixe NAT64 — c'est une décision à part.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from oto_mcp import egress, file_source as fs
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import web as W

_RACINE = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"


def _resolution(**table):
    def faux(hote, port):  # noqa: ARG001
        return set(table[hote])
    return faux


# ── 1. même décision, deux lecteurs ──────────────────────────────────────────

@pytest.mark.parametrize("url,plage", [
    ("http://127.0.0.1:9103/admin", "boucle locale"),
    ("http://10.0.0.7/", "réseau privé"),
    ("http://169.254.169.254/latest/meta-data/", "lien-local"),
    ("http://[::1]/", "boucle locale"),
])
def test_web_read_refuse_en_nommant_l_hote_et_la_plage(url, plage):
    with pytest.raises(McpError) as e:
        W.check_url_public(url)
    msg = str(e.value)
    assert "adresse interne" in msg and plage in msg
    assert "web_read" in msg


def test_file_source_refuse_par_la_meme_garde():
    with pytest.raises(fs.FileSourceError) as e:
        fs._assert_public_host("10.0.0.7")
    assert "adresse interne" in str(e.value) and "réseau privé" in str(e.value)


def test_une_v6_litterale_est_encadree_pour_file_source():
    """`_assert_public_host` reçoit un HÔTE, pas une URL : une v6 nue doit être
    encadrée pour que la garde la lise — sinon elle refuse pour « URL sans hôte »
    et le mot de la plage manque."""
    with pytest.raises(fs.FileSourceError) as e:
        fs._assert_public_host("::1")
    assert "boucle locale" in str(e.value)


def test_le_deguisement_par_nom_de_domaine_est_refuse_chez_les_deux(monkeypatch):
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"piege.example.com": ["93.184.216.34", "10.0.0.7"]}))
    with pytest.raises(McpError) as e:
        W.check_url_public("https://piege.example.com/api")
    assert "piege.example.com" in str(e.value) and "10.0.0.7" in str(e.value)
    with pytest.raises(fs.FileSourceError):
        fs._assert_public_host("piege.example.com")


def test_une_destination_publique_passe_chez_les_deux(monkeypatch):
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"api.example.com": ["93.184.216.34"]}))
    W.check_url_public("https://api.example.com/v2/records")
    fs._assert_public_host("api.example.com")


# ── 2. la politique : aucune exception déclarée pour une URL d'agent ─────────

def test_une_exception_declaree_ouvre_le_connecteur_mais_PAS_web_read(monkeypatch):
    """Le cœur de la décision : le pont déclaré pour une organisation reste
    joignable par SON credential, et par rien d'autre."""
    monkeypatch.setenv(egress.ALLOW_VAR, "pont=127.0.0.1:80")
    egress.check_url("http://127.0.0.1/", connector="api")      # passe
    with pytest.raises(McpError) as e:
        W.check_url_public("http://127.0.0.1/")
    assert "boucle locale" in str(e.value)
    # `_assert_public_host` ne connaît que l'hôte : la garde le lit sur le port 80,
    # celui de l'exception — c'est bien la politique qui refuse, pas le port.
    with pytest.raises(fs.FileSourceError):
        fs._assert_public_host("127.0.0.1")


def test_le_refus_d_une_url_d_agent_dit_SON_geste_pas_celui_d_un_operateur():
    """« Déclare-la dans OTO_EGRESS_ALLOW » est le geste d'un opérateur de la
    plateforme ; l'agent qui lit ce refus ne peut pas le faire. Son geste : un
    administrateur pose le service comme connecteur `api`."""
    with pytest.raises(McpError) as e:
        W.check_url_public("http://192.168.1.10/")
    msg = str(e.value)
    assert egress.ALLOW_VAR not in msg
    assert "connecteur `api`" in msg and "administrateur" in msg


def test_le_refus_d_un_credential_garde_le_geste_de_l_operateur():
    with pytest.raises(egress.EgressRefused) as e:
        egress.check_url("http://192.168.1.10/", connector="api")
    assert egress.ALLOW_VAR in str(e.value)


# ── 3. le câblage : une seule couture, plus de copie ─────────────────────────

def _appels(chemin: pathlib.Path) -> set[str]:
    arbre = ast.parse(chemin.read_text(encoding="utf-8"))
    vus = set()
    for n in ast.walk(arbre):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            base = n.func.value
            vus.add(f"{getattr(base, 'id', getattr(base, 'attr', '?'))}.{n.func.attr}")
    return vus


@pytest.mark.parametrize("module", ["tools/web.py", "file_source.py"])
def test_le_lecteur_appelle_la_garde_et_ne_resout_plus_lui_meme(module):
    """Une copie locale de la règle est exactement ce que ce lot retire : si un
    de ces modules recommence à résoudre l'hôte lui-même, il rougit ici."""
    appels = _appels(_RACINE / module)
    assert "egress.check_url" in appels, f"{module} ne passe plus par la garde"
    assert "socket.getaddrinfo" not in appels, f"{module} résout l'hôte à part"
    assert not any(a.endswith(".is_global") for a in appels)


def test_la_garde_d_agent_passe_par_le_meme_verdict_que_les_connecteurs(monkeypatch):
    """Preuve que ce n'est pas une troisième règle : remplacer `internal_reason`
    change le verdict des DEUX lecteurs."""
    monkeypatch.setattr(egress, "internal_reason", lambda adresse: None)
    monkeypatch.setattr(egress, "resolved_addresses", _resolution(**{"x.example.com": ["10.0.0.7"]}))
    W.check_url_public("http://x.example.com/")
    fs._assert_public_host("x.example.com")
