"""Capacité d'édition du bloc plateforme A (#50). La prose init plateforme vit
désormais dans `guides` (delivery='init', ADR 0042) : on monkeypatche le seam
`db.{get,set}_init_guide_db` — pas de vraie DB. (Le bloc onboarding a disparu :
l'onboarding est un projet, ADR 0032 §7.)
"""
import pytest

from oto_mcp import instructions
from oto_mcp.capabilities import platform_instructions as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="admin1", org_id=None)


@pytest.fixture
def store(monkeypatch):
    import oto_mcp.db as db
    rows: dict[tuple, dict] = {}

    def _get(scope, owner, slug):
        return rows.get((scope, owner, slug))

    def _set(scope, owner, slug, body_md, *, expected_rev=None, **_):
        row = {"scope": scope, "owner_id": owner, "slug": slug,
               "body_md": body_md or "", "delivery": "init", "updated_at": "2026-06-30"}
        rows[(scope, owner, slug)] = row
        return row

    monkeypatch.setattr(db, "get_init_guide_db", _get)
    monkeypatch.setattr(db, "set_init_guide_db", _set)
    return rows


def test_get_returns_seed_when_absent(store):
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="secret_sauce"))
    assert out["is_seed"] is True
    assert "TA boîte à outils" in out["body_md"]            # le seed constant
    assert out["default_md"] == instructions.default_block("secret_sauce")


def test_set_then_get(store):
    P._platform_instructions(CTX, P.PlatformInstrInput(
        op="set", key="secret_sauce", body_md="NOUVELLE PROSE"))
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="secret_sauce"))
    assert out["is_seed"] is False and out["body_md"] == "NOUVELLE PROSE"
    assert out["updated_by"] is None                       # guides ne porte pas d'auteur
    # le défaut reste accessible (bouton « rétablir »)
    assert "TA boîte à outils" in out["default_md"]


def test_list_covers_block(store):
    out = P._platform_instructions(CTX, P.PlatformInstrInput(op="list"))
    assert out["keys"] == ["secret_sauce"]
    assert {b["key"] for b in out["blocks"]} == {"secret_sauce"}


def test_unknown_key_rejected(store):
    with pytest.raises(AuthzDenied) as e:
        P._platform_instructions(CTX, P.PlatformInstrInput(op="get", key="bogus"))
    assert e.value.code == "unknown_key"


def test_set_requires_body(store):
    with pytest.raises(AuthzDenied) as e:
        P._platform_instructions(CTX, P.PlatformInstrInput(op="set", key="secret_sauce"))
    assert e.value.code == "missing_body"


def test_capability_registered():
    from oto_mcp.capabilities.registry import CAPABILITIES
    by_key = {c.key: c for c in CAPABILITIES}
    assert by_key["platform.instructions"].mcp == "oto_admin_platform_instructions"
    rest = by_key["platform.instructions.set"].rest
    assert rest is not None and rest.verb == "PUT"
    assert rest.path == "/api/admin/platform-instructions/{key}"


# ── L'écran admin dit ce que le handshake SERT, pas autre chose ─────────────────
def test_un_bloc_vide_se_lit_comme_le_SEED_pas_comme_une_edition(monkeypatch):
    """Les deux notions d'« édité » divergeaient, et l'écran mentait.

    `instructions._platform_block` sert le seed dès que l'override est vide ; cette vue,
    elle, se réglait sur `updated_at`. Vider un bloc pose une ligne DATÉE à corps vide :
    l'agent recevait donc le seed pendant que la surface admin annonçait « édité » avec
    un corps vide. Vider l'override EST le geste « rétablir le défaut » — il doit se lire
    comme tel.
    """
    from oto_mcp.capabilities import platform_instructions as P

    monkeypatch.setattr(P.guide_store, "get_init_guide",
                        lambda scope, key: {"body_md": "", "updated_at": "2026-08-14 10:00:00"})
    vue = P._view("secret_sauce")
    assert vue["is_seed"] is True
    assert vue["body_md"] == vue["default_md"] and vue["body_md"]
    # Et ce qui est ANNONCÉ est ce qui est SERVI au handshake.
    assert P.instructions._platform_block("secret_sauce", vue["default_md"]) == vue["body_md"]


def test_un_bloc_blanc_compte_comme_vide(monkeypatch):
    from oto_mcp.capabilities import platform_instructions as P

    monkeypatch.setattr(P.guide_store, "get_init_guide",
                        lambda scope, key: {"body_md": "   \n  ", "updated_at": "2026-08-14"})
    assert P._view("secret_sauce")["is_seed"] is True


def test_un_override_REEL_reste_annonce_comme_edite(monkeypatch):
    from oto_mcp.capabilities import platform_instructions as P

    monkeypatch.setattr(P.guide_store, "get_init_guide",
                        lambda scope, key: {"body_md": "prose maison", "updated_at": "2026-08-14"})
    vue = P._view("secret_sauce")
    assert vue["is_seed"] is False and vue["body_md"] == "prose maison"


# ── La sonde de dérive : ce que la base SERT vs ce que le code PORTE ────────────
def test_les_quatre_etats_de_derive():
    from oto_mcp.capabilities import platform_instructions as P

    # Pas d'override → le code est servi, rien à faire.
    assert P._etat("", "prose du code") == "seed"
    assert P._etat("   ", "prose du code") == "seed"
    # Override identique au code : inutile, mais inoffensif.
    assert P._etat("prose du code", "prose du code") == "aligné"
    # LE cas dangereux : la base gagne, le code n'atteint plus personne.
    assert P._etat("ancienne prose", "prose du code") == "divergent"
    # Né en base : un environnement NEUF naîtra sans cette prose.
    assert P._etat("prose maison", "") == "hors_code"
    assert P._etat("", "") == "vide"


def test_la_sonde_ISOLE_ce_qui_demande_une_action(monkeypatch):
    from oto_mcp.capabilities import platform_instructions as P

    monkeypatch.setattr(P.guide_store, "get_init_guide",
                        lambda scope, key: {"body_md": "", "updated_at": None})
    monkeypatch.setattr(P.instructions, "default_block", lambda key: "bloc A du code")
    monkeypatch.setattr(P.guide_store, "list_file_guides", lambda: [
        {"slug": "aligne", "body_md": "même texte"},
        {"slug": "derive", "body_md": "texte NEUF du code"},
    ])
    import oto_mcp.db as db
    monkeypatch.setattr(db, "list_guides_db", lambda scope, owner: [
        {"slug": "aligne", "body_md": "même texte", "updated_at": "2026-08-01"},
        {"slug": "derive", "body_md": "vieux texte figé", "updated_at": "2026-07-01"},
        {"slug": "ne-du-clic", "body_md": "écrit en ligne", "updated_at": "2026-08-10"},
    ])

    out = P._drift(None, P._NoInput())
    par_slug = {ligne["slug"]: ligne["etat"] for ligne in out["proses"]}
    assert par_slug["secret_sauce"] == "seed"
    assert par_slug["aligne"] == "aligné"
    assert par_slug["derive"] == "divergent"
    assert par_slug["ne-du-clic"] == "hors_code"
    # `a_traiter` ne retient QUE ce sur quoi il y a un geste à faire — une sonde qui
    # rend tout au même niveau se lit comme un inventaire, pas comme une alerte.
    assert {ligne["slug"] for ligne in out["a_traiter"]} == {"derive", "ne-du-clic"}
    assert out["resume"]["divergent"] == 1 and out["resume"]["hors_code"] == 1
