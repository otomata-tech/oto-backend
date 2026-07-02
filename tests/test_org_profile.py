"""Profil d'org (domaine de marque + logo dérivé logo.dev).

Cible les helpers PURS (`normalize_domain`, `effective_logo_url`) — pas de DB.
Le contrat clé : l'upload prime toujours, le domaine ne dérive un logo que si
le token logo.dev est posé, et une saisie non-domaine lève (pas de fallback
silencieux)."""
import pytest

from oto_mcp import logodev, org_store


# --- normalize_domain --------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("acme.com", "acme.com"),
    ("  ACME.com  ", "acme.com"),
    ("https://acme.com/about?x=1", "acme.com"),
    ("http://www.acme.co.uk", "acme.co.uk"),
    ("sub.acme.io.", "sub.acme.io"),
])
def test_normalize_domain_tolerates_url_input(raw, expected):
    assert org_store.normalize_domain(raw) == expected


def test_normalize_domain_empty_clears():
    assert org_store.normalize_domain("") is None
    assert org_store.normalize_domain("   ") is None


@pytest.mark.parametrize("raw", ["acme", "not a domain", "a..b", "-acme.com", "acme.c"])
def test_normalize_domain_rejects_non_domains(raw):
    with pytest.raises(ValueError):
        org_store.normalize_domain(raw)


# --- effective_logo_url ------------------------------------------------------

def test_uploaded_logo_wins_over_domain(monkeypatch):
    monkeypatch.setenv("LOGODEV_TOKEN", "pk_test")
    org = {"logo_url": "https://media.example/logo.png", "domain": "acme.com"}
    assert org_store.effective_logo_url(org) == "https://media.example/logo.png"


def test_domain_derives_logodev_url(monkeypatch):
    monkeypatch.setenv("LOGODEV_TOKEN", "pk_test")
    url = org_store.effective_logo_url({"logo_url": None, "domain": "acme.com"})
    assert url == "https://img.logo.dev/acme.com?token=pk_test&size=256&format=png&retina=true"


def test_no_token_or_no_domain_means_no_logo(monkeypatch):
    monkeypatch.delenv("LOGODEV_TOKEN", raising=False)
    assert org_store.effective_logo_url({"logo_url": None, "domain": "acme.com"}) is None
    monkeypatch.setenv("LOGODEV_TOKEN", "pk_test")
    assert org_store.effective_logo_url({"logo_url": None, "domain": None}) is None
    # dict sans clé domain (rows historiques) — pas de KeyError.
    assert org_store.effective_logo_url({"logo_url": None}) is None


def test_logodev_url_size_param(monkeypatch):
    monkeypatch.setenv("LOGODEV_TOKEN", "pk_test")
    assert "size=128" in logodev.logo_url("acme.com", size=128)
