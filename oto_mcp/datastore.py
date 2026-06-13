"""Datastore — couche Google Sheets sur l'API datastore d'oto.

Une namespace = un spreadsheet Google Sheets dans le Drive du user. Schéma
auto-managé :

- Row 1 = headers. Les 3 premières colonnes sont `_id`, `_created_at`,
  `_updated_at` (auto). Les colonnes suivantes apparaissent au fur et à
  mesure que les rows introduisent des champs nouveaux.
- Row 2+ = données.
- Les valeurs sont stockées en string (Sheets RAW). Pour les listes/dicts,
  on JSON-encode automatiquement.

Concurrence : Sheets n'a pas de transactions. `values.append` est atomique
par appel. **Contrainte d'exploitation** : la création de colonnes
(`_ensure_headers`) est non-atomique avec le write des headers. La sûreté
repose sur uvicorn **single-worker** — voir `deploy/oto-mcp.service`.
Si on passe à `--workers > 1`, deux appends concurrents sur la même
namespace peuvent corrompre les headers. Idem pour `update_row` qui peut
écrire sur la mauvaise row si un delete concurrent décale les indices.

Le `DatastoreSheets` class est instancié **par requête** (state-less) à partir
du `sub`. Multi-compte (#9) : chaque namespace porte son compte Google
propriétaire (`owner_email`, figé à la création) ; les ops résolvent les
credentials de CE compte — plus de 403 quand un sheet vit hors du compte par
défaut. Clients mis en cache par compte au sein de l'instance, pas de cache global.

Encodage des cellules : on préserve les types Python via un préfixe
sentinel `__j:` pour tout ce qui n'est pas string. Les strings utilisateur
sont écrites brutes ; les non-strings (int/float/bool/dict/list) sont
encodées JSON avec le préfixe. Une string qui ressemble à du JSON
(commence par `{`, `[`, `"` ou vaut `true`/`false`/`null`) est aussi
préfixée pour éviter l'ambiguïté au read-back.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import db


_META_COLS = ("_id", "_created_at", "_updated_at")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    # uuid7-ish : timestamp ms + random. uuid.uuid7 dispo en 3.13 ; ici on
    # construit à la main pour compat 3.10+.
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = uuid.uuid4().int & ((1 << 74) - 1)
    raw = (ms << 80) | (0x7 << 76) | (rand << 2)
    return str(uuid.UUID(int=raw))


def _col_letter(n: int) -> str:
    """0-indexed col → A, B, …, Z, AA, AB, … ."""
    s = ""
    n += 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


# Sentinel pour les valeurs typées. Visible-mais-discret dans Sheets,
# unambigu au read-back. Cf. docstring module.
_TYPE_PREFIX = "__j:"
_AMBIGUOUS_STRINGS = ("true", "false", "null")


def _looks_like_json(v: str) -> bool:
    return bool(v) and (v[0] in '{["' or v in _AMBIGUOUS_STRINGS)


def _serialize(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        # String qui pourrait être confondu avec une valeur typée au
        # read-back → on la tagge pour préserver son type.
        if v.startswith(_TYPE_PREFIX) or _looks_like_json(v):
            return _TYPE_PREFIX + json.dumps(v, ensure_ascii=False)
        return v
    # Tout le reste (int/float/bool/dict/list) : JSON + sentinel
    return _TYPE_PREFIX + json.dumps(v, ensure_ascii=False)


def _deserialize(v: str) -> Any:
    if v == "":
        return None
    if v.startswith(_TYPE_PREFIX):
        try:
            return json.loads(v[len(_TYPE_PREFIX):])
        except Exception:
            return v
    return v


class NamespaceNotFound(Exception):
    pass


class RowNotFound(Exception):
    pass


class NamespaceExists(Exception):
    pass


class GoogleNotConnected(Exception):
    """User n'a pas grant Google Drive — pointer vers /account."""
    pass


def make_store(sub: str) -> "DatastoreSheets":
    """Construit un DatastoreSheets pour `sub`.

    Raises GoogleNotConnected si l'user n'a aucun compte Google connecté —
    chaque caller (MCP tool, REST handler) doit traduire en erreur de sa surface.
    """
    from . import google_oauth
    try:
        google_oauth.credentials_for(sub)  # valide qu'au moins un compte existe
    except RuntimeError as e:
        raise GoogleNotConnected(str(e))
    return DatastoreSheets(sub)


class DatastoreSheets:
    """Wrapper Sheets/Drive multi-compte pour un user authentifié.

    Résout les credentials Google par **compte propriétaire de chaque
    namespace** (#9) : un user multi-comptes accède à un sheet hors de son
    compte par défaut sans 403. Les clients sont mis en cache par compte.
    """

    def __init__(self, sub: str):
        self.sub = sub
        self._svc_cache: dict = {}

    # --- google clients per owner account ------------------------------------

    def _services(self, account: Optional[str]):
        """(sheets, drive) pour `account` (email) ; None = compte par défaut."""
        if account not in self._svc_cache:
            from . import google_oauth
            from googleapiclient.discovery import build
            creds = google_oauth.credentials_for(self.sub, account=account)
            # cache_discovery=False : évite le warning oauth2client manquant
            self._svc_cache[account] = (
                build("sheets", "v4", credentials=creds, cache_discovery=False),
                build("drive", "v3", credentials=creds, cache_discovery=False),
            )
        return self._svc_cache[account]

    def _default_email(self) -> Optional[str]:
        row = db.get_google_oauth(self.sub)
        return row.get("google_email") if row else None

    def _account_for(self, namespace: str, spreadsheet_id: str) -> Optional[str]:
        """Compte Google propriétaire du sheet de ce namespace.

        `owner_email` figé → direct. Sinon (namespace créé avant le
        multi-compte) back-fill : on essaie le défaut puis chaque compte
        connecté, et on **fige** celui qui a accès. None = compte par défaut.
        """
        ns = db.get_datastore_namespace(self.sub, namespace)
        if ns and ns.get("owner_email"):
            return ns["owner_email"]
        if not ns:
            return None  # namespace partagé : tenté avec le défaut du lecteur
        candidates: list = [None]
        for a in db.list_google_accounts(self.sub):
            em = a.get("google_email")
            if em and em not in candidates:
                candidates.append(em)
        for acc in candidates:
            try:
                sheets, _ = self._services(acc)
                sheets.spreadsheets().get(
                    spreadsheetId=spreadsheet_id, fields="spreadsheetId"
                ).execute()
            except Exception:
                continue
            owner = acc or self._default_email()
            if owner:
                db.set_datastore_owner(self.sub, namespace, owner)
            return acc
        return None  # aucun accès trouvé — laisse l'op échouer avec l'erreur réelle

    def _ns_record(self, namespace: str) -> dict:
        rec = db.get_datastore_namespace(self.sub, namespace) \
            or db.get_shared_namespace(self.sub, namespace)
        if not rec:
            raise NamespaceNotFound(namespace)
        return rec

    def _ctx(self, namespace: str):
        """(sheets, drive, spreadsheet_id) pour un namespace existant."""
        rec = self._ns_record(namespace)
        sid = rec["spreadsheet_id"]
        sheets, drive = self._services(self._account_for(namespace, sid))
        return sheets, drive, sid

    # --- namespace lifecycle -------------------------------------------------

    def list_namespaces(self) -> list[dict]:
        own = db.list_datastore_namespaces(self.sub)
        for n in own:
            n["url"] = f"https://docs.google.com/spreadsheets/d/{n['spreadsheet_id']}/edit"
            n["shared"] = False
        shared = db.list_shared_namespaces(self.sub)
        for n in shared:
            n["url"] = f"https://docs.google.com/spreadsheets/d/{n['spreadsheet_id']}/edit"
            n["shared"] = True
        return own + shared

    def create_namespace(self, namespace: str) -> dict:
        # Créé dans le compte par défaut, dont on fige l'email comme propriétaire.
        sheets, drive = self._services(None)
        owner = self._default_email()
        title = f"oto.{namespace}"
        body = {
            "properties": {"title": title},
            "sheets": [{
                "properties": {"title": "data"},
                "data": [{
                    "startRow": 0,
                    "startColumn": 0,
                    "rowData": [{
                        "values": [
                            {"userEnteredValue": {"stringValue": h},
                             "userEnteredFormat": {"textFormat": {"bold": True}}}
                            for h in _META_COLS
                        ],
                    }],
                }],
            }],
        }
        created = sheets.spreadsheets().create(
            body=body,
            fields="spreadsheetId,spreadsheetUrl",
        ).execute()
        sid = created["spreadsheetId"]
        try:
            db.create_datastore_namespace(self.sub, namespace, sid, owner_email=owner)
        except ValueError as e:
            # Race ou collision : on a déjà créé le sheet côté Drive,
            # on le met à la corbeille pour ne pas laisser d'orphelin.
            try:
                drive.files().update(fileId=sid, body={"trashed": True}).execute()
            except Exception:
                pass
            raise NamespaceExists(str(e))
        return {
            "namespace": namespace,
            "spreadsheet_id": sid,
            "url": created.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}/edit",
        }

    def delete_namespace(self, namespace: str, *, trash: bool = True) -> None:
        rec = db.get_datastore_namespace(self.sub, namespace)
        if not rec:
            raise NamespaceNotFound(namespace)
        if trash:
            sid = rec["spreadsheet_id"]
            _, drive = self._services(self._account_for(namespace, sid))
            try:
                drive.files().update(fileId=sid, body={"trashed": True}).execute()
            except Exception:
                # Si le file n'existe plus côté Drive, on continue à nettoyer la DB
                pass
        db.delete_datastore_namespace(self.sub, namespace)

    def get_url(self, namespace: str) -> str:
        ns = db.get_datastore_namespace(self.sub, namespace)
        if not ns:
            ns = db.get_shared_namespace(self.sub, namespace)
        if not ns:
            raise NamespaceNotFound(namespace)
        return f"https://docs.google.com/spreadsheets/d/{ns['spreadsheet_id']}/edit"

    # --- row ops -------------------------------------------------------------

    def _read_headers(self, sheets, spreadsheet_id: str) -> list[str]:
        result = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="data!1:1",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        values = result.get("values", [])
        return values[0] if values else []

    def _ensure_headers(self, sheets, spreadsheet_id: str, fields: list[str]) -> list[str]:
        """S'assure que les `fields` existent comme colonnes. Renvoie l'ordre complet."""
        headers = self._read_headers(sheets, spreadsheet_id)
        if not headers:
            headers = list(_META_COLS)
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="data!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
        missing = [f for f in fields if f not in headers]
        if missing:
            start = len(headers)
            new_headers = headers + missing
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"data!{_col_letter(start)}1",
                valueInputOption="RAW",
                body={"values": [missing]},
            ).execute()
            headers = new_headers
        return headers

    def append_row(self, namespace: str, data: dict) -> dict:
        sheets, _, sid = self._ctx(namespace)
        user_fields = [k for k in data.keys() if k not in _META_COLS]
        headers = self._ensure_headers(sheets, sid, user_fields)
        now = _now_iso()
        row_id = _new_id()
        record = dict(data)
        record["_id"] = row_id
        record["_created_at"] = now
        record["_updated_at"] = now
        row = [_serialize(record.get(h)) for h in headers]
        sheets.spreadsheets().values().append(
            spreadsheetId=sid,
            range="data!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return self._row_to_dict(headers, row)

    def _read_all(self, sheets, spreadsheet_id: str) -> tuple[list[str], list[list[str]]]:
        result = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="data",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return [], []
        headers = rows[0]
        data_rows = rows[1:]
        # Pad rows to header length
        data_rows = [r + [""] * (len(headers) - len(r)) for r in data_rows]
        return headers, data_rows

    def _row_to_dict(self, headers: list[str], row: list[str]) -> dict:
        padded = row + [""] * (len(headers) - len(row))
        return {h: _deserialize(padded[i]) for i, h in enumerate(headers)}

    def _find_row(self, sheets, spreadsheet_id: str, row_id: str) -> Optional[tuple[int, list[str], list[str]]]:
        """Renvoie `(row_index_1based, headers, raw_row)` ou None.

        row_index_1based : indice de la row dans la sheet (1 = headers, 2 = première data).
        """
        headers, data_rows = self._read_all(sheets, spreadsheet_id)
        if not headers or "_id" not in headers:
            return None
        id_col = headers.index("_id")
        for i, r in enumerate(data_rows, start=2):
            if r[id_col] == row_id:
                return i, headers, r
        return None

    def get_row(self, namespace: str, row_id: str) -> dict:
        sheets, _, sid = self._ctx(namespace)
        found = self._find_row(sheets, sid, row_id)
        if not found:
            raise RowNotFound(row_id)
        _, headers, row = found
        return self._row_to_dict(headers, row)

    def list_rows(
        self,
        namespace: str,
        filter: Optional[dict] = None,
        limit: int = 100,
    ) -> list[dict]:
        sheets, _, sid = self._ctx(namespace)
        headers, data_rows = self._read_all(sheets, sid)
        if not headers:
            return []
        out = []
        for row in data_rows:
            record = self._row_to_dict(headers, row)
            if record.get("_id") in (None, ""):
                continue  # ligne vide
            if filter:
                if not all(str(record.get(k)) == str(v) for k, v in filter.items()):
                    continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

    def update_row(self, namespace: str, row_id: str, patch: dict) -> dict:
        sheets, _, sid = self._ctx(namespace)
        new_fields = [k for k in patch.keys() if k not in _META_COLS]
        headers = self._ensure_headers(sheets, sid, new_fields)
        found = self._find_row(sheets, sid, row_id)
        if not found:
            raise RowNotFound(row_id)
        idx, found_headers, raw = found
        # `_find_row` peut renvoyer un set de headers plus court si lui a lu
        # avant `_ensure_headers` (impossible ici car ensure vient avant,
        # mais on garde `headers` autoritatif).
        record = self._row_to_dict(found_headers, raw)
        for k, v in patch.items():
            if k in _META_COLS:
                continue
            record[k] = v
        record["_updated_at"] = _now_iso()
        record["_id"] = row_id  # préserve
        row_values = [_serialize(record.get(h)) for h in headers]
        last_col = _col_letter(len(headers) - 1)
        sheets.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"data!A{idx}:{last_col}{idx}",
            valueInputOption="RAW",
            body={"values": [row_values]},
        ).execute()
        return self._row_to_dict(headers, row_values)

    def delete_row(self, namespace: str, row_id: str) -> None:
        sheets, _, sid = self._ctx(namespace)
        found = self._find_row(sheets, sid, row_id)
        if not found:
            raise RowNotFound(row_id)
        idx, _, _ = found
        # data sheet a sheetId=0 par défaut sur create — on récupère via meta
        meta = sheets.spreadsheets().get(
            spreadsheetId=sid, fields="sheets(properties(sheetId,title))"
        ).execute()
        sheet_id = next(
            (s["properties"]["sheetId"] for s in meta.get("sheets", [])
             if s["properties"]["title"] == "data"),
            0,
        )
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": idx - 1,
                        "endIndex": idx,
                    }
                }
            }]},
        ).execute()
