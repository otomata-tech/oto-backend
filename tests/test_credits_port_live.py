"""Le port du budget d'unités (`capabilities/service_credits.py`), sous l'identité du
commerce : qui passe, puis ce que le stockage porte, sur une vraie base.

Le point le plus cher est le CURSEUR : relu jusqu'à la fin, il ne manque aucun
mouvement, y compris celui d'une transaction encore ouverte quand une plus récente a
déjà validé. Son pendant est `as_of`, l'horizon des DATES : rien de daté d'avant ne
peut plus apparaître ; et `settled_as_of`, celui des APPELS : toute réservation posée
avant est close.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import access, db
from oto_mcp.auth import service_identity
from oto_mcp.capabilities import _authz, registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.db import credits as db_credits
from oto_mcp.db._conn import _connect

_SERVICE = {"sub": "service:m2m-commerce", "client_id": "m2m-commerce",
            "roles": frozenset({"commerce"})}
_CLES = ("service.credits.scale.put", "service.credits.scale.current",
         "service.org.credits.payer_mode", "service.credits.movement.post",
         "service.credits.balance", "service.credits.movements")


def _cap(cle):
    return next(c for c in registry.CAPABILITIES if c.key == cle)


def _appel(cle, **champs):
    cap = _cap(cle)
    ctx = ResolvedCtx(sub=_SERVICE["sub"], org_id=None, role="service:commerce")
    return cap.handler(ctx, cap.Input(**champs))


def _refus(cle, **champs) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        _appel(cle, **champs)
    return e.value


@pytest.fixture(autouse=True)
def _nettoie():
    yield
    service_identity.set_current(None)


@pytest.mark.parametrize("cle", _CLES)
def test_chaque_capacite_est_ouverte_au_service_et_a_lui_seul(cle, monkeypatch):
    cap = _cap(cle)
    assert cap.mcp is None, "un tuyau de service, pas un outil d'agent"
    assert _authz.accepts_service(cap.authz)
    service_identity.set_current(dict(_SERVICE))
    assert cap.authz(RawCtx(sub=_SERVICE["sub"])).role == "service:commerce"
    service_identity.set_current(None)
    monkeypatch.setattr(access, "is_super_admin", lambda sub: True)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    with pytest.raises(AuthzDenied) as refus:
        cap.authz(RawCtx(sub="u-super"))
    assert refus.value.code == "service_required"


def _org() -> int:
    with _connect() as conn:
        return conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                            (f"org-{uuid.uuid4().hex[:8]}",)).fetchone()["id"]


def _compte() -> str:
    sub = f"u-{uuid.uuid4().hex[:8]}"
    db.upsert_user(sub, email=f"{sub}@exemple.test")
    return sub


def _ligne(**c):
    return {"connector": "fullenrich", "function": "submit", "unit": "credits",
            "budget": 1, "per_n": 1, **c}


# ── le barème ────────────────────────────────────────────────────────────────

def test_le_bareme_se_pose_une_fois_par_version_et_la_plus_haute_fait_foi(live):
    assert _appel("service.credits.scale.put", version=7, lines=[_ligne()])["created"]
    assert not _appel("service.credits.scale.put", version=7,
                      lines=[_ligne()])["created"], "la même pose ne change rien"
    assert _refus("service.credits.scale.put", version=7,
                  lines=[_ligne(budget=2)]).code == "scale_version_conflict"
    _appel("service.credits.scale.put", version=8,
           lines=[_ligne(budget=3, per_n=10), _ligne(function="fetch", budget=0)])
    lu = _appel("service.credits.scale.current")
    assert lu["version"] == 8
    assert [(l["function"], l["budget"], l["per_n"]) for l in lu["lines"]] == [
        ("fetch", 0, 1), ("submit", 3, 10)]


def test_une_ligne_en_double_est_refusee_a_la_saisie():
    with pytest.raises(ValueError):
        _cap("service.credits.scale.put").Input(version=1, lines=[_ligne(), _ligne()])


# ── qui paie ─────────────────────────────────────────────────────────────────

def test_le_mode_de_payeur_se_pose_et_se_retire(live):
    org = _org()
    _appel("service.org.credits.payer_mode", org_id=org, mode="person")
    assert org in db_credits.orgs_en_mode_personne()
    _appel("service.org.credits.payer_mode", org_id=org, mode="org")
    assert org not in db_credits.orgs_en_mode_personne()
    assert _refus("service.org.credits.payer_mode", org_id=10 ** 9,
                  mode="person").code == "unknown_org"


# ── versements et solde ──────────────────────────────────────────────────────

def test_un_versement_est_idempotent_par_payeur_et_par_cle(live):
    payeur = f"org:{_org()}"
    neuf = _appel("service.credits.movement.post", payer=payeur, amount=100,
                  reason="pack", idempotency_key="k1")
    assert neuf["created"] and neuf["movement"]["amount"] == 100
    redit = _appel("service.credits.movement.post", payer=payeur, amount=100,
                   reason="pack", idempotency_key="k1")
    assert not redit["created"] and redit["movement"]["id"] == neuf["movement"]["id"]
    assert _refus("service.credits.movement.post", payer=payeur, amount=90,
                  reason="pack", idempotency_key="k1").code == "idempotency_conflict"
    solde = _appel("service.credits.balance", payer=payeur)
    assert (solde["balance"], solde["holds_open"], solde["available"]) == (100, 0, 100)
    assert solde["as_of"].endswith("+00:00")


def test_la_meme_cle_sert_a_deux_payeurs(live):
    a, b = f"org:{_org()}", f"user:{_compte()}"
    for p in (a, b):
        assert _appel("service.credits.movement.post", payer=p, amount=5, reason="r",
                      idempotency_key="partagee")["created"]


@pytest.mark.parametrize("payeur, code", [
    ("orga:1", "invalid_payer"), ("org:abc", "invalid_payer"), ("user:", "invalid_payer"),
    ("org:1000000000", "unknown_org"), ("user:u-personne", "unknown_user")])
def test_un_payeur_illisible_ou_inconnu_est_refuse_nommement(live, payeur, code):
    assert _refus("service.credits.movement.post", payer=payeur, amount=1, reason="r",
                  idempotency_key="k").code == code
    assert _refus("service.credits.balance", payer=payeur).code == code


def test_un_payeur_sans_ligne_a_un_solde_nul(live):
    s = _appel("service.credits.balance", payer=f"org:{_org()}")
    assert (s["balance"], s["holds_open"], s["available"]) == (0, 0, 0)


# ── le curseur ───────────────────────────────────────────────────────────────

def _tout_lire(apres=None, garder=None) -> tuple[list[dict], str]:
    """Tout ce qui est servi après `apres` ; avec `garder`, les seuls mouvements qu'il
    retient. Les bancs voisins écrivent dans la même table, et l'horizon peut retenir
    les leurs au-delà d'un curseur pris avant : ils sortent alors après lui."""
    lus = []
    while True:
        page = _appel("service.credits.movements", after=apres, limit=2)
        lus += [m for m in page["movements"] if garder is None or garder(m)]
        if page["next_cursor"] == apres:
            return lus, apres
        apres = page["next_cursor"]


def _lire_jusqu_a(n: int, garder, apres=None) -> tuple[list[dict], str]:
    """Relit, borné, jusqu'à `n` mouvements retenus par `garder`. L'horizon est celui
    de tout le serveur : sur le serveur de test partagé, une transaction d'une AUTRE
    base (une suite voisine) le retient aussi. Un retard, jamais une perte ni un
    désordre."""
    lus, fin = [], time.monotonic() + 120
    while True:
        page, apres = _tout_lire(apres, garder)
        lus += page
        if len(lus) >= n or time.monotonic() >= fin:
            return lus, apres
        time.sleep(0.5)


def test_le_curseur_ne_sert_pas_au_dela_d_une_transaction_encore_ouverte(live):
    """La transaction A ouvre et écrit, B écrit APRÈS et valide la première. Servir B
    avant A ferait avancer le curseur au-delà d'A : A, validée ensuite, ne serait
    jamais lue. Tant qu'A est ouverte, ni A ni B ne sont servies ; après, les deux,
    dans l'ordre de leurs transactions."""
    import psycopg

    payeur = f"org:{_org()}"

    def du_payeur(m):
        return m["payer"] == payeur

    _, depart = _tout_lire()
    lente = psycopg.connect(_dsn())
    try:
        lente.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                      "VALUES (%s, 'deposit', 1, 'lente')", (payeur,))
        _appel("service.credits.movement.post", payer=payeur, amount=2, reason="r",
               idempotency_key="rapide")
        lus, apres = _tout_lire(depart, du_payeur)
        assert [m["idempotency_key"] for m in lus] == []
        lente.commit()
    finally:
        lente.close()
    lus, _ = _lire_jusqu_a(2, du_payeur, apres)
    assert [m["idempotency_key"] for m in lus] == ["lente", "rapide"]


# ── as_of : l'horizon des dates ──────────────────────────────────────────────

def _as_of(apres=None) -> datetime:
    page = _appel("service.credits.movements", after=apres, limit=1000)
    assert page["as_of"] is not None, "le banc lit toutes les sessions (superutilisateur)"
    return datetime.fromisoformat(page["as_of"])


def _attendre(condition, delai_s: float = 120) -> bool:
    """Vrai dès que `condition()` l'est, faux passé `delai_s`. L'horizon est partagé
    avec les suites voisines (cf. `_lire_jusqu_a`) : un mouvement de CETTE base que
    leur transaction retient au curseur retient aussi `as_of`. Un retard, jamais un
    recul."""
    fin = time.monotonic() + delai_s
    while not condition():
        if time.monotonic() >= fin:
            return False
        time.sleep(0.5)
    return True


def _ouvrir_et_attendre(dsn: str):
    """Une transaction ouverte, qui n'a fait que LIRE (aucun xid encore), et son
    début ; puis le temps qu'il passe nettement sous l'instant de lecture."""
    import psycopg
    conn = psycopg.connect(dsn)
    debut = conn.execute("SELECT now()").fetchone()[0]
    time.sleep(2.5)
    return conn, debut


def test_une_page_vide_dit_un_as_of_proche_de_maintenant(live):
    _, fin = _tout_lire()
    page = _appel("service.credits.movements", after=fin)
    assert page["movements"] == [] and page["next_cursor"] == fin
    assert page["as_of"].endswith("+00:00")
    assert _as_of(fin) <= datetime.now(timezone.utc)
    avant = datetime.now(timezone.utc)
    assert _attendre(lambda: _as_of(fin) >= avant - timedelta(seconds=3))


def test_une_transaction_ouverte_retient_as_of_puis_le_relache(live):
    """Elle n'a fait que lire : sans xid, elle ne retient pas le curseur, mais elle
    peut encore écrire un mouvement daté de son DÉBUT — c'est là qu'`as_of` s'arrête,
    tant qu'elle court, et pas plus tard."""
    lente, debut = _ouvrir_et_attendre(_dsn())
    try:
        assert _as_of() <= debut
        assert _as_of() <= debut, "tenu tant qu'elle court"
    finally:
        lente.close()
    assert _attendre(lambda: _as_of() > debut)


def test_un_mouvement_ecrit_plus_tard_par_une_transaction_ouverte_reste_au_dessus(live):
    """Le piège : une transaction ouverte AVANT la lecture, sans xid au moment de la
    lecture, écrit ensuite un mouvement daté de son début (`created_at = NOW()`). Un
    horizon tiré des seules transactions qui portent un xid l'aurait promis complet."""
    payeur = f"org:{_org()}"
    lente, debut = _ouvrir_et_attendre(_dsn())
    try:
        promis = _as_of()
        assert promis <= debut
        lente.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                      "VALUES (%s, 'deposit', 1, 'tardif')", (payeur,))
        lente.commit()
    finally:
        lente.close()
    lus, _ = _lire_jusqu_a(1, lambda m: m["payer"] == payeur)
    assert datetime.fromisoformat(lus[0]["created_at"]) >= promis


def test_as_of_ne_recule_jamais_quel_que_soit_le_curseur(live):
    payeur = f"org:{_org()}"
    vus = [_as_of()]
    lente, _ = _ouvrir_et_attendre(_dsn())
    try:
        vus += [_as_of(), _as_of()]
        lente.execute("INSERT INTO credit_movements (payer, kind, amount, idempotency_key) "
                      "VALUES (%s, 'deposit', 1, 'monotone')", (payeur,))
        _appel("service.credits.movement.post", payer=payeur, amount=2, reason="r",
               idempotency_key="apres")
        vus += [_as_of(), _as_of()]
        lente.commit()
    finally:
        lente.close()
    _, fin = _tout_lire()
    for apres in (None, fin, None, fin):
        vus.append(_as_of(apres))
    assert vus == sorted(vus)


def test_une_transaction_d_une_autre_base_ne_retient_as_of_que_par_le_curseur(
        live, pg_dsn):
    """Une autre base ne peut pas écrire ici : sa transaction qui ne fait que LIRE ne
    retient rien. Celle qui porte un xid retient l'horizon du serveur entier, donc le
    curseur — et `as_of` avec lui, au plus ancien mouvement d'ici qu'il retient."""
    import psycopg
    nom = "oto_test_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    try:
        ailleurs, debut = _ouvrir_et_attendre(pg_dsn.rsplit("/", 1)[0] + "/" + nom)
        try:
            assert _attendre(lambda: _as_of() > debut), "une lecture ailleurs ne retient rien"
            ailleurs.execute("SELECT pg_current_xact_id()")
            payeur = f"org:{_org()}"
            ecrit = _appel("service.credits.movement.post", payer=payeur, amount=1,
                           reason="r", idempotency_key="retenu")["movement"]
            time.sleep(2.5)
            lus, apres = _tout_lire(None, lambda m: m["payer"] == payeur)
            assert lus == [], "retenu au curseur par le xid d'ailleurs"
            assert _as_of(apres) <= datetime.fromisoformat(ecrit["created_at"])
        finally:
            ailleurs.close()
        assert _lire_jusqu_a(1, lambda m: m["payer"] == payeur)[0]
        assert _attendre(lambda: _as_of() > datetime.fromisoformat(ecrit["created_at"]))
    finally:
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def test_une_session_au_debut_illisible_rend_as_of_nul(live, monkeypatch):
    """Sans `pg_read_all_stats`, la session d'un autre rôle n'expose ni son état ni
    son début : elle peut être une transaction ouverte, et rien ne la date. Le cœur ne
    promet rien plutôt qu'un horizon faux — la page, elle, est servie."""
    import psycopg
    from urllib.parse import urlparse
    from oto_mcp.db import _conn as dbconn
    role = "oto_test_asof_" + uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'x'")
        conn.execute(f"GRANT SELECT ON credit_movements, credit_holds TO {role}")
    lente = psycopg.connect(_dsn())
    lente.execute("SELECT 1")
    pool_avant = dbconn._pool
    try:
        u = urlparse(_dsn())
        monkeypatch.setenv("DATABASE_URL", u._replace(
            netloc=f"{role}:x@{u.hostname}:{u.port}").geturl())
        dbconn._pool = None
        page = _appel("service.credits.movements", limit=1000)
        assert page["as_of"] is None
        assert page["settled_as_of"] is None, "nul avec as_of"
        assert page["movements"], "la page reste servie"
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        monkeypatch.undo()
        lente.close()
        with _connect() as conn:
            conn.execute(f"DROP OWNED BY {role}")
            conn.execute(f"DROP ROLE {role}")



def test_une_session_lisible_qui_demarre_borne_as_of_sans_le_rendre_nul(live):
    """Une connexion que le pool ouvre pendant la lecture : lisible (le rôle lit toutes
    les sessions), `state` encore NULL. Elle ne rend plus l'horizon nul : son
    `backend_start` le borne. Une session illisible (`backend_start` NULL aussi) et une
    au suivi coupé le rendent toujours indéterminable. `pg_stat_activity` est doublé par
    une vue temporaire, que la session lit avant `pg_catalog`."""
    from oto_mcp.db import credits as mod
    with _connect() as conn:
        def lire(state, backend_start):
            conn.execute("DROP VIEW IF EXISTS pg_temp.pg_stat_activity")
            conn.execute(
                "CREATE TEMP VIEW pg_stat_activity AS SELECT current_database() AS datname, "
                "0 AS pid, (SELECT oid FROM pg_roles WHERE rolname = current_user) "
                "AS usesysid, 'client backend'::text AS backend_type, "
                f"({state})::text AS state, NULL::timestamptz AS xact_start, "
                "NULL::timestamptz AS query_start, "
                f"({backend_start})::timestamptz AS backend_start")
            return conn.execute(mod._SESSIONS_ECRIVANTES, (mod._MARGE_S,)).fetchone()
        s = lire("NULL", "NOW() - interval '1 hour'")
        assert s["illisibles"] == s["sans_suivi"] == 0
        assert datetime.fromisoformat(s["borne"]) <= datetime.now(timezone.utc) - timedelta(minutes=59)
        s = lire("NULL", "NULL")
        assert s["illisibles"] == 1
        s = lire("'disabled'", "NOW()")
        assert s["sans_suivi"] == 1
        conn.execute("DROP VIEW pg_temp.pg_stat_activity")
        conn.rollback()

# ── settled_as_of : l'horizon des appels réglés ──────────────────────────────

def _horizons(apres=None) -> tuple[datetime, datetime]:
    page = _appel("service.credits.movements", after=apres, limit=1000)
    assert page["as_of"] is not None, "le banc lit toutes les sessions (superutilisateur)"
    return (datetime.fromisoformat(page["as_of"]),
            datetime.fromisoformat(page["settled_as_of"]))


def _settled_as_of(apres=None) -> datetime:
    return _horizons(apres)[1]


@pytest.mark.parametrize("clore", ["regler", "liberer"])
def test_une_reservation_ouverte_la_veille_tient_settled_as_of_jusqu_a_sa_cloture(
        live, clore):
    """Le cas d'une soumission asynchrone : posée le jour J, elle attend sa relève (ou
    son échéance de 24 h) sans écrire de mouvement. `as_of` a passé J+1 — aucun
    MOUVEMENT de J ne peut plus apparaître —, mais l'appel de J n'est pas réglé :
    `settled_as_of` reste au début de la réservation. Réglée ou libérée, il remonte à
    `as_of` — une fois le règlement servi : l'horizon est celui du serveur entier."""
    call_uid = f"veille-{uuid.uuid4().hex[:8]}"
    assert db_credits.reserver(
        call_uid=call_uid, payer=f"org:{_org()}", connector="fullenrich",
        function="submit", unit="credits", scale_version=1, quantity=1, reserved=3,
        deadline_s=24 * 3600, forcer=True).posee
    try:
        with _connect() as conn:
            posee = datetime.fromisoformat(conn.execute(
                "UPDATE credit_holds SET created_at = NOW() - interval '25 hours' "
                "WHERE call_uid = %s RETURNING created_at", (call_uid,)
            ).fetchone()["created_at"]).replace(tzinfo=timezone.utc)
        lendemain = datetime.combine(posee.date() + timedelta(days=1), datetime.min.time(),
                                     tzinfo=timezone.utc)
        assert _attendre(lambda: _horizons()[0] > lendemain)
        as_of, regle = _horizons()
        assert as_of > lendemain
        assert regle == posee < lendemain, "l'appel de J n'est pas réglé"
    finally:
        if clore == "regler":
            assert db_credits.regler(call_uid, montant=None, quantite=None, meta={})
        else:
            assert db_credits.liberer(call_uid)
    assert _attendre(lambda: (lambda h: h[1] == h[0])(_horizons()))
    as_of, regle = _horizons()
    assert regle == as_of > lendemain



def test_settled_as_of_par_payeur_n_attend_pas_la_reservation_d_un_autre(live):
    """`payer` : la réservation ouverte d'un payeur ne retient que SON horizon. Celui
    d'un autre payeur suit `as_of` ; celui du flux entier (sans `payer`) l'attend."""
    retenu, libre = f"org:{_org()}", f"org:{_org()}"
    call_uid = f"autre-{uuid.uuid4().hex[:8]}"
    assert db_credits.reserver(
        call_uid=call_uid, payer=retenu, connector="fullenrich", function="submit",
        unit="credits", scale_version=1, quantity=1, reserved=3, deadline_s=24 * 3600,
        forcer=True).posee
    try:
        with _connect() as conn:
            posee = datetime.fromisoformat(conn.execute(
                "UPDATE credit_holds SET created_at = NOW() - interval '25 hours' "
                "WHERE call_uid = %s RETURNING created_at", (call_uid,)
            ).fetchone()["created_at"]).replace(tzinfo=timezone.utc)

        def horizons(payer=None):
            page = _appel("service.credits.movements", limit=1000, payer=payer)
            return (datetime.fromisoformat(page["as_of"]),
                    datetime.fromisoformat(page["settled_as_of"]))
        assert _attendre(lambda: (lambda h: h[1] == h[0])(horizons(libre))), \
            "l'horizon d'un payeur sans réservation suit as_of"
        as_of, regle = horizons(retenu)
        assert regle == posee < as_of, "le payeur qui attend reste à sa réservation"
        assert horizons()[1] <= posee, "sans payer : tous payeurs, comme avant"
    finally:
        assert db_credits.liberer(call_uid)
    assert _refus("service.credits.movements", payer="pas-un-payeur").code == "invalid_payer"

def test_un_reglement_que_l_horizon_retient_tient_settled_as_of(live, pg_dsn):
    """La réservation est réglée, sa transaction validée, mais un xid plus ancien — d'une
    autre base : `_SESSIONS_ECRIVANTES` ne le voit pas, `as_of` passe la réservation —
    retient son mouvement au-dessus de l'horizon du curseur : il n'est pas servi.
    L'appel ne compte comme réglé qu'une fois son mouvement servi."""
    import psycopg
    nom = "oto_test_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    call_uid = f"retenu-{uuid.uuid4().hex[:8]}"

    def du_reglement(m):
        return m["call_uid"] == call_uid

    try:
        ailleurs = psycopg.connect(pg_dsn.rsplit("/", 1)[0] + "/" + nom)
        try:
            ailleurs.execute("SELECT pg_current_xact_id()")
            assert db_credits.reserver(
                call_uid=call_uid, payer=f"org:{_org()}", connector="fullenrich",
                function="submit", unit="credits", scale_version=1, quantity=1,
                reserved=3, deadline_s=3600, forcer=True).posee
            with _connect() as conn:
                posee = datetime.fromisoformat(conn.execute(
                    "SELECT created_at FROM credit_holds WHERE call_uid = %s",
                    (call_uid,)).fetchone()["created_at"]).replace(tzinfo=timezone.utc)
            time.sleep(1.1)
            assert db_credits.regler(call_uid, montant=None, quantite=None, meta={})
            time.sleep(2.5)
            assert _tout_lire(None, du_reglement)[0] == [], "retenu par le xid d'ailleurs"
            as_of, regle = _horizons()
            assert as_of > posee, "aucune transaction de cette base ne retient as_of"
            assert regle <= posee, "le règlement n'est pas servi : l'appel n'est pas réglé"
        finally:
            ailleurs.close()
        assert _lire_jusqu_a(1, du_reglement)[0]
        assert _attendre(lambda: _settled_as_of() > posee)
    finally:
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _une_reservation_validee_en_retard_ne_fait_pas_reculer(dsn: str) -> None:
    """Le piège : une transaction ouverte AVANT la lecture pose ensuite une réservation,
    datée de son DÉBUT (`created_at = NOW()`), et la valide après. Si la lecture ne la
    comptait pas, `settled_as_of` aurait été servi au-dessus de ce début, puis
    reculerait dessous."""
    call_uid = f"tardive-{uuid.uuid4().hex[:8]}"
    vus = [_settled_as_of()]
    lente, debut = _ouvrir_et_attendre(dsn)
    try:
        vus += [_settled_as_of(), _settled_as_of()]
        assert vus[-1] <= debut, "la transaction ouverte tient settled_as_of à son début"
        lente.execute(
            "INSERT INTO credit_holds (call_uid, payer, connector, function, unit, "
            "scale_version, quantity, reserved, deadline) VALUES (%s, %s, 'fullenrich', "
            "'submit', 'credits', 1, 1, 3, NOW() + interval '1 day')",
            (call_uid, f"org:{_org()}"))
        vus.append(_settled_as_of())
        lente.commit()
        vus += [_settled_as_of(), _settled_as_of()]
    finally:
        lente.close()
    try:
        assert db_credits.reservation(call_uid)["state"] == "open"
        assert vus == sorted(vus), "settled_as_of ne recule jamais"
        assert vus[-1] <= debut, "tenu, une fois validée, par la réservation elle-même"
    finally:
        assert db_credits.liberer(call_uid)
    assert _attendre(lambda: _settled_as_of() > debut)


def test_settled_as_of_ne_recule_pas_devant_une_reservation_validee_en_retard(live):
    _une_reservation_validee_en_retard_ne_fait_pas_reculer(_dsn())


def test_le_role_qui_ne_peut_que_reserver_est_compte_lui_aussi(live):
    """Un rôle qui peut poser une réservation sans pouvoir écrire un mouvement : sa
    transaction ouverte ne retient pas `as_of`, mais elle retient `settled_as_of` —
    `_SESSIONS_ECRIVANTES` le compte."""
    from urllib.parse import urlparse
    role = "oto_test_hold_" + uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'x'")
        conn.execute(f"GRANT INSERT ON credit_holds TO {role}")
    try:
        u = urlparse(_dsn())
        _une_reservation_validee_en_retard_ne_fait_pas_reculer(
            u._replace(netloc=f"{role}:x@{u.hostname}:{u.port}").geturl())
    finally:
        with _connect() as conn:
            conn.execute(f"DROP OWNED BY {role}")
            conn.execute(f"DROP ROLE {role}")


def test_le_contrat_sert_settled_as_of_requis_et_nullable():
    from oto_mcp import openapi
    doc = openapi.build()
    schema = (doc["paths"]["/api/service/credits/movements"]["get"]["responses"]["200"]
              ["content"]["application/json"]["schema"])
    if "$ref" in schema:
        schema = doc["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    assert {"as_of", "settled_as_of"} <= set(schema["required"])
    assert schema["properties"]["settled_as_of"]["anyOf"] == [{"type": "string"},
                                                              {"type": "null"}]


def test_un_curseur_illisible_est_refuse_nommement(live):
    assert _refus("service.credits.movements", after="abc").code == "invalid_cursor"


def test_la_limite_est_bornee():
    assert _cap("service.credits.movements").Input(limit=5000).limit == 1000


def _dsn() -> str:
    import os
    return os.environ["DATABASE_URL"]


# ── deux comptes d'une même personne ─────────────────────────────────────────

def test_le_solde_personnel_suit_la_fusion_des_comptes(live):
    from oto_mcp.db import users
    vieux, neuf = _compte(), _compte()
    _appel("service.credits.movement.post", payer=f"user:{vieux}", amount=40,
           reason="r", idempotency_key="v")
    _appel("service.credits.movement.post", payer=f"user:{neuf}", amount=2,
           reason="r", idempotency_key="n")
    with _connect() as conn:
        users.repointer_patrimoine(conn, vieux, neuf)
    assert _appel("service.credits.balance", payer=f"user:{vieux}")["balance"] == 0
    assert _appel("service.credits.balance", payer=f"user:{neuf}")["balance"] == 42
    payeurs = {f"user:{vieux}", f"user:{neuf}"}
    lus, _ = _lire_jusqu_a(
        2, lambda m: m["kind"] == "transfer" and m["payer"] in payeurs)
    transferts = [(m["payer"], m["amount"]) for m in lus]
    assert transferts == [(f"user:{vieux}", -40), (f"user:{neuf}", 40)]
