"""La surface STRICTE de gouvernance : `oto_resource_v2` / `POST /api/resources/v2`.

**Pourquoi une seconde surface, et pas un durcissement de la première.** Le 2026-09-01,
#756 a rendu `resource_type` obligatoire sur `oto_resource`. Le motif était juste — un
défaut implicite sur un DISCRIMINANT ne raccourcit pas l'appel, il le rend faux, et sur
`transfer`/`share` il déplace le geste vers une autre ressource. Mais le champ était
déclaré sans défaut sur le modèle d'entrée, donc obligatoire sur **toutes** les op :
le journal des appels a montré des appelants réels sur cette surface, dont un `op=list`
qui serait passé de « fonctionne » à « refusé » sans préavis. Le lot a été reverté
(#774) avant d'atteindre un tag.

**Arbitrage d'Alexis (2026-09-01) : on duplique, on ne bascule pas.** L'héritée
continue de servir son défaut — écrit noir sur blanc dans sa description, cf.
`resources.py` — et celle-ci exige le champ. Les deux vivent côte à côte, la migration
se fait appelant par appelant, **sans date-couperet ni préavis à négocier**. C'est
l'inverse d'un alias déprécié (`docs/alias-deprecies.md`) : là-bas l'ancien nom part à
une date écrite ; ici l'ancien contrat reste tant que quelqu'un s'en sert.

**Ce que la duplication porte, et ce qu'elle ne porte pas.** UNIQUEMENT le contrat
d'entrée. Le handler, la règle d'autorisation et la forme de sortie sont les mêmes
objets — deux handlers seraient deux comportements, à faire diverger au premier lot.

**Le régime : bêta, sur option.** `oto_resource_v2` est dans `BETA_TOOLS` : il ne se
propose qu'aux comptes à qui un admin a posé l'option `beta` (`oto_admin_set_option`),
lue par `access.has_option`. Le masquage est fail-CLOSED, comme tout le bloc bêta.
⚠️ **La face REST n'est PAS gatée** — même écart assumé que pour les verbes `oto_node*`
(`docs/tool-visibility.md`). Ici il est moins gênant qu'ailleurs : le chemin `/v2` est
lui-même l'opt-in, personne n'y arrive par accident.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from ._authz import RESOURCE_GOVERN
from ._types import Capability, RestBinding
from .docs import partage as page
from .registry import CAPABILITIES
from .resources import (CREDENTIALS_DESCRIPTION, EXPIRY_DESCRIPTION,
                        APP_DESCRIPTION, PENDING_DESCRIPTION, PUBLICATION_DESCRIPTION, TRANSFER_PROCEDURE,
                        ResourceInput, _resources)
from .resources_contract import REFUS, ResourceOut, ResourceType


class ResourceInputV2(ResourceInput):
    """L'entrée héritée, avec les deux durcissements que #756 avait raison d'apporter.

    ⚠️ Elle DÉRIVE de `ResourceInput` au lieu de la recopier : un champ ajouté à la
    surface héritée arrive ici sans qu'on y pense. Recopier les seize champs les
    ferait diverger au premier lot, et la migration cesserait d'être un simple
    changement de nom d'outil pour devenir une réécriture.
    """
    # OBLIGATOIRE, et `Literal` : c'est tout l'objet de cette surface. L'énuméré est
    # publié dans le schéma, donc lisible AVANT d'appeler — ce que le client perd en
    # message d'erreur nommé (`unsupported_resource_type` n'existe pas ici, la
    # validation refuse avant le handler), le contrat le lui rend.
    resource_type: ResourceType
    # Identifiant NUMÉRIQUE pour les trois familles (clé primaire PG). La garde est
    # sur l'ENTRÉE et pas dans le handler parce que c'est la seule couche qui
    # s'exécute avant l'autz, et que c'est l'AUTZ qui levait : `RESOURCE_GOVERN` →
    # `ownership.can_govern` → `int(rid)`, d'où un 500 sur un tableau ou un projet
    # (`ValueError`, que l'adaptateur REST ne rattrape pas) et un 403 sur un guide —
    # trois comportements pour une même saisie fautive, dont deux faux.
    resource_id: Optional[str] = Field(
        None, pattern=r"^\d+$",
        description="Identifiant numérique de la ressource (clé primaire).")
    # ⚠️ `permission` n'est PAS redéclaré ici, et c'est délibéré. Son défaut « partager
    # = laisser lire » (ADR 0068) est posé sur l'entrée HÉRITÉE dont cette classe
    # dérive — décision d'Alexis du 04/09 : « pas de v2 » pour ce changement-là, on
    # corrige l'outil que les gens utilisent. Le redéclarer donnerait deux endroits à
    # tenir d'accord, exactement ce que la dérivation existe pour éviter.


CAPABILITIES += [
    Capability(
        key="resources.govern.v2",
        # Le MÊME handler et la MÊME règle que la surface héritée, à dessein : ce qui
        # est dupliqué est le contrat d'entrée, rien d'autre.
        handler=_resources,
        Input=ResourceInputV2,
        authz=RESOURCE_GOVERN(),
        Output=ResourceOut,
        errors=REFUS,
        description=(
            "BETA. Same governance surface as oto_resource (ADR 0030), with a STRICT "
            "input contract: resource_type is REQUIRED (no default) ∈ {datastore_namespace, "
            "project, procedure, doc}, and resource_id must be numeric. "
            + TRANSFER_PROCEDURE + " " + page.DESCRIPTION + " " + APP_DESCRIPTION
            + " Prefer this tool over "
            "oto_resource: on the legacy one, omitting resource_type silently targets a "
            "datastore namespace, so op=transfer/share act on a DIFFERENT resource than the "
            "one you meant. Everything else is identical — op=list: resources you govern "
            "(platform admins see all); op=get: owner + shares + metadata (each grant carries "
            "a `role`); op=transfer: hand ownership to a user (`new_owner_email`), to one of "
            "YOUR orgs (`new_owner_org`, you must be a member) OR to one of YOUR teams "
            "(`new_owner_group`, ADR 0049 — scoping a resource to a pôle IS the way to "
            "restrict it); a user-owned previous owner keeps editor access (transfer is "
            "owner/admin only, never a grantee). ANTI-LOCKOUT: if the transfer would leave "
            "YOU unable to ever get the resource back (handing to a third party, or to an "
            "org/team you don't administer), it is refused with code "
            "`confirm_loss_of_control` — resend with `confirm_transfer=true` to proceed "
            "consciously (a platform admin is never blocked and can always recover it). "
            "op=share/unshare — ONE unified « Share », two axes (ADR 0048): "
            "AUDIENCE (`audience`) = where it goes: `person` (`email`) / `team` (`group_id`, a "
            "group of an org you belong to) / `org` (`org_id`, a whole org, client delivery) → a "
            "grant; `public`/`secret` → PUBLISH the project (public = listed, secret = "
            "unguessable link) with `mcp_tools` (defaults to the already-published set); "
            "`private` → unpublish. ROLE (`role`) = what they can do: `viewer` (read), `editor` "
            "(write), `manager` (GOVERNANCE — re-share / delete / publish, grantable, but NOT "
            "ownership transfer); public/secret force viewer. " + PUBLICATION_DESCRIPTION
            + " Legacy `permission` read|write is "
            "still accepted (mapped to viewer/editor). " + CREDENTIALS_DESCRIPTION
            + " " + EXPIRY_DESCRIPTION + " " + PENDING_DESCRIPTION
            + " DELIVER A FULL PROJECT (#52): "
            "share/transfer a project with cascade=true to carry its linked entities in one "
            "gesture — linked tableaux get the same share/transfer, linked procedures are "
            "share-granted read (readable cross-org via oto_procedure op=get guide_id) or "
            "COPIED into the target org on transfer (link re-pointed, source untouched), "
            "connector links report `recipient_credential` (the recipient plugs their own key; "
            "the project's pre-made identity/instructions overrides travel with it) or "
            "`inherited_credential` under credentials=inherit; docs & "
            "files follow automatically. Returns a per-entity cascade report. "
            "Owner OR org/platform admin governing it; never exposes row content."
        ),
        mcp="oto_resource_v2",
        rest=RestBinding("POST", "/api/resources/v2"),
    ),
]
