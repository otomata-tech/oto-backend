"""Garde LECTURE SEULE op-aware (consultation d'org / view-as). Le dashboard LIT en
POST `{op:…}` : la garde ne peut pas se baser sur la méthode HTTP, elle lit l'`op` du
corps (`_peek_op`) et n'autorise que les ops de lecture (`_READ_OPS`), en rejouant le
corps intact au handler aval.
"""
import json

import pytest

from oto_mcp.api.routes import _peek_op, _READ_OPS


def _single_body(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}
    return receive


@pytest.mark.asyncio
async def test_extracts_read_op_and_replays_body():
    body = json.dumps({"op": "list", "project_id": 8}).encode()
    op, replay = await _peek_op(_single_body(body))
    assert op == "list" and op in _READ_OPS
    assert (await replay())["body"] == body  # corps rejoué INTACT au handler


@pytest.mark.asyncio
async def test_write_op_not_in_read_set():
    op, _ = await _peek_op(_single_body(b'{"op":"create","name":"x"}'))
    assert op == "create" and op not in _READ_OPS  # → rejeté par le middleware


@pytest.mark.asyncio
async def test_no_op_is_treated_as_write():
    # POST sans `op` (ex. setLocale {locale}, upload) → op=None ∉ _READ_OPS → rejeté.
    op, _ = await _peek_op(_single_body(b'{"locale":"fr"}'))
    assert op is None and op not in _READ_OPS


@pytest.mark.asyncio
async def test_malformed_body_is_write():
    op, _ = await _peek_op(_single_body(b'not json'))
    assert op is None and op not in _READ_OPS


@pytest.mark.asyncio
async def test_multi_chunk_body_reassembled():
    chunks = [
        {"type": "http.request", "body": b'{"op":', "more_body": True},
        {"type": "http.request", "body": b'"get"}', "more_body": False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)
    op, replay = await _peek_op(receive)
    assert op == "get"
    # rejeu : les deux chunks reviennent dans l'ordre
    assert (await replay())["body"] == b'{"op":'
    assert (await replay())["body"] == b'"get"}'


def test_read_ops_cover_dashboard_reads():
    for o in ("list", "get", "search", "revisions", "inventory", "list_templates",
              "state"):  # `state` : compteurs d'une campagne (oto#221)
        assert o in _READ_OPS


# ── Chaque op est CLASSÉE (oto#221) ─────────────────────────────────────────
# `_READ_OPS` est une liste à la main, par NOM d'op, commune à toutes les routes :
# `fleets op=state` y manquait, et les compteurs d'une campagne étaient illisibles en
# consultation. Ce test classe chaque op de chaque capacité op-aware (corps `{op}`
# sur une route non-GET) : ce qui n'est pas une lecture est nommé ici comme ÉCRITURE.
# Une op neuve non classée rougit — on décide, on ne découvre pas en production.
ECRITURES = {
    "admin.account": {"suspend", "resume"},
    "admin.outreach": {"test", "send", "optout_clear"},
    "me.doc": {"create", "bulk_create", "update", "patch", "delete", "move", "revert",
               "set_public"},
    "me.function": {"create", "propose", "run", "test", "publish", "refuse"},
    "me.kb": {"create", "ensure"},
    "me.node.edit": {"create", "update", "move", "delete"},
    "me.project": {"create", "update", "archive", "unarchive", "copy", "link", "unlink",
                   "publish_mcp", "unpublish_mcp"},
    "platform.connector.setting": {"reload", "clear", "set"},
    "platform.runner.worker": {"create", "revoke"},
    "resources.govern": {"share", "unshare", "transfer"},
    "resources.govern.v2": {"share", "unshare", "transfer"},
    "runner.fleets": {"create", "update", "launch", "stop", "take", "beat", "ack_stop"},
    "runner.jobs": {"enqueue", "claim", "bind_run", "extend", "complete"},
    "runner.triggers": {"create", "update", "delete", "clear_queue", "rotate_secret",
                        "take_over", "rotate_address"},
    "runs.thread": {"append"},
    "usage.notify_reporters": {"send"},
}

# Capacités dont l'`op` est un texte LIBRE (pas un `Literal`) : leurs ops ne se lisent
# pas dans le schéma, elles sont donc énumérées ici, à la main et en entier.
OPS_LIBRES = {
    "platform.connector.setting": {"list", "reload", "clear", "set"},
}


def _ops_des_capacites() -> dict[str, set]:
    """{clé de capacité : ops} pour chaque capacité dont une route non-GET lit `op`."""
    import typing

    from oto_mcp.capabilities import registry

    def valeurs(annotation) -> set:
        if typing.get_origin(annotation) is typing.Literal:
            return set(typing.get_args(annotation))
        return set().union(*(valeurs(a) for a in typing.get_args(annotation)))

    out: dict[str, set] = {}
    for cap in registry.CAPABILITIES:
        champ = cap.Input.model_fields.get("op")
        if champ is None or all(b.verb == "GET" for b in cap.rest_bindings()):
            continue
        ops = valeurs(champ.annotation)
        if not ops:
            assert cap.key in OPS_LIBRES, (
                f"{cap.key} : `op` n'est pas un Literal — énumérer ses ops dans "
                "OPS_LIBRES, sinon ce test ne peut pas les classer")
            ops = OPS_LIBRES[cap.key]
        out[cap.key] = ops
    return out


def test_chaque_op_est_classee_lecture_ou_ecriture():
    ops = _ops_des_capacites()
    assert ops.keys() >= OPS_LIBRES.keys(), "OPS_LIBRES nomme une capacité disparue"
    for cle, les_ops in ops.items():
        ecritures = ECRITURES.get(cle, set())
        assert ecritures <= les_ops, f"{cle} : écritures classées inconnues {ecritures - les_ops}"
        ambigues = ecritures & _READ_OPS
        assert not ambigues, (
            f"{cle} : {sorted(ambigues)} est une ÉCRITURE ici et une lecture dans "
            "`_READ_OPS` — la liste est par NOM : renommer l'op, jamais l'ouvrir")
        non_classees = les_ops - ecritures - _READ_OPS
        assert not non_classees, (
            f"{cle} : ops ni lecture ni écriture {sorted(non_classees)} — lecture pure "
            "⇒ `_READ_OPS` (oto_mcp/api/routes.py), sinon ⇒ ECRITURES ci-dessus")


def test_READ_OPS_ne_porte_que_des_lectures_servies():
    """Une entrée que plus aucune capacité ne sert est une porte ouverte à la
    prochaine op de ce nom, lecture ou pas : elle sort de la liste."""
    servies = set().union(*_ops_des_capacites().values())
    assert not _READ_OPS - servies, f"lectures sans capacité : {sorted(_READ_OPS - servies)}"
