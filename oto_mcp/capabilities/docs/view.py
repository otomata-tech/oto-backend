"""La FORME servie par le domaine « doc » : la vue d'une page et sa projection.

C'est le module que le front tiers épingle — tout ce qui sort de `me.doc` passe par
`view()` puis, sauf lecture nue, par `projected()`. Une colonne ajoutée ici apparaît
dans la liste, la lecture et l'accusé d'écriture d'un seul coup ; c'est voulu, et c'est
la raison pour laquelle les trois ne calculent pas leur forme chacune de leur côté.
"""
from __future__ import annotations

from typing import Optional

from ... import db, output_projection

# Colonnes gardées quoi qu'on demande : de quoi ADRESSER la page ensuite (la relire, la
# patcher, la situer dans l'arbre). UNE seule liste pour la liste, la lecture projetée et
# l'accusé d'écriture — trois règles d'adressage divergeraient à la première évolution.
# `url` en fait partie depuis #599 : l'accusé d'une écriture est justement le moment
# où l'on demande « c'est où ? », et c'est le moment où la projection est la plus
# agressive. Une adresse qu'une projection emporte ne répond jamais à la question.
ALWAYS = ("id", "project_id", "parent_id", "title", "url")

# Les ops qui savent PROJETER leur sortie. Passer `fields` ailleurs est REFUSÉ, pas avalé :
# c'est la leçon générale du signal #461, où `op=get` acceptait `fields` et rendait quand
# même les ~30 K caractères de la page. Un argument accepté-et-ignoré coûte exactement ce
# qu'il prétendait économiser, et rien ne le signale à l'appelant.
FIELDS_OPS = frozenset({"list", "get", "create", "update", "patch", "move", "revert"})

# La phrase servie dans la notice d'un ACCUSÉ d'écriture. Distincte de « vue de tri » :
# l'agent ne trie pas, il vient d'écrire — lui dire le contraire l'enverrait relire.
HINT_ACCUSE = ("Write receipt: the page is saved, its body is not echoed back "
               "— you just wrote it. "
               '`fields=["*"]` returns it, `fields=[…]` picks the columns.')


def public_doc_url(token: str, sub: Optional[str] = None) -> Optional[str]:
    """Lien public d'un doc partagé (gap #4a). Suit le tenant de celui qui partage :
    ce lien part chez des TIERS, c'est la vitrine la plus visible de la marque.

    `None` si le produit du partenaire n'a pas de page publique — la page reste
    partagée, elle n'a simplement pas d'adresse à sa marque."""
    from ... import links
    return links.link_for("public_doc", sub=sub, token=token)


def doc_url(sub: Optional[str], row: dict) -> Optional[str]:
    """L'adresse de CETTE page chez ce lecteur, ou None (signal #599).

    Le manque remonté : après `op=create`, la réponse porte l'id, le projet, le `rev`
    — rien qui réponde à « et je la lis où ? ». Les contournements observés étaient
    tous mauvais : rendre la page publique (inacceptable pour de l'interne), ou
    RECONSTRUIRE l'adresse en lisant le routeur du tableau de bord — un patron appris
    par cœur dans une consigne, qui fabrique des liens plausibles et faux dès que la
    route bouge. L'adresse se sert donc d'ici, où elle est déjà connue, comme
    `data_url` la sert pour un tableau depuis toujours.

    `None` n'est pas un échec : le produit du lecteur peut n'avoir aucune vue de page
    (`links.link_for`, « pas de patron, pas de lien »), et le patron d'un tenant peut
    réclamer un paramètre qu'on ne porte pas ici — `{org}` par exemple, qu'une page ne
    connaît pas sans une requête de plus par ligne de liste. Dans les deux cas la
    réponse part SANS adresse, ce qui reste juste ; un lien mort, lui, ne se
    diagnostique pas, il se subit."""
    from ... import access, links
    # `org` is only resolved when this reader's paths carry it (`/org/{org}/…`):
    # one org lookup per page served, and none at all on a product that opens a
    # page by its id alone (cf. `links.patron_reclame`).
    org = (access.current_org(sub)
           if links.patron_reclame("doc", "org", sub=sub) else None)
    return links.link_for("doc", sub=sub, id=row.get("id"),
                          project_id=row.get("project_id"), org=org)


def view(row: dict, sub: Optional[str] = None) -> dict:
    out = {k: row.get(k) for k in
           ("id", "project_id", "parent_id", "title", "description", "position",
            "body_md", "kind", "created_at", "updated_at", "updated_by")}
    # `archived_at` (#1087) : retirée de la découverte, lisible ici ; `verified_at` /
    # `verified_by` : la dernière relecture DÉCLARÉE (op=verify), jamais `updated_at`.
    # Servis seulement quand ils sont posés : la liste paie au NOMBRE de pages
    # (test_list_view_budget), et l'absence dit « jamais archivée / jamais vérifiée ».
    out.update({k: row[k] for k in ("archived_at", "verified_at", "verified_by")
                if row.get(k)})
    # `updated_by` (oto#274) : le compte de la dernière modification, avec sa date —
    # un identifiant, que l'écran résout en nom ; `null` = inconnu (page déplacée
    # depuis, écrivain anonyme), jamais deviné.
    # L'adresse web de la page, à côté de son id (#599).
    out["url"] = doc_url(sub, row)
    # rev = ETag de contenu : à relire par le client et repasser en `expected_rev`
    # sur op=update pour détecter un écrasement concurrent (oto/#6).
    out["rev"] = db.doc_rev(row.get("title"), row.get("body_md"))
    # Le lien public n'est rangé que haché : la lecture dit s'il existe, jamais son
    # adresse — servie une seule fois, par `op=set_public` qui l'émet.
    out["public"] = bool(row.get("public"))
    return out


def projected(row: dict, sub: Optional[str], fields: Optional[list[str]], *,
              brut_par_defaut: bool, hint: Optional[str] = None) -> dict:
    """Une page passée au MÊME seam de projection que la liste (`summarize`).

    **La décision de forme, et son pourquoi (signaux #461, #506, #525, #530) :**

    - Une **LECTURE** (`op=get`) rend la page ENTIÈRE par défaut : livrer le contenu EST
      son travail, et le dashboard en dépend. Elle honore `fields` quand on lui en donne —
      le cas courant étant
      « relis-moi juste le `rev` avant de patcher », que `update`/`patch` exigent.
    - Une **ÉCRITURE** (`create`/`update`/`patch`/`move`) rend un **ACCUSÉ** par défaut :
      identité, titre, `rev`, `updated_at`, et la TAILLE du corps. L'appelant vient
      d'écrire ce corps — le lui rejouer ne lui apprend rien et lui coûte tout : sur les
      deux pages réelles de la KB d'un client (128 K et 85 K caractères), la réponse
      dépassait le plafond de résultat du client, si bien qu'**une écriture RÉUSSIE était
      rendue à l'agent comme un échec** (#530). Un agent qui lit cet échec au premier degré
      réécrit — double écriture — ou déclare l'opération ratée. Le corps reste à un
      `fields=["*"]` de distance.
    - Ce n'est pas une exception mais un RALLIEMENT : les écritures qui ne passaient pas
      par `view` rendent déjà un accusé (`bulk_create`, `delete`, `set_public`). Les
      quatre ci-dessus étaient les dernières à rejouer la page.

    ⚠️ Projeter ≠ tronquer : on retire des COLONNES et on le DIT (`projection`), on ne
    coupe jamais un texte — sinon l'agent croit avoir lu."""
    if fields is None and brut_par_defaut:
        return view(row, sub)
    rows, notice = output_projection.summarize(
        [view(row, sub)], body_fields=("body_md",), fields=fields,
        always=ALWAYS, hint=hint)
    return {**rows[0], **({"projection": notice} if notice else {})}
