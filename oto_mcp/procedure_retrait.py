"""Ce qu'une nouvelle version d'une procédure RETIRE — l'avertissement qui manquait.

Le digest d'auto-amélioration raconte ce qu'on **ajoute**. Rien n'oblige à dire ce qu'on
**retire**, et une réécriture « resserrée » retire par construction.

Mesuré le 01/09/2026 sur une procédure réelle de 132 versions (oto#61, signal 644) :
**22 colonnes du tableau de travail ne sont nommées nulle part dans la version servie**,
alors que plusieurs sont massivement remplies — 471 fiches sur 504 en portent une. Des
agents ne remplissent pas 300 fiches sans qu'une consigne le leur dise : la consigne le
disait, et ne le dit plus. Trois coupes datées, dont une de trois sections **sans un
mot** dans un digest par ailleurs méticuleux, qui justifiait chaque autre changement.

⚠️ **Le contenu perdu ne manque à personne sur le moment.** Il ne manque qu'aux agents
des mois suivants, qui ne peuvent pas regretter ce qu'ils n'ont jamais lu. C'est ce qui
rend ce défaut invisible sans instrument : il n'a pas de victime au moment où il se
produit.

⚠️ **Warning, jamais un refus** — même régime que `procedure_digest`, `procedure_diagram`
et `slots` (ADR 0014/0035). Retirer une section est parfaitement légitime : ce qui ne
l'est pas, c'est de le faire sans le savoir. La procédure s'enregistre, l'auteur reçoit
le signal, et il peut le porter dans son digest à la version suivante.

## Pourquoi les TITRES, et rien d'autre

On compare les titres de sections (`#`, `##`, …) présents avant et absents après. C'est
grossier, et c'est voulu : un diff de prose produirait du bruit à chaque reformulation,
et un avertissement qu'on reçoit toujours cesse d'être lu — c'est exactement le sort du
journal d'appels, que personne ne regarde. Un titre qui disparaît, en revanche, est un
pan de consigne qui s'en va.

⚠️ Ce module ne voit donc PAS un paragraphe retiré à l'intérieur d'une section conservée.
La coupe la plus fréquente reste visible (les trois occurrences mesurées portaient bien
sur des sections), mais ce n'est pas une garantie, et il ne faut pas le vendre comme
telle : un `retrait_warning` à `None` veut dire « aucune section entière n'a disparu »,
jamais « rien n'a été retiré ».
"""
from __future__ import annotations

import re

#: Un titre markdown, à n'importe quel niveau. Le `#` doit être suivi d'un espace —
#: sinon `#tag` en début de ligne compterait comme une section.
_TITRE = re.compile(r"^[ \t]*(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$")

#: Au-delà, on compte plutôt que d'énumérer : un avertissement qui déroule quarante
#: noms ne se lit pas, et une réécriture complète n'a pas besoin d'un inventaire pour
#: être reconnue par son auteur.
_MAX_NOMMES = 8


def _titres(body_md: str) -> list[str]:
    """Les titres du corps, dans l'ordre, sans les `#` ni la ponctuation de fin.

    Les blocs de code sont sautés : `# commentaire` dans un exemple shell n'est pas une
    section, et le compter ferait crier au retrait à chaque exemple modifié."""
    out: list[str] = []
    dans_code = False
    for ligne in (body_md or "").split("\n"):
        depouillee = ligne.strip()
        if depouillee.startswith("```") or depouillee.startswith("~~~"):
            dans_code = not dans_code
            continue
        if dans_code:
            continue
        m = _TITRE.match(ligne)
        if m:
            out.append(m.group(2).strip())
    return out


def sections_retirees(ancien_md: str, nouveau_md: str) -> list[str]:
    """Les titres présents dans l'ancien corps et absents du nouveau, dans l'ordre
    d'origine.

    La comparaison est faite sur le titre NORMALISÉ (casse et espaces) : renommer
    « ## Étapes » en « ## étapes » n'est pas un retrait, et le signaler apprendrait à
    ignorer l'avertissement. Un titre renommé pour de bon, lui, est bien un retrait —
    on ne sait pas distinguer un renommage d'une suppression suivie d'un ajout, et
    prétendre le contraire demanderait de deviner l'intention."""
    def _cle(t: str) -> str:
        return " ".join(t.lower().split())

    apres = {_cle(t) for t in _titres(nouveau_md)}
    vus: set[str] = set()
    out: list[str] = []
    for t in _titres(ancien_md):
        k = _cle(t)
        if k not in apres and k not in vus:
            vus.add(k)
            out.append(t)
    return out


def retrait_check(ancien_md: str, nouveau_md: str) -> dict:
    """Check croisé à l'écriture, dans la forme des autres (`digest_check`,
    `diagram_check`, `slots_check`) : la clé est TOUJOURS présente, `None` = rien à
    signaler. Best-effort — un check ne casse jamais une écriture.

    `ancien_md` vide (création, ou ancien corps illisible) ⟹ `None` : il n'y a rien
    dont on puisse dire qu'il a été retiré."""
    try:
        if not (ancien_md or "").strip():
            return {"retrait_warning": None}
        partis = sections_retirees(ancien_md, nouveau_md)
        if not partis:
            return {"retrait_warning": None}
        nommes = ", ".join(f"« {t} »" for t in partis[:_MAX_NOMMES])
        reste = len(partis) - _MAX_NOMMES
        if reste > 0:
            nommes += f", et {reste} de plus"
        return {"retrait_warning": (
            f"cette version RETIRE {len(partis)} section(s) : {nommes}. "
            "Si c'est voulu, dis-le dans le digest — le prochain agent ne peut pas "
            "regretter une consigne qu'il n'a jamais lue. Si ça ne l'est pas, relis "
            "`op=get with_history=true` et rejoue ton édition sur la version à jour.")}
    # noqa: SILENT — contrôle de forme optionnel : pas d'avertissement plutôt qu'un faux
    except Exception:  # noqa: BLE001 — cf. `digest_check`
        return {"retrait_warning": None}
