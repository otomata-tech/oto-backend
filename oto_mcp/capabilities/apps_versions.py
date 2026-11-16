"""Apps : brouillon, publication, lecture d'une version — les ops d'`oto_apps` qui
donnent à une app l'expérience d'un dépôt (`docs/apps.md` § Brouillon et publication).

**Une app qui passe par un brouillon** (`drafts_enabled`, `op=set_drafts`) ne change
plus pour ses lecteurs à chaque écriture : `update` et `patch` écrivent son manifeste
et son HTML au BROUILLON (le titre et la vignette, la carte, restent écrits en
direct). Ses éditeurs l'ouvrent en aperçu (`op=get draft=true`, et
`POST /api/me/apps/{id}/call` avec `draft: true`, jugé contre le manifeste et l'auteur
du brouillon) ; `op=publish` le rend vivant pour tous, avec une note.

**Publier, c'est réécrire l'app sous son nom** — comme restaurer : le manifeste du
brouillon est rejugé contre celui qui publie, qui en devient l'auteur ; il nomme le
brouillon qu'il a vu (`expected_rev`), sinon un brouillon changé entre l'aperçu et le
clic deviendrait vivant sous son nom sans qu'il l'ait lu.

**Un lecteur ne sait pas qu'un brouillon existe** : `op=get` ne le nomme qu'à qui peut
modifier l'app, et `draft=true` refuse un lecteur de la même façon qu'il y ait un
brouillon ou non.

**Une version se lit avant de se restaurer** (`op=revision`, éditeurs seuls : un
HTML d'hier peut porter ce qu'on en a retiré), et se restaure vers le brouillon
(`op=restore to_draft=true`) pour l'essayer avant de la publier.
"""
from __future__ import annotations

from typing import Any, Optional

from .. import db
from ._types import AuthzDenied, ResolvedCtx
from .apps_manifeste import org_de_l_app

MAX_NOTE = 500


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _note(note: Optional[str]) -> Optional[str]:
    if note is None or not note.strip():
        return None
    _require(len(note) <= MAX_NOTE, "invalid_note", f"A version note is at most {MAX_NOTE} characters.")
    return note.strip()


def resume(draft: Optional[dict]) -> Optional[dict]:
    """Ce qu'un éditeur voit du brouillon sans l'ouvrir : qui, quand, son `rev`."""
    if draft is None:
        return None
    return {"rev": draft["rev"], "updated_by": draft["by"], "updated_at": draft["at"]}


def _conflit(e: db.app_versions.DraftConflict) -> AuthzDenied:
    if isinstance(e, db.app_versions.DraftExists):
        return AuthzDenied(
            409, "draft_exists",
            f"This app has a draft (rev {e.current_rev}) and you wrote against its "
            "published version: read the draft with op=get draft=true, then write "
            "against its rev.")
    return AuthzDenied(409, "draft_conflict",
                       f"The draft changed since you read it (now at rev {e.current_rev}): "
                       "read it again with op=get draft=true.")


def ecrire(ctx: ResolvedCtx, row: dict, *, expected_rev: Optional[str],
           manifest: Optional[dict] = None, html: Optional[str] = None,
           auteur_ou_refus) -> dict:
    """Écrit le brouillon. `manifest`, s'il est donné, est déjà jugé contre l'appelant ;
    sinon, l'appelant devient l'auteur du brouillon et doit atteindre ce que son
    manifeste déclare (`auteur_ou_refus`, la règle d'une écriture de l'app)."""
    if manifest is None:
        courant = db.app_versions.draft(row["id"])
        base = ({"manifest": courant["manifest"], "updated_by": courant["by"]} if courant
                else {"manifest": row["manifest"], "updated_by": row["updated_by"]})
        auteur_ou_refus(ctx, {**row, **base})
    try:
        out = db.app_versions.write_draft(row["id"], by=ctx.sub, expected_rev=expected_rev,
                                          manifest=manifest, html=html)
    except db.app_versions.DraftConflict as e:
        raise _conflit(e) from None
    except db.app_versions.DraftsOff:
        raise AuthzDenied(409, "drafts_off",
                          "This app doesn't use drafts: its edits are live. Turn them on "
                          "first (op=set_drafts enabled=true).") from None
    _require(out is not None, "unknown_app", f"App {row['id']} doesn't exist.", 404)
    return out


def base_du_patch(row: dict) -> tuple[str, dict, str]:
    """Le HTML, le manifeste et le `rev` sur lesquels un `patch` s'applique dans une app
    à brouillon : le brouillon en cours, sinon l'état vivant dont il naîtra."""
    courant = db.app_versions.draft(row["id"])
    if courant is not None:
        return courant["html"], courant["manifest"], courant["rev"]
    return row.get("html") or "", row.get("manifest") or {}, row["rev"]


def lire(row: dict, can_edit: bool) -> tuple[dict, dict]:
    """`op=get draft=true` : le brouillon, à la place du manifeste et du HTML vivants."""
    _require(can_edit, "draft_editors_only", "Only the app's editors open its draft.", 403)
    courant = db.app_versions.draft(row["id"])
    _require(courant is not None, "no_draft", "This app has no draft.", 404)
    return {**row, "manifest": courant["manifest"], "html": courant["html"],
            "rev": courant["rev"]}, courant


def publier(ctx: ResolvedCtx, row: dict, expected_rev: Optional[str], note: Optional[str],
            checked_manifest, avertir) -> tuple[dict, dict]:
    _require(expected_rev, "missing_rev",
             "Name the draft you publish: `expected_rev`, the draft's rev from op=get draft=true.")
    courant = db.app_versions.draft(row["id"])
    _require(courant is not None, "no_draft", "This app has no draft to publish.", 409)
    # Le brouillon LU doit être celui que la personne a vu, AVANT de juger son
    # manifeste : sinon un brouillon changé entre-temps ferait juger un manifeste et
    # publier le HTML d'un autre (la base le refuserait, mais après le jugement).
    if courant["rev"] != expected_rev:
        raise _conflit(db.app_versions.DraftConflict(courant["rev"]))
    manifest = checked_manifest(ctx, org_de_l_app(row), courant["manifest"])
    extra = avertir(manifest, courant["html"])
    try:
        out = db.app_versions.publish(row["id"], by=ctx.sub, expected_rev=expected_rev,
                                      manifest=manifest, note=_note(note))
    except db.app_versions.NoDraft:
        raise AuthzDenied(409, "no_draft", "This app has no draft to publish.") from None
    except db.app_versions.DraftConflict as e:
        raise _conflit(e) from None
    _require(out is not None, "unknown_app", f"App {row['id']} doesn't exist.", 404)
    return out, extra


def supprimer(row: dict, expected_rev: Optional[str]) -> bool:
    try:
        return db.app_versions.delete_draft(row["id"], expected_rev=expected_rev)
    except db.app_versions.DraftConflict as e:
        raise _conflit(e) from None


def regler(row: dict, enabled: Optional[bool]) -> dict:
    _require(enabled is not None, "missing_enabled", "`enabled` is required (true or false).")
    try:
        out = db.app_versions.set_enabled(row["id"], bool(enabled))
    except db.app_versions.DraftPending:
        raise AuthzDenied(409, "draft_pending",
                          "This app has a draft: publish it or delete it before turning "
                          "drafts off.") from None
    _require(out is not None, "unknown_app", f"App {row['id']} doesn't exist.", 404)
    return out


def version(row: dict, revision_id: Optional[int]) -> dict[str, Any]:
    """`op=revision` : une version entière, pour la lire ou la comparer avant de la
    restaurer. Éditeurs seuls (l'appelant l'a jugé)."""
    _require(revision_id is not None, "missing_revision", "`revision_id` is required.")
    old = db.apps.revision(row["id"], int(revision_id))
    _require(old is not None, "unknown_revision", "No such revision of this app.", 404)
    return {k: old.get(k) for k in ("id", "title", "manifest", "html", "thumbnail",
                                    "edited_by", "created_at", "saved_at", "published_at",
                                    "note")}
