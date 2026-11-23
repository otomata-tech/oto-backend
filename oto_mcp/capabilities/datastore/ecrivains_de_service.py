"""Les ÉCRIVAINS DE SERVICE d'un tableau : un service machine écrit les lignes d'un
tableau d'org que l'org lui a ouvert — sans jeton de personne.

**Le besoin.** Un service à part (celui des visites de site, par exemple) remplit un
tableau d'org pendant des mois, sans personne devant. Jusqu'ici il n'avait qu'un moyen :
garder un jeton `oto_` porté au tableau, émis par l'admin qui l'avait branché. Ce jeton
appartient à la PERSONNE : il vit dans sa liste de clés, meurt si elle le révoque ou
perd l'accès, et une écriture du service se lit dans l'historique comme la sienne.

**Le geste, en deux faces.**

1. Une personne qui GOUVERNE le tableau (`govern_ns`, le droit de le partager) l'ouvre
   à un rôle de service (`PUT /api/datastores/{datastore}/service-writers/{role}`), et
   peut le refermer (`DELETE`) ou relire les ouvertures (`GET …/service-writers`).
   Seul un tableau d'ORG s'ouvre : le service écrit sous l'autorité de l'org
   propriétaire, jamais sous celle d'une personne. Seuls les rôles de
   `service_identity.ROLES_ECRIVAINS` s'inscrivent — le commerce n'écrit aucune ligne.
2. Le service, sous SON identité (`auth/service_identity`, client machine portant le
   rôle), écrit un lot : `POST /api/service/orgs/{id}/datastores/{datastore}/rows/batch`.
   Trois verrous, chacun rend le même 404 nu (on ne dit pas à un service ce qui existe
   hors de ce qu'on lui a ouvert) : le tableau est ouvert à CE rôle, il appartient à
   l'org du CHEMIN, et le store n'en voit aucun autre (`make_org_store`,
   `allowed_ns_ids={ns_id}`). Un identifiant de tableau faux côté service ne peut donc
   écrire ni chez une autre org, ni dans un autre tableau de la même.

L'écriture est le moteur du lot REST (`write_rows`), mêmes refus de forme ; l'historique
de ligne porte l'acteur `service:<client_id>` (`geste`, posé par l'adaptateur REST), et
le journal du tableau le même. Une org suspendue n'est pas écrite (409, comme un webhook
entrant, `docs/orgs-suspendues.md`).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ... import db, org_store, org_suspension, ownership
from ...auth import service_identity
from ...datastore import identite
from ...datastore import journal as datastore_journal
from ...datastore.core import DatastoreNotFound, DatastoreReadOnly, make_org_store
from ...datastore.identite import Adresse
from .._authz import SUB_ONLY, VISITORS_SERVICE
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES
from ._refus import _REFUS_D_ECRITURE
from .common import EntreeDatastore, HORODATAGE, govern_ns
from .rows import WrittenBatch, _verifier_contenu, _write_refusal

_ID = {"id": "org_id"}

_ROLE_INCONNU = DeclaredError(
    400, "unknown_service_role",
    "le rôle n'est pas un rôle de service ÉCRIVAIN de cette instance : le message dit "
    "lesquels le sont")
_TABLEAU_D_ORG_REQUIS = DeclaredError(
    400, "org_table_required",
    "le tableau n'appartient pas à une org : un service n'écrit que sous l'autorité "
    "d'une org, jamais sous celle d'une personne ou d'une équipe")
_INTROUVABLE = DeclaredError(
    404, "datastore_not_found",
    "le tableau n'existe pas, n'appartient pas à l'org du chemin, ou n'est pas ouvert "
    "à ce rôle de service — le même refus pour les trois")


def _role_ecrivain(role: str) -> str:
    r = (role or "").strip()
    if r not in service_identity.ROLES_ECRIVAINS:
        raise AuthzDenied(400, "unknown_service_role",
                          f"`{r}` n'est pas un rôle de service écrivain "
                          f"(attendu l'un de {sorted(service_identity.ROLES_ECRIVAINS)}).")
    return r


def _vue(row: dict) -> dict:
    return {"datastore": str(row["ns_id"]), "role": row["role"],
            "granted_by": row.get("granted_by"),
            "granted_at": None if row.get("granted_at") is None else str(row["granted_at"])}


# ── face PERSONNE : ouvrir, refermer, relire ─────────────────────────────────

class ServiceWriterInput(EntreeDatastore):
    datastore: Adresse
    role: str = Field(description="The service role to open the table to (e.g. `visitors`).")


class ServiceWritersInput(EntreeDatastore):
    datastore: Adresse


class ServiceWriter(BaseModel):
    datastore: str
    role: str
    granted_by: Optional[str] = None
    granted_at: Optional[str] = Field(default=None, description=HORODATAGE)


class ServiceWriters(BaseModel):
    service_writers: list[ServiceWriter]


class ServiceWriterRemoved(BaseModel):
    ok: bool
    datastore: str
    role: str


def _ouvrir(ctx: ResolvedCtx, inp: ServiceWriterInput) -> dict:
    role = _role_ecrivain(inp.role)
    ns_id = govern_ns(ctx.sub, inp.datastore)
    owner = ownership.owner_of(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id))
    if not owner or str(owner[0]) != "org":
        raise AuthzDenied(400, "org_table_required",
                          "Seul un tableau d'org s'ouvre à un service : celui-ci "
                          "appartient à une personne ou à une équipe.")
    return _vue(db.ecrivains_de_service.poser(ns_id, role, ctx.sub))


def _refermer(ctx: ResolvedCtx, inp: ServiceWriterInput) -> dict:
    role = _role_ecrivain(inp.role)
    ns_id = govern_ns(ctx.sub, inp.datastore)
    if not db.ecrivains_de_service.retirer(ns_id, role):
        raise AuthzDenied(404, "service_writer_not_found",
                          f"Le tableau n'est pas ouvert au rôle `{role}`.")
    return {"ok": True, "datastore": str(ns_id), "role": role}


def _lister(ctx: ResolvedCtx, inp: ServiceWritersInput) -> dict:
    ns_id = govern_ns(ctx.sub, inp.datastore)
    return {"service_writers": [_vue(r) for r in db.ecrivains_de_service.lister(ns_id)]}


# ── face SERVICE : écrire un lot ─────────────────────────────────────────────

class ServiceWriteRowsInput(BaseModel):
    org_id: int
    datastore: str = Field(description="The table's NUMBER (`ns_id`); a name is refused.")
    rows: list = Field(description="The rows, one object per row, one key per column.")
    key: Optional[str] = Field(default=None, description=(
        "Business-key column that DESIGNATES the rows: an existing key value MODIFIES "
        "its row, a new one creates a row."))


def _introuvable() -> AuthzDenied:
    return AuthzDenied(404, "datastore_not_found")


def _ecrire(ctx: ResolvedCtx, inp: ServiceWriteRowsInput) -> dict:
    # Le rôle vient de la RÈGLE qui a laissé passer (`SERVICE_ROLE`), jamais de l'entrée.
    role = str(ctx.role or "").removeprefix("service:")
    adresse = (inp.datastore or "").strip()
    if not adresse.isdigit():
        raise AuthzDenied(400, "datastore_by_id_required",
                          "Un service désigne le tableau par son numéro (`ns_id`).")
    ns_id = int(adresse)
    if not org_store.get_org(inp.org_id) or org_store.is_archived_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue ou archivée.")
    if db.ecrivains_de_service.ouvert(ns_id, role) is None:
        raise _introuvable()
    owner = ownership.owner_of(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id))
    if not owner or (str(owner[0]), str(owner[1])) != ("org", str(inp.org_id)):
        raise _introuvable()
    if org_suspension.etat(inp.org_id):
        raise AuthzDenied(409, "org_suspended",
                          "L'espace de cette org est suspendu : rien n'y est écrit.")
    _verifier_contenu(inp.rows)
    store = make_org_store(inp.org_id, allowed_ns_ids={ns_id})
    try:
        recap = store.write_rows(str(ns_id), inp.rows, key=inp.key)
    except DatastoreNotFound:
        raise _introuvable()
    except DatastoreReadOnly:
        raise AuthzDenied(403, "datastore_read_only")
    except ValueError as e:
        raise _write_refusal(e)
    tableau = store.dernier_tableau or {}
    datastore_journal.record(
        datastore_journal.TOOL_WRITE, sub=ctx.sub,
        ctx=datastore_journal.NsContext(ns_id=tableau.get("ns_id"),
                                        name=tableau.get("datastore") or str(ns_id)),
        fields=sorted({k for r in inp.rows if isinstance(r, dict) for k in r}),
        forced=store.off_forced)
    return {**identite.de_releve(tableau, str(ns_id)), **recap, **store.off_schema_report(),
            **identite.numero(tableau)}


_CHEMIN = "/api/datastores/{datastore}/service-writers"

CAPABILITIES += [
    Capability(
        key="me.datastore.service_writers.list",
        handler=_lister, Input=ServiceWritersInput, Output=ServiceWriters,
        authz=SUB_ONLY, mcp=None,
        rest=RestBinding(verb="GET", path=_CHEMIN),
        description=("The service roles this table is open to for writing (governance "
                     "right: whoever may share the table)."),
    ),
    Capability(
        key="me.datastore.service_writer.put",
        handler=_ouvrir, Input=ServiceWriterInput, Output=ServiceWriter,
        authz=SUB_ONLY, mcp=None,
        errors=(_ROLE_INCONNU, _TABLEAU_D_ORG_REQUIS),
        rest=RestBinding(verb="PUT", path=_CHEMIN + "/{role}"),
        description=("Opens an ORG table to a service role: that service may then write "
                     "its rows under the org's authority, with no person's token. "
                     "Idempotent. Governance right required."),
    ),
    Capability(
        key="me.datastore.service_writer.delete",
        handler=_refermer, Input=ServiceWriterInput, Output=ServiceWriterRemoved,
        authz=SUB_ONLY, mcp=None,
        errors=(_ROLE_INCONNU,
                DeclaredError(404, "service_writer_not_found",
                              "le tableau n'était pas ouvert à ce rôle")),
        rest=RestBinding(verb="DELETE", path=_CHEMIN + "/{role}"),
        description="Closes a table to a service role. Governance right required.",
    ),
    Capability(
        key="service.datastore.write_rows",
        handler=_ecrire, Input=ServiceWriteRowsInput, Output=WrittenBatch,
        authz=VISITORS_SERVICE, mcp=None,
        errors=(_INTROUVABLE,
                DeclaredError(404, "unknown_org", "l'org n'existe pas ou est archivée"),
                DeclaredError(400, "datastore_by_id_required",
                              "le tableau se désigne par son numéro"),
                DeclaredError(409, "org_suspended", "l'espace de l'org est suspendu"),
                DeclaredError(400, "row_invalid",
                              "une ligne du lot est refusée par le schéma : le message "
                              "nomme son rang")) + _REFUS_D_ECRITURE,
        rest=RestBinding("POST", "/api/service/orgs/{id}/datastores/{datastore}/rows/batch",
                         _ID),
        description=("[service visitors] Writes a batch of rows into an org table the org "
                     "opened to this service role (`PUT …/service-writers/{role}`). The "
                     "table must belong to the org in the path; anything else is the same "
                     "404."),
    ),
]
