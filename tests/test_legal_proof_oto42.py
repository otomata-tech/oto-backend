"""Sortir la preuve d'une acceptation légale — oto#42 lot 2, « le seul qui se paie
ailleurs qu'en confusion ».

Le journal portait l'adresse, l'agent, le contexte et l'org payeuse ; aucune surface ne
les rendait. Ces bancs tiennent les trois choses que la surface promet et qu'un
raccourci ferait tomber en silence.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oto_mcp.capabilities import legal_proof as LP
from oto_mcp.capabilities._types import ResolvedCtx

CTX = ResolvedCtx(sub="u-operateur", org_id=1, role="super_admin")
T = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _ligne(**kw):
    base = {"id": 1, "doc_slug": "terms", "version": "3.0", "accepted_at": T,
            "context": "access", "ip": "203.0.113.7", "user_agent": "Mozilla/5.0",
            "org_id": 4}
    base.update(kw)
    return base


@pytest.fixture()
def catalogue(monkeypatch):
    """Le sujet vit chez un tenant qui sert SES propres CGU en 9.9 ; la plateforme
    en sert 3.0. Deux catalogues différents, exprès."""
    monkeypatch.setattr(LP.tenancy, "current",
                        lambda: type("R", (), {"tenant_of": staticmethod(
                            lambda sub: "partenaire" if sub == "u-sujet" else "oto")})())
    monkeypatch.setattr(LP.legal_docs, "docs_for", lambda slug: (
        {"terms": {"version": "9.9", "label": "CGU", "url": "https://exemple.invalid/cgu"}}
        if slug == "partenaire"
        else {"terms": {"version": "3.0", "label": "CGU", "url": "https://oto.cx/terms"}}))


def _appel(monkeypatch, rows, total, **inp):
    monkeypatch.setattr(LP.db, "list_acceptance_events",
                        lambda sub, doc_slug=None, limit=200: (rows, total))
    return LP._legal_proof(CTX, LP.LegalProofInput(sub="u-sujet", **inp))


def test_le_total_est_celui_du_jeu_entier_pas_de_la_page(monkeypatch, catalogue):
    """oto#42 règle 2. Deux lignes rendues sur trente : « il a accepté deux fois » est
    la phrase fausse que le total et le drapeau existent pour empêcher."""
    out = _appel(monkeypatch, [_ligne(id=2), _ligne(id=1)], 30, limit=2)
    assert out["total"] == 30, "le total doit compter le jeu entier, pas la page"
    assert out["truncated"] is True
    assert len(out["events"]) == 2


def test_un_historique_entier_ne_se_dit_pas_tronque(monkeypatch, catalogue):
    """Le drapeau doit rester faux quand rien n'est coupé : un `truncated` toujours
    vrai ne vaut pas mieux qu'un `truncated` absent."""
    out = _appel(monkeypatch, [_ligne()], 1)
    assert out["truncated"] is False


def test_une_version_passee_ne_recoit_pas_l_url_du_texte_d_aujourdhui(monkeypatch, catalogue):
    """La borne la plus facile à franchir sans s'en apercevoir : le registre ne garde
    que la version COURANTE, donc pointer son URL sur une acceptation ancienne ferait
    passer le texte d'aujourd'hui pour celui qui a été accepté."""
    out = _appel(monkeypatch, [_ligne(version="2.0"), _ligne(id=2, version="9.9")], 2)
    ancienne, courante = out["events"]
    assert ancienne["version_courante"] is False and ancienne["url"] is None
    assert courante["version_courante"] is True
    assert courante["url"] == "https://exemple.invalid/cgu"


def test_le_catalogue_compare_est_celui_du_SUJET_pas_de_l_appelant(monkeypatch, catalogue):
    """Le sujet est chez un tenant tiers dont les CGU sont en 9.9 ; l'opérateur qui
    appelle est chez oto, où elles sont en 3.0. Comparer au catalogue de l'APPELANT
    dirait « périmée » sur un document que le sujet n'a jamais eu à accepter."""
    out = _appel(monkeypatch, [_ligne(version="9.9")], 1)
    assert out["events"][0]["version_courante"] is True, (
        "la version du sujet (tenant partenaire, 9.9) doit être lue comme courante ; "
        "elle ne l'est pas dans le catalogue d'oto")


def test_une_trace_absente_reste_nulle_et_ne_devient_pas_une_valeur(monkeypatch, catalogue):
    """`null` se lit « aucune trace enregistrée ». Le rendre en `""` ou `0` ferait
    croire à une trace vide, et l'origine d'une ligne ne se déduit de toute façon
    pas de ces quatre colonnes (cf. l'en-tête du module)."""
    out = _appel(monkeypatch, [_ligne(context=None, ip=None, user_agent=None,
                                      org_id=None)], 1)
    e = out["events"][0]
    assert (e["context"], e["ip"], e["user_agent"], e["org_id"]) == (None, None, None, None)


def test_la_preuve_porte_ce_qui_situe_l_acte(monkeypatch, catalogue):
    """Sans ces quatre colonnes il reste une date : c'est l'état, pas la preuve."""
    out = _appel(monkeypatch, [_ligne()], 1)
    e = out["events"][0]
    assert e["ip"] == "203.0.113.7" and e["user_agent"] == "Mozilla/5.0"
    assert e["context"] == "access" and e["org_id"] == 4
    assert e["accepted_at"].startswith("2026-09-01T10:00")
