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
  procédure du travail (`payload.procedure`), au palier où la cascade la résout. Un autre
  slug, un autre palier, une lecture par id, toute écriture et la bibliothèque sont refusés
  (`procedure_outside_job`, 403) ; un travail sans procédure n'en lit aucune. Les écritures
  étaient ouvertes jusqu'ici : un agent pouvait réécrire la procédure qu'il exécute, ou une
  autre — une injection de prompt s'y rendait PERSISTANTE. Écrire une procédure est un geste
  de personne, comme élargir les outils d'un agent (`tools_human_only`) ;
- **à la pose** (`exiger_connue`) : un agent ne se pose ou ne se repointe que sur un slug qui
  existe au palier org ou dans la copie personnelle de son propriétaire (`unknown_procedure`,
  400). Un slug inconnu de ces deux paliers ne se résoudrait pour personne — ou, pire, plus
  tard, chez qui l'aurait créé ailleurs.

**La source** : le travail porté par le jeton de la requête
(`verrou_org.travail_de_la_requete`, quel que soit le mode du verrou d'org) et sa charge EN
BASE, jamais un argument de l'appel. Hors jeton de délégation, rien ne change.
"""
from __future__ import annotations

from typing import Optional

from .. import db, deprecations, org_store, verrou_org
from ._types import AuthzDenied, DeclaredError, ResolvedCtx

#: Le code du refus d'exécution. Testé par les appelants, jamais la phrase.
CODE = "procedure_outside_job"
#: Le code du refus de pose.
CODE_INCONNUE = "unknown_procedure"

#: Les seuls gestes servis sous un jeton de délégation : relire sa procédure.
_LECTURES = ("get", "list")
#: La lecture par id stable, sous ses deux noms (l'ancien vit dans `deprecations.CLES`).
_NOMS_DE_L_ID = ("guide_id", *(a for a, n in deprecations.CLES.items() if n == "guide_id"))

REFUS = DeclaredError(
    403, CODE,
    "sous le jeton de délégation d'un agent hébergé : tout geste hors de la lecture de la "
    "procédure de son travail (`get`/`list`), au palier où elle se résout — autre slug, "
    "`scope='group'`, lecture par id, écriture, bibliothèque, ou travail sans procédure")
REFUS_INCONNUE = DeclaredError(
    400, CODE_INCONNUE,
    "`procedure` qui n'existe ni au palier org ni dans la copie personnelle du "
    "propriétaire de l'agent")


def _refus(raison: str) -> AuthzDenied:
    return AuthzDenied(
        403, "procedure_outside_job",
        f"{raison} A hosted agent reads only its own job's procedure, at the scope it "
        "resolves to (its owner's personal copy, then the org's). Writing a procedure is "
        "done by a person: tell whoever you report to what should change.")


def palier_servi(org_id: Optional[int], sub: Optional[str], slug: str) -> Optional[str]:
    """Sync (base) : le palier où `op=get` sans `scope` résout `slug` pour `sub` — `user`
    (copie personnelle EN SERVICE), `org`, ou None s'il n'est à aucun des deux. Même
    cascade que `orgs.instructions._read_guide` : une copie perso archivée ne masque pas
    celle de l'org."""
    if sub is not None and org_store.get_instruction_en_service("user", str(sub), slug):
        return "user"
    if org_id is not None and org_store.get_instruction("org", int(org_id), slug):
        return "org"
    return None


def procedure_du_travail() -> tuple[bool, Optional[str]]:
    """Sync (base) : `(sous_delegation, slug)` — le slug de la procédure du travail de la
    requête, None si le travail n'en a pas (ou est introuvable)."""
    v = verrou_org.travail_de_la_requete()
    if v is None:
        return False, None
    charge = db.charge_du_travail(int(v.job_id)) if v.job_id is not None else None
    slug = charge.get("procedure") if isinstance(charge, dict) else None
    return True, (str(slug) if slug else None)


def borner(ctx: ResolvedCtx, inp) -> Optional[tuple[str, Optional[str]]]:
    """Sync (base) : None hors jeton de délégation. Sinon, refuse tout ce qui sort de la
    procédure du travail et rend `(slug, palier)` — le palier où elle se résout, None si
    elle n'existe à aucun (la lecture dira alors « introuvable », la liste sera vide)."""
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
    voulu = org_store.normalize_slug(slug)
    if inp.op == "get" and org_store.normalize_slug(inp.slug or "") != voulu:
        raise _refus(f"`{inp.slug}` is not this job's procedure (`{slug}`).")
    palier = palier_servi(ctx.org_id, ctx.sub, slug)
    if inp.scope is not None and inp.scope != palier:
        raise _refus(f"`scope='{inp.scope}'` is not where this job's procedure `{slug}` "
                     f"resolves ({palier or 'nowhere'}): omit `scope`.")
    return voulu, palier


def liste_bornee(out: dict, slug: str) -> dict:
    """La liste réduite à la procédure du travail (`op=list` sous délégation).

    ⚠️ Sous ses DEUX noms (`guides` et l'ancien, `deprecations.CLES`) : filtrer l'un
    laisserait l'autre servir la liste entière."""
    guides = [g for g in out.get("guides") or []
              if org_store.normalize_slug(g.get("slug") or "") == slug]
    reste = {k: v for k, v in out.items() if k not in deprecations.CLES}
    return deprecations.avec_les_deux_noms({**reste, "guides": guides})


def exiger_connue(org_id: Optional[int], proprietaire: Optional[str], slug: str) -> None:
    """Sync (base) : refuse `unknown_procedure` si `slug` n'existe ni au palier org ni dans
    la copie personnelle de `proprietaire` (en service ou archivée : une procédure archivée
    a son propre refus, `procedure_archived`, au lancement)."""
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
