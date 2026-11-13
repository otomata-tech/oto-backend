"""L'EMPREINTE d'une panne d'agent, et la copie qu'une cause dit au client (13/11/2026).

Le médecin des agents range une panne par regex (`medecin_agents.classer`) : trois classes,
et tout le reste `inconnu`, que l'agent médecin diagnostique UN ÉPISODE À LA FOIS. Ce
qu'il trouve reste sur cet épisode : la même erreur dans une autre org repart de zéro, et
deux diagnostics de la même erreur ont déjà rendu deux côtés opposés.

L'empreinte est le motif d'échec dont on a retiré ce qui change d'une occurrence à
l'autre — adresses, courriels, identifiants, dates, nombres, texte cité —, passé par une
LISTE BLANCHE de mots (`_liste_blanche` : ne reste que ce qui a la forme d'un mot du
vocabulaire d'erreur, le reste devient `<id>` ou `<nom>`), puis mis en minuscules et
resserré. Deux pannes de même cause dans deux orgs ont la même empreinte ;
le CATALOGUE (`db/causes_agents.py`) range une empreinte une fois, pour toutes les orgs.

⚠️ L'empreinte se calcule sur le motif TRONQUÉ (`alertes_agents.MOTIF_TETE`), comme la
classe : deux erreurs qui ne diffèrent qu'au-delà de la tête partagent l'empreinte. C'est
pourquoi, tant qu'une cause n'a pas fait ses preuves, elle ne décide d'AUCUN geste :
le catalogue tourne d'abord en OMBRE (`medecin_agents.balayer`), il dit ce qu'il aurait
décidé et la regex décide.

⚠️ La copie d'une cause est lue par le CLIENT : `copie_refusee` la refuse si elle porte
ce qu'un client n'a pas à lire — un nom d'outil, un code, une adresse, un identifiant.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

#: Une empreinte plus longue ne distingue plus rien d'utile : la tête du motif suffit.
LONGUEUR_MAX = 160

# L'ordre compte : une adresse contient des nombres, une date aussi.
_EFFACER: tuple[tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"https?://\S+", re.I), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
     "<id>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                r"(?:z|[+-]\d{2}:?\d{2})?)?", re.I), "<date>"),
    (re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"), "<date>"),
    (re.compile(r"\b(?:req|msg|run|job|toolu|call)_[A-Za-z0-9]{6,}\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{12,}\b", re.I), "<id>"),
    # Un jeton de 12 caractères ou plus qui mêle au moins trois chiffres et deux lettres
    # est un identifiant généré (nom d'unité, de sandbox), pas un mot.
    (re.compile(r"\b(?=[a-z0-9]*\d[a-z0-9]*\d[a-z0-9]*\d)(?=[a-z0-9]*[a-z][a-z0-9]*[a-z])"
                r"[a-z0-9]{12,}\b", re.I), "<id>"),
    # Une apostrophe COLLÉE à une lettre est une élision (« l'org n'a pas ») : seule une
    # apostrophe qui ouvre après un blanc et ferme avant un blanc cite un texte.
    (re.compile(r"«[^»]{0,200}»|“[^”]{0,200}”|\"[^\"]{0,200}\"|`[^`\n]{0,120}`"
                r"|(?<!\w)'[^'\n]{1,80}'(?!\w)"),
     "<texte>"),
    (re.compile(r"\d+(?:[.,]\d+)*"), "<n>"),
)
_BLANCS = re.compile(r"\s+")

# La LISTE BLANCHE, passée après `_EFFACER` : le catalogue est commun à toutes les orgs,
# une empreinte ne garde donc que ce qu'on SAIT être sans client. Effacer ce qu'on
# reconnaît (ci-dessus) laisse passer ce qu'on n'a pas prévu — un hôte sans schéma, un
# chemin, un nom propre, un identifiant court ; ici, un mot n'est GARDÉ que s'il a la
# forme d'un mot du vocabulaire d'erreur, et tout le reste est remplacé :
#   - un repère déjà posé (`<id>`, `<n>`…) et la ponctuation ;
#   - un mot en minuscules, lettres seules (élision, `-` et `_` entre lettres admis :
#     `n'a`, `x-api-key`, `overloaded_error`) ;
#   - un sigle de 2 à 6 capitales (`HTTP`, `POST`, `JSON`) ;
#   - une majuscule initiale EN TÊTE DE PHRASE seulement (`Unit`, `Aucun`) ; ailleurs,
#     c'est un nom propre → `<nom>`, comme toute casse mêlée ou tout sigle plus long ;
#   - tout le reste — chiffre, point, barre, `@`, `=`… — est un identifiant → `<id>`.
# ⚠️ Limite connue, dite plutôt que tue : un nom propre EN TÊTE DE PHRASE, ou écrit tout
# en minuscules, a la forme d'un mot et passe. `tests/test_causes_pannes.py` fixe ce que
# la liste blanche garantit.
_REPERE = re.compile(r"^(?:<(?:url|email|id|date|texte|n|nom)>)+$")
_BORDS = re.compile(r"^([^\w<]*)(.*?)([^\w>]*)$", re.S)
_SEPARATEURS_DE_MOT = re.compile(r"['’_-]")
_FIN_DE_PHRASE = (".", "!", "?")


def _mot(coeur: str, tete_de_phrase: bool) -> str:
    if not coeur or _REPERE.match(coeur):
        return coeur
    morceaux = _SEPARATEURS_DE_MOT.split(coeur)
    if not all(m.isalpha() for m in morceaux):
        return "<id>"
    lettres = "".join(morceaux)
    if lettres.islower():
        return coeur
    if lettres.isupper() and 2 <= len(lettres) <= 6:
        return coeur
    if lettres[0].isupper() and (len(lettres) == 1 or lettres[1:].islower()):
        return coeur if tete_de_phrase else "<nom>"
    return "<nom>"


def _liste_blanche(texte: str) -> str:
    sortie, tete = [], True
    for jeton in texte.split():
        avant, coeur, apres = _BORDS.match(jeton).groups()
        sortie.append(avant + _mot(coeur, tete) + apres)
        tete = jeton.endswith(_FIN_DE_PHRASE)
    return " ".join(sortie)


def de(motif: Optional[str]) -> Optional[str]:
    """L'empreinte d'un motif d'échec, ou None pour un motif vide."""
    texte = (motif or "").strip()
    if not texte:
        return None
    for motif_re, remplacement in _EFFACER:
        texte = motif_re.sub(remplacement, texte)
    texte = _BLANCS.sub(" ", _liste_blanche(texte).lower()).strip()
    return texte[:LONGUEUR_MAX] or None


def cle(empreinte: str) -> str:
    """La clé d'une empreinte dans le catalogue : courte, stable, sans rien du motif."""
    return hashlib.sha256(empreinte.encode("utf-8")).hexdigest()[:16]


#: Ce qu'une copie au client ne porte jamais : une adresse (le bouton porte le lien), un
#: nom d'outil ou de colonne (`snake_case`), du code entre accents graves, un code HTTP
#: ou un identifiant (trois chiffres ou plus d'affilée), une accolade hors `{marque}`.
_INTERDITS_COPIE: tuple[tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"https?://|www\.", re.I), "une adresse"),
    (re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b", re.I), "un nom d'outil ou de code"),
    (re.compile(r"`"), "du code"),
    (re.compile(r"\d{3,}"), "un code ou un identifiant"),
    (re.compile(r"\{(?!marque\})[^}]*\}"), "une variable inconnue"),
)


def copie_refusee(texte: Optional[str]) -> Optional[str]:
    """Pourquoi ce texte ne peut pas être dit au client, ou None s'il le peut."""
    for motif_re, quoi in _INTERDITS_COPIE:
        if motif_re.search(texte or ""):
            return f"la copie au client porte {quoi}"
    return None
