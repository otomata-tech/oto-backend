"""Dispatch `op=` de `drive_file` (ADR 0047 §Amendement, appliqué au produit
`drive` du connecteur `google` : 8 tools → 2).

Ce module n'avait AUCUN test de surface, et c'est le seul consolidé jusqu'ici qui
ÉCRIT sur les données personnelles de l'utilisateur : une op mal câblée ne rend pas
un résultat faux, elle **supprime un fichier** ou **change qui peut le voir**, sans
que rien ne casse au boot. D'où, op par op : la méthode client atteinte, et — pour
chaque op destructrice ou d'écriture — la preuve qu'aucune méthode voisine
dangereuse n'est appelée (`assert_not_called`).

S'y ajoutent les invariants de sécurité de la consolidation :
- `op` par défaut = `"list"` (une LECTURE) : un appel sans `op` n'écrit jamais ;
- une op inconnue est REFUSÉE en nommant les ops valides (jamais un repli muet sur
  le défaut, l'agent croirait sa demande honorée) ;
- les verbes de partage ne sont PAS des ops de `drive_file` (ils vivent dans
  `drive_access`, resté seul) : `op="share"` doit lever, pas partager ;
- un argument obligatoire manquant lève en nommant l'op ET l'argument.

Et les endroits où le module ne se contente pas de passer le plat : le choix du
mime d'export (mot-clé `format`, défaut dérivé du type SOURCE, refus argumenté d'un
non-natif Google) et le rendu du contenu (`file_content.render_for_agent`, préfixes
de dépôt distincts download/export).
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError
EXPECTED_TOOLS = {"drive_file", "drive_access"}

# Les méthodes qu'on ne veut JAMAIS voir appelées par erreur : elles écrivent,
# suppriment ou changent le partage.
DANGEROUS = ("delete_file", "rename_file", "move_file", "create_folder",
             "upload_bytes", "share", "unshare")


def _register():
    from fastmcp import FastMCP
    from oto_mcp.tools import drive as D

    m = FastMCP("t")
    D.register(m)
    return m


def _fn(name: str):
    return asyncio.run(_register().get_tool(name)).fn


def _call(tool_name, /, **kwargs):
    """Les tools drive sont `async` : on résout la coroutine ici. Le nom du tool est
    positional-only — `name` est un argument métier de `drive_file`."""
    return asyncio.run(_fn(tool_name)(**kwargs))


def _assert_untouched(client, *methods):
    for m in methods:
        getattr(client, m).assert_not_called()


@pytest.fixture
def client(monkeypatch):
    """Faux `DriveClient` + `sub` résolu (download/export en ont besoin)."""
    from oto_mcp.tools import drive as D

    inst = MagicMock()
    monkeypatch.setattr(D, "_client_for_user", lambda account=None: inst)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr(D.file_content, "render_for_agent",
                        lambda data, filename, mime, *, sub, prefix, **_: {
                            "filename": filename, "mimeType": mime, "prefix": prefix,
                            "encoding": "text", "content": "…"})
    inst.get_file_bytes.return_value = {"data": b"hello", "filename": "notes.txt",
                                        "mimeType": "text/plain"}
    inst.export_file_bytes.return_value = {"data": b"# titre", "filename": "spec.md"}
    inst.list_files.return_value = [{"id": "1"}, {"id": "2"}]
    inst.list_permissions.return_value = [{"id": "p1"}]
    # Métadonnée par défaut = un Doc natif, pour que op="export" sans `format`
    # trouve son mime de sortie (le cas nominal ; les autres types sont testés).
    inst.get_file_metadata.return_value = {
        "mimeType": "application/vnd.google-apps.document", "name": "Doc"}
    return inst


# --- la surface elle-même ------------------------------------------------------

def test_surface_is_exactly_the_two_consolidated_tools():
    """Un tool oublié en route (ou resté en double) se voit ici, pas en prod."""
    assert {t.name for t in asyncio.run(_register()._list_tools())} == EXPECTED_TOOLS


def test_drive_access_stays_a_separate_tool_without_an_op():
    """Le partage n'est pas un verbe de `drive_file` : changer qui voit un fichier
    ne doit pas partager l'énumération d'`op` avec « supprimer ce fichier »."""
    import inspect

    params = inspect.signature(_fn("drive_access")).parameters
    assert "op" not in params
    assert {"file_id", "email", "role", "remove", "notify"} <= set(params)


# --- lectures ------------------------------------------------------------------

@pytest.mark.parametrize("op,kwargs,method", [
    ("list", {}, "list_files"),
    ("metadata", {"file_id": "f1"}, "get_file_metadata"),
    ("download", {"file_id": "f1"}, "get_file_bytes"),
    ("export", {"file_id": "f1", "format": "markdown"}, "export_file_bytes"),
])
def test_read_ops_route_to_the_right_client_method(client, op, kwargs, method):
    _call("drive_file", op=op, **kwargs)
    getattr(client, method).assert_called_once()
    _assert_untouched(client, *DANGEROUS)


def test_default_op_is_a_read(client):
    """`op` omis ⟹ LECTURE. C'est l'invariant qui rend l'outil sûr par défaut :
    un appel malformé ne peut pas supprimer ni partager."""
    out = _call("drive_file")
    assert out == {"files": [{"id": "1"}, {"id": "2"}], "count": 2}
    client.list_files.assert_called_once()
    _assert_untouched(client, *DANGEROUS)


def test_list_forwards_its_three_filters_positionally(client):
    _call("drive_file", op="list", folder_id="fold", query="name contains 'x'",
          page_size=7)
    assert client.list_files.call_args.args == ("fold", "name contains 'x'", 7)


def test_list_defaults_are_no_filter_and_page_size_100(client):
    _call("drive_file")
    assert client.list_files.call_args.args == (None, None, 100)


def test_metadata_reads_the_file_it_was_given(client):
    _call("drive_file", op="metadata", file_id="f42")
    assert client.get_file_metadata.call_args.args == ("f42",)


# --- lecture de contenu : download vs export ------------------------------------

def test_download_renders_through_the_shared_file_renderer(client):
    """Le rendu inline-vs-URL signée est mutualisé (`file_content`) ; le préfixe de
    dépôt distingue les fichiers téléchargés des exports."""
    out = _call("drive_file", op="download", file_id="f1")
    assert client.get_file_bytes.call_args.args == ("f1",)
    assert out["prefix"] == "drive-files"
    assert out["filename"] == "notes.txt"
    client.export_file_bytes.assert_not_called()


def test_export_renders_with_its_own_storage_prefix(client):
    out = _call("drive_file", op="export", file_id="f1", format="markdown")
    assert out["prefix"] == "drive-exports"
    client.get_file_bytes.assert_not_called()


@pytest.mark.parametrize("fmt,mime", [
    ("markdown", "text/markdown"), ("md", "text/markdown"),
    ("text", "text/plain"), ("txt", "text/plain"),
    ("html", "text/html"), ("pdf", "application/pdf"), ("csv", "text/csv"),
])
def test_export_maps_the_format_word_to_its_mime(client, fmt, mime):
    """L'agent passe un MOT, pas un mime à recopier."""
    _call("drive_file", op="export", file_id="f1", format=fmt)
    assert client.export_file_bytes.call_args.args == ("f1", mime)


@pytest.mark.parametrize("src,mime", [
    ("application/vnd.google-apps.document", "text/markdown"),
    ("application/vnd.google-apps.spreadsheet", "text/csv"),
    ("application/vnd.google-apps.presentation", "text/plain"),
])
def test_export_without_format_derives_the_default_from_the_source_type(
        client, src, mime):
    """Un tableur n'a pas de markdown, une présentation non plus : le défaut se
    dérive du type SOURCE, il ne se devine pas."""
    client.get_file_metadata.return_value = {"mimeType": src, "name": "doc"}
    _call("drive_file", op="export", file_id="f1")
    assert client.export_file_bytes.call_args.args == ("f1", mime)


def test_export_refuses_an_unknown_format_and_lists_the_accepted_ones(client):
    with pytest.raises(McpError, match="markdown"):
        _call("drive_file", op="export", file_id="f1", format="epub")
    client.export_file_bytes.assert_not_called()


def test_export_of_a_non_native_file_points_to_download(client):
    """Un binaire n'a pas d'export (l'API répond 403 « Only files with binary
    content can be downloaded » dans l'autre sens) : le refus doit nommer l'op qui
    marche, pas laisser l'agent bloqué."""
    client.get_file_metadata.return_value = {"mimeType": "application/pdf",
                                             "name": "plaquette.pdf"}
    with pytest.raises(McpError, match="download"):
        _call("drive_file", op="export", file_id="f1")
    client.export_file_bytes.assert_not_called()


def test_download_surfaces_the_upstream_error_as_actionable(client):
    """Un natif Google passé à op="download" échoue amont : l'erreur remonte en
    McpError (message du provider), jamais en stacktrace."""
    client.get_file_bytes.side_effect = RuntimeError(
        "Only files with binary content can be downloaded")
    with pytest.raises(McpError, match="binary content"):
        _call("drive_file", op="download", file_id="f1")


# --- écritures : chacune a son cas, et ses voisines restent muettes --------------

def test_create_folder_creates_and_touches_nothing_else(client):
    _call("drive_file", op="create_folder", name="Clients", parent_folder_id="p1")
    assert client.create_folder.call_args.args == ("Clients", "p1")
    _assert_untouched(client, "delete_file", "rename_file", "move_file",
                      "share", "unshare")


def test_create_folder_without_a_parent_stays_at_the_root(client):
    _call("drive_file", op="create_folder", name="Clients")
    assert client.create_folder.call_args.args == ("Clients", None)


def test_update_rename_only_renames(client):
    out = _call("drive_file", op="update", file_id="f1", new_name="Devis 2026")
    assert client.rename_file.call_args.args == ("f1", "Devis 2026")
    assert "renamed" in out and "moved" not in out
    _assert_untouched(client, "move_file", "delete_file", "create_folder",
                      "share", "unshare")


def test_update_move_only_moves(client):
    out = _call("drive_file", op="update", file_id="f1", move_to_folder="fold")
    assert client.move_file.call_args.args == ("f1", "fold")
    assert "moved" in out and "renamed" not in out
    _assert_untouched(client, "rename_file", "delete_file", "create_folder",
                      "share", "unshare")


def test_update_does_both_when_both_are_given(client):
    out = _call("drive_file", op="update", file_id="f1", new_name="n",
                move_to_folder="fold")
    client.rename_file.assert_called_once()
    client.move_file.assert_called_once()
    assert set(out) == {"renamed", "moved"}
    _assert_untouched(client, "delete_file", "share", "unshare")


def test_update_without_anything_to_change_is_refused(client):
    """Ni renommage ni déplacement = appel vide : refus explicite, pas un no-op
    silencieux que l'agent prendrait pour un succès."""
    with pytest.raises(McpError, match="new_name"):
        _call("drive_file", op="update", file_id="f1")
    _assert_untouched(client, *DANGEROUS)


def test_delete_deletes_exactly_the_file_it_was_given(client):
    """L'op destructrice : la bonne méthode, le bon id, et rien d'autre."""
    _call("drive_file", op="delete", file_id="f9")
    client.delete_file.assert_called_once()
    assert client.delete_file.call_args.args == ("f9",)
    _assert_untouched(client, "rename_file", "move_file", "create_folder",
                      "share", "unshare")


@pytest.mark.parametrize("op", ["list", "metadata", "download", "export",
                                "create_folder", "update", "upload"])
def test_no_read_or_write_op_ever_deletes(client, monkeypatch, op):
    """Le scénario redouté d'une consolidation : une op inoffensive qui atteint
    `delete`. On l'interdit op par op, pas par relecture."""
    kwargs = {"file_id": "f1"}
    if op == "create_folder":
        kwargs = {"name": "X"}
    elif op == "update":
        kwargs = {"file_id": "f1", "new_name": "n"}
    elif op == "list":
        kwargs = {}
    elif op == "upload":
        _resolved(monkeypatch)
        kwargs = {"source": {"kind": "url", "url": "https://x/f.xlsx"}}
    _call("drive_file", op=op, **kwargs)
    client.delete_file.assert_not_called()


# --- dépôt depuis une source ------------------------------------------------------

def _resolved(monkeypatch, filename="Suivi.xlsx", mime="application/octet-stream"):
    from oto_mcp import file_source
    seen = {}

    def resolve(source, **kw):
        seen["source"] = source
        return file_source.ResolvedFile(data=b"PK..", filename=filename, mime=mime)

    monkeypatch.setattr(file_source, "resolve", resolve)
    return seen


def test_upload_resolves_the_source_and_converts_under_the_given_name(client, monkeypatch):
    seen = _resolved(monkeypatch)
    src = {"kind": "api", "path": "/export.xlsx"}
    _call("drive_file", op="upload", source=src, name="Suivi octobre",
          parent_folder_id="fold", convert=True)
    assert seen["source"] == src
    client.upload_bytes.assert_called_once_with(
        b"PK..", "Suivi octobre", "application/octet-stream", "fold", True)
    _assert_untouched(client, "delete_file", "rename_file", "move_file", "share")


def test_upload_keeps_the_source_name_and_does_not_convert_by_default(client, monkeypatch):
    _resolved(monkeypatch, filename="note.pdf", mime="application/pdf")
    _call("drive_file", op="upload", source={"kind": "url", "url": "https://x/note.pdf"})
    client.upload_bytes.assert_called_once_with(b"PK..", "note.pdf", "application/pdf",
                                                None, False)


def test_upload_refusal_of_the_client_is_readable(client, monkeypatch):
    from oto.tools.google.drive.lib.drive_client import DriveClientError
    _resolved(monkeypatch, filename="note.pdf", mime="application/pdf")
    client.upload_bytes.side_effect = DriveClientError("no Google-native counterpart")
    with pytest.raises(McpError, match="no Google-native counterpart"):
        _call("drive_file", op="upload", source={"kind": "url", "url": "https://x/n.pdf"},
              convert=True)


def test_source_and_convert_are_refused_outside_upload(client):
    with pytest.raises(McpError, match="only apply to op='upload'"):
        _call("drive_file", op="list", convert=True)
    _assert_untouched(client, *DANGEROUS)


# --- refus ---------------------------------------------------------------------

def test_unknown_op_is_refused_with_the_allowed_list(client):
    with pytest.raises(McpError, match="op must be") as e:
        _call("drive_file", op="nope")
    msg = e.value.error.message
    for op in ("list", "metadata", "download", "export", "upload", "create_folder",
               "update", "delete"):
        assert f"'{op}'" in msg
    _assert_untouched(client, *DANGEROUS)


@pytest.mark.parametrize("op", ["share", "unshare", "access", "permissions"])
def test_sharing_verbs_are_not_ops_of_drive_file(client, op):
    """Le partage vit dans `drive_access`. Un agent qui tente `op="share"` doit se
    faire refuser — surtout pas voir sa demande retomber sur le défaut."""
    with pytest.raises(McpError, match="op must be"):
        _call("drive_file", op=op, file_id="f1")
    _assert_untouched(client, "share", "unshare", "delete_file")


def test_drive_file_carries_no_sharing_vocabulary():
    """Corollaire de la non-fusion : aucun paramètre de permission n'entre dans
    `drive_file` (ils n'y recouvrent rien, et les y mettre rendrait `op="share"`
    plausible aux yeux du modèle)."""
    import inspect

    params = set(inspect.signature(_fn("drive_file")).parameters)
    assert not ({"email", "role", "remove", "notify"} & params)


@pytest.mark.parametrize("op,kwargs,missing", [
    ("metadata", {}, "file_id"),
    ("download", {}, "file_id"),
    ("export", {"format": "markdown"}, "file_id"),
    ("delete", {}, "file_id"),
    ("update", {"new_name": "n"}, "file_id"),
    ("create_folder", {}, "name"),
    ("upload", {"convert": True}, "source"),
])
def test_missing_required_arg_names_the_op_and_the_arg(client, op, kwargs, missing):
    with pytest.raises(McpError, match=missing) as e:
        _call("drive_file", op=op, **kwargs)
    assert f"op='{op}'" in e.value.error.message
    _assert_untouched(client, *DANGEROUS)


# --- partage (tool resté seul) --------------------------------------------------

def test_access_without_an_email_only_lists(client):
    """Le défaut de `drive_access` est une LECTURE : sans destinataire, on inspecte
    les permissions, on n'en accorde aucune."""
    out = _call("drive_access", file_id="f1")
    assert out == {"permissions": [{"id": "p1"}], "count": 1}
    client.list_permissions.assert_called_once()
    _assert_untouched(client, "share", "unshare")


def test_access_grants_with_the_role_and_the_notification_flag(client):
    _call("drive_access", file_id="f1", email="a@b.c", role="writer", notify=False)
    assert client.share.call_args.args == ("f1", "a@b.c", "writer", False)
    client.unshare.assert_not_called()


def test_access_revokes_and_never_grants(client):
    _call("drive_access", file_id="f1", email="a@b.c", remove=True)
    assert client.unshare.call_args.args == ("f1", "a@b.c")
    client.share.assert_not_called()


# --- export xlsx : un FICHIER à transmettre, pas un contenu à lire -----------------

def test_export_xlsx_sert_une_url_signee_et_ne_rend_pas_le_contenu(client, monkeypatch):
    """Signal oto #1376 : un classeur à envoyer. Le rendu générique relirait le .xlsx en
    CSV ; ici l'agent reçoit une URL vers le fichier, avec l'extension attendue."""
    from oto_mcp.tools import drive as D

    depose = {}
    monkeypatch.setattr(D.file_content, "render_for_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("rendu inline")))
    import oto_mcp.media_store as ms
    monkeypatch.setattr(ms, "upload_private",
                        lambda prefix, sub, data, mime, name: depose.update(
                            prefix=prefix, sub=sub, mime=mime, name=name, n=len(data)) or "https://s/x")
    monkeypatch.setattr(ms, "presign_expiry", lambda: 3600)
    client.export_file_bytes.return_value = {"data": b"PK\x03\x04", "filename": "BP v6"}
    out = _call("drive_file", op="export", file_id="f1", format="xlsx")
    assert client.export_file_bytes.call_args.args[1] == D._XLSX
    assert out == {"filename": "BP v6.xlsx", "mimeType": D._XLSX, "size": 4,
                   "encoding": "url", "url": "https://s/x", "expires_in": 3600}
    assert depose == {"prefix": "drive-exports", "sub": "sub-1", "mime": D._XLSX,
                      "name": "BP v6.xlsx", "n": 4}


@pytest.mark.parametrize("fmt,ext", [("pptx", ".pptx"), ("docx", ".docx")])
def test_export_pptx_docx_sert_une_url_signee_comme_xlsx(client, monkeypatch, fmt, ext):
    """Un deck Slides ou un Doc en fichier Office : le modèle d'un client dont l'agent
    part pour bâtir un nouveau deck. Même chemin que l'xlsx — une URL, jamais un rendu."""
    from oto_mcp.tools import drive as D

    depose = {}
    monkeypatch.setattr(D.file_content, "render_for_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("rendu inline")))
    import oto_mcp.media_store as ms
    monkeypatch.setattr(ms, "upload_private",
                        lambda prefix, sub, data, mime, name: depose.update(
                            mime=mime, name=name) or "https://s/x")
    monkeypatch.setattr(ms, "presign_expiry", lambda: 3600)
    client.export_file_bytes.return_value = {"data": b"PK\x03\x04", "filename": "Modèle"}
    out = _call("drive_file", op="export", file_id="f1", format=fmt)
    mime = D._EXPORT_MIME[fmt]
    assert client.export_file_bytes.call_args.args == ("f1", mime)
    assert out["encoding"] == "url" and out["filename"] == f"Modèle{ext}"
    assert depose == {"mime": mime, "name": f"Modèle{ext}"}
