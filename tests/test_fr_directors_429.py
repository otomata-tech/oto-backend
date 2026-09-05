"""Le lot ne déclenche plus son propre quota — otomata-tech/oto#44.

Le fait mesuré : 50 SIREN en un lot rendaient 21 erreurs 429 ; les **mêmes** 21,
redemandés aussitôt en deux fois, passaient sans erreur. Rien n'avait changé sauf
la pression. Le 429 était donc un artefact du lot, pas un fait sur les SIREN — et
il partait pourtant dans la réponse comme une propriété de l'entreprise.

Deux causes, deux épreuves ici :

 * le client FOD ne retentait que le 503. Le 429 partait en exception, et le
   `Retry-After` que FOD relaie — la seule information qui dise QUAND réessayer —
   n'était jamais lu ;
 * un plafond de quatre requêtes en vol ne borne pas le débit : elles se relaient
   dès qu'un slot se libère, donc aussi vite que l'amont répond.
"""
from __future__ import annotations

import time

import pytest


class _Reponse:
    def __init__(self, code, headers=None, corps=None):
        self.status_code = code
        self.headers = headers or {}
        self._corps = corps if corps is not None else {"ok": True}
        self.text = str(self._corps)

    def json(self):
        return self._corps

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ClientQuiRefuse:
    """Rend `refus` fois un 429, puis répond. Note ce qu'on lui a demandé."""

    def __init__(self, refus: int, retry_after: str | None = None):
        self.restants, self.retry_after = refus, retry_after
        self.appels = 0

    def request(self, method, path, **kw):
        self.appels += 1
        if self.restants > 0:
            self.restants -= 1
            entetes = {"Retry-After": self.retry_after} if self.retry_after else {}
            return _Reponse(429, entetes, {"detail": "quota amont"})
        return _Reponse(200)


@pytest.fixture
def fod(monkeypatch):
    from oto_mcp.fod import http as F

    monkeypatch.setattr(F, "_RETRY_BACKOFF_S", 0.001)
    return F


def test_un_429_est_repris_au_lieu_de_partir_en_erreur(fod, monkeypatch):
    cli = _ClientQuiRefuse(refus=2)
    monkeypatch.setattr(fod, "_c", lambda: cli)
    assert fod.get("/x") == {"ok": True}
    assert cli.appels == 3, "le 429 doit être repris, pas rendu tel quel"


def test_le_delai_DEMANDE_par_l_amont_est_honore(fod, monkeypatch):
    """`Retry-After` est la seule information qui dise quand le quota se rouvre.
    L'ignorer, c'est deviner — et deviner court, c'est re-cogner."""
    cli = _ClientQuiRefuse(refus=1, retry_after="0.05")
    monkeypatch.setattr(fod, "_c", lambda: cli)
    debut = time.monotonic()
    fod.get("/x")
    attendu = time.monotonic() - debut
    assert attendu >= 0.05, f"attente {attendu:.3f}s : le délai demandé n'a pas été honoré"


def test_un_Retry_After_absurde_ne_fige_pas_l_appel(fod, monkeypatch):
    """Un amont qui demande une heure ne doit pas faire patienter un agent : on
    retombe sur notre propre backoff, borné."""
    cli = _ClientQuiRefuse(refus=1, retry_after="3600")
    monkeypatch.setattr(fod, "_c", lambda: cli)
    debut = time.monotonic()
    fod.get("/x")
    assert time.monotonic() - debut < 1.0


def test_un_Retry_After_illisible_ne_casse_rien(fod, monkeypatch):
    """Forme date HTTP, ou n'importe quoi : on backoff, on ne lève pas."""
    cli = _ClientQuiRefuse(refus=1, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
    monkeypatch.setattr(fod, "_c", lambda: cli)
    assert fod.get("/x") == {"ok": True}


def test_un_429_qui_PERSISTE_se_dit_REESSAYABLE(fod, monkeypatch):
    """La reprise est bornée, et ce qui remonte doit dire ce qu'on peut en faire.

    Sans cette phrase, le refus arrivait en `HTTPStatusError: … 429 …` dans la
    ligne de résultat, où il se lisait comme une propriété de l'entreprise — le
    cœur d'oto#44 : un artefact de notre pression devenait un fait sur la donnée."""
    cli = _ClientQuiRefuse(refus=99)
    monkeypatch.setattr(fod, "_c", lambda: cli)
    with pytest.raises(RuntimeError, match="RÉESSAYABLE") as e:
        fod.get("/x")
    assert "pas un fait sur la donnée" in str(e.value)
    assert cli.appels == fod._RETRY_ATTEMPTS + 1


def test_le_503_reste_repris_comme_avant(fod, monkeypatch):
    """Non-régression : le 429 s'AJOUTE au 503, il ne le remplace pas."""
    class _Sature(_ClientQuiRefuse):
        def request(self, method, path, **kw):
            self.appels += 1
            if self.restants > 0:
                self.restants -= 1
                return _Reponse(503, corps={"detail": "scan saturé"})
            return _Reponse(200)

    cli = _Sature(refus=1)
    monkeypatch.setattr(fod, "_c", lambda: cli)
    assert fod.get("/x") == {"ok": True} and cli.appels == 2


# --- la cadence du lot -----------------------------------------------------

def test_le_lot_etale_ses_departs(monkeypatch):
    """Un plafond en vol ne borne pas le débit. On mesure l'écart entre départs :
    sans cadence, quatre threads partent ensemble et l'écart est nul."""
    from oto_mcp.tools import fr

    departs: list[float] = []

    class _Ent:
        @staticmethod
        def get_by_siren(s):
            departs.append(time.monotonic())
            return {"siren": s, "nom_complet": "X", "dirigeants": []}

    monkeypatch.setattr("oto_mcp.fod.fr.entreprises", _Ent())
    monkeypatch.setattr(fr, "_FR_DIRECTORS_CADENCE_S", 0.02, raising=False)

    class _Reg:
        def __init__(self):
            self.tools = {}

        def tool(self, *a, **k):
            def deco(f):
                self.tools[f.__name__] = f
                return f
            return deco

    reg = _Reg()
    fr.register(reg)
    reg.tools["fr_directors"](sirens=[str(i).zfill(9) for i in range(8)])

    assert len(departs) == 8
    ecarts = [b - a for a, b in zip(sorted(departs), sorted(departs)[1:])]
    assert min(ecarts) >= 0.015, (
        f"départs trop rapprochés ({min(ecarts):.4f}s) : le lot déclenchera son "
        "propre quota, exactement ce que oto#44 corrige")
