"""Le store de l'agent intégré dans Slack (`db/copilot.py`, fragment `schema/copilot.py::SLACK`).

Sur une vraie base : ce que ces fonctions promettent tient aux contraintes (la clé
`(channel, ts)` qui fait qu'un seul événement prend un message, l'unicité par personne et
par fil, la cascade avec la conversation), qu'un stub ne montrerait pas.
"""
from __future__ import annotations

import pytest

from oto_mcp.db import copilot as store
from oto_mcp.db._conn import _connect


@pytest.fixture
def fil(live):
    """Une conversation (la clé étrangère l'exige), retirée à la sortie."""
    store.creer("thr_slack_t1", "ada-sub", 1, None, "sys", "m")
    yield "thr_slack_t1"
    with _connect() as c:
        c.execute("DELETE FROM copilot_threads WHERE id LIKE 'thr_slack_t%'")
        c.execute("DELETE FROM copilot_slack_events WHERE channel = 'C_T'")


def test_un_message_n_est_pris_qu_une_fois(live, fil):
    assert store.slack_reclamer("C_T", "1.1") is True
    assert store.slack_reclamer("C_T", "1.1") is False
    assert store.slack_reclamer("C_T", "1.2") is True
    assert store.slack_reclamer("C_AUTRE", "1.1") is True, "un ts est unique PAR canal"


def test_la_conversation_se_retrouve_par_fil_et_par_personne(live, fil):
    assert store.slack_fil_existe("T1", "C_T", "1.1") is False
    store.slack_lier(fil, "T1", "C_T", "1.1", "ada-sub")
    assert store.slack_fil_existe("T1", "C_T", "1.1") is True
    assert store.slack_fil_existe("T1", "C_T", "9.9") is False
    ligne = store.slack_fil("T1", "C_T", "1.1", "ada-sub")
    assert ligne["thread_id"] == fil and 0 <= ligne["inactif_min"] < 1
    assert store.slack_fil("T1", "C_T", "1.1", "someone-else") is None
    assert store.slack_fil("T2", "C_T", "1.1", "ada-sub") is None
    assert store.slack_de(fil) == {"team_id": "T1", "channel": "C_T", "thread_ts": "1.1",
                                   "sub": "ada-sub"}
    assert store.slack_de("thr_absent") is None


def test_une_conversation_neuve_remplace_celle_du_meme_fil_et_de_la_meme_personne(live, fil):
    store.creer("thr_slack_t2", "ada-sub", 1, None, "sys", "m")
    store.slack_lier(fil, "T1", "C_T", "", "ada-sub")
    store.slack_lier("thr_slack_t2", "T1", "C_T", "", "ada-sub")
    assert store.slack_fil("T1", "C_T", "", "ada-sub")["thread_id"] == "thr_slack_t2"
    assert store.slack_de(fil) is None


def test_la_conversation_emporte_son_rangement(live, fil):
    store.slack_lier(fil, "T1", "C_T", "1.1", "ada-sub")
    with _connect() as c:
        c.execute("DELETE FROM copilot_threads WHERE id = %s", (fil,))
    assert store.slack_fil_existe("T1", "C_T", "1.1") is False


def test_la_purge_retire_les_messages_pris_trop_vieux_et_garde_les_recents(live, fil):
    store.slack_reclamer("C_T", "old")
    store.slack_reclamer("C_T", "new")
    with _connect() as c:
        c.execute("UPDATE copilot_slack_events SET created_at = NOW() - INTERVAL '3 days' "
                  "WHERE channel = 'C_T' AND ts = 'old'")
    store.purger(30)
    assert store.slack_reclamer("C_T", "old") is True, "le vieux a été retiré"
    assert store.slack_reclamer("C_T", "new") is False, "le récent est gardé"
