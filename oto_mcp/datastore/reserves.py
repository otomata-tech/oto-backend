"""Les champs que l'appelant n'écrit pas — le geste du store (#586, #606, #607).

Trois crans de schéma, UNE garde. Ce qu'ils protègent est la donnée remise par le
client, contre trois gestes mesurés sur la même campagne (28-29/08/2026) :

- **écraser** une colonne source (#606) : quatorze valeurs sur douze fiches par cent,
  à l'exact — l'agent « complète » l'adresse avec ce que dit le registre, et la valeur
  remise n'existe plus nulle part. Cran `readonly: true` (+ `report_to`) : une
  écriture qui CHANGE la valeur en place est refusée en nommant la colonne, la raison
  et où va la divergence ;
- **détruire la copie de secours** (#586) : la couche `<champ>.origine` censée garder
  la valeur remise était écrite par l'agent, donc réécrite par lui — une fois sur
  quarante et une, et c'était l'unique copie. Cran `origine: "system"` : la plateforme
  la pose elle-même, à la première écriture qui change la valeur, une seule fois ; la
  couche est fermée à l'appelant ;
- **graver une déclaration à la place d'une trace** (#607) : une colonne `modele`
  que l'agent remplit de mémoire dérive — `…2407` sur une fiche, `…2511` sur une
  autre le lendemain, quand les 102 travaux enregistrés du run disent tous `…2512`.
  Cran `system: "<source>"` : la plateforme pose la VALEUR à chaque écriture, depuis
  ce qu'elle OBSERVE (le run de l'appel, son ouverture, l'instant de l'écriture) ;
  l'appelant ne l'écrit pas. *Une valeur recopiée dit ce que l'agent croit ; une
  valeur posée dit ce que le serveur sait.*

**Les trois répondent à la même question dans le même ordre**, et c'est ce qui en
fait une famille plutôt que trois drapeaux : *à qui appartient cette destination ?*
— la colonne existe-t-elle (`unknown_fields`, #614/#678, jugé au seam de validation
parce qu'il n'a besoin que du format) ; est-elle à moi (ici) ; la valeur est-elle
recevable (la validation de type). Chaque refus dit les trois mêmes choses : le
champ, la raison, où va la chose.

La DÉCISION vit dans `schema.py` (`reserved_refusals`, à côté des autres
déclarations, et sondée par `enforced_keys`) ; ce module en fait le geste : refuser en
levant, et poser. Il est appelé aux cinq chemins d'écriture du store — création (ligne
seule, lot, upload signé), fusion sous verrou, patch par identifiant, remplacement.

⚠️ **Les TROIS PREMIERS crans bornent TOUT LE MONDE PAR DÉFAUT, faces humaine et REST
comprises.** Le store ne sait pas distinguer un agent d'un humain (il connaît un sub et
une org ; le run n'est pas obligatoire sur toute écriture), et une exemption par défaut
serait un trou.

⚠️ **Amendement du 06/09/2026 (oto#83) — un QUATRIÈME cran, celui-là ciblé.**
`agent_access` ferme une colonne à l'AGENT SEUL : l'écran de son propriétaire continue
de la voir et de l'écrire. La phrase ci-dessus reste vraie du store — il ne devine
toujours rien : la face MCP le lui DIT (paramètre `agent`, posé par
`acces_agent.appel_d_agent()`), et hors de cette déclaration le cran est inerte. C'est
le seul cran de la famille qui ne se force pas : la sortie du propriétaire n'est pas un
paramètre d'appel, c'est son écran, où le cran ne s'applique pas.

⚠️ **Amendement du 02/09/2026 (#658) — `readonly` seul.** La sortie du propriétaire
était le schéma : `data_patch_schema(fields=[{key, readonly: false}])`, écrire,
refermer. Cette manœuvre-là est le défaut, pas la sortie : *une exécution interrompue
entre « lever » et « remettre » laisse le verrou ouvert sans aucun signal* (mesuré sur
`key_required`, #668). Il existe donc un forçage `readonly_override` **sur l'appel**,
ouvert au propriétaire du tableau ou à qui le gouverne, tracé au journal des appels —
et le refus le NOMME. Rien à refermer, rien à oublier. La règle et le palier vivent
dans `forcage.py` ; les deux autres crans (`origine`, `system`) ne se forcent pas :
ils ferment ce que la PLATEFORME pose.

⚠️ Pas dans le registre des jetons (#602) : celui-ci juge AVANT la résolution, sans
schéma ; un champ réservé est une propriété du TABLEAU, il se juge là où le schéma est
connu. Les deux se complètent — jeton mal placé : « il s'écrit dans tel champ » ;
champ réservé : « il ne s'écrit pas, voici où va la chose ».
"""
from __future__ import annotations

from typing import Optional

from . import schema as dsv2
from .columns import _existing_layers
from .errors import RowValidationError
from .forcage import Forcage


def iso_utc(valeur) -> str:
    """La forme CANONIQUE d'un horodatage posé par la PLATEFORME (#859).

    Une seule forme, pour une raison mesurée : les deux sources système d'une
    date en produisaient deux. `write.at` rendait `2026-09-03T11:22:19+00:00`
    (précision seconde) pendant que `run.started_at` rendait
    `2026-09-03T11:22:19.619406+00:00` (microsecondes) — deux colonnes
    déterministes du même tableau, deux écritures. Un troisième format observé en
    production, `…T00:00:00.000Z`, ne vient d'aucune des deux : il précède ce
    cran.

    ⚠️ **Le tri est ce qui paie.** `Z` et `+00:00` désignent le même décalage et
    ne se rangent pas pareil dans l'alphabet — `Z` passe après `+`. Deux instants
    IDENTIQUES notés différemment se rangeaient donc l'un après l'autre. Le tri
    caste désormais en horodatage plutôt que de comparer des chaînes, ce qui
    absorbe l'existant ; mais la donnée ne doit pas naître hétérogène pour
    autant : *corriger la lecture d'une donnée qu'on écrit soi-même de travers,
    c'est réparer autour de la source.*

    Ramène tout en UTC, à la seconde. La précision perdue sur l'ouverture d'un
    passage n'a aucun usage — on date un travail, pas une mesure physique — et
    l'uniformité, elle, se voit à chaque tri.
    """
    from datetime import timezone as _tz
    if hasattr(valeur, "astimezone"):
        return valeur.astimezone(_tz.utc).isoformat(timespec="seconds")
    return str(valeur)


def valeurs_systeme(schema: Optional[dict], *, run: Optional[str],
                    maintenant: str) -> dict:
    """Ce que la plateforme POSERAIT sur ce geste → `{colonne: valeur}` (#607).

    Calculée AVANT le refus, parce que le refus s'en sert : une valeur identique à
    celle qu'on s'apprête à poser est un non-geste, pas une invention.

    ⚠️ **Hors run, on ne pose RIEN** — et surtout pas un repli. Le champ reste vide,
    et le refus reste : une estampille devinée serait exactement la déclaration de
    mémoire que ce cran remplace, avec le sceau de la plateforme en plus. Un champ
    vide se voit ; une valeur fausse se croit."""
    sources = dsv2.system_value_fields(schema)
    if not sources:
        return {}
    out: dict = {}
    depart = None
    if run and "run.started_at" in sources.values():
        depart = _ouverture_du_run(run)
    for cle, source in sources.items():
        if source == "write.at":
            out[cle] = maintenant
        elif source == "run.id" and run:
            out[cle] = run
        elif source == "run.started_at" and depart is not None:
            out[cle] = depart
    return out


# run_id -> ouverture (ISO), ou None quand le run est inconnu de `runs`. Même parti
# que `run_org._ORG_DU_RUN` et pour la même raison : `runs.started_at` est immuable
# (seuls `outcome`/`note`/`finished_at` bougent après la pose), et sans cache chaque
# écriture d'une campagne relirait la table sur le chemin chaud.
_OUVERTURE_DU_RUN: dict = {}
_CAP = 10_000


def _ouverture_du_run(run: str):
    """`runs.started_at` du run, en ISO — `None` si le run est inconnu.

    Un run inconnu n'est PAS mis en cache : sa ligne peut naître après (`run_start`
    la pose en best-effort), et mémoriser l'absence gèlerait la colonne à vide pour
    toute la campagne."""
    if run in _OUVERTURE_DU_RUN:
        return _OUVERTURE_DU_RUN[run]
    from .. import db
    head = db.get_run_head(run) or {}
    depart = head.get("started_at")
    if depart is None:
        return None
    valeur = iso_utc(depart)
    _OUVERTURE_DU_RUN[run] = valeur
    while len(_OUVERTURE_DU_RUN) > _CAP:
        del _OUVERTURE_DU_RUN[next(iter(_OUVERTURE_DU_RUN))]
    return valeur


def poser_valeurs_systeme(schema: Optional[dict], apres: dict,
                          pose: Optional[dict]) -> list[str]:
    """Écrit dans `apres` les valeurs que la plateforme pose (#607). Rend les
    colonnes posées ; `apres` est modifiée en place.

    Trois règles, chacune fermant une porte :

    - **posée à CHAQUE écriture, sans que l'appelant nomme la colonne** — c'est
      tout l'objet du cran : l'estampille ne doit dépendre ni de la mémoire de
      l'agent, ni de ce qu'il a pensé à inclure dans son corps ;
    - **la colonne plate reste plate** : on n'enveloppe en couches que ce qui l'est
      déjà. Sans ça, poser une estampille sur un tableau à colonnes simples
      changerait la forme SERVIE de toutes ses lignes — un dégât de format silencieux
      pour un gain nul (même règle que `poser_origine_systeme`) ;
    - **les couches en place survivent** : un `comment` posé sur l'estampille reste,
      la valeur seule est remplacée.

    ⚠️ La pose n'entre PAS dans les clés « écrites » que voit la validation : la
    borne de longueur et le motif se jugent sur ce que l'APPELANT pose, et un refus
    portant sur une valeur qu'il ne contrôle pas serait inactionnable — il ne
    pourrait ni la corriger, ni s'en passer."""
    posees: list[str] = []
    for cle, valeur in sorted((pose or {}).items()):
        couches = _existing_layers(apres.get(cle))
        if len(couches) > 1 or dsv2.VALUE_LAYER not in couches:
            couches[dsv2.VALUE_LAYER] = valeur
            apres[cle] = couches
        else:
            apres[cle] = valeur
        posees.append(cle)
    return posees


def refuser_champs_reserves(schema: Optional[dict], payload: Optional[dict], *,
                            avant: Optional[dict] = None,
                            pose_systeme: Optional[dict] = None,
                            forcage: Optional[Forcage] = None,
                            agent: bool = False) -> None:
    """Refuse ce que l'appelant n'écrit pas — en nommant le champ, la raison et où
    va la chose. `RowValidationError`, donc `row_invalid` côté REST (avec
    `details.expected_column`, #545) et INVALID_PARAMS côté MCP : le code ne
    change pas, c'est le texte qui enseigne.

    `forcage` (#658) = le forçage DEMANDÉ sur cet appel, déjà tranché par le store
    (aucune lecture d'ownership ici : ce chemin passe sous un verrou de ligne). Il ne
    lève que le cran `readonly`, et le relevé qu'il remplit part au journal.

    `agent` (oto#83) = cet appel est-il piloté par un modèle. **Décidé par la FACE, en
    amont** (`acces_agent.appel_d_agent()`), et passé jusqu'ici plutôt que relu : ce
    module a écrit noir sur blanc que « le store ne sait pas distinguer un agent d'un
    humain », et c'est resté vrai — ce qui a changé, c'est qu'on le lui DIT. `False`
    par défaut, donc aucun appelant existant ne change de comportement."""
    errors, details = dsv2.reserved_refusals(schema, payload, avant,
                                             pose_systeme=pose_systeme,
                                             forcage=forcage, agent=agent)
    if errors:
        raise RowValidationError(errors, details=details)


def poser_origine_systeme(schema: Optional[dict], avant: Optional[dict],
                          apres: dict, cles) -> list[str]:
    """Pose `<champ>.origine` = la valeur d'AVANT sur les colonnes `origine: "system"`
    que le geste vient de MODIFIER — une seule fois, jamais réécrite. Rend les
    colonnes posées ; `apres` est modifiée en place.

    Trois règles, chacune fermant une porte du défaut :

    - une origine DÉJÀ là (posée par le système, ou écrite par un agent avant le
      cran) n'est jamais touchée — les 40 fiches de la campagne restent lues telles
      quelles ;
    - une valeur INCHANGÉE ne pose rien : relire → repousser n'est pas une
      modification, et une colonne plate reste plate ;
    - un champ VIDE au départ reçoit `""` — le marqueur « rien n'avait été remis ».
      Sans lui, la deuxième écriture capturerait la première valeur de l'agent comme
      si elle venait du client. `flat_layers` ne sert pas une couche vide : à la
      lecture, « vide à l'origine » et « jamais modifié » se confondent, et c'est
      juste — dans les deux cas il n'y a rien à rétablir.

    La capture est PARESSEUSE (à la première écriture, pas à la pose du schéma) et
    rend la même valeur : entre la pose et la première modification, rien n'a bougé.
    Un format ne vaut que pour l'avenir — le poser ne réécrit aucune ligne."""
    posees: list[str] = []
    for cle in sorted(dsv2.system_origin_fields(schema) & set(cles)):
        col_avant = (avant or {}).get(cle)
        if dsv2.layer_value(col_avant, dsv2.ORIGIN_LAYER) is not None:
            continue
        val_avant, val_apres = dsv2.unwrap(col_avant), dsv2.unwrap(apres.get(cle))
        if val_avant == val_apres:
            continue
        couches = _existing_layers(apres.get(cle))
        couches[dsv2.ORIGIN_LAYER] = val_avant if val_avant is not None else ""
        apres[cle] = couches
        posees.append(cle)
    return posees
