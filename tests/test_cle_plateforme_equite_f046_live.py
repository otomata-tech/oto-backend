"""F-046 — un blocage de crédit qui ne bloquait pas, et une re-pose qui fermait la clé.

Deux défauts du palier plateforme, reproduits sur une base réelle :

1. **Révoquer ne coupait rien sur une clé OUVERTE.** `platform_revoke` retirait le scope
   du `share_down` ; sur une clé `open` à `share_down` vide (ouverte à tous),
   `_platform_instance_usable` rendait toujours vrai. Côté chaîne, révoquer un scope
   sans arête n'archivait rien : la chaîne restait muette, l'ancien chemin servait.
   Et lever le blocage (`platform_grant`) FERMAIT la clé aux autres orgs.
2. **Re-poser une clé plateforme la fermait** (`set_platform_key` forçait `closed` à
   chaque pose) et l'upsert écrasait `meta` — quotas par bénéficiaire compris.
"""
from __future__ import annotations

import os

import pytest

from _base_jetable import base_jetable


@pytest.fixture
def base(pg_dsn, monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", os.urandom(32).hex())
    with base_jetable(pg_dsn):
        from oto_mcp.db import init_db
        init_db()
        yield


def _instance(provider: str) -> dict:
    from oto_mcp import credentials_store

    inst, = credentials_store.list_platform_instances(provider)
    return inst


def _cle_payante_ouverte_a_tous(provider: str = "aiark", label: str = "commune") -> None:
    """Une clé payante posée OUVERTE à tous — l'état d'une clé posée avant la fermeture à
    la pose (le défaut de colonne `open`, `share_down` vide), ou ouverte délibérément."""
    from oto_mcp import credentials_store

    credentials_store.set_credential(credentials_store.PLATFORM, label, provider, "k-1",
                                     set_by="op")
    inst = _instance(provider)
    assert inst["share_mode"] == "open" and not inst["share_down"]


def test_les_deux_noms_de_la_cle_de_revocation_sont_le_meme():
    from oto_mcp import credentials_store
    from oto_mcp.access import platform_grant

    assert credentials_store._REVOQUES == platform_grant.DENIED_META_KEY


# ── 1. Révoquer coupe, sur une clé ouverte non chaînée ───────────────────────

def test_revoquer_une_org_sur_une_cle_ouverte_la_coupe_elle_seule(base):
    from oto_mcp import credentials_store, grants_chain
    from oto_mcp.access.platform_grant import _platform_grant_meta

    assert not grants_chain.is_chained("aiark")
    _cle_payante_ouverte_a_tous()
    assert _platform_grant_meta("membre", "aiark", 42) is not None

    credentials_store.platform_revoke("aiark", "org:42")

    assert _platform_grant_meta("membre", "aiark", 42) is None
    assert _platform_grant_meta("autre", "aiark", 77) is not None


def test_lever_le_blocage_ne_ferme_pas_la_cle_aux_autres(base):
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    _cle_payante_ouverte_a_tous()
    credentials_store.platform_revoke("aiark", "org:42")
    credentials_store.platform_grant("aiark", "org:42")

    assert _platform_grant_meta("membre", "aiark", 42) is not None
    assert _platform_grant_meta("autre", "aiark", 77) is not None
    inst = _instance("aiark")
    assert inst["share_mode"] == "open" and not inst["share_down"]
    assert not inst["meta"].get("share_denied")


def test_revoquer_un_compte_le_coupe_meme_s_il_change_d_org(base):
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    _cle_payante_ouverte_a_tous()
    credentials_store.platform_revoke("aiark", "user:bloque")

    assert _platform_grant_meta("bloque", "aiark", 42) is None
    assert _platform_grant_meta("bloque", "aiark", 77) is None
    assert _platform_grant_meta("autre", "aiark", 42) is not None


def test_la_revocation_prime_sur_un_accord_de_l_org(base):
    from oto_mcp.access.platform_grant import _platform_instance_usable

    inst = {"share_mode": "open", "share_down": ["org:42"],
            "meta": {"share_denied": ["org:42"]}}
    assert not _platform_instance_usable("membre", 42, inst)


def test_retirer_le_dernier_beneficiaire_n_ouvre_pas_la_cle_a_tous(base):
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    # Une clé `open` à liste (« seulement ceux-là ») : la vider ne veut pas dire « tous ».
    credentials_store.set_credential(credentials_store.PLATFORM, "liste", "aiark", "k",
                                     set_by="op")
    from oto_mcp.db import _connect
    with _connect() as conn:
        conn.execute("UPDATE connector_credentials SET share_down = %s::jsonb "
                     "WHERE entity_type = %s AND connector = 'aiark'",
                     ('["org:42"]', credentials_store.PLATFORM))
    assert _platform_grant_meta("autre", "aiark", 77) is None

    credentials_store.platform_revoke("aiark", "org:42")

    assert _platform_grant_meta("autre", "aiark", 77) is None
    assert _platform_grant_meta("membre", "aiark", 42) is None


def test_le_blocage_se_voit_a_la_resolution_reelle(base, monkeypatch):
    """Bout en bout : `resolve_credential` refuse le membre de l'org bloquée et sert
    celui d'une autre org, sans clé à eux ni l'un ni l'autre."""
    from oto_mcp import credentials_store
    from oto_mcp import access
    from oto_mcp.access import resolve
    from oto_mcp.mcp_errors import McpError

    _cle_payante_ouverte_a_tous()
    credentials_store.platform_revoke("aiark", "org:42")

    org_de = {"membre": 42, "autre": 77}
    monkeypatch.setattr(access, "current_org", lambda sub: org_de.get(sub))
    with pytest.raises(McpError):
        resolve._resolve_credential_impl("aiark", "auto", "membre")
    servi = resolve._resolve_credential_impl("aiark", "auto", "autre")
    assert servi.is_platform and servi.secret == "k-1"


# ── 1 bis. Révoquer coupe, sur une clé ouverte CHAÎNÉE sans arête ─────────────

def test_revoquer_sans_arete_sur_un_connecteur_chaine_coupe(base):
    from oto_mcp import credentials_store, grants_chain
    from oto_mcp.access.platform_grant import _platform_grant_meta

    assert grants_chain.is_chained("serper")
    credentials_store.set_platform_key("commune", "serper", "sk-1", set_by="op")
    assert _platform_grant_meta("membre", "serper", 42) is not None

    credentials_store.platform_revoke("serper", "org:42")

    assert _platform_grant_meta("membre", "serper", 42) is None
    assert _platform_grant_meta("autre", "serper", 77) is not None

    credentials_store.platform_grant("serper", "org:42")
    assert _platform_grant_meta("membre", "serper", 42) is not None


# ── 2. Re-poser garde l'état de la clé ─────────────────────────────────────────

def test_re_poser_une_cle_ouverte_la_laisse_ouverte(base):
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    _cle_payante_ouverte_a_tous()
    credentials_store.set_platform_key("commune", "aiark", "k-2", set_by="op")

    inst = _instance("aiark")
    assert inst["share_mode"] == "open"
    assert _platform_grant_meta("quelconque", "aiark", 77) is not None
    assert credentials_store.get_credential(credentials_store.PLATFORM, "commune",
                                            "aiark") == "k-2"


def test_re_poser_garde_beneficiaires_quotas_et_revocations(base):
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    credentials_store.set_platform_key("partenaire", "forager", "fg-1", set_by="op")
    credentials_store.platform_grant("forager", "org:42", daily_quota=50)
    credentials_store.platform_grant("forager", "org:43", daily_quota=10)
    credentials_store.platform_revoke("forager", "org:43")

    credentials_store.set_platform_key("partenaire", "forager", "fg-2", set_by="op")

    inst = _instance("forager")
    assert inst["share_mode"] == "closed"
    assert inst["share_down"] == ["org:42"]
    assert inst["meta"]["rate_limit_by"] == {"org:42": 50}
    assert inst["meta"]["share_denied"] == ["org:43"]
    assert _platform_grant_meta("membre", "forager", 42) == {
        "label": "partenaire", "daily_quota": 50}
    assert _platform_grant_meta("membre", "forager", 43) is None


def test_une_cle_neuve_payante_nait_toujours_fermee(base):
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    credentials_store.set_platform_key("neuve", "aiark", "k", set_by="op")
    assert _instance("aiark")["share_mode"] == "closed"
    assert _platform_grant_meta("quelconque", "aiark", 77) is None


# ── 3. Révoquer coupe sur TOUTES les clés plateforme du connecteur ─────────────

def _deux_cles_ouvertes(provider: str = "aiark") -> None:
    """Deux clés plateforme du même connecteur, ouvertes à tous : une ancienne et une
    récente (une rotation sous un nouveau libellé, ou une clé de secours)."""
    from oto_mcp import credentials_store

    for label in ("ancienne", "recente"):
        credentials_store.set_credential(credentials_store.PLATFORM, label, provider,
                                         f"k-{label}", set_by="op")


def test_revoquer_coupe_aussi_l_autre_cle_du_connecteur(base):
    """Révoquer marquait la seule clé la plus récente : l'ancienne, ouverte à tous,
    servait l'org bloquée au tour suivant de la boucle de résolution."""
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    _deux_cles_ouvertes()
    credentials_store.platform_revoke("aiark", "org:42")

    assert _platform_grant_meta("membre", "aiark", 42) is None
    assert _platform_grant_meta("autre", "aiark", 77) is not None


def test_une_cle_posee_apres_la_revocation_ne_la_leve_pas(base):
    """Un connecteur à palier gratuit pose sa clé ouverte : une clé neuve, sans la
    révocation, servait de nouveau l'org bloquée."""
    from oto_mcp import credentials_store, grants_chain, providers
    from oto_mcp.access.platform_grant import _platform_grant_meta

    assert not grants_chain.is_chained("sirene")
    assert providers.REGISTRY["sirene"].platform_key_open
    credentials_store.set_platform_key("ancienne", "sirene", "s-1", set_by="op")
    credentials_store.platform_revoke("sirene", "org:42")
    credentials_store.set_platform_key("recente", "sirene", "s-2", set_by="op")

    assert _platform_grant_meta("membre", "sirene", 42) is None
    assert _platform_grant_meta("autre", "sirene", 77) is not None


def test_lever_le_blocage_rouvre_toutes_les_cles(base):
    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    _deux_cles_ouvertes()
    credentials_store.platform_revoke("aiark", "org:42")
    credentials_store.platform_grant("aiark", "org:42")

    assert _platform_grant_meta("membre", "aiark", 42) is not None
    for inst in credentials_store.list_platform_instances("aiark"):
        assert not inst["meta"].get("share_denied"), inst["label"]


def test_un_accord_concurrent_n_efface_pas_la_revocation(base, monkeypatch):
    """Accorder et révoquer relisent puis réécrivent `meta` : sans verrou de ligne,
    l'accord à l'org 43, lu avant la révocation de l'org 42 et écrit après, effaçait le
    refus. On met l'accord en pause entre sa lecture et son écriture, et on révoque
    pendant ce temps."""
    import threading
    from contextlib import contextmanager

    from oto_mcp import credentials_store
    from oto_mcp.access.platform_grant import _platform_grant_meta

    _cle_payante_ouverte_a_tous()
    vrai_connect = credentials_store._connect
    accord_a_lu, revocation_finie = threading.Event(), threading.Event()
    fil_accord: list[threading.Thread] = []

    class _Pause:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            cur = self._conn.execute(sql, *a, **k)
            if (threading.current_thread() in fil_accord
                    and sql.lstrip().startswith("SELECT") and "share_mode" in sql):
                accord_a_lu.set()
                # Avec le verrou, la révocation attend la fin de l'accord : on ne
                # l'attend donc qu'un temps, puis l'accord écrit et libère la ligne.
                revocation_finie.wait(2)
            return cur

        def __getattr__(self, nom):
            return getattr(self._conn, nom)

    @contextmanager
    def _connect_avec_pause(*a, **k):
        with vrai_connect(*a, **k) as conn:
            yield _Pause(conn)

    monkeypatch.setattr(credentials_store, "_connect", _connect_avec_pause)
    accord = threading.Thread(
        target=credentials_store.platform_grant, args=("aiark", "org:43", 10))
    fil_accord.append(accord)
    accord.start()
    assert accord_a_lu.wait(10)
    credentials_store.platform_revoke("aiark", "org:42")
    revocation_finie.set()
    accord.join(10)
    assert not accord.is_alive()

    assert _platform_grant_meta("membre", "aiark", 42) is None
    assert _instance("aiark")["meta"]["rate_limit_by"] == {"org:43": 10}
