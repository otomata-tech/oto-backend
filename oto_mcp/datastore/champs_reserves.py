"""Les champs que l'appelant N'ÉCRIT PAS — le cran de schéma et son préavis (#586, #606).

Deux crans, une seule garde (`reserved_refusals`), et ils répondent à la même question
dans le même ordre : *à qui appartient cette destination ?*

- `readonly: true` — la colonne porte la valeur remise par le client ; une écriture
  qui la CHANGE en place est refusée en nommant la colonne, la raison et où porter la
  divergence (`report_to`). Le forçage sur l'appel est arbitré par `forcage.py` ;
- `origine` — la couche `<champ>.origine` est fermée à l'appelant : la lui laisser
  écrire revenait à lui laisser détruire l'unique copie de la valeur d'import.
  ⚠️ Le cran `origine: "system"` qui la POSAIT automatiquement a été SUPPRIMÉ le
  08/09/2026 (`declaration.system_origin_fields` rend `set()`) : plus rien ne capture
  de lui-même. L'origine se pose désormais par `donnees_d_origine`, sur l'appel qui
  apporte la donnée. Le refus d'écrire la couche, lui, reste — daté au 01/10/2026 ;
- `agent_access` — délégué à `acces_agent.py`, dont ce module appelle les refus.

La seconde moitié du fichier est un PRÉAVIS DATÉ, pas une règle : le paramètre
`origine_override` reste accepté jusqu'à `ORIGINE_REFUS_LE`, avec avertissement, puis
il est refusé. Tout ce qui l'entoure — la date lue de l'environnement (`date_refus`),
sa forme française, la description servie à l'agent, les deux gestes de remplacement —
existe pour que la bascule soit un seul chiffre à changer, et pour qu'un agent qui
lit l'outil sache AVANT le refus ce qui va se passer.

Ce qu'il ne tient pas :
- **le palier qui décide qui peut forcer** → `core.DatastorePg._forcage_readonly` ;
- **le geste du store** qui applique ces refus → `reserves.py` ;
- **la grammaire d'`agent_access`** et ses valeurs → `acces_agent.py` ;
- **la validation à la POSE** de ces crans (valeur inconnue, cran sous un
  sous-record) → `definition._validate_reserved_def`.
"""
from __future__ import annotations

from datetime import date as _date, datetime as _datetime, timezone as _timezone
from typing import Any, Optional

from . import acces_agent as aga
from . import forcage as fcg

from .couches import (
    layer_value,
    names_layers,
    ORIGIN_LAYER,
    ORIGINE_INCONNUE,
    same_value,
    unwrap,
    VALUE_LAYER,
)
from .declaration import readonly_fields
from .formule import colonnes_formule

#: Le paramètre par lequel un appelant DÉCLARE qu'il pose l'origine en connaissance de
#: cause. Nommé par cohérence stricte avec `readonly_override` (#658) : même famille de
#: geste — un cran qu'on lève EXPLICITEMENT sur l'appel, jamais par un état qu'on laisse
#: traîner — et un agent qui connaît l'un devine l'autre.
#:
#: ⚠️ **Ce n'est pas un droit à accorder, c'est une déclaration à faire** (décision
#: d'Alexis, 05/09/2026 : « c'est notre modèle d'agent experience »). Écrire l'origine
#: reste possible pour tout le monde ; ce qui est refusé, c'est de l'écrire EN SILENCE,
#: sans dire qu'on sait ce qu'on fait. Rien à demander à personne, rien à provisionner :
#: le paramètre suffit, et sa présence engage celui qui l'envoie.
PARAMETRE_ORIGINE = "origine_override"

#: La date à partir de laquelle une écriture d'origine NON DÉCLARÉE est refusée.
#:
#: ⚠️ **Elle vit ici, dans le code, et c'est délibéré** — le contraire de ce que ce
#: commentaire disait au barreau 1. Une date qui n'existerait que dans l'env d'une box
#: se lit « prochainement » partout où personne ne l'a posée : le produit annoncerait
#: une échéance floue et n'en tiendrait aucune, et l'écart ne se verrait nulle part.
#: Ici, ce que le tronc ANNONCE est exactement ce qu'il REFUSERA, sans dépendre d'un
#: geste sur une machine.
#:
#: Le réglage ci-dessous la DÉPLACE sans déploiement (`YYYY-MM-DD`), ce qui était la
#: vraie exigence : la fenêtre bougera si un écrivain se manifeste.
#:
#: Pourquoi le 1er octobre 2026 (arbitré le 05/09/2026, et contestable comme tel) : les
#: écritures concernées vont de sept à cinquante-deux lignes par semaine et MONTENT, il
#: faut donc couvrir plusieurs cycles hebdomadaires entiers — vingt-six jours en
#: couvrent trois — et le lecteur de l'avertissement est un agent, qui peut s'adapter
#: dès sa première lecture.
ORIGINE_REFUS_LE = _date(2026, 10, 1)

#: Déplace la date sans déployer. Format `YYYY-MM-DD` — la seule forme non ambiguë, et
#: le texte français servi en est DÉRIVÉ : deux réglages (« la date affichée » et « la
#: date qui refuse ») divergeraient, et c'est l'affichage qui aurait tort.
#: ⚠️ Une valeur illisible LÈVE, elle ne retombe pas sur le défaut : un préavis dont la
#: date est muette annoncerait une échéance que rien n'applique.
ENV_ORIGINE_REFUS_LE = "OTO_ORIGINE_REFUS_LE"

_MOIS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre")


def date_refus() -> "_date":
    """La date en vigueur : le réglage s'il est posé, le défaut du code sinon."""
    import os

    brut = (os.environ.get(ENV_ORIGINE_REFUS_LE) or "").strip()
    if not brut:
        return ORIGINE_REFUS_LE
    try:
        return _date.fromisoformat(brut)
    except ValueError:
        raise ValueError(
            f"{ENV_ORIGINE_REFUS_LE}={brut!r} n'est pas une date `YYYY-MM-DD`. "
            "Ce réglage décide à la fois de ce qui est ANNONCÉ et de ce qui est "
            "REFUSÉ : le lire de travers ferait promettre une échéance que rien "
            "n'applique.") from None


def date_refus_fr() -> str:
    """La date en vigueur, telle qu'on l'écrit dans un texte servi."""
    return _en_francais(date_refus())


def description_parametre_origine(en: bool = False) -> str:
    """Ce que les DEUX faces disent du paramètre, dans leur description servie.

    ⚠️ **Une capacité qu'aucun texte ne nomme n'existe pas pour un agent** — il ne la
    découvrira pas, il retombera sur la manœuvre qu'on cherche à supprimer. C'est
    exactement ce qui s'est passé sur l'autre verrou (#658/#668) : le refus était exact,
    la sortie n'était écrite nulle part, et deux agents ont réinventé « lever, écrire,
    remettre ».

    ⚠️ **« l'origine est conservée » a été retiré le 08/09/2026 : c'était une promesse
    INCONDITIONNELLE, et elle ne vaut que dans un sens de l'ordre des gestes.** La
    plateforme ne conserve l'origine que si la colonne portait déjà le cran quand la
    ligne est arrivée ; déclaré après coup, le cran ne reconstitue rien et les lignes
    déjà là reçoivent le marqueur. Ce texte est servi sur la porte de l'IMPORT
    (`uploads`) — précisément là où l'ordre se joue — et il a fait croire au
    propriétaire du produit que des valeurs d'origine existaient. **Un texte servi est
    cru davantage qu'une mesure** : celui qui lit n'a aucune raison d'aller vérifier ce
    que la plateforme lui promet sur elle-même. La condition se dit donc DANS la
    phrase, pas dans une documentation à côté.

    ⚠️ Une seule phrase pour les deux faces, dérivée comme le reste : la face REST et la
    face MCP décriraient sinon le même paramètre en deux termes, et l'écart se lirait
    comme deux paramètres différents.

    Évaluée à l'appel, pas figée à l'import : la date peut être déplacée par le réglage
    et la description doit dire celle qui refuse.

    `en` = la face MCP, dont les descriptions d'outils sont en anglais. MÊME fonction
    et non deux textes indépendants : le nom du paramètre et la date sortent d'une
    seule source, et c'est sur ces deux-là qu'un écart se paierait — une description
    qui nommerait un autre paramètre, ou une autre date, que celle qui refuse.

    ⚠️ La face MCP ne peut pas COMPOSER sa description : `@mcp.tool()` lit la
    docstring littérale du handler, et la remplacer par `description=` emporterait
    aussi les descriptions d'arguments. Le texte anglais y est donc RECOPIÉ, et un
    banc exige qu'il soit celui-ci, mot pour mot — la copie est surveillée, faute de
    pouvoir être évitée."""
    if en:
        return (f"`{PARAMETRE_ORIGINE}=true` states that this call sets the "
                f"`origine` layer (the value at the START, at import time) "
                f"knowingly. Without it, writing an origin is refused from "
                f"{date_refus()} on. ⚠️ Nothing captures an origin automatically any "
                f"more: `origine: \"system\"` was REMOVED on 2026-09-08, so writing "
                f"the value alone keeps nothing — an overwrite is final. For a real "
                f"IMPORT, prefer `donnees_d_origine=true`, which writes both versions "
                f"— the current value and the origin — in the same gesture, at the "
                f"moment the value enters. This parameter only says \"I know I am "
                f"setting that layer\", and it applies to this call only. "
                f"⚠️ You may still meet the marker \"{ORIGINE_INCONNUE}\" in an "
                f"`origine` layer: it was left by the removed mechanism on rows it "
                f"could not reconstruct. It is a LOSS, not a capture.")
    return (f"`{PARAMETRE_ORIGINE}=true` déclare que cet appel pose la couche "
            f"`origine` (la valeur du DÉPART, à l'import) en le sachant. Sans lui, une "
            f"écriture d'origine est refusée à partir du {date_refus_fr()}. "
            f"⚠️ Plus rien ne capture une origine automatiquement : "
            f"`origine: \"system\"` a été SUPPRIMÉ le 08/09/2026, donc écrire la "
            f"valeur seule ne garde rien — un écrasement est définitif. Pour un vrai "
            f"IMPORT, préférez `donnees_d_origine=true`, qui écrit les DEUX versions "
            f"— la valeur courante et l'origine — dans le même geste, au moment où la "
            f"valeur entre. Ce paramètre-ci dit seulement « je sais que je pose cette "
            f"couche », et il ne vaut que pour cet appel. ⚠️ Vous pouvez encore "
            f"rencontrer le marqueur « {ORIGINE_INCONNUE} » dans une couche "
            f"`origine` : il a été laissé par le mécanisme retiré sur les lignes "
            f"qu'il ne pouvait pas reconstituer. C'est une PERTE, pas une capture.")


def _en_francais(quand: "_date") -> str:
    """La date telle qu'une personne la lit. DÉRIVÉE de la date qui refuse — un texte
    saisi à côté d'elle finirait par annoncer un autre jour que celui qui coupe."""
    jour = "1er" if quand.day == 1 else str(quand.day)
    return f"{jour} {_MOIS_FR[quand.month - 1]} {quand.year}"


def refus_arme(aujourdhui: Optional["_date"] = None) -> bool:
    """Le refus est-il tombé ? — `aujourdhui` en UTC, pas au fuseau du process : la
    bascule doit tomber au même instant sur toutes les box, et le fuseau d'une machine
    n'est pas un fait de produit."""
    jour = aujourdhui or _datetime.now(_timezone.utc).date()
    return jour >= date_refus()


def _les_deux_gestes(colonnes: list, maintenant: bool = False) -> str:
    """Les deux issues, côte à côte — le CORPS que l'avertissement et le refus
    partagent.

    ⚠️ Partagé, pas recopié : le refus doit dire exactement ce que l'avertissement
    disait, sinon celui qui s'est préparé pendant le préavis découvre au moment du
    refus qu'on lui demandait autre chose.

    ⚠️ **Les DEUX, toujours.** Celui qui n'a pas besoin d'écrire l'origine ne doit pas
    ajouter un paramètre pour rien, et celui qui en a besoin ne doit pas réécrire son
    import. Un texte qui ne dirait qu'une des deux issues ferait bouger des appels qui
    n'ont rien à changer.

    ⚠️ **Et le chemin de l'UPLOAD est nommé**, parce que c'est là que le conseil « ajoutez
    le paramètre à cet appel » est impossible à suivre : un import par URL signée pousse
    des octets, il ne passe aucun paramètre. Lui dire seulement où le paramètre va sur un
    appel MCP, c'est l'envoyer chercher une manœuvre — il faut qu'il lise, là où il est,
    que sa déclaration se fait au moment où l'URL est créée.

    ⚠️ **Et la DESTINATION de l'intention est nommée (oto#79).** Sur dix-huit écritures
    refusées, huit rejouaient le geste refusé — la reprise la plus rapide à neuf
    secondes. L'agent ne cherchait pas à écrire une valeur : il cherchait à dire D'OÙ
    elle venait, et aucun de ces textes ne nommait la couche qui accueille précisément
    ça. Le refus voisin, celui d'une colonne servie en lecture, le fait depuis toujours
    — le bon texte existait déjà à côté, il manquait là où il comptait le plus. Une
    garde neuve qui ferme un geste répandu doit dire par quoi le remplacer.

    ⚠️ Les deux gestes restent DEUX : la phrase ajoutée ne décrit pas un troisième geste
    sur l'origine, elle renvoie une AUTRE intention vers une autre couche."""
    quand = ", dès maintenant" if maintenant else ""
    col = str(colonnes[0]) if colonnes else "<colonne>"
    return (
        f"Deux gestes, l'un ou l'autre{quand} : si vous n'avez pas besoin d'écrire "
        "l'origine, écrivez la valeur seule (l'origine est conservée, et posée par la "
        "plateforme quand elle manque) ; si votre import doit vraiment la poser, "
        f"ajoutez `{PARAMETRE_ORIGINE}: true` à cet appel — ou, si vous chargez un "
        f"fichier par URL signée, à l'appel qui a CRÉÉ l'URL (`oto_upload_url`), le PUT "
        "ne portant aucun paramètre. Rien à demander à personne : ce paramètre déclare "
        "que vous savez ce que vous écrivez, et il suffit. "
        f"⚠️ Et si votre intention était de dire D'OÙ VIENT cette valeur — sa source, "
        f"qui vous l'a remise —, ce n'est pas `origine` : cela se pose en commentaire de "
        f"la colonne (`{col}.comment`, soit `{{\"{col}\": {{\"comment\": …}}}}`), qui "
        f"reste ouverte à l'écriture. `origine` ne dit qu'une chose : quelle était la "
        f"valeur du DÉPART, à l'import.")


def avertissement_origine(colonnes: list) -> str:
    """La phrase servie à qui pose une origine sans le déclarer, AVANT la date.

    ⚠️ Elle VOUVOIE et nomme le geste exact : c'est une personne qui décidera d'agir
    dessus, et un avertissement qui ne dit pas quoi faire à la place ne fait que gêner.
    Servie par le SERVEUR — l'écran comme l'agent la rendent telle quelle.

    ⚠️ **Elle est la SEULE annonce.** Décision d'Alexis (05/09/2026) : aucun client ne
    sera prévenu par un envoi. Personne ne recevra de courriel, personne ne lira de note
    de version — ce texte-ci, répété à chaque écriture, est tout ce que l'écrivain aura.

    ⚠️ Premier temps d'un préavis en DEUX temps (oto#70 lot 2). Ce premier temps est
    aussi l'INSTRUMENT — le journal d'appels ne porte pas les couches (clés de premier
    niveau seulement, arguments tronqués), donc seuls les écrivains peuvent nous dire
    combien ils sont."""
    quoi = ", ".join(f"`{c}`" for c in colonnes)
    return (
        f"Cette écriture pose la couche `origine` de {quoi}. L'origine est la valeur du "
        f"départ, à l'import : la poser SANS LE DIRE sera refusé à partir du "
        f"{_en_francais(date_refus())}. Écrire l'origine reste possible — ce qui change, "
        f"c'est qu'il faudra le déclarer. {_les_deux_gestes(colonnes, maintenant=True)}")


def refus_origine(colonnes: list) -> str:
    """Le refus, une fois la date passée. MÊME corps que l'avertissement.

    ⚠️ Il ne renvoie vers personne, et c'est le fond de la décision : il n'y a pas de
    droit à obtenir, donc pas de tiers à qui écrire. Un refus qui enverrait demander
    quelque chose ferait attendre une réponse qui ne viendra jamais — et, comme sur
    l'autre verrou de la plateforme (#668), enverrait chercher une manœuvre."""
    quoi = ", ".join(f"`{c}`" for c in colonnes)
    return (
        f"Cette écriture pose la couche `origine` de {quoi} sans la déclarer — rien n'a "
        f"été écrit. L'origine est la valeur du départ, à l'import : depuis le "
        f"{_en_francais(date_refus())}, la poser exige de le dire. Écrire l'origine "
        f"reste possible. {_les_deux_gestes(colonnes)}")


def origine_posee(payload: Optional[dict], avant: Optional[dict] = None) -> list[str]:
    """Les colonnes dont CET appel pose ou modifie la couche `origine`.

    Sert l'avertissement du premier temps (oto#70 lot 2) : le journal d'appels ne peut
    pas dire qui écrit une couche — `arg_keys` ne garde que le premier niveau, et la
    fiche d'un appel tronque les arguments. Ce sont donc les écritures elles-mêmes qui
    doivent se signaler.

    ⚠️ **Une origine réécrite À L'IDENTIQUE ne compte pas.** Relire une ligne puis la
    repousser telle quelle est un geste banal, et le compter ferait crier l'avertissement
    sur des appels qui ne changent rien — un avertissement qu'on reçoit toujours cesse
    d'être lu, et c'est justement l'instrument qu'on essaie de fabriquer.

    ⚠️ Indépendante du format déclaré : elle regarde ce que l'APPELANT écrit, pas ce que
    la colonne autorise. Sur une colonne déclarée, `reserved_refusals` refuse déjà — cette
    liste-ci sert les autres, celles où l'écriture passe aujourd'hui sans un mot.
    """
    out: list[str] = []
    for cle, neuf in (payload or {}).items():
        if not (names_layers(neuf) and ORIGIN_LAYER in neuf):
            continue
        if same_value(neuf[ORIGIN_LAYER], _origine_attendue(avant, cle, neuf)):
            continue
        out.append(cle)
    return sorted(out)


def marqueurs_poses_warning(combien: int) -> Optional[str]:
    """Ce qu'une déclaration tardive du cran `origine: "system"` faisait.

    ⚠️ **Le cran est SUPPRIMÉ depuis le 08/09/2026** : cette fonction ne parle plus
    d'aucun mécanisme vivant. Elle est gardée tant que `_capturer_origine_des_colonnes_neuves`
    l'est — ce dernier rend `0` en nommant le retrait, et les deux s'éteindront ensemble.

    ⚠️ **La clé servie s'appelle `origines_capturees`, et c'est l'inverse de ce qui
    s'est passé.** Rien n'a été capturé : la plateforme a écrit le marqueur « origine
    inconnue » sur des lignes dont la valeur de départ était déjà perdue. Un nombre
    sous ce nom se lit comme un succès — « 837 origines capturées ! » — alors qu'il
    compte des aveux.

    Mesuré le 08/09/2026 : un tableau de production porte **837 marqueurs sur 846
    couches d'origine**, et la restitution promise à une cliente y est muette. Le
    nombre avait été rendu à la pose, personne n'avait de raison de le lire comme une
    alerte, et la découverte s'est faite trois semaines plus tard.

    ⚠️ **La clé n'est PAS renommée** — elle est servie, et un consommateur peut la
    lire. On ajoute la phrase à côté, on ne déplace pas ce qui existe. Ce qui nuisait
    n'était pas le nom seul, c'était le nom SANS phrase.

    **Et la phrase dit le geste qui l'évite**, parce que c'est un ordre et non un
    défaut : déclarer le cran AVANT l'import ne balise rien du tout.
    """
    if not combien:
        return None
    return (f"{combien} cellule(s) marquées « origine inconnue » — et c'est une PERTE, "
            "pas une capture. Ces lignes existaient déjà quand le format d'origine a "
            "été déclaré : leur valeur de départ avait pu être écrasée par un agent, "
            "et la plateforme refuse de présenter le travail d'un agent comme la "
            "donnée de la personne qui l'a fournie. Ce qui manque là ne se "
            "reconstituera pas.\n"
            "⚠️ Le geste qui l'évite entièrement : porter `donnees_d_origine=true` sur "
            "l'appel qui APPORTE la donnée — il pose la version d'origine au moment où "
            "la valeur entre, donc il ne dépend d'aucun ordre. (L'ancien conseil "
            "— déclarer `origine: \"system\"` avant d'importer — n'a plus d'objet : ce "
            "cran est SUPPRIMÉ depuis le 08/09/2026.)")


def reserved_refusals(schema: Optional[dict], payload: Optional[dict],
                      avant: Optional[dict] = None, *,
                      forcage: Optional["fcg.Forcage"] = None,
                      agent: bool = False) -> tuple[list[str], dict]:
    """Les refus « champ que l'appelant n'écrit pas » → `(messages, details)`.

    `payload` = ce que le geste POSE (après arbitrage des vides, #608) ; `avant` = la
    ligne en place (`None` sur une création). Une seule question : ce geste écrit-il
    ce qui ne lui appartient pas ? — et **une valeur identique n'est pas une
    écriture** (29/08/2026, huit charges d'écriture échantillonnées : le geste
    dominant réémet la fiche entière, valeurs verrouillées comprises ; #623 refusait
    l'identique et aurait arrêté la campagne — une flotte à l'arrêt, pas un garde-fou).

    - `origine` — la couche est NOMMÉE dans le payload avec une valeur DIFFÉRENTE de
      ce que le système poserait → refus, création comprise. Ce que le système
      poserait : l'origine déjà stockée ; sinon la valeur de base en place ; à la
      création, la valeur écrite. Égale → acceptée, c'est un no-op (le geste dominant
      du terrain : `{"valeur": <identique>, "origine": <la même>}`).
      ⚠️ Ce refus ne dépend PLUS d'un cran de schéma : `origine: "system"` est supprimé
      depuis le 08/09/2026, la règle vaut pour toute colonne — et elle est datée au
      01/10/2026 (`ORIGINE_REFUS_LE`), levable par `origine_override` ;
    - `readonly: true` — le payload NOMME la valeur (nue, `null`, ou `{"valeur": …}`)
      d'une ligne en place ET elle CHANGE → refus. Identique → no-op silencieux, les
      couches restent (substrat, `_merge_column`) ; `{"valeur": <identique>,
      "comment": …}` écrit le comment, c'est le geste utile. Une création n'écrase
      rien (un tableau qui ne doit pas grossir se ferme par `key_required`). La
      colonne-clé ne se pose pas en `readonly` (refusé à la déclaration : elle se
      protège par `key_required`) ; un schéma legacy qui la porterait n'est pas
      fermé, puisque l'identique passe. **Un `forcage` TENU lève ce refus-là, pour
      cet appel seulement** (#658, `forcage.py`) — l'autre cran, lui, ne se force
      pas : il ferme ce que la PLATEFORME pose, pas ce que le client a remis ;

    - `agent_access: "read" | "none"` (oto#83) — la colonne appartient au PROPRIÉTAIRE
      du tableau et le geste vient d'un AGENT (`agent=True`, décidé par la face, jamais
      deviné ici) → refus. `"none"` refuse TOUTE mention : la colonne ne lui est pas
      servie, il ne peut donc pas la tenir d'une lecture — s'il la nomme, il l'a
      inventée ou héritée d'un état d'avant le réglage. `"read"` suit la règle de
      `readonly` — une valeur IDENTIQUE n'est pas une écriture, et les couches
      `comment`/`link` restent ouvertes, c'est là qu'un agent pose ce qu'il a constaté.
      ⚠️ Contrairement à `readonly`, la CRÉATION est concernée : `readonly` protège une
      valeur remise par le client, qu'une création n'écrase pas ; ici c'est la
      DESTINATION qui n'est pas à l'agent, et elle ne l'est pas davantage sur une ligne
      neuve. Aucun forçage : la sortie du propriétaire est son écran, où rien de tout
      ceci ne s'applique (`agent=False`) — il n'y a donc rien à lever.

    `details.expected_column` = `<colonne>.comment`, pour la face REST (#545) — un
    front pointe la destination sans reparser une phrase. ⚠️ **Le refus NOMME
    désormais le geste** (#658, arbitré le 02/09/2026) : qui peut forcer et comment.
    Il ne l'enseignait pas, au motif que la sortie du propriétaire était le schéma —
    or c'est précisément ce silence qui a produit la manœuvre « lever, écrire,
    remettre » sur `key_required` (#668), dont une exécution interrompue laisse le
    verrou ouvert sans aucun signal.

    ⚠️ Ici et pas dans le registre des jetons (#602) : celui-ci juge AVANT la
    résolution, sans schéma ; un champ réservé est une propriété du TABLEAU."""
    ro = readonly_fields(schema)
    cf = colonnes_formule(schema)
    # oto#83 : vides hors face agent — le cran ne borne que ce que la face a déclaré
    # être un appel de modèle. Deux ensembles disjoints : ce qui n'est pas servi du
    # tout, et ce qui est servi en lecture seule.
    masques = aga.masquees(schema) if agent else set()
    lecture = (aga.fermees(schema) - masques) if agent else set()
    errors: list[str] = []
    details: dict = {}
    if not ro and not masques and not lecture:
        return errors, details
    for cle, neuf in (payload or {}).items():
        if cle in masques:
            errors.append(aga.refus(schema, cle, aga.AUCUN))
            continue
        if cle in lecture \
                and (not names_layers(neuf) or VALUE_LAYER in neuf) \
                and not same_value(unwrap(neuf), unwrap((avant or {}).get(cle))):
            errors.append(aga.refus(schema, cle, aga.LECTURE))
            continue
        # ⚠️ La branche qui refusait ici l'écriture d'une origine RÉSERVÉE est retirée
        # (oto#79). Son cran a été supprimé le 08/09/2026 : `system_origin_fields` rend
        # `set()` même sur une colonne qui le déclare, donc elle ne pouvait plus jamais
        # servir — et son texte promettait un filet qui n'existe plus (« l'origine est
        # conservée, et posée si elle manque »). L'écriture d'une origine se juge
        # désormais ailleurs, par la déclaration (`origine_override`).
        if cle in ro and avant is not None \
                and (not names_layers(neuf) or VALUE_LAYER in neuf) \
                and not same_value(unwrap(neuf), unwrap(avant.get(cle))):
            # #658 : le forçage se juge ICI, sur la même condition que le refus —
            # ce qui garantit qu'il ne peut porter QUE sur ce que le cran refusait.
            # `arbitrer` rend `None` quand il passe (et relève la substitution pour
            # le journal), sinon le refus, qui nomme le geste dans les deux cas :
            # paramètre absent → comment le passer ; palier non tenu → à qui il est
            # ouvert. Le forçage ne touche PAS l'autre cran de la famille :
            # `origine` est posée par la plateforme, pas par le client, et il n'y a
            # rien à y corriger de la main du propriétaire.
            refus = fcg.arbitrer(forcage, cle, unwrap(avant.get(cle)), unwrap(neuf),
                                  colonne_formule=cle in cf)
            if refus is not None:
                errors.append(refus)
                details["expected_column"] = f"{cle}.comment"
    return errors, details


def _origine_attendue(avant: Optional[dict], cle: str, neuf: dict) -> Any:
    """Ce que le système POSERAIT en `<cle>.origine` : l'origine déjà stockée, sinon
    la valeur de base en place, sinon (création) la valeur écrite. Une origine
    égale à ça n'est pas une écriture — c'est la réémission de ce qui est."""
    if avant is None:
        return neuf.get(VALUE_LAYER)
    stockee = layer_value(avant.get(cle), ORIGIN_LAYER)
    return stockee if stockee is not None else unwrap(avant.get(cle))
