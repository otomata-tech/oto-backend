"""Mise en ligne YouTube d'un GROS fichier de projet, en fond (`youtube_upload_worker`).

Ce que ce fichier verrouille, avec un faux client YouTube et un faux stockage (aucune
base, aucun Google) :
- `youtube_act op=upload_video` sur un fichier de projet au-delà de 100 Mo ne lit pas
  le fichier : il dépose un travail avec le compte Google RÉSOLU à l'appel ;
- le worker relaie un morceau à la fois, lu par plage, et repart du prochain octet que
  YOUTUBE dit attendre, jamais de son propre compte ;
- reprise après redémarrage : la session rangée est relue, YouTube dit où il en est ;
- une session oubliée par YouTube (404) est rouverte depuis le début ;
- un refus 4xx est terminal, une panne passagère est reportée, au plus `MAX_ESSAIS` ;
- l'accès au projet et l'identité sont rejugés à chaque réclamation ;
- `youtube op=job_status` ne rend jamais la session.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import youtube_upload_worker as W

MIB = 1024 * 1024


class _YouTube:
    """Faux client : retient ce qu'il reçoit ; `garde` = combien d'octets il garde de
    chaque morceau (moins que reçu : on doit repartir de SON compte)."""

    def __init__(self, total, garde=None, statut=None):
        self.total, self.recu, self.ouvertures = total, bytearray(), []
        self.garde, self.statut, self.etats = garde, statut, []

    def start_video_upload(self, total, mime, **kw):
        self.ouvertures.append((total, mime, kw))
        return f"https://upload.exemple/session/{len(self.ouvertures)}"

    def video_job_status(self, session, total):
        self.etats.append(session)
        if isinstance(self.statut, Exception):
            raise self.statut
        return self.statut

    def send_video_chunk(self, session, data, offset, total, mime):
        assert offset == len(self.recu), "un morceau envoyé hors de la position attendue"
        garde = len(data) if self.garde is None else min(self.garde, len(data))
        self.recu += data[:garde]
        if len(self.recu) == total:
            return {"done": True, "video": {"id": "vid-1"}}
        return {"done": False, "next_offset": len(self.recu)}


@pytest.fixture
def monde(monkeypatch):
    from oto_mcp import db
    contenu = bytes(range(256)) * (10 * MIB // 256 + 1)
    etat = {"lectures": [], "sessions": {}, "avances": [], "fini": None, "echec": None,
            "report": None, "acces": True, "refus": None, "contenu": contenu}

    def lire_plage(key, debut, fin):
        etat["lectures"].append((debut, fin))
        return etat["contenu"][debut:fin + 1]

    yt = db.youtube_uploads
    monkeypatch.setattr(W.media_store, "lire_plage", lire_plage)
    monkeypatch.setattr(yt, "poser_session",
                        lambda jid, enc: etat["sessions"].__setitem__(jid, enc))
    monkeypatch.setattr(yt, "avancer",
                        lambda jid, off, bail: etat["avances"].append(off) or True)
    monkeypatch.setattr(yt, "terminer", lambda jid, vid: etat.__setitem__("fini", vid))
    monkeypatch.setattr(yt, "echouer", lambda jid, err: etat.__setitem__("echec", err))
    monkeypatch.setattr(yt, "reporter",
                        lambda jid, err, d: etat.__setitem__("report", (err, d)))
    monkeypatch.setattr(db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr("oto_mcp.garde_identite.refus", lambda sub: etat["refus"])
    monkeypatch.setattr("oto_mcp.ownership.can_access", lambda *a, **k: etat["acces"])
    monkeypatch.setattr(W, "_encrypt", lambda v, aad: f"enc[{aad}]:{v}")
    monkeypatch.setattr(W, "_decrypt", lambda v, aad: v.split(":", 1)[1])
    monkeypatch.setattr(W, "CHUNK", 1 * MIB)
    return etat


def _job(total, **kw):
    return {"id": 9, "project_id": 7, "file_id": 41, "sub": "sub-a", "org_id": 3,
            "account": "chaine@exemple.test", "status": "running", "total_bytes": total,
            "mime": "video/mp4", "meta": {"title": "Film", "privacy_status": "private"},
            "notify_subscribers": False, "session_url_enc": None, "next_offset": 0,
            "attempts": 1, "s3_key": "project-files/7/abc/film.mp4", **kw}


def test_releve_un_morceau_a_la_fois_depuis_le_compte_de_youtube(monde, monkeypatch):
    total = 3 * MIB + 5
    yt = _YouTube(total, garde=MIB - 256 * 1024)          # YouTube garde moins
    monkeypatch.setattr(W, "_client", lambda job: yt)
    W.mener(_job(total))
    assert bytes(yt.recu) == monde["contenu"][:total]
    assert monde["fini"] == "vid-1"
    assert all(fin - debut + 1 <= MIB for debut, fin in monde["lectures"])
    # chaque lecture repart du prochain octet que YouTube a dit attendre
    assert [d for d, _ in monde["lectures"]][1:] == monde["avances"]
    (total_o, mime, kw), = yt.ouvertures
    assert (total_o, mime) == (total, "video/mp4")
    assert kw["title"] == "Film" and kw["notify_subscribers"] is False
    assert monde["sessions"][9].startswith("enc[youtube_upload_jobs:9]:")


def test_reprise_apres_redemarrage(monde, monkeypatch):
    total = 2 * MIB
    yt = _YouTube(total, statut={"done": False, "next_offset": MIB})
    yt.recu += monde["contenu"][:MIB]                       # déjà reçu avant l'arrêt
    monkeypatch.setattr(W, "_client", lambda job: yt)
    W.mener(_job(total, session_url_enc="x:https://upload.exemple/session/old",
                 next_offset=0))
    assert yt.etats == ["https://upload.exemple/session/old"]
    assert yt.ouvertures == []                              # pas de nouvelle session
    assert monde["lectures"] == [(MIB, 2 * MIB - 1)]
    assert monde["fini"] == "vid-1"


def test_session_oubliee_par_youtube_rouverte(monde, monkeypatch):
    from oto.tools.google.youtube import YouTubeError
    total = MIB
    yt = _YouTube(total, statut=YouTubeError(404, {"error": {"message": "gone"}}))
    monkeypatch.setattr(W, "_client", lambda job: yt)
    W.mener(_job(total, session_url_enc="x:https://upload.exemple/session/old"))
    assert len(yt.ouvertures) == 1 and monde["fini"] == "vid-1"


def test_deja_fini_chez_youtube(monde, monkeypatch):
    yt = _YouTube(MIB, statut={"done": True, "video": {"id": "vid-0"}})
    monkeypatch.setattr(W, "_client", lambda job: yt)
    W.mener(_job(MIB, session_url_enc="x:https://upload.exemple/session/old"))
    assert monde["fini"] == "vid-0" and monde["lectures"] == []


def _tour(monkeypatch, job, client):
    from oto_mcp import db
    monkeypatch.setattr(db.youtube_uploads, "reclamer", lambda bail: job)
    monkeypatch.setattr(W, "_client", client)
    return W._tour()


def test_refus_youtube_terminal(monde, monkeypatch):
    from oto.tools.google.youtube import YouTubeError

    class Refus(_YouTube):
        def start_video_upload(self, *a, **k):
            raise YouTubeError(400, {"error": {"message": "quota", "errors": [
                {"reason": "uploadLimitExceeded"}]}})

    assert _tour(monkeypatch, _job(MIB), lambda job: Refus(MIB)) == 1
    assert "uploadLimitExceeded" in monde["echec"] and monde["report"] is None


def test_panne_passagere_reportee_puis_abandonnee(monde, monkeypatch):
    class Panne(_YouTube):
        def send_video_chunk(self, *a, **k):
            raise ConnectionError("coupé")

    _tour(monkeypatch, _job(MIB, attempts=2), lambda job: Panne(MIB))
    raison, delai = monde["report"]
    assert "resume" in raison and delai == 240 and monde["echec"] is None
    monde["report"] = None
    _tour(monkeypatch, _job(MIB, attempts=W.MAX_ESSAIS), lambda job: Panne(MIB))
    assert "gave up" in monde["echec"] and monde["report"] is None


def test_jeton_expire_en_cours_reporte(monde, monkeypatch):
    from oto.tools.google.youtube import YouTubeError

    class Expire(_YouTube):
        def send_video_chunk(self, *a, **k):
            raise YouTubeError(401, {"error": {"message": "expired"}})

    _tour(monkeypatch, _job(MIB), lambda job: Expire(MIB))
    assert monde["report"] is not None and monde["echec"] is None


@pytest.mark.parametrize("cas,attendu", [
    ("acces", "can no longer read"), ("refus", "Compte en pause"), ("fichier", "removed")])
def test_garde_rejugee_a_la_reclamation(monde, monkeypatch, cas, attendu):
    job = _job(MIB)
    if cas == "acces":
        monde["acces"] = False
    elif cas == "refus":
        monde["refus"] = ("account_paused", "Compte en pause.")
    else:
        job["s3_key"] = None
    _tour(monkeypatch, job, lambda j: pytest.fail("aucun appel à YouTube"))
    assert attendu in monde["echec"]


def test_fichier_plus_court_que_prevu_terminal(monde, monkeypatch):
    monde["contenu"] = monde["contenu"][:100]
    _tour(monkeypatch, _job(MIB), lambda job: _YouTube(MIB))
    assert "shorter" in monde["echec"]


def test_etat_public_sans_session():
    out = W.etat_public({**_job(4 * MIB, next_offset=MIB, status="running",
                                session_url_enc="secret", video_id=None, error=None)})
    assert out == {"job_id": 9, "status": "running", "progress": 25.0,
                   "bytes": 4 * MIB, "title": "Film"}
    fini = W.etat_public({**_job(MIB, status="done", video_id="vid-1", error=None)})
    assert fini["studio_url"] == "https://studio.youtube.com/video/vid-1/edit"
    rate = W.etat_public({**_job(MIB, status="failed", error="quota")})
    assert rate["error"] == "quota"


# --- la face outil -------------------------------------------------------------------

def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import youtube as T

    m = FastMCP("t")
    T.register(m)
    fn = asyncio.run(m.get_tool(name)).fn
    return lambda **kw: asyncio.run(fn(**kw))


@pytest.fixture
def appel(monkeypatch):
    from oto_mcp import access, db, file_source
    from oto_mcp.auth import google as google_oauth
    etat = {"crees": []}
    row = {"id": 41, "project_id": 7, "filename": "film.mov", "mime": "video/quicktime",
           "size_bytes": 3 * 1024 ** 3, "s3_key": "k"}
    monkeypatch.setattr(file_source, "fichier_de_projet", lambda src: dict(row))
    monkeypatch.setattr(file_source, "resolve",
                        lambda *a, **k: pytest.fail("un gros fichier n'est jamais lu"))
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "sub-a")
    monkeypatch.setattr(access, "current_org", lambda sub: 3)
    monkeypatch.setattr(google_oauth, "credentials_for", lambda *a, **k: object())

    class Rc:
        account = "chaine@exemple.test"

    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: Rc())

    def creer(**kw):
        etat["crees"].append(kw)
        return {"id": 12, "status": "pending", "next_offset": 0, "video_id": None,
                "error": None, **kw}

    monkeypatch.setattr(db.youtube_uploads, "creer", creer)
    monkeypatch.setattr(db.youtube_uploads, "lire",
                        lambda jid, sub: ({"id": 12, "status": "running",
                                           "next_offset": 1024 ** 3,
                                           "total_bytes": 4 * 1024 ** 3,
                                           "meta": {"title": "Film"},
                                           "session_url_enc": "secret"}
                                          if (jid, sub) == (12, "sub-a") else None))
    etat["row"] = row
    return etat


def test_un_gros_fichier_de_projet_part_en_fond(appel):
    out = _tool("youtube_act")(op="upload_video", title="Film",
                               video={"kind": "project_file", "project_id": 7, "file_id": 41})
    assert out["queued"] is True and out["job_id"] == 12 and "job_status" in out["note"]
    (cree,) = appel["crees"]
    assert cree["account"] == "chaine@exemple.test" and cree["org_id"] == 3
    assert cree["total_bytes"] == 3 * 1024 ** 3 and cree["mime"] == "video/quicktime"
    assert cree["meta"]["privacy_status"] == "private" and cree["file_id"] == 41


def test_publish_at_sans_prive_refuse_avant_la_file(appel):
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError, match="publish_at"):
        _tool("youtube_act")(op="upload_video", title="Film", privacy_status="public",
                             publish_at="2026-12-01T09:00:00Z",
                             video={"kind": "project_file", "project_id": 7, "file_id": 41})
    assert appel["crees"] == []


def test_job_status(appel):
    from oto_mcp.mcp_errors import McpError
    out = _tool("youtube")(op="job_status", job_id=12)
    assert out["progress"] == 25.0 and "session_url_enc" not in str(out)
    with pytest.raises(McpError, match="no upload job"):
        _tool("youtube")(op="job_status", job_id=13)
    with pytest.raises(McpError, match="does not take"):
        _tool("youtube")(op="job_status", job_id=12, video_id="v1")


# --- le client réel d'oto-core et la résolution du compte hors appel -----------------

@pytest.mark.exige_pin_oto_core
def test_oto_core_porte_l_envoi_par_morceaux():
    """Le worker pilote l'envoi lui-même : sans ces trois appels (connectors#306), chaque
    travail mourrait en `AttributeError` après `MAX_ESSAIS` — ce test le dit avant."""
    from oto.tools.google.youtube import YouTubeClient
    for nom in ("start_video_upload", "send_video_chunk", "video_job_status"):
        assert callable(getattr(YouTubeClient, nom, None)), nom


def test_client_resolu_sous_le_contexte_du_depot(monkeypatch):
    """Hors de tout appel : l'org et le projet du dépôt sont reposés pendant la
    résolution du compte Google, puis retirés."""
    from oto_mcp import session_org
    from oto_mcp.auth import google as google_oauth
    vu = {}

    class Creds:
        token = "ya29.jeton"

    def credentials_for(sub, account=None, service=None):
        vu.update(sub=sub, account=account, service=service,
                  org=session_org.current_call_org(),
                  projet=session_org.current_call_project())
        return Creds()

    monkeypatch.setattr(google_oauth, "credentials_for", credentials_for)
    c = W._client(_job(MIB))
    assert vu == {"sub": "sub-a", "account": "chaine@exemple.test", "service": "youtube",
                  "org": 3, "projet": 7}
    assert c._token == "ya29.jeton"
    assert session_org.current_call_org() is None
    assert session_org.current_call_project() is None


def test_compte_google_retire_est_terminal(monkeypatch):
    from oto_mcp.auth import google as google_oauth
    monkeypatch.setattr(google_oauth, "credentials_for", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("The Google account has not yet authorized YouTube")))
    with pytest.raises(W._Terminal, match="authorized YouTube"):
        W._client(_job(MIB))


def test_jeton_expire_en_cours_de_morceau_renvoye_une_fois(monde, monkeypatch):
    from oto.tools.google.youtube import YouTubeError
    total = MIB
    clients = []

    class Expire(_YouTube):
        def send_video_chunk(self, *a, **k):
            raise YouTubeError(401, {"error": {"message": "expired"}})

    def client(job):
        c = Expire(total) if not clients else _YouTube(total)
        clients.append(c)
        return c

    monkeypatch.setattr(W, "_client", client)
    W.mener(_job(total))
    assert len(clients) == 2 and monde["fini"] == "vid-1"
