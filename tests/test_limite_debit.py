"""La limite de débit applicative (F-025) : `oto_mcp/limite_debit.py`.

Chaque test décrit ce que voit un client sur le fil — le statut, `Retry-After`, le
corps — pour une suite de requêtes ASGI réelles, horloge injectée. Les adresses sont
celles des plages de documentation (RFC 5737)."""
from __future__ import annotations

import json
import logging

import pytest

from oto_mcp import client_trace
from oto_mcp import limite_debit as ld

A, B = "192.0.2.10", "198.51.100.20"


@pytest.fixture(autouse=True)
def _proxy_de_la_box(monkeypatch):
    """Les requêtes de ce banc arrivent du proxy de la box (pair `127.0.0.1`), déclaré
    de confiance : son `CF-Connecting-IP` est l'adresse du client (#1232)."""
    monkeypatch.setenv("OTO_PROXIES_DE_CONFIANCE", "127.0.0.1/32,::1/128")
    monkeypatch.setattr(client_trace, "_RESEAUX", None)


class Horloge:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _app_qui_rend(statut: int = 200, morceaux: tuple[bytes, ...] = (b"ok",)):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": statut, "headers": []})
        for i, m in enumerate(morceaux):
            await send({"type": "http.response.body", "body": m,
                        "more_body": i < len(morceaux) - 1})
    return app


def _scope(chemin="/mcp", methode="POST", ip=A, jeton=None, entetes=()):
    h = [(b"cf-connecting-ip", ip.encode())]
    if jeton is not None:
        h.append((b"authorization", f"Bearer {jeton}".encode()))
    h += list(entetes)
    return {"type": "http", "method": methode, "path": chemin, "headers": h,
            "client": ("127.0.0.1", 5000)}


async def _appel(garde, scope):
    msgs = []

    async def receive():                                   # pragma: no cover
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        msgs.append(m)

    await garde(scope, receive, send)
    return msgs


def _statut(msgs):
    return msgs[0]["status"]


def _entetes(msgs):
    return {k: v for k, v in msgs[0]["headers"]}


def _garde(app=None, mode=ld.ENFORCE, horloge=None, exemptees=(), publiees=None,
           murale=None, refus=None, **par_minute):
    bornes = {c: ld.Borne(par_minute.get(c, d)) for c, d in ld.DEFAUTS.items()}
    return ld.LimiteDebit(app or _app_qui_rend(), mode=mode, bornes=bornes,
                          horloge=horloge or Horloge(), exemptees=exemptees,
                          horloge_murale=murale or (lambda: 60_000.0),
                          publier=(publiees.extend if publiees is not None
                                   else (lambda lignes: None)),
                          publier_refus=(refus.extend if refus is not None
                                         else (lambda lignes: None)))


def _app_par_jeton(statuts: dict, defaut: int = 401, *, verifie: bool = True):
    """Rend le statut associé au jeton porté (`defaut` sinon). `verifie` : joue la
    couche d'auth — un statut < 400 publie le principal dans le scope, comme
    `api.base` ; `False` = une route publique qui répond sans rien vérifier."""
    async def app(scope, receive, send):
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        statut = statuts.get(auth.removeprefix("Bearer "), defaut)
        if verifie and statut < 400:
            scope["oto_principal"] = {"sub": "s"}
        await send({"type": "http.response.start", "status": statut, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return app


# ── la classe d'une requête ────────────────────────────────────────────────

@pytest.mark.parametrize("methode,chemin,jeton,classe", [
    ("OPTIONS", "/api/me", False, None),
    ("POST", "/mcp", True, "jeton"),
    ("GET", "/api/me/projects", True, "jeton"),
    ("POST", "/mcp", False, "anonyme"),
    ("GET", "/.well-known/oauth-protected-resource", False, "anonyme"),
    ("POST", "/api/upload/abc", False, "anonyme"),
    ("GET", "/api/invitations/abc", False, "sensible"),
    ("GET", "/api/public/docs/abc", False, "sensible"),
    ("GET", "/p/d/abc", False, "sensible"),
    ("POST", "/o/u/abc", False, "sensible"),
    ("POST", "/oauth/register", False, "anonyme"),
    ("GET", "/api/mcp/auth_callback", False, "sensible"),
    ("GET", "/api/zoho/oauth/callback", False, "sensible"),
    ("POST", "/api/hooks/42", False, "webhook"),
    ("POST", "/api/hooks/42", True, "webhook"),
    ("POST", "/api/receivers/apollo/phones/abc", False, "webhook"),
    ("POST", "/api/billing/webhook", False, "webhook"),
    ("POST", "/api/logto/webhook", False, "webhook"),
])
def test_classer(methode, chemin, jeton, classe):
    assert ld.classer(methode, chemin, jeton) == classe


# ── les seaux ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_un_jeton_qui_martele_recoit_429_avec_retry_after_puis_repasse():
    h = Horloge()
    garde = _garde(horloge=h, jeton=60)              # rafale 30, 1 jeton/s
    for _ in range(30):
        assert _statut(await _appel(garde, _scope(jeton="t1"))) == 200
    msgs = await _appel(garde, _scope(jeton="t1"))
    assert _statut(msgs) == 429
    entetes = _entetes(msgs)
    assert entetes[b"retry-after"] == b"1"
    assert b"x-oto-version" in entetes
    corps = json.loads(msgs[1]["body"])
    assert corps["error"] == "rate_limited"
    assert corps["details"] == {"retryable": True, "retry_after_seconds": 1,
                                "limit_per_minute": 60, "scope": "jeton"}
    h.t += 1.0
    assert _statut(await _appel(garde, _scope(jeton="t1"))) == 200


@pytest.mark.asyncio
async def test_le_jeton_est_la_cle_pas_ladresse():
    """Un client hébergé sert tous ses utilisateurs depuis quelques adresses : un jeton
    épuisé ne coupe pas son voisin de même adresse, et changer d'adresse ne remplit
    pas le seau d'un jeton."""
    garde = _garde(jeton=2)                          # rafale 1
    assert _statut(await _appel(garde, _scope(jeton="t1", ip=A))) == 200
    assert _statut(await _appel(garde, _scope(jeton="t1", ip=B))) == 429
    assert _statut(await _appel(garde, _scope(jeton="t2", ip=A))) == 200


@pytest.mark.asyncio
async def test_lanonyme_est_borne_par_adresse():
    garde = _garde(anonyme=2)
    assert _statut(await _appel(garde, _scope(ip=A))) == 200
    assert _statut(await _appel(garde, _scope(ip=A))) == 429
    assert _statut(await _appel(garde, _scope(ip=B))) == 200


@pytest.mark.asyncio
async def test_ladresse_suit_la_regle_unique_du_depot():
    """`CF-Connecting-IP` d'abord (`client_trace.pick_ip`), jamais le pair TCP du
    proxy : sans quoi tout le trafic partagerait le seau de `127.0.0.1`."""
    garde = _garde(anonyme=2)
    s = _scope(ip=A)
    assert s["client"][0] == "127.0.0.1"
    assert _statut(await _appel(garde, s)) == 200
    assert _statut(await _appel(garde, _scope(ip=B))) == 200


@pytest.mark.asyncio
async def test_sans_proxy_declare_len_tete_ne_choisit_pas_le_seau(monkeypatch):
    """#1232 : sans `OTO_PROXIES_DE_CONFIANCE`, `CF-Connecting-IP` est ignoré — changer
    l'en-tête à chaque requête ne rend pas un seau neuf."""
    monkeypatch.delenv("OTO_PROXIES_DE_CONFIANCE")
    monkeypatch.setattr(client_trace, "_RESEAUX", None)
    garde = _garde(anonyme=2)
    assert _statut(await _appel(garde, _scope(ip=A))) == 200
    assert _statut(await _appel(garde, _scope(ip=B))) == 429


@pytest.mark.asyncio
async def test_les_routes_sensibles_sont_plus_strictes_que_lanonyme():
    garde = _garde(sensible=2, anonyme=600)
    assert _statut(await _appel(garde, _scope("/api/invitations/x", "GET"))) == 200
    msgs = await _appel(garde, _scope("/api/invitations/y", "GET"))
    assert _statut(msgs) == 429
    assert json.loads(msgs[1]["body"])["details"]["scope"] == "sensible"
    assert _statut(await _appel(garde, _scope("/mcp"))) == 200


@pytest.mark.asyncio
async def test_un_webhook_se_compte_par_adresse_et_par_crochet():
    garde = _garde(webhook=2)
    assert _statut(await _appel(garde, _scope("/api/hooks/1", ip=A))) == 200
    assert _statut(await _appel(garde, _scope("/api/hooks/1", ip=A))) == 429
    assert _statut(await _appel(garde, _scope("/api/hooks/2", ip=A))) == 200
    assert _statut(await _appel(garde, _scope("/api/hooks/1", ip=B))) == 200


@pytest.mark.asyncio
async def test_options_passe_toujours():
    garde = _garde(anonyme=2)
    for _ in range(10):
        assert _statut(await _appel(garde, _scope("/api/me", "OPTIONS"))) == 200


@pytest.mark.asyncio
async def test_zero_desarme_une_classe():
    garde = _garde(anonyme=0)
    for _ in range(50):
        assert _statut(await _appel(garde, _scope())) == 200


# ── le budget d'échecs : le jeton inventé ne s'achète pas un seau neuf ─────

@pytest.mark.asyncio
async def test_des_jetons_inventes_epuisent_le_budget_de_ladresse():
    garde = _garde(app=_app_qui_rend(401), echecs=4)    # rafale 2
    for i in range(2):
        assert _statut(await _appel(garde, _scope(jeton=f"faux{i}"))) == 401
    msgs = await _appel(garde, _scope(jeton="faux-suivant"))
    assert _statut(msgs) == 429
    assert json.loads(msgs[1]["body"])["details"]["scope"] == "echecs"
    # une autre adresse n'en pâtit pas
    assert _statut(await _appel(garde, _scope(jeton="faux9", ip=B))) == 401


@pytest.mark.asyncio
async def test_un_jeton_deja_accepte_passe_malgre_le_budget_epuise():
    """Les 401 de découverte d'un client hébergé remplissent le compteur de son
    adresse ; ses utilisateurs déjà connectés ne doivent pas en pâtir."""
    garde = _garde(app=_app_par_jeton({"bon": 200}), echecs=4)
    assert _statut(await _appel(garde, _scope(jeton="bon"))) == 200
    for i in range(2):
        await _appel(garde, _scope(jeton=f"faux{i}"))
    assert _statut(await _appel(garde, _scope(jeton="faux-suivant"))) == 429
    assert _statut(await _appel(garde, _scope(jeton="bon"))) == 200
    # la découverte OAuth sans jeton (classe `anonyme`) n'est jamais fermée par les
    # 401 des voisins : elle paie son propre seau par adresse.
    assert _statut(await _appel(garde, _scope())) == 401


@pytest.mark.asyncio
async def test_un_404_compte_comme_echec_sur_une_route_a_secret_seulement():
    garde = _garde(app=_app_qui_rend(404), echecs=4, sensible=600, jeton=600)
    for _ in range(5):
        await _appel(garde, _scope("/api/me/x", "GET", jeton="t"))
    assert _statut(await _appel(garde, _scope("/api/me/x", "GET", jeton="u"))) == 404
    for _ in range(2):
        await _appel(garde, _scope("/api/invitations/devine", "GET"))
    assert _statut(await _appel(garde, _scope("/api/invitations/autre", "GET"))) == 429


@pytest.mark.asyncio
async def test_le_401_dun_jeton_reconnu_ne_coute_rien_a_ses_voisins():
    """Un jeton déjà accepté qui expire (401, puis rafraîchissement) ne débite pas le
    budget de l'adresse qu'il partage : ses voisins inconnus passent toujours."""
    statuts = {"connu": 200}
    garde = _garde(app=_app_par_jeton(statuts), echecs=4, jeton=600)   # rafale 2
    assert _statut(await _appel(garde, _scope(jeton="connu"))) == 200
    statuts["connu"] = 401                                 # il expire
    for _ in range(10):
        assert _statut(await _appel(garde, _scope(jeton="connu"))) == 401
    statuts["voisin"] = 200
    assert _statut(await _appel(garde, _scope(jeton="voisin"))) == 200


@pytest.mark.asyncio
async def test_un_webhook_ne_consomme_ni_ne_subit_le_budget_dechecs():
    """Un envoyeur partagé qui retente pendant des jours sur un crochet supprimé (404)
    n'épuise pas le budget de son adresse, et un budget épuisé par ailleurs ne ferme
    pas ses crochets vivants."""
    garde = _garde(app=_app_qui_rend(404), echecs=4, webhook=600)
    for _ in range(20):
        assert _statut(await _appel(garde, _scope("/api/hooks/supprime"))) == 404
    assert _statut(await _appel(garde, _scope("/api/invitations/x", "GET"))) == 404

    garde = _garde(app=_app_par_jeton({}, 401), echecs=4, webhook=600)
    for i in range(2):
        await _appel(garde, _scope(jeton=f"faux{i}"))
    assert _statut(await _appel(garde, _scope(jeton="faux-suivant"))) == 429
    assert _statut(await _appel(garde, _scope("/api/hooks/vivant"))) == 401


@pytest.mark.asyncio
async def test_faire_tourner_le_chemin_dun_webhook_bute_sur_le_plafond_dadresse():
    """Sans plafond par adresse, `/api/hooks/<deviné>` rendrait un seau neuf à chaque
    essai."""
    garde = _garde(app=_app_qui_rend(404), webhook=2)        # plafond 20/min, rafale 10
    statuts = [_statut(await _appel(garde, _scope(f"/api/hooks/{i}"))) for i in range(11)]
    assert statuts[:10] == [404] * 10 and statuts[10] == 429
    assert _statut(await _appel(garde, _scope("/api/hooks/0", ip=B))) == 404


@pytest.mark.asyncio
async def test_une_adresse_exemptee_ne_paie_que_ses_jetons():
    garde = _garde(app=_app_par_jeton({}, 401), exemptees=("192.0.2.0/24",),
                   anonyme=2, sensible=2, echecs=2, jeton=2)
    for _ in range(10):
        assert _statut(await _appel(garde, _scope(ip=A))) == 401
        assert _statut(await _appel(garde, _scope("/p/d/x", "GET", ip=A))) == 401
    for i in range(10):
        assert _statut(await _appel(garde, _scope(jeton=f"faux{i}", ip=A))) == 401
    # la classe `jeton` s'applique toujours
    assert _statut(await _appel(garde, _scope(jeton="meme", ip=A))) == 401
    assert _statut(await _appel(garde, _scope(jeton="meme", ip=A))) == 429
    # une adresse hors du réseau exempté est bornée comme toute autre
    assert _statut(await _appel(garde, _scope(ip=B))) == 401
    assert _statut(await _appel(garde, _scope(ip=B))) == 429


def test_adresses_exemptees_lues_dans_lenv(monkeypatch):
    monkeypatch.setenv("OTO_DEBIT_ADRESSES_EXEMPTEES", " 192.0.2.7, 198.51.100.0/24 ,")
    reseaux = ld.adresses_exemptees_depuis_env()
    assert [str(r) for r in reseaux] == ["192.0.2.7/32", "198.51.100.0/24"]
    monkeypatch.delenv("OTO_DEBIT_ADRESSES_EXEMPTEES")
    assert ld.adresses_exemptees_depuis_env() == ()


def test_une_adresse_exemptee_illisible_leve(monkeypatch):
    """Ni exemption devinée, ni machine à nous bornée sans qu'on le sache."""
    monkeypatch.setenv("OTO_DEBIT_ADRESSES_EXEMPTEES", "192.0.2.7,pas-une-adresse")
    with pytest.raises(ld.ReglageDebitInvalide, match="pas-une-adresse"):
        ld.adresses_exemptees_depuis_env()


# ── modes, transport ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_laisse_passer_et_le_dit(caplog):
    garde = _garde(mode=ld.REPORT, anonyme=2)
    with caplog.at_level(logging.WARNING, logger="oto_mcp.limite_debit"):
        for _ in range(5):
            assert _statut(await _appel(garde, _scope())) == 200
    lignes = [r.getMessage() for r in caplog.records]
    assert len(lignes) == 1, "une ligne par clé et par minute, pas une par requête"
    assert "serait refusé" in lignes[0] and A in lignes[0]


@pytest.mark.asyncio
async def test_le_journal_ne_porte_jamais_le_jeton(caplog):
    garde = _garde(jeton=2)
    with caplog.at_level(logging.WARNING, logger="oto_mcp.limite_debit"):
        for _ in range(3):
            await _appel(garde, _scope(jeton="secret-tres-long"))
    assert caplog.records
    assert all("secret-tres-long" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_off_est_un_passe_partout():
    garde = _garde(mode=ld.OFF, anonyme=2)
    for _ in range(5):
        assert _statut(await _appel(garde, _scope())) == 200


@pytest.mark.asyncio
async def test_le_flux_passe_intact_sans_tampon():
    """Le SSE de `/mcp` : chaque morceau ressort tel quel, dans l'ordre."""
    garde = _garde(app=_app_qui_rend(200, (b"a", b"b", b"c")))
    msgs = await _appel(garde, _scope(jeton="t"))
    assert [m.get("body") for m in msgs[1:]] == [b"a", b"b", b"c"]
    assert [m.get("more_body") for m in msgs[1:]] == [True, True, False]


@pytest.mark.asyncio
async def test_hors_http_rien_nest_touche():
    vus = []

    async def app(scope, receive, send):
        vus.append(scope["type"])

    garde = _garde(app=app, anonyme=2)
    for _ in range(5):
        await garde({"type": "lifespan"}, None, None)
    assert vus == ["lifespan"] * 5


# ── réglages ───────────────────────────────────────────────────────────────

def test_le_defaut_est_report(monkeypatch):
    """La même image tourne derrière des proxys qui n'écrasent pas les en-têtes
    d'adresse : `enforce` se pose instance par instance, jamais par défaut."""
    monkeypatch.delenv("OTO_DEBIT_MODE", raising=False)
    assert ld.mode_depuis_env() == ld.REPORT
    monkeypatch.setenv("OTO_DEBIT_MODE", "")
    assert ld.mode_depuis_env() == ld.REPORT
    monkeypatch.setenv("OTO_DEBIT_MODE", " Enforce ")
    assert ld.mode_depuis_env() == ld.ENFORCE
    monkeypatch.setenv("OTO_DEBIT_MODE", "off")
    assert ld.mode_depuis_env() == ld.OFF


def test_un_mode_inconnu_empeche_le_demarrage(monkeypatch):
    """`enforced` pour `enforce` ne tourne pas en mesure sans bruit : la garde ne se
    construit pas, donc le serveur ne démarre pas."""
    monkeypatch.setenv("OTO_DEBIT_MODE", "enforced")
    with pytest.raises(ld.ReglageDebitInvalide, match="enforced"):
        ld.mode_depuis_env()
    with pytest.raises(ld.ReglageDebitInvalide):
        ld.LimiteDebit(lambda scope, receive, send: None)


def test_bornes_lues_dans_lenv(monkeypatch):
    monkeypatch.setenv("OTO_DEBIT_JETON_PAR_MIN", "30")
    monkeypatch.setenv("OTO_DEBIT_ANONYME_PAR_MIN", "")
    monkeypatch.setenv("OTO_DEBIT_SENSIBLE_PAR_MIN", "0")
    monkeypatch.delenv("OTO_DEBIT_WEBHOOK_PAR_MIN", raising=False)
    b = ld.bornes_depuis_env()
    assert b["jeton"].par_minute == 30 and b["jeton"].rafale == 15
    assert b["anonyme"].par_minute == ld.DEFAUTS["anonyme"]
    assert b["sensible"].par_minute == 0
    assert b["webhook"].par_minute == ld.DEFAUTS["webhook"]


@pytest.mark.parametrize("brut", ["beaucoup", "-5"])
def test_une_borne_illisible_leve(monkeypatch, brut):
    monkeypatch.setenv("OTO_DEBIT_ANONYME_PAR_MIN", brut)
    with pytest.raises(ld.ReglageDebitInvalide, match="OTO_DEBIT_ANONYME_PAR_MIN"):
        ld.bornes_depuis_env()


def test_le_debit_par_jeton_couvre_la_minute_de_chaque_route_lourde():
    """Le débit par jeton s'applique EN PLUS des routes lourdes (#1145). Ce qu'on
    garantit : le débit SOUTENU et ce qu'un seau plein laisse passer en une minute
    couvrent la borne par minute de chacune. Ce qu'on ne garantit PAS : une rafale
    instantanée de toute une minute de route lourde (sa fenêtre glissante l'admet,
    notre rafale est la moitié du débit) — aucun consommateur recensé n'en émet."""
    from oto_mcp.api.routes_lourdes import ROUTES_LOURDES
    jeton = ld.Borne(ld.DEFAUTS["jeton"])
    plus_haute = max(r.par_minute for r in ROUTES_LOURDES)
    assert jeton.par_minute >= plus_haute
    assert jeton.rafale + jeton.par_minute >= plus_haute


# ── la mesure : agrégée en mémoire, écrite au plus une fois par classe et par minute ──

@pytest.mark.asyncio
async def test_la_mesure_compte_par_classe_et_ne_publie_qua_la_minute_suivante():
    publiees, murale = [], {"t": 60_000.0}
    garde = _garde(mode=ld.REPORT, anonyme=4, jeton=600, publiees=publiees,
                   murale=lambda: murale["t"])
    for _ in range(5):
        await _appel(garde, _scope(ip=A))           # rafale 2 : 2 passent, 3 « refusés »
    await _appel(garde, _scope(jeton="t"))
    assert publiees == [], "rien n'est écrit tant que la minute n'est pas close"

    murale["t"] += 60
    await _appel(garde, _scope(ip=B))
    par_classe = {l["tool"]: l for l in publiees}
    assert set(par_classe) == {"debit:anonyme", "debit:jeton"}
    anon = par_classe["debit:anonyme"]
    assert anon["kind"] == "debit" and anon["ok"] is False and "sub" not in anon
    assert {k: anon["args"][k] for k in ("mode", "allowed", "would_refuse", "refused",
                                         "peak_pct", "limit_per_minute")} == {
        "mode": "report", "allowed": 2, "would_refuse": 3, "refused": 0,
        "peak_pct": 100.0, "limit_per_minute": 4}
    assert anon["args"]["minute"] == "1970-01-01T16:40:00+00:00"
    assert par_classe["debit:jeton"]["args"]["allowed"] == 1
    assert par_classe["debit:jeton"]["ok"] is True
    assert A not in json.dumps(publiees), "jamais une adresse dans la mesure"


@pytest.mark.asyncio
async def test_en_enforce_la_mesure_compte_les_refus_servis():
    publiees, murale = [], {"t": 60_000.0}
    garde = _garde(anonyme=2, publiees=publiees, murale=lambda: murale["t"])
    for _ in range(3):
        await _appel(garde, _scope())
    murale["t"] += 61
    await _appel(garde, _scope(ip=B))
    (anon,) = [l for l in publiees if l["tool"] == "debit:anonyme"]
    assert (anon["args"]["allowed"], anon["args"]["would_refuse"],
            anon["args"]["refused"]) == (1, 0, 2)


@pytest.mark.asyncio
async def test_mille_requetes_dans_la_minute_font_une_ligne_par_classe():
    publiees, murale = [], {"t": 60_000.0}
    garde = _garde(mode=ld.REPORT, publiees=publiees, murale=lambda: murale["t"])
    for i in range(1000):
        await _appel(garde, _scope(ip=f"192.0.2.{i % 200}"))
    murale["t"] += 60
    await _appel(garde, _scope())
    assert [l["tool"] for l in publiees] == ["debit:anonyme"]
    assert publiees[0]["args"]["allowed"] == 1000


def test_publier_ecrit_dans_le_journal_hors_boucle(monkeypatch):
    from oto_mcp import db, limite_debit_mesure as mesure
    ecrites = []
    monkeypatch.setattr(db, "insert_tool_call", ecrites.append)
    m = mesure.Mesure("report")
    m.basculer(1000, {})
    m.noter_decision("anonyme", "allowed")
    lignes = m.basculer(1001, {"anonyme": 600.0})
    mesure.publier(lignes)
    assert len(ecrites) == 1 and ecrites[0]["tool"] == "debit:anonyme"
    assert ecrites[0]["args"]["limit_per_minute"] == 600


def test_un_echec_decriture_de_la_mesure_ne_leve_pas(monkeypatch, caplog):
    from oto_mcp import db, limite_debit_mesure as mesure

    def boom(_):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(db, "insert_tool_call", boom)
    with caplog.at_level(logging.WARNING, logger="oto_mcp.limite_debit_mesure"):
        mesure.publier([{"tool": "debit:anonyme"}])
    assert any("en échec" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_un_2xx_sur_une_route_publique_ne_reconnait_pas_le_jeton():
    # Revue F-025 : un bearer inventé qui obtient 200 sur `/api/version` (aucune
    # vérification) ne doit pas devenir « reconnu » — sinon ses 401 sur `/mcp`
    # ne coûteraient plus rien au budget d'échecs de son adresse.
    statuts = {"invente": 200}
    publique = _garde(app=_app_par_jeton(statuts, verifie=False), echecs=4, jeton=600)
    assert _statut(await _appel(publique, _scope(jeton="invente"))) == 200
    statuts["invente"] = 401
    vus = [_statut(await _appel(publique, _scope(jeton="invente"))) for _ in range(10)]
    assert 429 in vus


def test_jeton_valide_lit_le_principal_rest_ou_l_utilisateur_mcp():
    from types import SimpleNamespace
    from oto_mcp.limite_debit import _jeton_valide
    assert not _jeton_valide({})
    assert _jeton_valide({"oto_principal": {"sub": "s"}})
    assert _jeton_valide({"user": SimpleNamespace(is_authenticated=True)})
    assert not _jeton_valide({"user": SimpleNamespace(is_authenticated=False)})
