"""Le budget d'unités au moment de l'appel (`access/credits.py`), sans base : le stockage
est doublé, ce qu'on tient ici est la RÈGLE — qui paie, combien réserver, quand refuser,
quand libérer, quand régler au montant réservé.

Le stockage lui-même, sa concurrence et son coût en requêtes sont tenus par
`test_credits_reglement_live.py`, contre une vraie base.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from oto_mcp import access, geste, session_org
from oto_mcp.access import credits
from oto_mcp.db import credits as db_credits
from oto_mcp.mcp_errors import McpError

# Une fonction au coût PAR ÉLÉMENT pondéré, asynchrone, relue dans la réponse : la forme
# la plus riche du format (`cost.worst_case`, `settled_quantity`, `collection_of`).
ASYNC = ("fullenrich", "submit")
# Une fonction au coût PAR REQUÊTE, synchrone.
SYNC = ("pennylane", "get_quote")


@pytest.fixture
def stock(monkeypatch):
    """Le stockage doublé : un barème, des modes de payeur, ce qui est écrit."""
    ecrit = {"reserver": [], "regler": [], "liberer": [], "releve": []}
    etat = {"version": 3, "lignes": [
        {"connector": "fullenrich", "function": "submit", "unit": "credits", "budget": 2,
         "per_n": 1},
        {"connector": "pennylane", "function": "get_quote", "unit": "requests",
         "budget": 5, "per_n": 10}] + [
        # La même fonction, tarifée PAR UNITÉ trouvée (`outil_nature`).
        {"connector": "fullenrich", "function": "submit", "unit": u, "budget": b,
         "per_n": 1} for u, b in (("work_emails", 5), ("personal_emails", 20),
                                  ("phones", 60))],
        "personnes": set(), "disponible": 1000, "panne": False}

    def reserver(**k):
        if etat["panne"]:
            raise RuntimeError("base indisponible")
        ecrit["reserver"].append(k)
        insuffisant = etat["disponible"] < k["reserved"]
        posee = not insuffisant or k["forcer"]
        return db_credits.Reservation(posee=posee, deja=False, insuffisant=insuffisant,
                                      disponible=etat["disponible"])

    monkeypatch.setattr(db_credits, "version_du_bareme", lambda: etat["version"])
    monkeypatch.setattr(db_credits, "lignes_du_bareme", lambda v: etat["lignes"])
    monkeypatch.setattr(db_credits, "orgs_en_mode_personne", lambda: etat["personnes"])
    monkeypatch.setattr(db_credits, "reserver", reserver)
    monkeypatch.setattr(db_credits, "regler",
                        lambda uid, **k: ecrit["regler"].append((uid, k)) or {"ok": 1})
    monkeypatch.setattr(db_credits, "liberer",
                        lambda uid: ecrit["liberer"].append(uid) or {"ok": 1})
    monkeypatch.setattr(db_credits, "attendre_la_releve",
                        lambda uid, job, d: ecrit["releve"].append((uid, job, d)) or True)
    credits.oublier()
    yield etat, ecrit
    credits.oublier()


@pytest.fixture
def perimetre(monkeypatch):
    monkeypatch.setattr(credits, "_PERIMETRE", {})
    credits.declarer("outil_async", *ASYNC)
    credits.declarer("outil_sync", *SYNC)
    credits.declarer("outil_nature", *ASYNC, unites=_par_nature)
    return credits._PERIMETRE


def _par_nature(args: dict) -> dict:
    """Le pire cas par unité d'un outil tarifé par nature trouvée : chaque contact, sur
    chaque nature demandée."""
    return {u: len(args["contacts"]) for u in args["natures"]}


# Le `tool_calls.call_uid` de l'appel servi : le geste que pose le journal d'appels.
CALL = "c0ffee"


@pytest.fixture
def appel():
    """Un relevé d'appel posé, comme par un bord, sous le geste de l'appel."""
    releve: dict = {}
    jeton = session_org.set_call_trace(releve)
    with geste.portee(geste.AGENT, "u1", CALL):
        yield releve
    session_org.reset_call_trace(jeton)


def _contacts(*champs):
    return {"contacts": [{"enrich_fields": list(c)} for c in champs]}


# ── réglages ─────────────────────────────────────────────────────────────────

def test_le_mode_par_defaut_est_report(monkeypatch):
    monkeypatch.delenv("OTO_CREDITS_MODE", raising=False)
    assert credits.mode() == "report"


@pytest.mark.parametrize("var, valeur", [
    ("OTO_CREDITS_MODE", "strict"), ("OTO_CREDITS_HOLD_MAX_S", "0"),
    ("OTO_CREDITS_HOLD_MAX_S", "un jour"), ("OTO_CREDITS_SYNC_MARGIN_S", "-5")])
def test_un_reglage_illisible_refuse_le_demarrage(monkeypatch, var, valeur):
    monkeypatch.setenv(var, valeur)
    with pytest.raises(ValueError, match=var):
        credits.verifier_reglages()


def test_le_demarrage_verifie_les_reglages():
    src = (pathlib.Path(credits.__file__).parents[1] / "server.py").read_text()
    assert "budget.verifier_reglages()" in src


# ── le périmètre ─────────────────────────────────────────────────────────────

def test_un_outil_ne_paie_pas_un_cout_que_personne_n_a_declare(monkeypatch):
    monkeypatch.setattr(credits, "_PERIMETRE", {})
    with pytest.raises(ValueError, match="no declared cost"):
        credits.declarer("x", "fullenrich", "n_existe_pas")
    credits.declarer("x", *ASYNC)
    with pytest.raises(ValueError, match="already declared"):
        credits.declarer("x", *SYNC)


def test_hors_perimetre_le_bord_n_ouvre_rien(perimetre, appel):
    credits.ouvrir_appel("serper_search", {"q": "x"})
    assert appel == {}
    credits.ouvrir_appel("outil_sync", {"id": 1})
    assert appel[credits.CLE]["outil"] == "outil_sync"
    assert appel[credits.CLE]["args"] == {"id": 1}


def _fonctions_du_module(fn) -> tuple[dict, list]:
    module = inspect.getmodule(fn)
    arbre = ast.parse(inspect.getsource(module))
    defs: dict = {}
    for n in ast.walk(arbre):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(n.name, []).append(n)
    depart = [n for n in defs.get(fn.__name__, [])
              if min([d.lineno for d in n.decorator_list] + [n.lineno])
              <= fn.__code__.co_firstlineno <= n.end_lineno]
    return defs, depart


def _appelle(fn, nom: str) -> bool:
    """La fonction de l'outil, ou une fonction de son module qu'elle atteint, appelle-t-elle
    `credits.<nom>` ? Même fermeture par nom que la garde de `units`
    (`test_reglement_appel._pose_units`)."""
    defs, a_voir = _fonctions_du_module(inspect.unwrap(fn))
    vus = set()
    while a_voir:
        n = a_voir.pop()
        if id(n) in vus:
            continue
        vus.add(id(n))
        for c in ast.walk(n):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == nom):
                return True
            if isinstance(c, ast.Name) and c.id in defs:
                a_voir.extend(defs[c.id])
    return False


def test_chaque_outil_du_perimetre_dit_son_envoi_et_ses_reponses():
    """Sans `envoi`, un échec après l'envoi serait pris pour un refus net (libéré) ; sans
    `reponses`, le règlement se ferait au pire cas. Un outil du périmètre dit les deux."""
    import asyncio
    from _mcp_app import static_mcp
    # Les outils du lecteur générique (`tools/decrits.py`) n'ont pas de `fn` : seuls ceux du
    # périmètre, écrits à la main, sont lus.
    outils = {t.name: t for t in asyncio.run(static_mcp().list_tools(run_middleware=False))}
    manquants = [(nom, f) for nom in credits.perimetre() for f in ("envoi", "reponses")
                 if not _appelle(outils[nom].fn, f)]
    assert not manquants, manquants


# ── réserver ─────────────────────────────────────────────────────────────────

def _ouvrir(outil, args):
    credits.ouvrir_appel(outil, args)


def test_le_pire_cas_fois_le_bareme_au_paquet_superieur(monkeypatch, stock, perimetre,
                                                        appel):
    etat, ecrit = stock
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    _ouvrir("outil_async", _contacts(["contact.phones", "contact.work_emails"],
                                     ["contact.personal_emails"]))
    credits.reserver("u1", 7, "platform")
    r = ecrit["reserver"][-1]
    assert (r["quantity"], r["reserved"], r["unit"]) == (14, 28, "credits"), \
        "10 + 1 + 3 crédits du fournisseur, 2 unités de budget chacun"
    assert (r["payer"], r["scale_version"], r["forcer"]) == ("org:7", 3, False)
    assert r["deadline_s"] == credits.hold_max_s(), "asynchrone : sa relève a son échéance"
    _ouvrir("outil_sync", {})
    credits.reserver("u1", 7, "platform")
    assert ecrit["reserver"][-1]["reserved"] == 5, "1 requête, paquet de 10 → 1 paquet"


def test_la_reservation_est_faite_une_fois_par_appel(stock, perimetre, appel):
    _, ecrit = stock
    _ouvrir("outil_sync", {})
    credits.reserver("u1", 7, "platform")
    credits.reserver("u1", 7, "platform")
    assert len(ecrit["reserver"]) == 1


@pytest.mark.parametrize("org, personnes, attendu", [
    (7, set(), "org:7"), (7, {7}, "user:u1"), (None, set(), "user:u1")])
def test_qui_paie(stock, perimetre, appel, org, personnes, attendu):
    etat, ecrit = stock
    etat["personnes"] = personnes
    _ouvrir("outil_sync", {})
    credits.reserver("u1", org, "platform")
    assert ecrit["reserver"][-1]["payer"] == attendu


def test_hors_perimetre_ou_hors_appel_rien_n_est_reserve(stock, perimetre, appel):
    _, ecrit = stock
    credits.reserver("u1", 7, "platform")
    credits.ouvrir_appel("serper_search", {})
    credits.reserver("u1", 7, "platform")
    assert ecrit["reserver"] == []


def test_off_ne_reserve_rien(monkeypatch, stock, perimetre, appel):
    monkeypatch.setenv("OTO_CREDITS_MODE", "off")
    _ouvrir("outil_sync", {})
    credits.reserver("u1", 7, "platform")
    assert stock[1]["reserver"] == []


def _code(e: McpError) -> str:
    return e.error.data["code"]


def test_enforce_refuse_un_solde_insuffisant_sans_rien_retenir(monkeypatch, stock,
                                                                perimetre, appel):
    etat, _ = stock
    etat["disponible"] = 3
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    _ouvrir("outil_sync", {})
    with pytest.raises(McpError) as e:
        credits.reserver("u1", 7, "platform")
    assert _code(e.value) == "insufficient_credits"
    assert e.value.error.data["retryable"] is False
    assert (e.value.error.data["needed"], e.value.error.data["available"]) == (5, 3)
    assert "hold" not in appel[credits.CLE]


def test_report_reserve_quand_meme_et_le_dit(monkeypatch, stock, perimetre, appel,
                                            caplog):
    etat, ecrit = stock
    etat["disponible"] = 3
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    _ouvrir("outil_sync", {})
    credits.reserver("u1", 7, "platform")
    assert ecrit["reserver"][-1]["forcer"] is True
    assert appel[credits.CLE]["hold"]["reserved"] == 5
    assert "servi" in caplog.text


@pytest.mark.parametrize("mode, refuse", [("enforce", True), ("report", False)])
def test_une_fonction_sans_ligne_au_bareme(monkeypatch, stock, perimetre, appel, mode,
                                           refuse):
    etat, ecrit = stock
    etat["lignes"] = etat["lignes"][:1]
    monkeypatch.setenv("OTO_CREDITS_MODE", mode)
    _ouvrir("outil_sync", {})
    if refuse:
        with pytest.raises(McpError) as e:
            credits.reserver("u1", 7, "platform")
        assert _code(e.value) == "credits_unpriced"
        assert e.value.error.data["retryable"] is False
    else:
        credits.reserver("u1", 7, "platform")
    assert ecrit["reserver"] == []


@pytest.mark.parametrize("mode, refuse", [("enforce", True), ("report", False)])
def test_un_registre_en_panne(monkeypatch, stock, perimetre, appel, mode, refuse):
    etat, _ = stock
    etat["panne"] = True
    monkeypatch.setenv("OTO_CREDITS_MODE", mode)
    _ouvrir("outil_sync", {})
    if refuse:
        with pytest.raises(McpError) as e:
            credits.reserver("u1", 7, "platform")
        assert _code(e.value) == "spend_ledger_unavailable"
        assert e.value.error.data["retryable"] is True
    else:
        credits.reserver("u1", 7, "platform")
    assert "hold" not in appel[credits.CLE]


# ── régler ───────────────────────────────────────────────────────────────────

def _reserve(outil, args):
    _ouvrir(outil, args)
    credits.reserver("u1", 7, "platform")
    return session_org.current_call_trace()


def test_un_appel_synchrone_se_regle_au_reel(stock, perimetre, appel):
    _, ecrit = stock
    releve = _reserve("outil_sync", {})
    credits.reponses([{"id": 1}])
    credits.solder_reussite(releve)
    uid, k = ecrit["regler"][-1]
    assert uid == releve[credits.CLE]["uid"] == CALL
    assert (k["montant"], k["quantite"], k["meta"]) == (
        5, 1, {"call_id": CALL, "key_mode": "platform", "from": "requests",
               "exceeded": False})


def test_un_appel_qui_ne_dit_pas_ses_reponses_se_regle_au_pire_cas(stock, perimetre, appel,
                                                                   caplog):
    _, ecrit = stock
    releve = _reserve("outil_sync", {})
    credits.solder_reussite(releve)
    assert ecrit["regler"][-1][1]["meta"]["from"] == "unreported"
    assert "réglé au pire cas" in caplog.text


def test_une_soumission_asynchrone_attend_sa_releve(stock, perimetre, appel):
    _, ecrit = stock
    releve = _reserve("outil_async", _contacts(["contact.phones"]))
    credits.reponses([{"enrichment_id": "e-42"}])
    credits.solder_reussite(releve)
    assert ecrit["regler"] == []
    uid, job, delai = ecrit["releve"][-1]
    assert job["function"] == "fullenrich.fetch"
    assert job["args"] == {"enrichment_id": "e-42"}
    assert delai == credits.hold_max_s(), "`no_deadline` : la borne de l'instance"
    # Ce que la relève, ou l'échéance, versera au mouvement : la réservation le garde.
    assert job["meta"] == {"call_id": CALL, "key_mode": "platform", "job_id": "e-42"}


def test_sans_reservation_rien_ne_se_regle(stock, perimetre, appel):
    _, ecrit = stock
    _ouvrir("outil_sync", {})
    credits.solder_reussite(session_org.current_call_trace())
    credits.solder_echec(session_org.current_call_trace(), RuntimeError())
    assert ecrit["regler"] == ecrit["liberer"] == []


class _Amont(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.mark.parametrize("envoye, exc, libere", [
    (False, RuntimeError("avant l'envoi"), True),
    (True, _Amont(400), True),
    (True, _Amont(404), True),
    (True, _Amont(408), False),
    (True, _Amont(429), False),
    (True, _Amont(502), False),
    (True, TimeoutError("lecture"), False),
])
def test_un_echec_libere_s_il_est_net_et_se_regle_sinon(stock, perimetre, appel, envoye,
                                                        exc, libere):
    _, ecrit = stock
    releve = _reserve("outil_sync", {})
    if envoye:
        credits.envoi()
    credits.solder_echec(releve, exc)
    if libere:
        assert ecrit["liberer"] == [releve[credits.CLE]["uid"]]
        assert ecrit["regler"] == []
    else:
        assert ecrit["liberer"] == []
        assert ecrit["regler"][-1][1] == {"montant": 5, "quantite": 1,
                                          "meta": {"call_id": CALL, "key_mode": "platform",
                                                   "from": "failure", "exceeded": False}}


def test_le_statut_amont_est_lu_comme_ailleurs():
    """Le classement réutilise la lecture de la taxonomie d'erreurs, pas une copie."""
    from oto_mcp import error_taxonomy
    assert error_taxonomy.upstream_status_in_chain(_Amont(404)) == 404


def test_le_reglement_d_un_bord_ne_leve_jamais(monkeypatch, stock, perimetre, appel,
                                                caplog):
    releve = _reserve("outil_sync", {})
    monkeypatch.setattr(db_credits, "liberer", lambda uid: 1 / 0)
    access.regler_echec("u1", "outil_sync", releve, RuntimeError())
    assert "reste ouverte" in caplog.text


# ── l'échéance, le relevé du jour, le règlement après l'échéance (19/10/2026) ─

def _borne(monkeypatch, secondes):
    import dataclasses
    from oto_mcp import bornes_processus
    reglages = dataclasses.replace(bornes_processus.lire(), outil_s=secondes)
    monkeypatch.setattr(bornes_processus, "reglages", lambda: reglages)


@pytest.mark.parametrize("borne, timeout_s, attendu", [
    (120, None, 120 + 60), (120, 300, 300 + 60), (120, 30, 120 + 60),
    (0, 45, 45 + 60), (0, None, None)])
def test_l_echeance_d_un_appel_synchrone(monkeypatch, borne, timeout_s, attendu):
    """max(OTO_BORNE_OUTIL_S, `timeout_s` déclaré) + la marge ; borne désarmée et rien
    de déclaré : `hold_max_s`."""
    monkeypatch.delenv("OTO_CREDITS_SYNC_MARGIN_S", raising=False)
    monkeypatch.delenv("OTO_CREDITS_HOLD_MAX_S", raising=False)
    _borne(monkeypatch, borne)
    declare = {"timeout_s": timeout_s} if timeout_s else {}
    assert credits._echeance(declare, {}) == (attendu or credits.hold_max_s())


def test_l_echeance_ne_depend_pas_du_mode_de_debit(monkeypatch):
    _borne(monkeypatch, 120)
    for m in ("report", "enforce", "off"):
        monkeypatch.setenv("OTO_DEBIT_MODE", m)
        assert credits._echeance({}, {}) == 120 + credits.marge_sync_s()


def test_asynchrone_et_tranche_de_fond_tiennent_hold_max(monkeypatch):
    _borne(monkeypatch, 120)
    assert credits._echeance({"async": {"collect": "fetch"}}, {}) == credits.hold_max_s()
    assert credits._echeance({}, {"hors_borne": True}) == credits.hold_max_s()


def test_la_tranche_de_fond_ouvre_hors_de_la_borne(stock, perimetre, appel, monkeypatch):
    _borne(monkeypatch, 120)
    credits.ouvrir_appel("outil_sync", {}, hors_borne=True)
    credits.reserver("u1", 7, "platform")
    assert stock[1]["reserver"][-1]["deadline_s"] == credits.hold_max_s()
    src = (pathlib.Path(credits.__file__).parents[1] / "jev_jobs_worker.py").read_text()
    assert 'ouvrir_appel("jev_rows", job["params"], hors_borne=True)' in src


def test_une_reservation_synchrone_tient_la_borne_et_la_marge(stock, perimetre, appel,
                                                              monkeypatch):
    _borne(monkeypatch, 120)
    _ouvrir("outil_sync", {})
    credits.reserver("u1", 7, "platform")
    assert stock[1]["reserver"][-1]["deadline_s"] == 120 + credits.marge_sync_s()


def test_une_borne_desarmee_se_dit_au_demarrage(monkeypatch, caplog):
    _borne(monkeypatch, 0)
    credits.verifier_reglages()
    assert "OTO_BORNE_OUTIL_S=0" in caplog.text


@pytest.fixture
def signaux(monkeypatch):
    from oto_mcp import limite_refus_top
    vus = []
    monkeypatch.setattr(limite_refus_top, "noter_credits",
                        lambda classe, payeur, mode, t: vus.append((classe, payeur, mode)))
    return vus


@pytest.mark.parametrize("mode", ["enforce", "report"])
def test_une_fonction_sans_bareme_va_au_releve_du_jour(monkeypatch, stock, perimetre, appel,
                                                       signaux, mode):
    etat, _ = stock
    etat["lignes"] = etat["lignes"][:1]
    monkeypatch.setenv("OTO_CREDITS_MODE", mode)
    _ouvrir("outil_sync", {})
    try:
        credits.reserver("u1", 7, "platform")
    except McpError:
        pass
    assert signaux == [("unpriced:pennylane.get_quote", "org:7", mode)]


def test_un_outil_qui_ne_dit_pas_ses_reponses_va_au_releve_du_jour(stock, perimetre, appel,
                                                                   signaux):
    releve = _reserve("outil_sync", {})
    credits.solder_reussite(releve)
    assert [s[:2] for s in signaux] == [("unreported:outil_sync", "org:7")]


def test_le_releve_du_jour_est_un_limiteur_du_palmares():
    from oto_mcp import limite_refus_top
    assert limite_refus_top.PALMARES_CREDITS.limiteur == "credits"


@pytest.mark.parametrize("etat_clos, journalise", [("expired", True), ("settled", False)])
def test_un_reglement_apres_l_echeance_n_est_que_journalise(monkeypatch, stock, perimetre,
                                                            appel, caplog, etat_clos,
                                                            journalise):
    """La réservation échue a été réglée au montant réservé : le règlement réel qui
    arrive ensuite ne rembourse rien, il se journalise sous un nom."""
    releve = _reserve("outil_sync", {})
    releve[credits.CLE]["envoye"] = True
    monkeypatch.setattr(db_credits, "regler", lambda uid, **k: None)
    monkeypatch.setattr(db_credits, "reservation", lambda uid: {
        "state": etat_clos, "connector": "pennylane", "function": "get_quote",
        "payer": "org:7", "reserved": 5})
    credits.solder_echec(releve, RuntimeError("après l'envoi"))
    assert ("credits_hold_expired_before_settle" in caplog.text) is journalise


class _UpstreamHTTPError(Exception):
    """La forme d'`oto.tools.common.errors.UpstreamHTTPError` (oto-core à venir) : le
    statut en attribut, sans hériter de `RuntimeError`."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _ReponseHTTP:
    def __init__(self, status_code):
        self.status_code = status_code


class _HTTPError(Exception):
    """La forme de `requests.HTTPError` : le statut dans `.response.status_code`."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = _ReponseHTTP(status_code)


@pytest.mark.parametrize("forme", [_UpstreamHTTPError, _HTTPError])
@pytest.mark.parametrize("statut, net", [(400, True), (404, True), (408, False),
                                         (429, False), (500, False)])
def test_le_refus_net_lit_le_statut_des_deux_formes_d_erreur_http(forme, statut, net):
    """Le statut se lit par attribut, sans importer une classe d'oto-core : levée
    directement ou enveloppée (`raise … from`)."""
    appel = {"envoye": True}
    assert credits.refus_net(appel, forme(statut)) is net
    try:
        try:
            raise forme(statut)
        except Exception as e:
            raise RuntimeError("enveloppe") from e
    except RuntimeError as enveloppe:
        assert credits.refus_net(appel, enveloppe) is net


# ── la garantie du mode `report` : aucun appel n'est JAMAIS refusé ────────────

@pytest.mark.parametrize("cas", ["solde_nul", "solde_negatif", "hors_bareme",
                                 "registre_qui_leve", "bareme_illisible"])
def test_en_report_aucun_appel_n_est_jamais_refuse(monkeypatch, stock, perimetre, appel,
                                                   cas):
    """`report` (le défaut, la valeur de prod au lancement) tient tout le registre et ne
    refuse RIEN : ni `insufficient_credits`, ni `credits_unpriced`, ni
    `spend_ledger_unavailable` — quel que soit le solde, le barème ou l'état de la base.
    L'appel part ; ce qu'`enforce` aurait refusé se journalise."""
    etat, ecrit = stock
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    if cas == "solde_nul":
        etat["disponible"] = 0
    elif cas == "solde_negatif":
        etat["disponible"] = -500
    elif cas == "hors_bareme":
        etat["lignes"] = []
    elif cas == "registre_qui_leve":
        etat["panne"] = True
    else:
        monkeypatch.setattr(db_credits, "version_du_bareme", lambda: 1 / 0)
    for outil, args in (("outil_sync", {}), ("outil_async", _contacts(["contact.phones"]))):
        _ouvrir(outil, args)
        credits.reserver("u1", 7, "platform")            # ne lève pas : l'appel part
        assert credits.CLE in session_org.current_call_trace()
    if cas in ("solde_nul", "solde_negatif"):
        # La réservation est posée quand même : le registre reste complet.
        assert [r["forcer"] for r in ecrit["reserver"]] == [True, True]


# ── tarifé PAR UNITÉ trouvée ─────────────────────────────────────────────────

def test_seul_un_asynchrone_se_tarife_par_unite(monkeypatch):
    monkeypatch.setattr(credits, "_PERIMETRE", {})
    with pytest.raises(ValueError, match="not asynchronous"):
        credits.declarer("x", *SYNC, unites=_par_nature)


def test_par_unite_la_reservation_somme_les_paquets_de_chaque_unite(stock, perimetre,
                                                                     appel):
    """Deux contacts, deux natures : 2 × 5 (e-mail pro) + 2 × 60 (téléphone). La
    réservation note ses unités jointes par `+`."""
    _, ecrit = stock
    _ouvrir("outil_nature", {"contacts": [{}, {}], "natures": ["work_emails", "phones"]})
    credits.reserver("u1", 7, "platform")
    r = ecrit["reserver"][-1]
    assert (r["unit"], r["quantity"], r["reserved"]) == ("work_emails+phones", 4, 130)
    assert appel[credits.CLE]["hold"]["reserved"] == 130


@pytest.mark.parametrize("mode, refuse", [("enforce", True), ("report", False)])
def test_par_unite_une_ligne_qui_manque_est_sans_bareme(monkeypatch, stock, perimetre,
                                                        appel, mode, refuse):
    etat, ecrit = stock
    etat["lignes"] = [l for l in etat["lignes"] if l["unit"] != "phones"]
    monkeypatch.setenv("OTO_CREDITS_MODE", mode)
    _ouvrir("outil_nature", {"contacts": [{}], "natures": ["work_emails", "phones"]})
    if refuse:
        with pytest.raises(McpError) as e:
            credits.reserver("u1", 7, "platform")
        assert _code(e.value) == "credits_unpriced"
    else:
        credits.reserver("u1", 7, "platform")      # `report` : servi, au relevé du jour
    assert ecrit["reserver"] == []


@pytest.fixture
def releve_attendue(stock, monkeypatch):
    """Une réservation par unité qui attend sa relève (e-mail pro + téléphone)."""
    hold = {"call_uid": "u-42", "connector": "fullenrich", "function": "submit",
            "unit": "work_emails+phones", "scale_version": 3, "quantity": 4,
            "reserved": 130}
    monkeypatch.setattr(db_credits, "reservation_de_la_releve", lambda f, a: hold)
    return stock


def test_la_releve_regle_chaque_unite_reservee_a_ce_qui_est_trouve(releve_attendue):
    """Une unité lue que la réservation n'a pas prise (`personal_emails`) n'est pas
    réglée : elle n'était pas demandée."""
    from oto_mcp.access import credits_releve
    _, ecrit = releve_attendue
    assert credits_releve.regler_releve("fullenrich.fetch", {"enrichment_id": "e"}, {
        "work_emails": 1, "personal_emails": 5, "phones": 2}) is True
    uid, k = ecrit["regler"][-1]
    assert (uid, k["montant"], k["quantite"]) == ("u-42", 1 * 5 + 2 * 60, 3)
    assert k["meta"] == {"from": "response", "exceeded": False,
                         "units": {"work_emails": 1, "phones": 2}}


def test_une_releve_sans_chiffre_se_regle_au_montant_reserve(releve_attendue):
    from oto_mcp.access import credits_releve
    _, ecrit = releve_attendue
    assert credits_releve.regler_releve("fullenrich.fetch", {"enrichment_id": "e"}, None)
    assert ecrit["regler"][-1][1] == {"montant": None, "quantite": None,
                                      "meta": {"from": "unreadable", "exceeded": False}}


@pytest.mark.parametrize("panne", ["ligne_manquante", "quantite_illisible"])
def test_une_releve_illisible_laisse_la_reservation_a_son_echeance(releve_attendue, panne):
    from oto_mcp.access import credits_releve
    etat, ecrit = releve_attendue
    quantites = {"work_emails": 1, "phones": 2}
    if panne == "ligne_manquante":
        etat["lignes"] = [l for l in etat["lignes"] if l["unit"] != "phones"]
    else:
        quantites["phones"] = -1
    assert credits_releve.regler_releve("fullenrich.fetch", {"enrichment_id": "e"},
                                        quantites) is False
    assert ecrit["regler"] == []


# ── l'appariement : chaque mouvement se rapproche de son appel ───────────────

def test_la_reservation_porte_le_call_uid_de_l_appel(stock, perimetre, appel):
    """La clé de la réservation est le `tool_calls.call_uid` de l'appel : le geste que
    le journal d'appels pose, lu à la réservation (le bord, plus externe, ne l'a pas)."""
    _, ecrit = stock
    _reserve("outil_sync", {})
    assert ecrit["reserver"][-1]["call_uid"] == CALL


def test_une_cible_apporte_son_propre_call_uid(stock, perimetre, appel):
    """N cibles d'une recette sous UN geste : N réservations distinctes."""
    _, ecrit = stock
    for uid in ("cible-1", "cible-2"):
        releve: dict = {}
        jeton = session_org.set_call_trace(releve)
        try:
            credits.ouvrir_appel("outil_sync", {}, call_uid=uid)
            credits.reserver("u1", 7, "platform")
        finally:
            session_org.reset_call_trace(jeton)
        assert releve[credits.CLE]["hold"]
    assert [k["call_uid"] for k in ecrit["reserver"]] == ["cible-1", "cible-2"]


def test_sans_geste_la_reservation_ne_se_pose_pas(stock, perimetre, monkeypatch, caplog):
    """Une réservation qui ne saurait nommer son appel ne se pose pas : le registre la
    refuse en `enforce`, la journalise en `report` — jamais un identifiant inventé."""
    monkeypatch.setenv("OTO_CREDITS_MODE", "enforce")
    _, ecrit = stock
    releve: dict = {}
    jeton = session_org.set_call_trace(releve)
    try:
        credits.ouvrir_appel("outil_sync", {})
        with pytest.raises(McpError) as e:
            credits.reserver("u1", 7, "platform")
    finally:
        session_org.reset_call_trace(jeton)
    assert _code(e.value) == "spend_ledger_unavailable"
    assert ecrit["reserver"] == []
    assert "cannot name its call" in caplog.text


def test_deux_appels_sous_un_call_uid_ne_partagent_pas_une_reservation(
        stock, perimetre, appel, monkeypatch):
    """Le registre dit la réservation déjà posée : la seconde ne règle jamais la
    première."""
    etat, ecrit = stock
    monkeypatch.setattr(db_credits, "reserver", lambda **k: db_credits.Reservation(
        posee=False, deja=True, insuffisant=False, disponible=0))
    releve = _reserve("outil_sync", {})
    assert "hold" not in releve[credits.CLE]
    assert releve[credits.CLE]["refus"] == "spend_ledger_unavailable"


def test_l_echeance_dit_l_appel_ou_le_tour(monkeypatch):
    """Une réservation échue dit son appel, ou le travail, la tentative et le tour d'un
    tour de runner ; le rang de la clé, seule une réservation asynchrone le garde."""
    ecrit = []
    monkeypatch.setattr(db_credits, "reservations_echues",
                        lambda limite: ["c0ffee", "runner:12:3:4"])
    monkeypatch.setattr(db_credits, "regler",
                        lambda uid, **k: ecrit.append((uid, k["meta"])) or {"ok": 1})
    assert credits.echoir() == 2
    assert ecrit == [
        ("c0ffee", {"call_id": "c0ffee", "from": "hold_expired", "exceeded": False,
                    "hold_expired": True}),
        ("runner:12:3:4", {"runner_job_id": 12, "attempt": 3, "turn": 4,
                           "from": "hold_expired", "exceeded": False,
                           "hold_expired": True})]


@pytest.mark.parametrize("mode, entite, rang", [
    ("platform", None, "platform"),
    ("tenant", "oto", "tenant"),
    ("tenant", "autre", None),
    ("org", "7", None),
    ("user", "u1", None),
])
def test_seules_la_plateforme_et_le_tenant_primaire_sont_refactures(monkeypatch, mode,
                                                                   entite, rang):
    """Un tenant non primaire paie sa propre clé : jamais réservée."""
    monkeypatch.setenv("OTO_TENANT_PRIMAIRE_SLUG", "oto")
    assert credits.rang_refacture(mode, entite) == rang


def test_le_resolveur_reserve_sur_la_cle_du_tenant_primaire_apres_son_quota():
    """Même prédicat qu'au runner ; l'arête du tenant d'abord : son refus ne laisse
    aucune réservation."""
    src = (pathlib.Path(credits.__file__).parent / "resolve.py").read_text()
    branche = src[src.index('win.mode == "tenant":'):src.index('if win.mode != "platform":')]
    assert branche.index("tenant_budget.enforce(") < branche.index("limite_du_jour.reserver(")
    assert "credits.rang_refacture(win.mode, win.entity_id)" in branche
    # `limite_du_jour.reserver` = la limite de dépense du jour, puis `credits.reserver`.
    assert ('limite_du_jour.reserver(sub, heritage.org_des_cles(sub, active_org), "tenant")'
            in branche)
