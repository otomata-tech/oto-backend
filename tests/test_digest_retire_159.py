"""Le « Self-improvement digest » est RETIRÉ — le mécanisme et la règle (oto#159).

La plateforme réclamait, à chaque écriture de procédure, un bloc de citation
`> **Self-improvement digest** — …` en tête du corps ; l'écriture qui ne l'avait pas
recevait un `digest_warning`. Trois faits ont décidé du RETRAIT plutôt que du
déménagement de ce bloc vers un champ structuré :

- il est né d'une contrainte de RENDU (`b34af1cc`, 23/08/2026) — il fallait que quelque
  chose apparaisse en tête de la page d'un process — et le choix du corps markdown
  plutôt que d'un champ n'a jamais été posé comme une question ; aucun ADR ne le porte,
  la décision vivait dans un message de commit et une docstring ;
- la plateforme porte DÉJÀ tout ce que ce bloc racontait : `version` porte la version,
  l'historique des versions porte ce qui a changé (l'écriture est un remplacement qui
  conserve la précédente, lisible par `with_history`, réversible par `from_version`),
  et ce qu'un passage a appris est un document de projet. Le bloc en était le TROISIÈME
  domicile, avec la dérive garantie entre les trois ;
- personne ne le suivait, et l'avertissement n'y changeait rien : sur 57 procédures
  mesurées, 19 sans digest et 5 le portant ailleurs qu'en tête. Le détecteur ne testant
  que le PREMIER bloc, ces 24-là recevaient l'avertissement à chaque écriture, depuis
  des mois, sans que rien ne change — la démonstration empirique qu'un avertissement
  délivré n'arrête rien.

Ce banc garde le retrait dans les DEUX sens où il peut revenir :

- **le mécanisme** — aucune des faces qui écrivent ou font circuler une procédure ne
  rend de clé `digest_warning`, et aucun modèle de sortie ne la déclare ;
- **la prescription** — aucune surface SERVIE ne réclame le bloc : ni le socle poussé à
  chaque connexion, ni une description du catalogue MONTÉ, ni un guide servi à l'agent,
  ni le message d'un check.

⚠️ La garde balaie des SURFACES ENTIÈRES, pas une liste de fichiers : le catalogue est
énuméré depuis son registre et monté pour de bon, les guides depuis leurs seeds. Une
prescription réintroduite ailleurs que là où elle vivait est attrapée quand même.

⚠️ **Les corps existants gardent leur bloc.** Il y est devenu du texte ordinaire. Rien
ici ne l'interdit ni ne le migre : le retirer est un geste d'auteur quand quelqu'un
touche au texte. Obliger tout le monde à un geste pour satisfaire une convention
reproduirait exactement le défaut qu'on retire — c'est pourquoi la garde porte sur ce
que la PLATEFORME dit et rend, jamais sur ce qu'une procédure contient.
"""
import asyncio
import json
import re

import pytest
from fastmcp import FastMCP

import oto_mcp.capabilities  # noqa: F401 — peuple registry.CAPABILITIES
from oto_mcp import guide_store, instructions, openapi, procedure_retrait
from oto_mcp.capabilities import _mcp_adapter, registry
from oto_mcp.capabilities import guide_library as dl
from oto_mcp.capabilities.groups import guide as gd
from oto_mcp.capabilities.orgs import instructions as oi

# ⚠️ **Le mot NU, pas le marqueur.** Première version de ce banc : chercher
# `self-improvement digest` ou `digest_warning`. L'épreuve de chute l'a prise en défaut
# — le message de `retrait_check` disait « dis-le dans le digest », qui ne contient ni
# l'un ni l'autre et passait vert. Une garde qui vise la FORME du marqueur rate toute
# reformulation ; celle-ci vise le mot. Le faux positif redouté (le digest de signaux
# par e-mail) a été mesuré sur les surfaces balayées ici : zéro occurrence, sur les 327
# capacités du registre, sur le socle et sur les guides seeds.
MOT = re.compile(r"digest", re.I)
CLE = "digest_warning"


def _prescrit(texte: str) -> bool:
    return bool(texte) and bool(MOT.search(texte))


# ── Le module n'existe plus ─────────────────────────────────────────────────
def test_le_detecteur_nexiste_plus():
    """La capacité se SUPPRIME, elle ne se débranche pas : pas de drapeau, pas de `if`
    qui dort. Un module encore importable serait rebranché au premier besoin."""
    with pytest.raises(ImportError):
        import oto_mcp.procedure_digest  # noqa: F401


# ── Le mécanisme : aucune face ne rend l'avertissement ──────────────────────
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


_SANS_DIGEST = "## Goal\n\nUne procédure qui n'ouvre sur aucun bloc de citation.\n"


def _set_org(monkeypatch, body):
    monkeypatch.setattr(oi.org_store, "set_instruction", lambda *a, **k: 3)
    # La relecture rend le corps posé (empreinte de la version écrite, oto#133).
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda *a, **k: {"slots": [], "body_md": body})

    async def _wc(body_md, **k):
        return {"referenced_tools": [], "unresolved_tools": []}

    monkeypatch.setattr(oi.tool_registry, "write_check", _wc)
    return asyncio.run(oi._set_instruction(_Ctx(), _Inp(body)))


def test_ecrire_une_procedure_sans_le_bloc_ne_produit_plus_davertissement(monkeypatch):
    """Le fait central du ticket : l'écriture aboutit, et NE DIT RIEN du bloc absent."""
    out = _set_org(monkeypatch, _SANS_DIGEST)
    assert out["ok"] is True and out["version"] == 3
    assert CLE not in out, out
    assert not any("digest" in k for k in out), out


def test_le_palier_equipe_non_plus(monkeypatch):
    monkeypatch.setattr(gd.org_store, "set_instruction", lambda *a, **k: 2)
    monkeypatch.setattr(gd.org_store, "get_instruction",   # empreinte, oto#133
                        lambda *a, **k: {"body_md": _SANS_DIGEST})

    class _GInp(_Inp):
        group_id = 4

    out = gd._set(_Ctx(), _GInp(_SANS_DIGEST))
    assert CLE not in out and out["version"] == 2, out


def test_le_revert_non_plus(monkeypatch):
    """Revenir en arrière ramène par construction un corps d'avant la règle : c'est la
    face qui aurait le plus « légitimement » gardé l'avertissement."""
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda *a, **k: {"body_md": _SANS_DIGEST, "title": "t",
                                         "description": "d", "slots": []})
    monkeypatch.setattr(oi.org_store, "set_instruction", lambda *a, **k: 5)

    class _R:
        slug = "ma-procedure"
        version = 2

    out = oi._instruction_revert(_Ctx(), _R())
    assert CLE not in out and out["reverted_from"] == 2, out


def test_publier_et_forker_non_plus(monkeypatch):
    """Publier ou forker fait CIRCULER une procédure : ces deux faces recopiaient le
    signal pour qu'il parte avec elle."""
    # Seul un super_admin publie ; le rôle se pose à sa source, d'où dérivent les deux
    # prédicats plateforme.
    monkeypatch.setattr(dl.access, "get_user_role", lambda sub: "super_admin")
    monkeypatch.setattr(dl.db_communaute, "etat", lambda kind, i: {"state": "listed"})
    monkeypatch.setattr(dl, "_author_for", lambda ctx: ("otomata", None, "Otomata"))
    monkeypatch.setattr(dl.org_store, "get_instruction",
                        lambda otype, oid, slug: {"body_md": _SANS_DIGEST, "title": "t",
                                                  "description": "d", "slots": []})
    monkeypatch.setattr(dl.org_store, "publish_guide",
                        lambda **k: {"id": 1, "slug": "s", "version": 1,
                                     "visibility": "public"})

    class _P:
        slug = "s"; public_slug = None; title = None; description = None
        category = None; tags = None; visibility = "public"

    assert CLE not in dl._publish(_Ctx(), _P())

    monkeypatch.setattr(dl.org_store, "get_library_entry",
                        lambda **k: {"id": 1, "body_md": _SANS_DIGEST})
    monkeypatch.setattr(dl.org_store, "fork_library_entry",
                        lambda **k: {"owner_type": k["owner_type"], "owner_id": k["owner_id"],
                                     "slug": "s", "version": 1, "guide_id": 11,
                                     "forked_from": 1, "source_title": "t"})
    monkeypatch.setattr(dl.roles, "is_org_admin", lambda sub, org_id: True)

    class _F:
        slug = "s"; new_slug = None; scope = None; project_id = None

    assert CLE not in dl._fork(_Ctx(), _F())


def test_aucun_modele_de_sortie_ne_declare_la_cle():
    """Le contrat SERVI, pas seulement le dict : un champ déclaré remonte dans
    l'OpenAPI et dans les types du dashboard même quand plus rien ne le remplit."""
    coupables = [c.key for c in registry.CAPABILITIES
                 if c.Output is not None and CLE in getattr(c.Output, "model_fields", {})]
    assert coupables == [], coupables


# ── La prescription : aucune surface SERVIE ne réclame le bloc ───────────────
def test_le_socle_pousse_a_chaque_connexion_ne_le_prescrit_plus():
    """Le seul texte livré sans que l'agent le demande."""
    assert not _prescrit(instructions._SECRET_SAUCE)
    assert not _prescrit(instructions.render())


def test_aucune_description_du_catalogue_mcp_monte_ne_le_prescrit():
    """Sur le catalogue MONTÉ, pas sur les fichiers : c'est la description que le client
    reçoit qui pilote l'agent, et une prose écrite au mauvais endroit d'un descripteur
    n'atteint jamais le fil sans qu'on le voie."""
    m = FastMCP("t")
    _mcp_adapter.register(m, registry.CAPABILITIES)

    async def go():
        fautifs = []
        for cap in registry.caps_with_mcp():
            if not cap.is_exposed():
                continue
            outil = await m.get_tool(cap.mcp)
            if outil is not None and _prescrit(outil.description or ""):
                fautifs.append(cap.key)
        return fautifs

    assert asyncio.run(go()) == []


def test_le_contrat_rest_servi_ne_le_prescrit_pas_non_plus():
    """⚠️ **L'autre face, et c'est la MAJORITAIRE.** L'épreuve de chute a montré que le
    balayage MCP seul ne mord pas sur `org.instruction.set` : cette capacité-là est
    REST-only (`mcp=None`), comme 222 des 327 capacités du registre. Sa description part
    dans `/api/openapi.json`, servi sans authentification — une prescription réintroduite
    là serait servie à tout intégrateur sans qu'aucune garde MCP ne bronche.

    Le document est balayé ENTIER (descriptions d'opérations ET schémas), donc il attrape
    aussi le retour d'un champ `digest_warning` dans un modèle de sortie."""
    doc = json.dumps(openapi.build(), ensure_ascii=False)
    assert not _prescrit(doc), [
        c.key for c in registry.CAPABILITIES if _prescrit(c.description or "")]


def test_aucun_guide_servi_ne_le_prescrit():
    """Les guides sont du CODE DE PRODUCTION : c'est ce texte qui pilote l'agent quand
    il écrit une procédure. ⚠️ Ce banc voit les SEEDS ; les lignes en base priment sur
    eux et doivent être réalignées à la main (`oto_admin_guide`)."""
    fautifs = [g["slug"] for g in guide_store.list_file_guides()
               if _prescrit(g["body_md"]) or _prescrit(g.get("title") or "")
               or _prescrit(g.get("description") or "")]
    assert fautifs == [], fautifs


def test_aucun_message_de_check_ne_renvoie_lauteur_au_digest():
    """`retrait_check` disait « dis-le dans le digest » à chaque version qui retire une
    section — un remède qui, seul, aurait survécu au mécanisme qu'il nommait."""
    ancien = "# T\n\n## Phase 1\n\ndu texte\n\n## Phase 2\n\ndu texte\n"
    out = procedure_retrait.retrait_check(ancien, "# T\n\n## Phase 1\n\ndu texte\n")
    assert out["retrait_warning"], "le check ne mord plus : la mesure ne prouve rien"
    assert not _prescrit(out["retrait_warning"]), out
