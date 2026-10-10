"""Le BANC d'exactitude des totaux par jour (#1147) : l'ancienne lecture du journal et la
nouvelle (totaux des jours consolidés + journal direct) rendent la MÊME réponse.

Pour chaque lecteur basculé, la lecture de RÉFÉRENCE est l'ancienne requête, recopiée ici
telle qu'elle lisait le journal seul avant #1147 — le banc ne peut donc pas devenir vert
en changeant les deux côtés à la fois. Un jeu de données aléatoire (graine fixe) couvre
douze jours : plusieurs orgs et comptes, NULL partout où le journal en porte, les quatre
natures, des jobs relevés plusieurs fois et sur plusieurs jours, des appels à minuit pile,
le jour courant. Trois états du registre : rien de la veille (lue en direct), tout
jusqu'à la veille, et le même jour consolidé deux fois.

⚠️ La consommation FACTURÉE en premier : `billable_usage_by_tool_for_org` (`usage/tools`)
et `org_usage_by_person` (`service.org.usage`) sont comparés sur des fenêtres aux bornes
arbitraires — au milieu d'un jour, à minuit pile, sur l'instant exact d'un appel (borne
incluse ou non selon le lecteur).
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from oto_mcp import db, tenancy
from oto_mcp.db import journal_calls, journal_jour, tenants, usage
from oto_mcp.db._conn import _connect

pytestmark = pytest.mark.usefixtures("live")

ORGS = (1, 2, 3, None)
SUBS = ("u1", "u2", "u3", "u4", "acme:u7", "acme:u8", None)
OUTILS = ("outil_a", "outil_b", "outil_c", "fullenrich_result", "outil_e")
FENETRES_GLISSANTES = (1, 2, 3, 7, 30)


def _c():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)


@pytest.fixture(scope="module")
def jeu(live):
    """Le jeu de données, posé une fois pour le module. Rend les instants remarquables
    (ISO) dont les fenêtres de facturation se servent."""
    rnd = random.Random(1147)
    with _c() as c:
        for t in ("journal_jours_consolides", "tool_calls", "org_members", "orgs", "users"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM tenants WHERE slug <> %s", (tenancy.primary_slug(),))
        c.execute("INSERT INTO tenants (slug, name) VALUES ('acme', 'Acme')")
        for s in SUBS + ("u9",):
            if s:
                c.execute("INSERT INTO users (sub, email, name) VALUES (%s, %s, %s)",
                          (s, f"{s.replace(':', '-')}@exemple.test", s.upper()))
        for o in (1, 2, 3):
            c.execute("INSERT INTO orgs (id, name, created_by) VALUES (%s, %s, 'u1')",
                      (o, f"org {o}"))
        for s, role in (("u1", "admin"), ("u2", "member"), ("u3", "member"), ("u9", "member")):
            c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (1, %s, %s)",
                      (s, role))
        maintenant = c.execute("SELECT now() AS n").fetchone()["n"]
        lignes = []
        for _ in range(3000):
            # Jamais à moins de 30 s d'une borne glissante : le `now()` du banc et celui
            # de la lecture diffèrent de quelques millisecondes.
            while True:
                age = rnd.uniform(1, 12 * 86400)
                if all(abs(age - j * 86400) > 30 for j in FENETRES_GLISSANTES):
                    break
            lignes.append(maintenant - timedelta(seconds=age))
        minuit = maintenant.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for j in range(0, 12):
            lignes.append(minuit - timedelta(days=j))                 # minuit pile
            lignes.append(minuit - timedelta(days=j, microseconds=1))  # la veille, au bout
        for created_at in lignes:
            kind = rnd.choices(("mcp", "connector", "rest", "protocol"), (80, 8, 10, 2))[0]
            outil = rnd.choice(OUTILS)
            args = {}
            if rnd.random() < 0.3:
                args["enrichment_id"] = f"job-{rnd.randint(1, 40)}"
            if rnd.random() < 0.5:
                args[journal_calls.ARGS_CLIENT_KEY] = {"name": rnd.choice(("cli-a", "cli-b"))}
            c.execute(
                """INSERT INTO tool_calls (created_at, kind, org_id, sub, tool, ok, duration_ms,
                                           result_size, quantity, key_mode, args)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (created_at, kind, rnd.choice(ORGS), rnd.choice(SUBS), outil,
                 rnd.random() < 0.85,
                 None if rnd.random() < 0.1 else rnd.randint(1, 5000),
                 None if rnd.random() < 0.4 else rnd.randint(0, 20000),
                 None if rnd.random() < 0.6 else rnd.randint(1, 5),
                 rnd.choice((None, "org", "platform", "user")),
                 psycopg.types.json.Jsonb(args) if args else None))
        exact = c.execute(
            "SELECT created_at FROM tool_calls WHERE kind = 'mcp' AND ok AND org_id = 1 "
            "ORDER BY created_at LIMIT 1 OFFSET 100").fetchone()["created_at"]
        exact2 = c.execute(
            "SELECT created_at FROM tool_calls WHERE kind = 'mcp' AND ok AND org_id = 1 "
            "ORDER BY created_at DESC LIMIT 1 OFFSET 20").fetchone()["created_at"]
    return {"maintenant": maintenant, "minuit": minuit, "exact": exact, "exact2": exact2}


def _consolider(jusqu_a_jours: int):
    """Consolide du premier jour du journal jusqu'à il y a `jusqu_a_jours` jours."""
    with _c() as c:
        c.execute("DELETE FROM journal_jours_consolides")
        au = c.execute("SELECT to_char((now() AT TIME ZONE 'UTC')::date - %s, 'YYYY-MM-DD') "
                       "AS j", (jusqu_a_jours,)).fetchone()["j"]
    for jour in journal_jour.jours_a_consolider(au=au):
        journal_jour.consolider_jour(jour)


@pytest.fixture(params=["veille_en_direct", "veille_consolidee", "reconsolide"])
def registre(request, jeu):
    if request.param == "veille_en_direct":
        _consolider(2)
    else:
        _consolider(1)
    if request.param == "reconsolide":
        for jour in journal_jour.jours_a_consolider(refaire=True)[:3]:
            journal_jour.consolider_jour(jour)
    return request.param


# ── les lectures de RÉFÉRENCE : le journal seul, telles qu'avant #1147 ─────────────


def _ref_tool_call_stats(since_days, org_id=None, sub=None):
    clauses = ["l.kind = 'mcp'", "l.created_at >= NOW() - make_interval(days => %s)"]
    params: list = [since_days]
    if org_id is not None:
        clauses.append("l.org_id = %s"); params.append(org_id)
    if sub is not None:
        clauses.append("l.sub = %s"); params.append(sub)
    w = " AND ".join(clauses)
    with _connect() as conn:
        a = conn.execute(
            f"""
            WITH f AS MATERIALIZED (
                SELECT l.tool, l.ok, l.sub, l.duration_ms, l.result_size,
                       l.args->'{journal_calls.ARGS_CLIENT_KEY}'->>'name' AS client_name,
                       l.created_at::date AS jour
                  FROM tool_calls l WHERE {w}
            )
            SELECT
              (SELECT json_build_object(
                          'total', COUNT(*), 'errors', COUNT(*) FILTER (WHERE NOT ok),
                          'users', COUNT(DISTINCT sub),
                          'served_chars', COALESCE(SUM(result_size), 0),
                          'sized_calls', COUNT(result_size),
                          'emitter_named', COUNT(client_name)) FROM f) AS totals,
              (SELECT COALESCE(json_agg(t ORDER BY t.calls DESC), '[]'::json) FROM (
                  SELECT tool AS tool_name, COUNT(*) AS calls,
                         COUNT(*) FILTER (WHERE NOT ok) AS errors,
                         ROUND(AVG(duration_ms))::int AS avg_ms,
                         ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::int AS p95_ms,
                         COALESCE(SUM(result_size), 0) AS total_chars,
                         ROUND(AVG(result_size))::int AS avg_chars,
                         ROUND(percentile_cont(0.95) WITHIN GROUP (ORDER BY result_size))::int AS p95_chars,
                         COUNT(result_size) AS sized
                    FROM f GROUP BY tool ORDER BY calls DESC LIMIT 100) t) AS by_tool,
              (SELECT COALESCE(json_agg(t ORDER BY t.calls DESC), '[]'::json) FROM (
                  SELECT f.sub, u.email, u.name, COUNT(*) AS calls,
                         COUNT(*) FILTER (WHERE NOT f.ok) AS errors
                    FROM f LEFT JOIN users u ON u.sub = f.sub
                   GROUP BY f.sub, u.email, u.name ORDER BY calls DESC LIMIT 100) t) AS by_user,
              (SELECT COALESCE(json_agg(t ORDER BY t.calls DESC), '[]'::json) FROM (
                  SELECT client_name, COUNT(*) AS calls
                    FROM f GROUP BY 1 ORDER BY calls DESC LIMIT 50) t) AS by_emitter,
              (SELECT COALESCE(json_agg(t ORDER BY t.day), '[]'::json) FROM (
                  SELECT to_char(jour, 'YYYY-MM-DD') AS day, COUNT(*) AS calls,
                         COUNT(*) FILTER (WHERE NOT ok) AS errors
                    FROM f GROUP BY jour) t) AS by_day
            """, tuple(params)).fetchone()
    t = a["totals"]
    return {"since_days": since_days, "total_calls": int(t["total"]),
            "error_count": int(t["errors"]), "active_users": int(t["users"]),
            "served_chars": int(t["served_chars"]), "sized_calls": int(t["sized_calls"]),
            "emitter_named_calls": int(t["emitter_named"]),
            "by_emitter": list(a["by_emitter"]), "by_tool": list(a["by_tool"]),
            "by_user": list(a["by_user"]), "by_day": list(a["by_day"])}


def _ref_billable(org_id, tools=None, since=None, until=None):
    with _connect() as conn:
        since, until = usage._fenetre_du_releve(conn, since, until)
        clauses, params = usage._audit_window_clauses(org_id, since, until)
        clauses.append("l.ok = TRUE")
        if tools:
            clauses.append("l.tool = ANY(%s)"); params.append(list(tools))
        rows = conn.execute(
            f"""SELECT l.tool, l.key_mode, count(*) AS calls,
                       sum(COALESCE(l.quantity, 1)) AS quantity,
                       count(DISTINCT {usage._BILLABLE_JOB_ID_SQL}) AS jobs
                  FROM tool_calls l WHERE {' AND '.join(clauses)}
                 GROUP BY l.tool, l.key_mode ORDER BY l.tool, l.key_mode NULLS LAST""",
            tuple(params)).fetchall()
    return {"since_effectif": since, "until_effectif": until,
            "tools": [{"tool": r["tool"], "key_mode": r["key_mode"], "calls": int(r["calls"]),
                       "quantity": int(r["quantity"]), "jobs": int(r["jobs"])} for r in rows]}


def _ref_usage_by_person(org_id, since, until):
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT c.sub, COUNT(*) AS calls,
                      COUNT(*) FILTER (WHERE c.key_mode = 'platform') AS platform_calls
                 FROM tool_calls c
                WHERE c.org_id = %s AND c.kind = 'mcp' AND c.ok AND c.sub IS NOT NULL
                  AND c.created_at >= %s AND c.created_at < %s
                GROUP BY c.sub ORDER BY calls DESC, c.sub""",
            (int(org_id), since, until)).fetchall()]


def _ref_connector(since_days, org_id=None):
    org = " AND l.org_id = %s" if org_id is not None else ""
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT l.tool AS provider, COUNT(*) AS failures,
                       COUNT(DISTINCT l.sub) AS users_affected, MAX(l.created_at) AS last_at
                  FROM tool_calls l
                 WHERE l.kind = 'connector'
                   AND l.created_at >= NOW() - make_interval(days => %s){org}
                 GROUP BY l.tool ORDER BY failures DESC LIMIT 100""",
            tuple([since_days] + ([org_id] if org_id is not None else []))).fetchall()
    return {"since_days": since_days, "total_failures": sum(int(r["failures"]) for r in rows),
            "by_provider": list(rows)}


def _ref_adoption_lignes(org_id, days):
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT m.sub, u.email, u.name, m.org_role,
                      COALESCE(a.n_calls, 0) AS calls, COALESCE(a.n_errors, 0) AS errors,
                      a.last_call_at, COALESCE(f.n_failures, 0) AS connector_failures
                 FROM org_members m
                 LEFT JOIN users u ON u.sub = m.sub
                 LEFT JOIN LATERAL (
                     SELECT COUNT(*) FILTER (
                                WHERE c.created_at >= NOW() - make_interval(days => %s)) AS n_calls,
                            COUNT(*) FILTER (
                                WHERE c.created_at >= NOW() - make_interval(days => %s)
                                  AND NOT c.ok) AS n_errors,
                            MAX(c.created_at) AS last_call_at
                       FROM tool_calls c
                      WHERE c.kind = 'mcp' AND c.sub = m.sub AND c.org_id = m.org_id) a ON TRUE
                 LEFT JOIN LATERAL (
                     SELECT COUNT(*) AS n_failures FROM tool_calls c
                      WHERE c.kind = 'connector' AND c.sub = m.sub AND c.org_id = m.org_id
                        AND c.created_at >= NOW() - make_interval(days => %s)) f ON TRUE
                WHERE m.org_id = %s ORDER BY calls DESC, u.email""",
            (days, days, days, int(org_id))).fetchall()]


def _ref_appels_par_sub(days, subs=None):
    """Le journal de la fiche d'un tenant (et de sa ligne) : par sub, appels et dernier."""
    par_sub = "" if subs is None else "sub = ANY(%(subs)s) AND "
    with _connect() as conn:
        return {r["sub"]: (int(r["appels"]), r["last_seen_at"]) for r in conn.execute(
            f"""SELECT sub, COUNT(*) AS appels, MAX(created_at) AS last_seen_at
                  FROM tool_calls WHERE {par_sub}kind = 'mcp'
                   AND created_at >= NOW() - make_interval(days => %(days)s)
                 GROUP BY sub""", {"days": days, "subs": subs}).fetchall()}


# ── les comparaisons ───────────────────────────────────────────────────────────────


def _trie(lignes, *cles):
    return sorted(lignes, key=lambda r: tuple(str(r.get(k)) for k in cles))


def _stats_comparables(s):
    return {**s, "by_tool": _trie(s["by_tool"], "tool_name"),
            "by_user": _trie(s["by_user"], "sub"),
            "by_emitter": _trie(s["by_emitter"], "client_name")}


@pytest.mark.parametrize("jours", FENETRES_GLISSANTES + (365,))
@pytest.mark.parametrize("portee", [{}, {"org_id": 1}, {"org_id": 2, "sub": "u2"},
                                    {"sub": "u1"}])
def test_tool_call_stats(registre, jours, portee):
    neuf = db.tool_call_stats(since_days=jours, **portee)
    assert _stats_comparables(neuf) == _stats_comparables(_ref_tool_call_stats(jours, **portee))


def _fenetres_facturables(jeu):
    m, minuit, exact, exact2 = jeu["maintenant"], jeu["minuit"], jeu["exact"], jeu["exact2"]
    iso = lambda d: d.astimezone(timezone.utc).isoformat()  # noqa: E731
    return [
        {},                                                     # la fenêtre maximale
        {"since": iso(m - timedelta(days=5, hours=7))},
        {"since": iso(minuit - timedelta(days=6)), "until": iso(minuit - timedelta(days=2))},
        {"since": iso(exact), "until": iso(exact2)},            # bornes sur un appel
        {"since": iso(m - timedelta(days=9, minutes=13)),
         "until": iso(m - timedelta(days=1, hours=3))},
        {"since": iso(minuit), "until": iso(m)},                # aujourd'hui seul
        {"since": iso(minuit - timedelta(days=1, microseconds=1)),
         "until": iso(minuit - timedelta(microseconds=1))},     # la veille, bornes au bout
    ]


@pytest.mark.parametrize("i", range(7))
@pytest.mark.parametrize("org_id", [1, 2, 3])
@pytest.mark.parametrize("outils", [None, ["fullenrich_result", "outil_b"]])
def test_releve_facturable_par_outil(registre, jeu, i, org_id, outils):
    f = _fenetres_facturables(jeu)[i]
    neuf = db.billable_usage_by_tool_for_org(org_id, tools=outils, **f)
    ref = _ref_billable(org_id, outils, f.get("since"), neuf["until_effectif"])
    assert neuf["tools"] == ref["tools"]
    assert neuf["since_effectif"] == ref["since_effectif"]


@pytest.mark.parametrize("i", range(1, 7))
@pytest.mark.parametrize("org_id", [1, 2])
def test_consommation_par_personne(registre, jeu, i, org_id):
    f = _fenetres_facturables(jeu)[i]
    since = datetime.fromisoformat(f["since"])
    until = datetime.fromisoformat(f["until"]) if "until" in f else jeu["maintenant"]
    neuf = db.org_usage_by_person(org_id, since, until)
    assert neuf == _ref_usage_by_person(org_id, since, until)


@pytest.mark.parametrize("jours", FENETRES_GLISSANTES)
@pytest.mark.parametrize("org_id", [None, 1, 3])
def test_echecs_de_connecteur(registre, jours, org_id):
    neuf = db.connector_failure_stats(since_days=jours, org_id=org_id)
    ref = _ref_connector(jours, org_id)
    assert neuf["total_failures"] == ref["total_failures"]
    assert _trie(neuf["by_provider"], "provider") == _trie(ref["by_provider"], "provider")


@pytest.mark.parametrize("jours", FENETRES_GLISSANTES)
def test_adoption(registre, jours):
    neuf = db.org_adoption(1, active_window_days=jours)
    ref = _ref_adoption_lignes(1, jours)
    assert _trie(neuf["members"], "sub") == _trie(ref, "sub")
    assert neuf["active"] == sum(1 for r in ref if r["calls"])
    assert neuf["blocked_by_connector"] == sum(1 for r in ref if r["connector_failures"])


@pytest.mark.parametrize("jours", FENETRES_GLISSANTES)
def test_tenants(registre, jours):
    ref = _ref_appels_par_sub(jours)
    primaire = tenancy.primary_slug()
    tiers = {s: v for s, v in ref.items() if s and s.startswith("acme:")}
    fiche = db.get_tenant_overview("acme", days=jours)
    assert {c["sub"]: (c["appels"], c["last_seen_at"]) for c in fiche["comptes_recents"]
            if c["appels"]} == tiers
    ligne = next(t for t in db.list_tenants_overview(days=jours) if t["slug"] == "acme")
    assert ligne["appels"] == sum(a for a, _ in tiers.values())
    assert ligne["comptes_actifs"] == len(tiers)
    assert ligne["last_seen_at"] == max((d for _, d in tiers.values()), default=None)
    nus = {s: v for s, v in ref.items() if s and not s.startswith("acme:")}
    prim = db.get_tenant_overview(primaire, days=jours)
    assert {c["sub"]: (c["appels"], c["last_seen_at"]) for c in prim["comptes_recents"]
            if c["appels"]} == nus


# ── les refus : jamais une somme fausse, jamais le journal en silence ──────────────


def test_un_trou_dans_le_registre_est_refuse(jeu):
    _consolider(1)
    with _c() as c:
        c.execute("DELETE FROM journal_jours_consolides WHERE jour = "
                  "(now() AT TIME ZONE 'UTC')::date - 4")
    with pytest.raises(journal_jour.AgregatIncomplet, match="absents du registre"):
        db.tool_call_stats(since_days=7, org_id=1)
    # Une fenêtre qui ne couvre pas le trou est servie.
    assert db.tool_call_stats(since_days=3, org_id=1)["total_calls"] >= 0


def test_une_maintenance_en_retard_est_refusee(jeu):
    _consolider(3)
    with pytest.raises(journal_jour.AgregatIncomplet, match="non consolidés"):
        db.billable_usage_by_tool_for_org(1)
    # La veille seule non consolidée, elle, se lit en direct.
    _consolider(2)
    db.billable_usage_by_tool_for_org(1)


def test_un_historique_non_rattrape_est_refuse(jeu):
    with _c() as c:
        c.execute("DELETE FROM journal_jours_consolides")
    with pytest.raises(journal_jour.AgregatIncomplet, match="aucun jour n.est consolidé"):
        db.connector_failure_stats(since_days=30)
    # Une fenêtre d'un jour ne couvre aucun jour clos : rien à rattraper.
    db.connector_failure_stats(since_days=1)
    # Un rattrapage partiel (les trois derniers jours) : la fenêtre qui remonte plus
    # loin est refusée, celle qui y tient est servie.
    _consolider(1)
    with _c() as c:
        c.execute("DELETE FROM journal_jours_consolides WHERE jour < "
                  "(now() AT TIME ZONE 'UTC')::date - 3")
    with pytest.raises(journal_jour.AgregatIncomplet, match="avant le premier jour"):
        db.connector_failure_stats(since_days=7)
    db.connector_failure_stats(since_days=3)
    _consolider(1)


def test_le_banc_mord(jeu):
    """Le banc lit bien les TOTAUX : un total faussé d'un appel sur un jour consolidé se
    voit dans chaque lecteur, puis disparaît quand le jour est reconsolidé."""
    _consolider(1)
    with _c() as c:
        jour = c.execute(
            "SELECT to_char(jour, 'YYYY-MM-DD') AS j FROM journal_totaux_jour "
            "WHERE kind = 'mcp' AND ok AND org_id = 1 AND sub = 'u1' "
            "  AND jour < (now() AT TIME ZONE 'UTC')::date - 2 "
            "ORDER BY jour DESC LIMIT 1").fetchone()["j"]
        c.execute("UPDATE journal_totaux_jour SET appels = appels + 1, quantite = quantite + 1 "
                  "WHERE ctid = (SELECT ctid FROM journal_totaux_jour WHERE jour = %s "
                  "AND kind = 'mcp' AND ok AND org_id = 1 AND sub = 'u1' LIMIT 1)", (jour,))
    assert db.tool_call_stats(since_days=7, org_id=1)["total_calls"] == \
        _ref_tool_call_stats(7, org_id=1)["total_calls"] + 1
    assert db.billable_usage_by_tool_for_org(1)["tools"] != _ref_billable(1)["tools"]
    journal_jour.consolider_jour(jour)
    assert db.billable_usage_by_tool_for_org(1)["tools"] == _ref_billable(1)["tools"]
