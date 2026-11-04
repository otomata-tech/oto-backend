"""La procédure d'un agent hébergé : celle que `oto_procedure` lui sert, et celle qu'il peut
désigner (#1499, étape 2).

Un agent hébergé désigne sa procédure par son SLUG seul (`runner_triggers.procedure`) ; son
instruction dit « lis la procédure `<slug>` », et il la lit par `oto_procedure` sous le jeton
de délégation de son travail, avec les droits de son porteur. La lecture sans palier suit la
cascade du porteur : sa copie personnelle en service, puis celle de l'org. L'équipe n'y entre
pas. Mais l'outil lui ouvrait TOUT ce que le porteur lit : `op=list` sans palier cumule
l'équipe active, et `op=get scope='group'` la lit. Or tout MEMBRE d'une équipe écrit à ce
palier (#681) : un slug qui n'existait qu'au palier équipe laissait un membre sans aucun droit
sur l'agent réécrire les consignes qu'il exécute avec les comptes de son propriétaire.

Deux bornes, une par moment :

- **à l'exécution** (`borner`) : sous un jeton de délégation, `oto_procedure` ne sert QUE la
  procédure du travail (`payload.procedure`) et celles qu'elle NOMME (ci-dessous), chacune au
  palier où la cascade la résout. Un autre slug, un autre palier, une lecture par id, toute
  écriture et la bibliothèque sont refusés (`procedure_outside_job`, 403) ; un travail sans
  procédure n'en lit aucune. Les écritures étaient ouvertes jusqu'ici : un agent pouvait
  réécrire la procédure qu'il exécute, ou une autre — une injection de prompt s'y rendait
  PERSISTANTE. Écrire une procédure est un geste de personne, comme élargir les outils d'un
  agent (`tools_human_only`) ;
- **à la pose** (`exiger_connue`) : un agent ne se pose ou ne se repointe que sur un slug qui
  existe au palier org ou dans la copie personnelle de son propriétaire (`unknown_procedure`,
  400). Un slug inconnu de ces deux paliers ne se résoudrait pour personne — ou, pire, plus
  tard, chez qui l'aurait créé ailleurs.

**Les procédures qu'elle NOMME** (#1499, suite) : composer des procédures en se nommant l'une
l'autre est un usage établi — un orchestrateur cite l'étape suivante par son slug, qui délègue
à son tour. Ne servir que `payload.procedure` coupait ces chaînes au premier maillon : l'agent
lisait l'orchestrateur, se voyait refuser la suite, faisait la première partie seule et
rendait « fait ». Le travail lit donc aussi, EN LECTURE SEULE, l'ensemble fermé suivant :

- **la règle de référence** : un slug écrit SEUL entre accents graves (`` `un-slug` ``) dans
  le corps d'une procédure de l'ensemble, qui SE RÉSOUT par `palier_servi` — copie
  personnelle EN SERVICE du porteur, puis l'org. Aucune convention n'existait (un `<slot:…>`
  ne binde jamais une procédure, `project_links` lie un projet, pas une procédure) ; les
  accents graves sont la forme que les procédures écrivent déjà. Un nom d'outil, de colonne
  ou de tableau entre accents graves ne se résout pas et tombe de lui-même ;
- **la fermeture** : en largeur depuis la procédure du travail, dans l'ordre d'apparition,
  au plus `PROFONDEUR_MAX` sauts et `PROCEDURES_MAX` procédures, celle du travail COMPRISE ;
  au-delà, tronquée de façon déterministe (premier vu, premier servi). Un cycle ne se suit
  qu'une fois ;
- ⚠️ **jamais l'équipe** : chaque slug cité se résout, et chaque corps qui sert à calculer
  la fermeture se LIT, aux deux paliers de `palier_servi` et à eux seuls. Un membre
  d'équipe ne fait donc entrer aucune procédure dans l'ensemble : ni en écrivant une copie
  d'équipe d'un slug cité (elle ne se résout pas), ni en citant un slug depuis un corps
  d'équipe (ce corps n'est jamais lu). Écrire au palier org ou dans la copie du porteur,
  c'est déjà avoir la main sur l'agent ;
- **calculée à chaque requête**, depuis la base, sans cache : une procédure change, la
  fermeture suit. Le nombre de lectures ne dépend PAS du nombre de noms entre accents
  graves (un corps en cite des dizaines : outils, colonnes) : les slugs de chaque palier se
  lisent une fois (`_slugs_par_palier`, trois lectures), puis au plus `PROCEDURES_MAX`
  corps.

Le reste ne bouge pas : écritures, bibliothèque, lecture par id refusées ; `scope` doit être
le palier où CE slug se résout ; `op=list` rend l'ensemble, chacune à son palier.

**La source** : le travail porté par le jeton de la requête
(`verrou_org.travail_de_la_requete`, quel que soit le mode du verrou d'org) et sa charge EN
BASE, jamais un argument de l'appel. Hors jeton de délégation, rien ne change.
"""
from __future__ import annotations

import re
from typing import Optional

from .. import db, deprecations, org_store, verrou_org
from ._types import AuthzDenied, DeclaredError, ResolvedCtx

#: Le code du refus d'exécution. Testé par les appelants, jamais la phrase.
CODE = "procedure_outside_job"
#: Le code du refus de pose.
CODE_INCONNUE = "unknown_procedure"

#: Les seuls gestes servis sous un jeton de délégation : relire ses procédures.
_LECTURES = ("get", "list")
#: La lecture par id stable, sous ses deux noms (l'ancien vit dans `deprecations.CLES`).
_NOMS_DE_L_ID = ("guide_id", *(a for a, n in deprecations.CLES.items() if n == "guide_id"))

#: La fermeture des procédures nommées : sauts depuis celle du travail, et taille de
#: l'ensemble, celle du travail COMPRISE.
PROFONDEUR_MAX = 3
PROCEDURES_MAX = 10
#: Un slug cité : le contenu ENTIER d'un segment entre accents graves, en forme de slug.
_CITE = re.compile(r"`([A-Za-z0-9][A-Za-z0-9_-]{0,79})`")

REFUS = DeclaredError(
    403, CODE,
    "sous le jeton de délégation d'un agent hébergé : tout geste hors de la lecture de la "
    "procédure de son travail et de celles qu'elle nomme (`get`/`list`), au palier où "
    "chacune se résout — autre slug, `scope='group'`, lecture par id, écriture, "
    "bibliothèque, ou travail sans procédure")
REFUS_INCONNUE = DeclaredError(
    400, CODE_INCONNUE,
    "`procedure` qui n'existe ni au palier org ni dans la copie personnelle du "
    "propriétaire de l'agent")


def _refus(raison: str, permises: Optional[dict] = None) -> AuthzDenied:
    lisibles = (" Readable here: " + ", ".join(f"`{s}`" for s in permises) + "."
                if permises else "")
    return AuthzDenied(
        403, "procedure_outside_job",
        f"{raison} A hosted agent reads only its own job's procedure and the procedures "
        "it names in backticks (transitively, bounded), each at the scope it resolves to "
        f"(its owner's personal copy, then the org's).{lisibles} Writing a procedure is "
        "done by a person: tell whoever you report to what should change.")


def _resolue(org_id: Optional[int], sub: Optional[str],
             slug: str) -> tuple[Optional[str], Optional[dict]]:
    """Sync (base) : `(palier, ligne)` où `op=get` sans `scope` résout `slug` pour `sub`
    — `user` (copie personnelle EN SERVICE), `org`, ou `(None, None)`. Même cascade que
    `orgs.instructions._read_guide` : une copie perso archivée ne masque pas celle de
    l'org. La ligne porte le corps LU À CE PALIER — celui qui sert à la fermeture."""
    if sub is not None:
        row = org_store.get_instruction_en_service("user", str(sub), slug)
        if row:
            return "user", row
    if org_id is not None:
        row = org_store.get_instruction("org", int(org_id), slug)
        if row:
            return "org", row
    return None, None


def palier_servi(org_id: Optional[int], sub: Optional[str], slug: str) -> Optional[str]:
    """Sync (base) : le palier où `op=get` sans `scope` résout `slug` pour `sub` — `user`,
    `org`, ou None s'il n'est à aucun des deux (`_resolue`)."""
    return _resolue(org_id, sub, slug)[0]


def _citees(body: Optional[str]) -> list[str]:
    """Les slugs cités entre accents graves, normalisés, dans l'ordre d'apparition."""
    vus: list[str] = []
    for m in _CITE.finditer(body or ""):
        s = org_store.normalize_slug(m.group(1))
        if s and s not in vus:
            vus.append(s)
    return vus


def _slugs_par_palier(org_id: Optional[int],
                      sub: Optional[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Sync (base) : `(perso, org)` — les slugs que `_resolue` trouve à chaque palier, en
    TROIS lectures quel que soit le nombre de noms cités. Perso : les copies EN SERVICE
    (`list_instructions` exclut les archivées, comme `get_instruction_en_service`) ; org :
    toutes, archivées comprises (`get_instruction` ne filtre pas l'archivage)."""
    perso = (frozenset(org_store.normalize_slug(r["slug"])
                       for r in org_store.list_instructions("user", str(sub)))
             if sub is not None else frozenset())
    org = (frozenset(org_store.normalize_slug(r["slug"]) for r in (
        *org_store.list_instructions("org", int(org_id)),
        *org_store.list_archived_instructions("org", int(org_id))))
        if org_id is not None else frozenset())
    return perso, org


def procedures_permises(org_id: Optional[int], sub: Optional[str],
                        racine: str) -> dict[str, Optional[str]]:
    """Sync (base) : `{slug: palier}` — la procédure du travail (palier None si elle ne se
    résout nulle part ; elle ne cite alors rien) puis la fermeture bornée de celles qu'elle
    nomme, qui se résolvent toutes (règle : docstring du module). Ordre : largeur, puis
    apparition. Un nom cité qui ne se résout pas (outil, colonne) ne coûte aucune lecture."""
    racine = org_store.normalize_slug(racine)
    palier, ligne = _resolue(org_id, sub, racine)
    permises: dict[str, Optional[str]] = {racine: palier}
    front = [ligne] if ligne else []
    paliers: Optional[tuple[frozenset[str], frozenset[str]]] = None
    for _ in range(PROFONDEUR_MAX):
        suivant: list[dict] = []
        for corps in front:
            for s in _citees(corps.get("body_md")):
                if s in permises:
                    continue
                if len(permises) >= PROCEDURES_MAX:
                    return permises
                if paliers is None:
                    paliers = _slugs_par_palier(org_id, sub)
                perso, org = paliers
                if s in perso:
                    p, r = "user", org_store.get_instruction("user", str(sub), s)
                elif s in org:
                    p, r = "org", org_store.get_instruction("org", int(org_id), s)
                else:
                    continue
                if r:
                    permises[s] = p
                    suivant.append(r)
        if not suivant:
            break
        front = suivant
    return permises


def procedure_du_travail() -> tuple[bool, Optional[str]]:
    """Sync (base) : `(sous_delegation, slug)` — le slug de la procédure du travail de la
    requête, None si le travail n'en a pas (ou est introuvable)."""
    v = verrou_org.travail_de_la_requete()
    if v is None:
        return False, None
    charge = db.charge_du_travail(int(v.job_id)) if v.job_id is not None else None
    slug = charge.get("procedure") if isinstance(charge, dict) else None
    return True, (str(slug) if slug else None)


def borner(ctx: ResolvedCtx, inp) -> Optional[dict[str, Optional[str]]]:
    """Sync (base) : None hors jeton de délégation. Sinon, refuse tout ce qui sort de la
    procédure du travail et de celles qu'elle nomme, et rend `{slug: palier}`
    (`procedures_permises`) — le palier de la procédure du travail est None si elle
    n'existe à aucun (la lecture dira alors « introuvable », la liste sera vide)."""
    sous_delegation, slug = procedure_du_travail()
    if not sous_delegation:
        return None
    if not slug:
        raise _refus("This hosted agent's job has no procedure, so it reads none.")
    if inp.op not in _LECTURES:
        raise _refus(f"`op={inp.op}` is not served to a hosted agent.")
    if any(getattr(inp, nom, None) is not None for nom in _NOMS_DE_L_ID):
        raise _refus("Reading by `guide_id` is not served to a hosted agent: read your "
                     f"procedure by its slug, `{slug}`.")
    permises = procedures_permises(ctx.org_id, ctx.sub, slug)
    if inp.op == "get":
        voulu = org_store.normalize_slug(inp.slug or "")
        if voulu not in permises:
            raise _refus(f"`{inp.slug}` is neither this job's procedure (`{slug}`) nor "
                         "one it names.", permises)
        palier = permises[voulu]
        if inp.scope is not None and inp.scope != palier:
            raise _refus(f"`scope='{inp.scope}'` is not where `{voulu}` resolves "
                         f"({palier or 'nowhere'}): omit `scope`.", permises)
    elif inp.scope is not None and inp.scope not in permises.values():
        raise _refus(f"`scope='{inp.scope}'` holds none of this job's procedures: omit "
                     "`scope`.", permises)
    return permises


def liste_bornee(sorties: list[dict], permises: dict[str, Optional[str]],
                 base: dict) -> dict:
    """La liste réduite aux procédures permises (`op=list` sous délégation), chacune À SON
    palier : `sorties` = une liste par palier présent dans `permises`, `base` = la forme
    rendue quand il n'y en a aucune.

    ⚠️ Sous ses DEUX noms (`guides` et l'ancien, `deprecations.CLES`) : filtrer l'un
    laisserait l'autre servir la liste entière."""
    paires = {(s, p) for s, p in permises.items() if p is not None}
    guides = [g for out in sorties for g in out.get("guides") or []
              if (org_store.normalize_slug(g.get("slug") or ""), g.get("scope")) in paires]
    tete = sorties[0] if sorties else base
    reste = {k: v for k, v in tete.items() if k not in deprecations.CLES}
    return deprecations.avec_les_deux_noms({**reste, "guides": guides})


def exiger_connue(org_id: Optional[int], proprietaire: Optional[str], slug: str) -> None:
    """Sync (base) : refuse `unknown_procedure` si `slug` n'existe ni au palier org ni dans
    la copie personnelle de `proprietaire` (en service ou archivée : une procédure archivée
    a son propre refus, `procedure_archived`, au lancement). Un orchestrateur de l'org
    passe comme toute autre procédure : ce qu'il nomme se résout à l'exécution."""
    if org_id is not None and org_store.get_instruction("org", int(org_id), slug):
        return
    if proprietaire is not None and org_store.get_instruction("user", str(proprietaire), slug):
        return
    raise AuthzDenied(
        400, "unknown_procedure",
        f"No procedure `{slug}` in this org, nor in the agent owner's personal procedures. "
        "An agent runs the procedure its owner reads by name (personal copy, then the "
        "org's): a team procedure is not one of them. Create it at one of those two scopes "
        "(`oto_procedure op=create`), then point the agent at it.")
