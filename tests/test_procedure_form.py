"""La FORME d'une procédure : un dessin FACULTATIF (retiré des exigences le 18/09/2026).

Couvre : le test de présence (mêmes seuils que le `isDrawing` du front), le fait que
seuls les blocs NON TAGUÉS comptent (c'est le routeur du front qui en décide), le
SILENCE du check quand il n'y a pas de dessin, sa remontée dans les faces d'écriture
(org, équipe, publication) quand il y en a deux, et le CLIQUET qui refuse le retour de
l'obligation dans les textes servis : elle y avait été posée le 23/08/2026 pour un
besoin d'affichage d'un front partenaire, et quatre tests en figeaient les renvois.

⚠️ Le DIGEST d'ouverture, longtemps l'autre moitié de cette « forme », a été retiré le
10/09/2026 (oto#159) — son retrait est gardé par `test_digest_retire_159.py`.
"""
import asyncio
import pathlib

from oto_mcp import guide_store, instructions, procedure_diagram as pd
from oto_mcp.capabilities.groups import guide as gd
from oto_mcp.capabilities.orgs import instructions as oi

_GUIDES = pathlib.Path(__file__).resolve().parents[1] / "oto_mcp" / "guides"

# Un dessin de référence, tel que les procédures qui en ont un le portent.
_DRAWING = """\
              Natural language input in Claude
              "Wholesale distributors in the East Bay, 200 employees. Source 40 accounts."
                         │
                         ▼
┌─────────────────────────────────────────────────┐
│  0 · Find companies on Apollo                   │   apollo_search_organizations
│  Search, then filter by industry code.          │
└────────────────────────┬────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────┐
│  1 · Send the email                             │
│  Enrol the person in the matching sequence.     │
└─────────────────────────────────────────────────┘"""


def _fenced(block: str, lang: str = "") -> str:
    return f"Une procédure.\n\n```{lang}\n{block}\n```\n\n## Phase 1\n"


# ── Présence du dessin ──────────────────────────────────────────────────────
def test_a_real_drawing_is_found():
    assert pd.is_drawing(_DRAWING)
    assert pd.has_diagram(_fenced(_DRAWING))
    assert pd.diagram_check(_fenced(_DRAWING))["diagram_warning"] is None


def test_no_drawing_is_silent():
    """Le dessin est facultatif : son absence n'appelle aucun avertissement."""
    assert pd.diagram_check("Une procédure sans le moindre bloc.")["diagram_warning"] is None


def test_a_tagged_block_is_never_a_drawing():
    """Le routeur du front ne dessine QUE les blocs non tagués : compter un ```text
    plein de caractères de tracé serait un faux positif silencieux."""
    assert not pd.has_diagram(_fenced(_DRAWING, lang="text"))
    assert pd.diagram_check(_fenced(_DRAWING, lang="text"))["diagram_warning"] is None


def test_thresholds_match_the_front():
    # Assez de glyphes mais sur 2 lignes seulement → pas un dessin.
    two_lines = "─" * 30 + "\n" + "─" * 30
    assert not pd.is_drawing(two_lines)
    # Assez de lignes mais pas assez de glyphes → pas un dessin non plus.
    three_thin = "│ a\n│ b\n│ c"
    assert not pd.is_drawing(three_thin)
    # Le plancher exact, des deux côtés.
    assert pd.is_drawing("\n".join(["─" * 7] * 3))       # 3 lignes, 21 glyphes
    assert not pd.is_drawing("\n".join(["─" * 6] * 3))   # 3 lignes, 18 glyphes


def test_a_shell_sample_with_a_stray_arrow_is_not_a_drawing():
    sample = "cat f | grep x   # ─\nls -l\necho ▶"
    assert not pd.is_drawing(sample)


def test_the_check_never_raises(monkeypatch):
    """⚠️ La mutation doit viser ce que `diagram_check` APPELLE vraiment — depuis qu'il
    compte les dessins au lieu d'en tester la présence, c'est `compter_les_dessins`.
    Posée sur `has_diagram`, elle ne mordait plus : le test aurait viré au vert creux
    (il a viré au rouge, ce qui l'a signalé — mais le vert était le mode d'échec
    possible)."""
    monkeypatch.setattr(pd, "compter_les_dessins",
                        lambda body: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pd.diagram_check("peu importe") == {"diagram_warning": None}


def test_deux_dessins_sont_annonces():
    """La page n'en rend qu'un — le premier. Le cas se fabrique quand un corps garde le
    marqueur ET porte un dessin neuf : `avec_le_dessin` remet le tracé stocké à côté."""
    deux = _fenced(_DRAWING) + "\n```\n" + _DRAWING + "\n```\n"
    assert pd.compter_les_dessins(deux) == 2
    assert pd.has_diagram(deux), "deux dessins, c'est toujours « il y a un dessin »"
    assert pd.diagram_check(deux) == {"diagram_warning": pd.DOUBLE}
    # Un seul reste muet : la garde vise le SECOND bloc, pas la présence.
    assert pd.diagram_check(_fenced(_DRAWING)) == {"diagram_warning": None}


# ── Remontée dans les faces d'écriture ──────────────────────────────────────
class _Ctx:
    sub = "u1"
    org_id = 7
    group_id = None     # comme un `ResolvedCtx` hors équipe (#1156 lit ce champ)


class _Inp:
    slug = "ma-procedure"
    title = None
    description = None
    from_version = None
    slots = None
    org = None

    def __init__(self, body_md):
        self.body_md = body_md


def _set_org(monkeypatch, body):
    monkeypatch.setattr(oi.org_store, "set_instruction",
                        lambda *a, **k: 3)
    # La relecture de la version écrite (empreinte, oto#133) rend le corps posé.
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda *a, **k: {"slots": [], "body_md": body})

    async def _wc(body_md, **k):
        return {"referenced_tools": [], "unresolved_tools": []}
    monkeypatch.setattr(oi.tool_registry, "write_check", _wc)
    return asyncio.run(oi._set_instruction(_Ctx(), _Inp(body)))


def test_org_set_without_drawing_is_silent(monkeypatch):
    out = _set_org(monkeypatch, "Une procédure sans dessin.")
    assert out["diagram_warning"] is None
    assert out["ok"] is True and out["version"] == 3


def test_org_set_surfaces_two_drawings(monkeypatch):
    out = _set_org(monkeypatch, _fenced(_DRAWING) + "\n" + _fenced(_DRAWING))
    assert out["diagram_warning"] == pd.DOUBLE
    assert out["ok"] is True   # non bloquant : l'écriture a eu lieu


def test_org_set_is_silent_when_the_drawing_is_there(monkeypatch):
    out = _set_org(monkeypatch, _fenced(_DRAWING))
    assert out["diagram_warning"] is None


def test_group_set_is_silent_without_drawing(monkeypatch):
    """Une procédure d'équipe est une procédure : même régime."""
    monkeypatch.setattr(gd.org_store, "set_instruction", lambda *a, **k: 2)
    monkeypatch.setattr(gd.org_store, "get_instruction",
                        lambda *a, **k: {"body_md": "corps relu"})

    class _GInp(_Inp):
        group_id = 4

    out = gd._set(_Ctx(), _GInp("Une procédure d'équipe sans dessin."))
    assert out["diagram_warning"] is None
    assert gd._set(_Ctx(), _GInp(_fenced(_DRAWING)))["diagram_warning"] is None


def test_written_models_declare_the_field():
    assert "diagram_warning" in oi.InstructionWritten.model_fields
    assert "diagram_warning" in gd.GroupInstructionWritten.model_fields


# ── Cliquet : l'obligation ne revient pas dans les textes servis ────────────
_PRESCRIPTIONS = ("procedure-flowchart", "must carry a FLOWCHART", "section requise",
                  "son dessin")


def test_aucun_texte_servi_ne_reclame_le_dessin():
    """Socle de session, descriptions servies, guides semés : aucun ne réclame plus un
    dessin. Le socle se vérifie sur la constante (sa surcharge éventuelle en base est
    un geste d'exploitation, pas du code)."""
    from oto_mcp.capabilities import procedure_console  # noqa: F401 — monte la console
    from oto_mcp.capabilities.registry import CAPABILITIES

    textes = {"socle": instructions._SECRET_SAUCE}
    textes.update({c.key: c.description or "" for c in CAPABILITIES})
    textes.update({f"guide {g['slug']}": g["body_md"]
                   for g in guide_store.list_file_guides()})
    fautifs = sorted(nom for nom, texte in textes.items()
                     if any(p in texte for p in _PRESCRIPTIONS))
    assert not fautifs, f"le dessin est de nouveau réclamé par : {fautifs}"
    assert textes, "aucun texte relu — le cliquet ne garde plus rien"


def test_le_guide_du_dessin_n_est_plus_seme_par_la_plateforme():
    assert not (_GUIDES / "procedure-flowchart.md").exists()
    assert "procedure-flowchart" not in {g["slug"] for g in guide_store.list_file_guides()}


def test_publish_and_fork_are_silent_without_drawing(monkeypatch):
    """Publier ou forker une procédure sans dessin : rien à signaler."""
    from oto_mcp.capabilities import guide_library as dl

    # Seul un super_admin publie ; le rôle se pose à sa source, d'où dérivent les deux
    # prédicats plateforme.
    monkeypatch.setattr(dl.access, "get_user_role", lambda sub: "super_admin")
    monkeypatch.setattr(dl.db_communaute, "etat", lambda kind, i: {"state": "listed"})
    monkeypatch.setattr(dl, "_author_for", lambda ctx: ("otomata", None, "Otomata"))
    monkeypatch.setattr(dl.org_store, "get_instruction",
                        lambda otype, oid, slug: {"body_md": "Sans dessin.", "title": "t",
                                                  "description": "d", "slots": []})
    monkeypatch.setattr(dl.org_store, "publish_guide",
                        lambda **k: {"id": 1, "slug": "s", "version": 1, "visibility": "public"})

    class _P:
        slug = "s"; public_slug = None; title = None; description = None
        category = None; tags = None; visibility = "public"

    assert dl._publish(_Ctx(), _P())["diagram_warning"] is None

    monkeypatch.setattr(dl.org_store, "get_library_entry",
                        lambda **k: {"id": 1, "body_md": _fenced(_DRAWING)})
    monkeypatch.setattr(dl.org_store, "fork_library_entry",
                        lambda **k: {"owner_type": k["owner_type"], "owner_id": k["owner_id"],
                                     "slug": "s", "version": 1, "guide_id": 11,
                                     "forked_from": 1, "source_title": "t"})
    monkeypatch.setattr(dl.roles, "is_org_admin", lambda sub, org_id: True)

    class _F:
        slug = "s"; new_slug = None; scope = None; project_id = None

    assert dl._fork(_Ctx(), _F())["diagram_warning"] is None
