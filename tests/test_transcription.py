"""Connecteur `transcription` (ADR 0074, #674) — verrouille : l'entrée registre
(clé secrète + langue et vocabulaire NON secrets, `byo_org` + plateforme, mono-compte), la
surface MCP (deux outils), la jointure outil↔client oto-core, et le geste de bout en
bout avec un transport Mistral SIMULÉ : un fichier du projet lu côté serveur, envoyé
avec la langue et le vocabulaire de l'instance, post-traité, rangé en page du projet
par le seam d'écriture des pages — et l'appel qui rend la page, pas le texte.

**ASYNCHRONE (#674)** : `transcription_create` dépose un travail et rend sa référence
tout de suite, SANS appeler Mistral — c'est `transcription_worker._transcribe_batch`
(la boucle de fond, ici invoquée directement, un tour) qui le fait, et
`transcription_status` qui relit le résultat.

Refus éprouvés AVANT la création du travail (donc avant tout appel payant) : sans
`_project`, sans droit d'écriture, un fichier rangé sur un AUTRE projet. Refus après
(côté worker, lus via `transcription_status`) : une clé rejetée par Mistral, aucune
parole reconnue.
"""
import asyncio
import io
import json

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of

AUDIO = b"ID3" + b"\x00" * 2048
FICHIER = {"id": 7, "project_id": 42, "s3_key": "project-files/42/abc/visite.m4a",
           "filename": "visite.m4a", "mime": "audio/mp4"}
# La forme mesurée sur l'API réelle : chaque segment porte `start`, `end` et
# `speaker_id` ; `language` revient null, même quand la langue est imposée.
REPONSE_MISTRAL = {
    "model": "voxtral-mini-2602", "language": None,
    "text": "Bonjour. On regarde la toiture. On regarde la toiture. Hum. D'accord.",
    "segments": [
        {"text": "Bonjour.", "start": 0.0, "end": 1.5, "speaker_id": "speaker_0"},
        {"text": "On regarde la toiture et la charpente du garage.", "start": 1.5,
         "end": 6.0, "speaker_id": "speaker_0"},
        {"text": "On regarde la toiture et la charpente du garage.", "start": 6.0,
         "end": 9.0, "speaker_id": "speaker_0"},
        # 0,4 s d'un locuteur inventé, 0,2 s après le précédent, 0,4 s avant le suivant
        {"text": "Hum.", "start": 9.2, "end": 9.6, "speaker_id": "speaker_2"},
        {"text": "D'accord, et les combles sont isolés depuis quand ?", "start": 10.0,
         "end": 14.0, "speaker_id": "speaker_1"},
    ],
    "usage": {"prompt_audio_seconds": 1800},
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


# --- registre et surface --------------------------------------------------------------

def test_entree_registre_cle_secrete_langue_et_vocabulaire_non_secrets():
    c = providers.REGISTRY["transcription"]
    assert c.kind == "tools" and c.secret_kind == "fields"
    assert c.auth_modes == frozenset({"byo_org", "platform"})
    assert c.default_active is False
    champs = {f.name: f for f in c.secret_fields}
    assert list(champs) == ["api_key", "language", "vocabulary"]
    assert champs["api_key"].secret is True
    for nom in ("language", "vocabulary"):
        assert champs[nom].secret is False and champs[nom].required is False
    # « pompe à chaleur » : le nettoyage par défaut retirerait tous les blancs.
    assert champs["vocabulary"].whitespace_significant is True


def test_mono_compte():
    c = providers.REGISTRY["transcription"]
    assert c.cardinality == "mono" and c.auth_multi_account is False


def test_deux_outils_sous_son_namespace(all_tools):
    assert {"transcription_create", "transcription_status"} <= set(all_tools)
    assert namespace_of("transcription_create") == "transcription"
    assert namespace_of("transcription_status") == "transcription"
    assert sorted(t for t in all_tools if t.startswith("transcription_")) == [
        "transcription_create", "transcription_status"]
    assert all_tools["transcription_create"].description
    assert all_tools["transcription_status"].description


def test_client_expose_les_methodes_appelees():
    from oto.tools.mistral import MistralClient
    for meth in ("transcribe", "list_models"):
        assert callable(getattr(MistralClient, meth, None)), f"MistralClient.{meth}"


# --- le monde simulé ----------------------------------------------------------------------

class _S3:
    def __init__(self, objets):
        self.objets = objets
        self.lus: list[str] = []
        self.supprimes: list[str] = []

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.objets[Key])}

    def get_object(self, *, Bucket, Key):
        self.lus.append(Key)
        return {"Body": io.BytesIO(self.objets[Key])}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        self.objets[Key] = Body if isinstance(Body, bytes) else Body.read()

    def delete_object(self, *, Bucket, Key):
        self.supprimes.append(Key)
        self.objets.pop(Key, None)


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def monde(monkeypatch):
    """Projet 42 visible, lisible et inscriptible ; il porte le fichier 7. Mistral
    répond `REPONSE_MISTRAL`. Chaque test ne débranche que ce qu'il éprouve.

    La file de travaux (`db.create_transcription_job`/`claim_next_transcription_job`/
    `get_transcription_job`/`mark_transcription_job_*`) est simulée en mémoire — même
    patron que `db.create_doc`/`get_doc_by_id` ci-dessous. Ces noms sont PRÉFIXÉS
    (pas `create_job`/`get_job`/`claim_next_job` nus) : `runner_jobs.py` porte déjà
    `get_job`/`claim_next_job` sous une forme différente dans le même namespace plat
    `db.*` — un nom nu écraserait l'un des deux à l'import (#674)."""
    from oto.tools.mistral import client as mistral_client
    from oto_mcp import access, db, file_source, media_store, ownership, session_org

    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)  # chiffrement réel du job

    etat = {"envois": [], "pages": [], "activite": [], "droits": [], "jobs": {},
            "creds": {"api_key": "k-test", "language": "", "vocabulary":
                      "pompe à chaleur, Placo\nBA13"},
            "reponse": _Resp(REPONSE_MISTRAL), "projet": 42, "journal": [],
            "traces": []}
    s3 = _S3({FICHIER["s3_key"]: AUDIO})
    etat["s3"] = s3

    monkeypatch.setattr(access, "current_project", lambda: etat["projet"])
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(file_source.access, "current_user_sub_or_raise", lambda: "u1")
    monkeypatch.setattr(file_source.access, "current_org", lambda sub: 3)
    class _Cred:
        is_platform = False
        mode = "org"
        secret = "unused"

        @property
        def fields(self):
            return dict(etat["creds"])

    class _CredPlateforme:
        """Le secret plateforme est le pack JSON, comme une clé d'org : `.secret`
        n'est pas la clé."""
        is_platform = True
        mode = "platform"
        secret = json.dumps({"api_key": "k-plateforme"})

        @property
        def fields(self):
            return json.loads(self.secret)

    def _resolve(provider, want="auto", **kw):
        etat["want"] = want
        return _CredPlateforme() if etat.get("plateforme") else _Cred()

    monkeypatch.setattr(access, "resolve_credential", _resolve)

    def _acces(sub, rtype, rid, want="read"):
        etat["droits"].append((rtype, rid, want))
        return True

    monkeypatch.setattr(ownership, "visible_in_org", lambda *a: True)
    monkeypatch.setattr(ownership, "can_access", _acces)
    monkeypatch.setattr(db, "get_project_file",
                        lambda fid: dict(FICHIER) if fid == FICHIER["id"] else None)
    monkeypatch.setattr(media_store, "_get_client", lambda: s3)
    monkeypatch.setattr(media_store, "_bucket", lambda: "media-test")

    def _create_doc(pid, title, **kw):
        etat["pages"].append({"id": 900 + len(etat["pages"]), "project_id": pid,
                              "parent_id": kw.get("parent_id"), "title": title,
                              "body_md": kw.get("body_md"), "kind": kw.get("kind"),
                              "description": None, "position": 16,
                              "public": False, "created_at": "t",
                              "updated_at": "t"})
        return etat["pages"][-1]["id"]

    monkeypatch.setattr(db, "create_doc", _create_doc)
    monkeypatch.setattr(db, "get_doc_by_id",
                        lambda did: next(p for p in etat["pages"] if p["id"] == did))
    monkeypatch.setattr(db, "log_project_activity",
                        lambda *a: etat["activite"].append(a))

    # --- la file de travaux, en mémoire -------------------------------------------
    def _create_job(*, project_id, sub, audio_key, filename, mime, language,
                    vocabulary, api_key_enc, key_mode):
        jid = 1 + len(etat["jobs"])
        etat["jobs"][jid] = {"id": jid, "project_id": project_id, "sub": sub,
                             "status": "pending", "audio_key": audio_key,
                             "filename": filename, "mime": mime, "language": language,
                             "vocabulary": vocabulary, "api_key_enc": api_key_enc,
                             "key_mode": key_mode, "org_id": 3,
                             "page_id": None, "result": None, "transcript": None,
                             "error": None}
        return jid

    def _claim_next_job():
        for j in etat["jobs"].values():
            if j["status"] == "pending":
                j["status"] = "running"
                return dict(j)
        return None

    def _get_job(jid):
        j = etat["jobs"].get(jid)
        return dict(j) if j else None

    def _mark_done(jid, *, page_id, result, transcript):
        etat["jobs"][jid].update(status="done", page_id=page_id, result=result,
                                 transcript=transcript)

    def _mark_failed(jid, *, error):
        etat["jobs"][jid].update(status="failed", error=error)

    monkeypatch.setattr(db, "create_transcription_job", _create_job)
    monkeypatch.setattr(db, "claim_next_transcription_job", _claim_next_job)
    monkeypatch.setattr(db, "get_transcription_job", _get_job)
    monkeypatch.setattr(db, "mark_transcription_job_done", _mark_done)
    monkeypatch.setattr(db, "mark_transcription_job_failed", _mark_failed)
    monkeypatch.setattr(db, "insert_tool_call", lambda row: etat["journal"].append(row))
    monkeypatch.setattr(session_org, "note_call_trace",
                        lambda **kw: etat["traces"].append(kw))
    monkeypatch.setattr(db, "transcription_queue",
                        lambda jid: etat.get("file", {"ahead": 0, "finishes": []}))

    def _post(url, headers=None, data=None, files=None, timeout=None, json=None):
        etat["envois"].append({"url": url, "headers": headers, "data": data,
                               "files": files, "timeout": timeout})
        return etat["reponse"]

    monkeypatch.setattr(mistral_client.requests, "post", _post)
    return etat


def _appeler(all_tools, **args):
    return all_tools["transcription_create"].fn(**args)


def _statut(all_tools, job_id):
    return all_tools["transcription_status"].fn(job_id=job_id)


def _tourner(monde):
    """Un tour du worker (hors event loop dans le serveur réel, direct ici)."""
    from oto_mcp import transcription_worker
    return transcription_worker._transcribe_batch()


def _pf(pid=42, fid=7):
    return {"kind": "project_file", "project_id": pid, "file_id": fid}


def _champs(envoi) -> dict:
    out: dict = {}
    for k, v in envoi["data"]:
        out.setdefault(k, []).append(v)
    return out


# --- création : rend une référence, n'appelle jamais Mistral --------------------------

def test_create_rend_une_reference_sans_appeler_mistral(all_tools, monde):
    out = _appeler(all_tools, source=_pf())
    assert out == {"job_id": 1, "status": "pending",
                   "note": "Re-read with transcription_status(job_id)."}
    assert monde["envois"] == [] and monde["pages"] == []
    assert monde["jobs"][1]["status"] == "pending"
    assert ("project", "42", "write") in monde["droits"]
    # L'audio est déjà lu et déposé (temporairement) au moment du dépôt du travail —
    # c'est ce qui permet au worker de ne plus avoir besoin du contexte de l'appel.
    assert monde["s3"].lus == [FICHIER["s3_key"]]


def test_sans_projet_rien_n_est_lu_ni_depose(all_tools, monde):
    monde["projet"] = None
    with pytest.raises(McpError, match="_project"):
        _appeler(all_tools, source=_pf())
    assert monde["jobs"] == {} and monde["s3"].lus == []


def test_sans_droit_d_ecriture_rien_n_est_depose(all_tools, monde, monkeypatch):
    from oto_mcp import ownership
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, rtype, rid, want="read": want != "write")
    with pytest.raises(McpError, match="Écriture refusée"):
        _appeler(all_tools, source=_pf())
    assert monde["jobs"] == {} and monde["s3"].lus == []


def test_un_fichier_d_un_autre_projet_est_refuse_comme_un_inconnu(all_tools, monde):
    """Le fichier 7 est rangé sur le projet 42 ; le désigner sous le projet 41 (où
    l'appelant a pourtant tous les droits) répond comme un fichier inconnu, et rien
    n'est lu dans le stockage ni déposé comme travail."""
    with pytest.raises(McpError) as autre:
        _appeler(all_tools, source=_pf(pid=41, fid=7))
    with pytest.raises(McpError) as inconnu:
        _appeler(all_tools, source=_pf(pid=41, fid=999))
    assert "introuvable" in autre.value.error.message
    assert (autre.value.error.message.replace("#7", "#999")
            == inconnu.value.error.message)
    assert monde["s3"].lus == [] and monde["jobs"] == {}


# --- le tour du worker : de bout en bout ----------------------------------------------

def test_un_fichier_du_projet_devient_une_page_du_projet(all_tools, monde):
    ref = _appeler(all_tools, source=_pf())
    assert _tourner(monde) == 1

    (envoi,) = monde["envois"]
    assert envoi["url"].endswith("/v1/audio/transcriptions")
    assert envoi["files"]["file"] == ("visite.m4a", AUDIO, "audio/mp4")
    assert envoi["timeout"][1] >= 60                 # un délai propre, pas l'infini
    champs = _champs(envoi)
    # Les trois ENSEMBLE, la combinaison du banc — la diarisation sans horodatage
    # par segment est refusée par l'amont (422).
    assert champs["language"] == ["fr"]              # langue vide = fr
    assert champs["timestamp_granularities"] == ["segment"]
    assert champs["diarize"] == ["true"]
    assert champs["context_bias"] == ["pompe,chaleur,Placo,BA13"]

    (page,) = monde["pages"]
    assert page["project_id"] == 42 and page["kind"] == "source"
    assert page["title"].startswith("Transcription — visite.m4a — ")
    paragraphes = page["body_md"].strip().split("\n\n")
    assert paragraphes[1:] == [
        "**Locuteur 1** [00:00] — Bonjour. On regarde la toiture et la charpente du "
        "garage. Hum.",
        "**Locuteur 2** [00:10] — D'accord, et les combles sont isolés depuis quand ?",
    ]

    out = _statut(all_tools, ref["job_id"])
    assert out["status"] == "done"
    assert out["page"]["id"] == page["id"] and out["page"]["title"] == page["title"]
    assert out["speakers"] == ["Locuteur 1", "Locuteur 2"]
    assert out["duration_s"] == 1800.0
    assert out["words"] == 20
    assert out["language"] == "fr"                   # la demandée : l'amont rend null
    assert out["vocabulary_terms"] == 4 and out["vocabulary_dropped"] == ["à"]
    # Le statut rend la page, PAS le texte.
    assert "D'accord" not in json.dumps(out)
    # L'objet audio temporaire est purgé après le tour.
    assert monde["s3"].supprimes == [monde["jobs"][1]["audio_key"]]


def test_langue_auto_n_envoie_aucune_langue_et_garde_l_horodatage(all_tools, monde):
    monde["creds"]["language"] = "auto"
    ref = _appeler(all_tools, source=_pf())
    _tourner(monde)
    champs = _champs(monde["envois"][0])
    assert "language" not in champs
    assert champs["timestamp_granularities"] == ["segment"]
    assert champs["diarize"] == ["true"]
    assert _statut(all_tools, ref["job_id"])["language"] == "auto"


def test_une_langue_posee_est_envoyee_telle_quelle(all_tools, monde):
    monde["creds"]["language"] = "en"
    ref = _appeler(all_tools, source=_pf())
    _tourner(monde)
    assert _champs(monde["envois"][0])["language"] == ["en"]


def test_une_cle_refusee_par_mistral_n_ecrit_rien(all_tools, monde):
    monde["reponse"] = _Resp({"message": "Unauthorized"}, status_code=401)
    ref = _appeler(all_tools, source=_pf())
    _tourner(monde)
    assert monde["pages"] == []
    out = _statut(all_tools, ref["job_id"])
    assert out["status"] == "failed" and "401" in out["error"]


def test_aucune_parole_aucune_page(all_tools, monde):
    monde["reponse"] = _Resp({"text": "", "segments": [], "usage": {}})
    ref = _appeler(all_tools, source=_pf())
    _tourner(monde)
    assert monde["pages"] == []
    out = _statut(all_tools, ref["job_id"])
    assert out["status"] == "failed" and "Aucune parole" in out["error"]
    # Sans durée facturée lue, rien n'est journalisé : jamais une durée inventée.
    assert monde["journal"] == []


def test_la_duree_facturee_est_journalisee_le_depot_ne_coute_rien(all_tools, monde):
    """Signal oto 1400 : la durée facturée par Mistral rejoint le journal des appels,
    une ligne par travail, sous le mode de la clé résolue au dépôt ; l'appel qui
    dépose le travail est tracé à 0 (NULL se lirait 1)."""
    monde["plateforme"] = True
    ref = _appeler(all_tools, source=_pf())
    assert {"quantity": 0} in monde["traces"]
    assert monde["journal"] == []
    _tourner(monde)
    (ligne,) = monde["journal"]
    assert ligne["tool"] == "transcription_create" and ligne["kind"] == "mcp"
    assert ligne["quantity"] == 1800                 # secondes, `usage.prompt_audio_seconds`
    assert ligne["key_mode"] == "platform" and ligne["org_id"] == 3
    assert ligne["sub"] == "u1" and ligne["ok"] is True
    assert ligne["args"]["transcription_job_id"] == ref["job_id"]


def test_une_duree_fractionnaire_s_arrondit_au_dessus(all_tools, monde):
    monde["reponse"] = _Resp({**REPONSE_MISTRAL, "usage": {"prompt_audio_seconds": 61.2}})
    _appeler(all_tools, source=_pf())
    _tourner(monde)
    (ligne,) = monde["journal"]
    assert ligne["quantity"] == 62 and ligne["key_mode"] == "org"


def test_un_refus_de_mistral_ne_journalise_aucune_duree(all_tools, monde):
    monde["reponse"] = _Resp({"message": "Unauthorized"}, status_code=401)
    _appeler(all_tools, source=_pf())
    _tourner(monde)
    assert monde["journal"] == []


def test_le_statut_dun_travail_dun_autre_projet_est_introuvable(all_tools, monde, monkeypatch):
    """Le job appartient au projet 42 ; un lecteur sans accès à ce projet reçoit le
    même refus qu'un job inexistant — pas d'indice sur son existence."""
    from oto_mcp import ownership
    ref = _appeler(all_tools, source=_pf())
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, rtype, rid, want="read": False)
    with pytest.raises(McpError, match="introuvable"):
        _statut(all_tools, ref["job_id"])


def test_statut_dun_travail_inconnu(all_tools, monde):
    with pytest.raises(McpError, match="introuvable"):
        _statut(all_tools, 999)


# --- vocabulaire à l'appel, palier plateforme ------------------------------------------

def test_le_credential_se_resout_en_cascade_plateforme_comprise(all_tools, monde):
    _appeler(all_tools, source=_pf())
    assert monde["want"] == "auto"


def test_le_vocabulaire_de_l_appel_complete_celui_de_l_instance(all_tools, monde):
    _appeler(all_tools, source=_pf(), vocabulary="zinguerie, chéneau")
    _tourner(monde)
    assert _champs(monde["envois"][0])["context_bias"] == [
        "pompe,chaleur,Placo,BA13,zinguerie,chéneau"]


def test_le_vocabulaire_de_l_appel_peut_remplacer(all_tools, monde):
    _appeler(all_tools, source=_pf(), vocabulary="zinguerie", vocabulary_replace=True)
    _tourner(monde)
    assert _champs(monde["envois"][0])["context_bias"] == ["zinguerie"]


def test_une_cle_plateforme_se_deballe_comme_une_cle_d_org_langue_par_defaut(all_tools, monde):
    monde["plateforme"] = True
    _appeler(all_tools, source=_pf(), vocabulary="zinguerie")
    _tourner(monde)
    (envoi,) = monde["envois"]
    assert envoi["headers"]["Authorization"] == "Bearer k-plateforme"
    champs = _champs(envoi)
    assert champs["language"] == ["fr"] and champs["context_bias"] == ["zinguerie"]


def test_un_audio_plus_gros_qu_une_image_est_relu_et_un_echec_de_lecture_purge(
        all_tools, monde, monkeypatch):
    """Le worker relit sous le plafond des fichiers de projet, pas celui d'une image
    (2 Mo) : un enregistrement de 10 Mo échouait à la lecture (première exécution réelle)."""
    from oto_mcp import media_store
    gros = b"ID3" + b"\x00" * (3 * 1024 * 1024)
    monde["s3"].objets[FICHIER["s3_key"]] = gros
    ref = _appeler(all_tools, source=_pf())
    _tourner(monde)
    assert _statut(all_tools, ref["job_id"])["status"] == "done"

    def _boum(key, **kw):
        raise media_store.MediaError(413, "file_too_large", "trop gros")

    ref2 = _appeler(all_tools, source=_pf())
    monkeypatch.setattr(media_store, "fetch_object", _boum)
    _tourner(monde)
    out = _statut(all_tools, ref2["job_id"])
    assert out["status"] == "failed" and "file_too_large" in out["error"]
    assert monde["jobs"][ref2["job_id"]]["audio_key"] in monde["s3"].supprimes


def test_le_meme_audio_soumis_deux_fois_a_deux_cles_et_deux_pages(all_tools, monde):
    """oto-backend#1301 : la clé de l'audio était celle du CONTENU — le même fichier
    soumis deux fois dans un projet partageait un objet, et la purge du premier travail
    supprimait l'audio du second, qui échouait à la relecture. Chaque travail a la
    sienne : les deux aboutissent, et chacun purge la sienne seulement."""
    ref1 = _appeler(all_tools, source=_pf())
    ref2 = _appeler(all_tools, source=_pf())
    cle1 = monde["jobs"][ref1["job_id"]]["audio_key"]
    cle2 = monde["jobs"][ref2["job_id"]]["audio_key"]
    assert cle1 != cle2
    assert cle1.startswith("transcription-jobs/42/") and cle1.endswith("/visite.m4a")
    _tourner(monde)
    assert monde["s3"].supprimes == [cle1] and cle2 in monde["s3"].objets
    _tourner(monde)
    assert _statut(all_tools, ref1["job_id"])["status"] == "done"
    assert _statut(all_tools, ref2["job_id"])["status"] == "done"
    assert monde["s3"].supprimes == [cle1, cle2]


# --- la face REST : la même file, et les tours en JSON ----------------------------------

def _ctx():
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub="u1", org_id=3)


@pytest.fixture
def rest(monde, monkeypatch):
    """Le monde, plus la ligne du projet que la ressource REST relit (existence et
    contexte d'org, comme toute route projet par id)."""
    from oto_mcp import db
    from oto_mcp.capabilities import transcription
    monkeypatch.setattr(db, "get_project_by_id", lambda pid: {"id": pid, "name": "Visite"})
    return transcription


def test_rest_deux_routes_une_ressource_sans_doublon_mcp():
    from oto_mcp.capabilities.registry import CAPABILITIES
    caps = {c.key: c for c in CAPABILITIES if c.key.startswith("me.transcription.")}
    [create] = caps["me.transcription.create"].rest_bindings()
    [read] = caps["me.transcription.read"].rest_bindings()
    assert (create.verb, create.path, create.status) == (
        "POST", "/api/me/projects/{project_id:int}/transcriptions", 202)
    assert (read.verb, read.path) == ("GET", "/api/me/transcriptions/{job_id:int}")
    # Les agents ont déjà transcription_create/status (projet ambiant `_project`).
    assert all(c.mcp is None for c in caps.values())


def test_rest_une_reference_project_file_prend_le_projet_de_l_url(rest, monde):
    out = rest._create(_ctx(), rest.TranscriptionCreateInput(
        project_id=42, source={"kind": "project_file", "file_id": 7}))
    assert out == {"job_id": 1, "status": "pending"}
    assert monde["jobs"][1]["project_id"] == 42
    assert monde["s3"].lus == [FICHIER["s3_key"]] and monde["envois"] == []


def test_rest_sans_droit_d_ecriture_rien_n_est_lu(rest, monde, monkeypatch):
    from oto_mcp import ownership
    from oto_mcp.capabilities._types import AuthzDenied
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, rtype, rid, want="read": want != "write")
    with pytest.raises(AuthzDenied) as refus:
        rest._create(_ctx(), rest.TranscriptionCreateInput(
            project_id=42, source={"kind": "project_file", "file_id": 7}))
    assert (refus.value.status, refus.value.code) == (403, "forbidden")
    assert monde["jobs"] == {} and monde["s3"].lus == []


def test_rest_rend_les_tours_en_json_le_mcp_rend_la_page(all_tools, rest, monde):
    """Les tours verbatim, horodatés : ce que la page met en paragraphes, la face REST
    le rend en données. La face MCP, elle, ne met jamais le texte dans le contexte de
    l'agent."""
    ref = rest._create(_ctx(), rest.TranscriptionCreateInput(
        project_id=42, source={"kind": "project_file", "file_id": 7}))
    _tourner(monde)
    out = rest._read(_ctx(), rest.TranscriptionReadInput(job_id=ref["job_id"]))
    assert out["status"] == "done" and out["project_id"] == 42
    assert out["filename"] == "visite.m4a" and out["page"]["id"] == monde["pages"][0]["id"]
    assert out["turns"] == 2
    assert out["transcript"] == [
        {"speaker": "Locuteur 1", "start": 0.0, "end": 9.6,
         "text": "Bonjour. On regarde la toiture et la charpente du garage. Hum."},
        {"speaker": "Locuteur 2", "start": 10.0, "end": 14.0,
         "text": "D'accord, et les combles sont isolés depuis quand ?"},
    ]
    mcp = _statut(all_tools, ref["job_id"])
    assert "transcript" not in mcp and "D'accord" not in json.dumps(mcp)


def test_rest_un_travail_d_un_projet_illisible_est_introuvable(rest, monde, monkeypatch):
    from oto_mcp import ownership
    from oto_mcp.capabilities._types import AuthzDenied
    ref = rest._create(_ctx(), rest.TranscriptionCreateInput(
        project_id=42, source={"kind": "project_file", "file_id": 7}))
    monkeypatch.setattr(ownership, "can_access", lambda *a, **k: False)
    for jid in (ref["job_id"], 999):
        with pytest.raises(AuthzDenied) as refus:
            rest._read(_ctx(), rest.TranscriptionReadInput(job_id=jid))
        assert (refus.value.status, refus.value.code) == (404, "unknown_transcription")


def test_un_credential_qui_ne_resout_pas(all_tools, rest, monde, monkeypatch):
    """REST : un refus nommé. MCP : l'erreur du résolveur telle quelle, déjà actionnable
    (connecteur à activer, quota) — pas une traduction qui en perdrait le sens."""
    from mcp.types import INVALID_PARAMS, ErrorData

    from oto_mcp import access
    from oto_mcp.capabilities._types import AuthzDenied

    def _absent(provider, want="auto", **kw):
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message="Connecteur transcription non activé."))

    monkeypatch.setattr(access, "resolve_credential", _absent)
    with pytest.raises(AuthzDenied) as refus:
        rest._create(_ctx(), rest.TranscriptionCreateInput(
            project_id=42, source={"kind": "project_file", "file_id": 7}))
    assert (refus.value.status, refus.value.code) == (400, "credential_unavailable")
    assert "non activé" in refus.value.message
    with pytest.raises(McpError, match="non activé"):
        _appeler(all_tools, source=_pf())
    assert monde["jobs"] == {}


# --- la file : place, estimation, voies (signal #1370) ------------------------------

def test_un_travail_en_file_dit_sa_place_et_quand_relire(all_tools, monde):
    t0 = 1_791_460_800.0                     # secondes Unix, comme les rend la base
    monde["file"] = {"ahead": 4, "finishes": [t0, t0 - 30, t0 - 60]}
    ref = _appeler(all_tools, source=_pf())
    out = _statut(all_tools, ref["job_id"])
    assert out["status"] == "pending" and out["queue_position"] == 4
    # 5 travaux à passer (4 devant + lui) au rythme mesuré d'un toutes les 30 s.
    assert out["estimated_wait_s"] == 150 and out["retry_after_s"] == 30
    assert "estimate" in out["note"] and "do not resubmit" in out["note"]


def test_sans_debit_recent_l_attente_est_inconnue_jamais_inventee(all_tools, monde):
    ref = _appeler(all_tools, source=_pf())
    out = _statut(all_tools, ref["job_id"])
    assert out["queue_position"] == 0 and out["estimated_wait_s"] is None
    assert "unknown" in out["note"]


def test_un_travail_en_cours_dit_quand_relire(all_tools, monde):
    ref = _appeler(all_tools, source=_pf())
    monde["jobs"][ref["job_id"]]["status"] = "running"
    out = _statut(all_tools, ref["job_id"])
    assert out["retry_after_s"] == 30 and "do not resubmit" in out["note"]
    assert "queue_position" not in out


def test_la_file_est_servie_par_plusieurs_voies(monkeypatch):
    """À une seule voie, 20 fichiers déposés passaient bout à bout (signal #1370)."""
    import threading
    import time

    from oto_mcp import transcription_worker as w

    etat = {"en_cours": 0, "max": 0}
    verrou = threading.Lock()

    def _lot():
        with verrou:
            etat["en_cours"] += 1
            etat["max"] = max(etat["max"], etat["en_cours"])
        time.sleep(0.2)
        with verrou:
            etat["en_cours"] -= 1
        return 1

    monkeypatch.setattr(w, "_transcribe_batch", _lot)
    monkeypatch.setattr(w.db, "fail_orphaned_transcription_jobs", lambda: 0)

    async def _tourner():
        try:
            await asyncio.wait_for(w.run_transcription_loop(interval=5), timeout=0.6)
        except asyncio.TimeoutError:
            pass

    asyncio.run(_tourner())
    assert w.CONCURRENCE > 1 and etat["max"] == w.CONCURRENCE
