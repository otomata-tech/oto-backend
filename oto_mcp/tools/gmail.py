"""Gmail — surface oto-core (GmailClient) exposée par-utilisateur, multi-compte.

Chaque user connecte un ou plusieurs comptes Google sur
`https://manage.oto.cx/` (section Google) via le flow OAuth unifié (scope
`gmail.modify`). Les tools `gmail_*` agissent sur le compte par défaut, ou sur
le compte ciblé par le paramètre `account` (l'adresse email).

Pas de clé plateforme : l'accès est strictement per-user via OAuth (comme le
datastore et WhatsApp), donc pas de `resolve_api_key` ici.

**Surface consolidée (ADR 0047 §Amendement, appliqué au produit gmail)** : un tool
par OBJET métier, le verbe en paramètre `op` — `gmail_message` (search/get/
attachment/drafts/archive/trash : tout ce qui désigne un message de la boîte, par
requête ou par id). Deux tools restent SEULS :
- `gmail_list_accounts` : aucun paramètre (il énumère les `account` que les autres
  consomment) — même cas que `zoho_modules`, fusionner de la découverte pure
  n'homogénéise rien ;
- `gmail_compose` : ses ~12 paramètres de rédaction (body/to/subject/reply_to/cc/
  bcc/html/from_name/markdown/attachments/mode) ne recouvrent AUCUN paramètre des
  ops ci-dessus — c'est une variante disjointe, qui pèserait dans le schéma
  exactement ce qu'elle pèse aujourd'hui séparée (critère = homogénéité des
  paramètres, pas le comptage).

⚠️ Ce module ÉCRIT sur la boîte de l'utilisateur : `op="archive"`/`op="trash"`
(gmail_message) et `gmail_compose` (envoi réel). Le défaut de `gmail_message` est
`op="search"` — une LECTURE : un appel sans `op` ne peut ni écrire ni supprimer.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_content, file_source
from ..auth import google as google_oauth

# Ops de `gmail_message`, dans l'ordre lectures → écritures. Source unique : la
# validation d'entrée ET le message de refus en dérivent, donc une op ajoutée ne
# peut pas être acceptée sans être annoncée (ni l'inverse).
_MESSAGE_READ_OPS = ("search", "get", "attachment", "drafts")
_MESSAGE_WRITE_OPS = ("archive", "trash")
_MESSAGE_OPS = _MESSAGE_READ_OPS + _MESSAGE_WRITE_OPS
_MESSAGE_OPS_ERROR = (
    "op doit être 'search', 'get', 'attachment', 'drafts', 'archive' ou 'trash'")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable, jamais de fallback.

    Une valeur VIDE compte comme absente : `message_ids=[]` sur `op='trash'`
    rendrait un `{"trashed": []}` qui passerait pour un succès alors que rien n'a
    été demandé, et `query=""` sur `op='search'` ratisserait la boîte entière.
    """
    if value is None or (isinstance(value, (str, list)) and not value):
        raise _bad(f"op='{op}' requiert {name}")
    return value


def _client_for_user(account: Optional[str] = None):
    """Instancie un GmailClient oto-core avec les credentials du user.

    `account` (email) cible un compte précis ; None = compte par défaut.
    Lève une McpError actionnable si aucun compte Google n'est connecté.
    """
    sub = access.current_user_sub_or_raise()
    try:
        creds = google_oauth.credentials_for(sub, account=account)
    except RuntimeError as e:
        raise _bad(str(e))
    from oto.tools.google.gmail.lib.gmail_client import GmailClient
    return GmailClient(credentials=creds)


_GOOGLE_CLIENT_TIMEOUT_S = 20
# oto-backend#867 lot 2 — `_client_for_user` peut déclencher un rafraîchissement de
# jeton (`google_oauth.credentials_for` → `_refresh_access_token`, HTTP synchrone
# 15s), dans un handler `async def` : hors boucle + borné, même méthode que la liste
# d'identités Unipile (lot 1) et les routes FOD (lot 2). Les appels à l'API Gmail,
# eux, sont déjà en `to_thread` — seule la construction du client (donc le refresh)
# tournait encore dans la boucle.
async def _client_for_user_async(account: Optional[str] = None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_client_for_user, account),
                                      timeout=_GOOGLE_CLIENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise _bad(f"Google n'a pas répondu dans les {_GOOGLE_CLIENT_TIMEOUT_S}s "
                   "(rafraîchissement de jeton) — réessaie.")


_ATTACHMENTS_TIMEOUT_S = 90


def _resolve_attachments(attachments):
    """Résout des refs `file_source` en fichiers TEMPORAIRES (le GmailClient attend
    des CHEMINS locaux pour ses pièces jointes, or le serveur n'a pas le disque de
    l'utilisateur). `attachments` = liste de `{"kind":"drive|gmail|url", …}` (cf.
    file_source.resolve). Renvoie `(paths, cleanup)` — l'appelant DOIT appeler
    `cleanup()` en finally. Lève FileSourceError sur une ref illisible (nettoie
    d'abord le temp déjà écrit)."""
    if not attachments:
        return [], (lambda: None)
    tmpdir = tempfile.mkdtemp(prefix="oto-gmail-att-")

    def cleanup():
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        paths = []
        for i, src in enumerate(attachments):
            rf = file_source.resolve(src)
            # basename défensif : jamais laisser un filename traverser le tmpdir.
            name = os.path.basename(rf.filename or "") or f"attachment-{i}"
            path = os.path.join(tmpdir, name)
            with open(path, "wb") as f:
                f.write(rf.data)
            paths.append(path)
        return paths, cleanup
    except Exception:
        cleanup()
        raise


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def gmail_list_accounts() -> dict:
        """List the Google accounts the user has connected.

        Returns {accounts: [{email, is_default}]}. Use an `email` value as the
        `account` argument of the other gmail_* tools to act on a specific
        account; omit `account` to use the default.
        """
        sub = access.current_user_sub_or_raise()
        accounts = google_oauth.list_accounts(sub)
        return {
            "accounts": [
                {"email": a.get("google_email"), "is_default": a.get("is_default", False)}
                for a in accounts
            ]
        }

    @mcp.tool()
    async def gmail_message(
        op: Literal["search", "get", "attachment", "drafts", "archive",
                    "trash"] = "search",
        query: Optional[str] = None,
        message_id: Optional[str] = None,
        message_ids: Optional[list[str]] = None,
        filename: Optional[str] = None,
        index: int = 0,
        max_results: int = 20,
        account: Optional[str] = None,
    ) -> dict:
        """A message in the user's mailbox — search, read, fetch an attachment,
        list drafts, archive, trash.

        `op`:
        - **"search"** (default): search the user's Gmail with Gmail query syntax.
          `query` e.g. `from:foo@bar.com is:unread newer_than:7d`. Returns
          {messages: [{id, threadId, from, subject, date, snippet, labelIds}], count}.
        - **"get"**: fetch a full message (headers, body, attachment metadata) by
          `message_id`.
        - **"attachment"**: fetch the CONTENT of a Gmail attachment, by filename.
          Identify the attachment by its `filename` (from the `attachments` list of
          `op="get"`). The response depends on the file:
          - **small text** (JSON/CSV/Markdown/plain, ≤256 KB) → returned INLINE:
            `{encoding: "text", content: "<decoded text>"}` — read it directly.
          - **binary or large** (PDF, image, big file) → uploaded to temporary
            storage and returned as a short-lived signed URL: `{encoding: "url",
            url, expires_in}` (seconds). Fetch the URL to get the bytes.
          Returns {filename, mimeType, size, encoding, content|url, expires_in?}.
        - **"drafts"**: list the user's Gmail drafts. Returns
          {drafts: [{id, message_id, to, subject, date, snippet}], count}.
        - **"archive"** — ⚠️ WRITES: removes the INBOX label from `message_ids`.
        - **"trash"** — ⚠️ WRITES: moves `message_ids` to the trash.

        Writing an email (send or save a draft, new message or reply) is a
        different tool: `gmail_compose`.

        Args:
            op: search (default) | get | attachment | drafts | archive | trash.
            query: op="search" — Gmail search query (e.g.
                `from:foo@bar.com is:unread newer_than:7d`).
            message_id: op="get"/"attachment" — Gmail message id (the one returned
                by op="search").
            message_ids: op="archive"/"trash" — Gmail message ids to act on.
            filename: op="attachment" — name of the attachment to fetch
                (e.g. "Contrat.pdf").
            index: op="attachment" — 0-based tiebreaker if several attachments
                share that name (e.g. inline images); default 0 = the first one.
            max_results: op="search"/"drafts" — max items to return (default 20).
            account: email of the Google account to use (default if omitted).
        """
        # Refus AVANT toute résolution de credential : une op inconnue n'atteint
        # jamais le client — donc jamais, par un chemin dérivé, une écriture.
        if op not in _MESSAGE_OPS:
            raise _bad(_MESSAGE_OPS_ERROR)
        client = await _client_for_user_async(account)

        # ---- lectures --------------------------------------------------------
        if op == "search":
            messages = await asyncio.to_thread(
                client.search, _need(query, "query", op), max_results)
            return {"messages": messages, "count": len(messages)}

        if op == "get":
            return await asyncio.to_thread(
                client.get_message, _need(message_id, "message_id", op))

        if op == "attachment":
            mid = _need(message_id, "message_id", op)
            name = _need(filename, "filename", op)
            try:
                att = await asyncio.to_thread(client.get_attachment, mid, name, index)
            except Exception as e:
                raise _bad(str(e))
            data, att_filename, mime = att["data"], att["filename"], att["mimeType"]
            sub = access.current_user_sub_or_raise()
            try:
                return await asyncio.to_thread(
                    file_content.render_for_agent, data, att_filename, mime,
                    sub=sub, prefix="gmail-attachments")
            except file_content.MediaUnavailable as e:
                raise _bad(str(e))

        if op == "drafts":
            drafts = await asyncio.to_thread(client.list_drafts, max_results)
            return {"drafts": drafts, "count": len(drafts)}

        # ---- écritures -------------------------------------------------------
        if op == "archive":
            results = await asyncio.to_thread(
                client.archive_messages, _need(message_ids, "message_ids", op))
            return {"archived": results}

        if op == "trash":
            # Gmail n'a pas de corbeille en lot : c'est un appel par message,
            # donc une boucle — et donc une écriture PARTIELLE possible. Si le
            # 3ᵉ échoue, les deux premiers SONT à la corbeille ; laisser
            # l'exception nue remonter ne dirait à l'agent qu'« échec », il
            # conclurait « rien n'est parti » et rejouerait une écriture déjà
            # faite. C'est le défaut que décrit le signal #227 (une action
            # appliquée dont l'appelant n'apprend rien), et la même faute que
            # #600 : annoncer un échec sur un succès. On nomme les trois lots.
            ids = list(_need(message_ids, "message_ids", op))
            trashed: list = []
            for i, mid in enumerate(ids):
                try:
                    res = await asyncio.to_thread(client.trash_message, mid)
                except Exception as e:  # noqa: BLE001 — re-levée nommée, avec l'état réel
                    restants = ids[i + 1:]
                    raise _bad(
                        "Corbeille PARTIELLE — l'écriture s'arrête au premier "
                        "échec, mais ce qui précède a bien eu lieu. "
                        f"DÉJÀ à la corbeille ({len(trashed)}) : {trashed}. "
                        f"ÉCHEC sur `{mid}` : {type(e).__name__}: {e}. "
                        f"NON TENTÉS ({len(restants)}) : {restants}. "
                        "Ne rejoue que les non-tentés — retenter les premiers "
                        "n'est pas nécessaire."
                    ) from e
                trashed.append(res.get("id", mid))
            return {"trashed": trashed}

        # Structurellement inatteignable (garde d'entrée ci-dessus) — filet contre
        # un `return None` implicite si une op était ajoutée à `_MESSAGE_OPS` sans
        # sa branche : mieux vaut refuser que rendre « rien » pour un succès.
        raise _bad(_MESSAGE_OPS_ERROR)

    @mcp.tool()
    async def gmail_compose(
        body: str,
        mode: Literal["send", "draft"] = "draft",
        to: Optional[str] = None,
        subject: Optional[str] = None,
        reply_to: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: Optional[str] = None,
        from_name: Optional[str] = None,
        markdown: bool = True,
        account: Optional[str] = None,
        attachments: Optional[list[dict]] = None,
    ) -> dict:
        """Compose an email — **saved as a DRAFT by default**, or sent explicitly.

        ⚠️ **`mode="send"` is required to actually send.** Omitting `mode` writes a draft
        the user can review; it does NOT leave the mailbox. Say plainly which one you did
        (read `kind` in the answer) — never report "sent" for a draft, or the reverse.

        ⚠️ **A freshly created draft can be MISSING from an already-open Gmail tab**,
        for an indeterminate time. The write is fine and the API lists it; it is the
        client view that lags. Measured 2026-09-04: the API confirmed the draft, three
        reads found it, and the person staring at their Drafts folder saw one older
        draft and nothing else — a full diagnosis was spent before the cause was found.
        So `kind: "draft"` means SAVED, not VISIBLE: tell the person to reload the tab
        or search `in:drafts` rather than to look again.

        Returns `kind` — **"sent" (the mail LEFT) or "draft" (saved, not sent)** — plus
        the message ids. Always read `kind` before reporting what you did: it is the
        only field that states the act.

        Args:
            body: message body (rendered from markdown to HTML by default).
            mode: "draft" (default) saves for human review; "send" delivers it now.
            to: recipient(s), comma-separated. REQUIRED for a new message (omit when replying).
            subject: subject line (new message only; a reply keeps the thread's subject).
            reply_to: id of the message to reply to. When set, this is a threaded REPLY
                (subject/thread preserved) and `to`/`subject` are ignored.
            cc / bcc: optional carbon copy (bcc: new message only).
            html: explicit HTML body (bypasses markdown rendering).
            from_name: optional display name for the From header.
            markdown: render `body` from markdown when `html` is absent (default True).
            account: email of the Google account to use (default if omitted).
            attachments: files to attach, as `source` refs oto resolves server-side
                (the agent has no local disk). Each item — `kind` selects the origin:
                - Drive: `{"kind":"drive","file_id":"<id>"}` (id from drive_list/metadata)
                - Gmail: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
                - URL:   `{"kind":"url","url":"https://…"}` — e.g. a signed URL from
                  `oto_upload_url` (upload a local PDF first) or drive_download.
        """
        if mode not in ("send", "draft"):
            raise _bad("mode doit être 'send' ou 'draft'.")
        client = await _client_for_user_async(account)
        try:
            # oto-backend#867 lot 2 — chaque pièce jointe (drive/gmail/url) est
            # résolue par un appel HTTP synchrone (file_source.resolve), en série :
            # hors boucle + borné, même méthode que le rafraîchissement de jeton
            # ci-dessus. 90s et non 20s : un envoi tolère l'attente de vraies
            # pièces jointes (jusqu'à 25 Mo chacune) — ce qui ne doit pas arriver,
            # c'est que ça gèle tout le processus pendant ce temps.
            att = await asyncio.wait_for(
                asyncio.to_thread(_resolve_attachments, attachments),
                timeout=_ATTACHMENTS_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise _bad(f"Récupération des pièces jointes trop longue "
                      f"(> {_ATTACHMENTS_TIMEOUT_S}s) — réessaie.")
        except file_source.FileSourceError as e:
            raise _bad(str(e))
        att_paths, _cleanup = att

        def _acte(res: object) -> dict:
            """Le retour NOMME l'acte. Sans ce champ, « envoyé » et « brouillon » ne se
            distinguent qu'au NOMBRE de clés rendues (3 vs 2) — une différence qu'il faut
            déjà connaître pour la lire. Un agent qui rapporte « brouillon créé » après un
            envoi réel n'a pas menti : il n'avait rien à lire qui le dise.
            Payé le 14/08 : trois mails partis chez une cliente."""
            out = dict(res) if isinstance(res, dict) else {"result": res}
            out["kind"] = "draft" if mode == "draft" else "sent"
            return out

        try:
            if reply_to:
                if mode == "draft":
                    return _acte(await asyncio.to_thread(
                        lambda: client.create_draft_reply(
                            message_id=reply_to, body=body, html=html, cc=cc, markdown=markdown,
                            attachments=att_paths,
                        )
                    ))
                return _acte(await asyncio.to_thread(
                    lambda: client.reply(
                        message_id=reply_to, body=body, html=html, cc=cc,
                        from_name=from_name, markdown=markdown, attachments=att_paths,
                    )
                ))
            if not to:
                raise _bad("`to` requis pour un nouveau message (ou fournis `reply_to` pour répondre).")
            if mode == "draft":
                return _acte(await asyncio.to_thread(
                    lambda: client.create_draft(
                        to=to, subject=subject or "", body=body, html=html, cc=cc, bcc=bcc,
                        markdown=markdown, attachments=att_paths,
                    )
                ))
            return _acte(await asyncio.to_thread(
                lambda: client.send(
                    to=to, subject=subject or "", body=body, html=html,
                    cc=cc, bcc=bcc, from_name=from_name, markdown=markdown, attachments=att_paths,
                )
            ))
        finally:
            _cleanup()
