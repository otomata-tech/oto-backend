"""La garde de l'état GLOBAL du processus (#1111) : elle voit, elle nomme, elle remet.

Le 09/10/2026, `csv.field_size_limit` relevée par un banc d'archive et jamais rendue a
désarmé, trois fichiers plus loin dans le même worker, la garde « cellule trop grosse » de
`csv_tolerant` — dans une seule part de la suite. `tests/conftest.py::_0_etat_global_rendu`
compare l'état avant et après chaque test (`tests/_etat_global.py`). Ces bancs tiennent
qu'elle voit un écart, le remet, et passe APRÈS les remises de `monkeypatch`.
"""
from __future__ import annotations

import csv
import logging
import socket
import sys

import _etat_global as eg


def test_un_reglage_change_est_nomme_et_remis():
    avant, dec = eg.releve(), eg.contexte_decimal()
    limite = csv.field_size_limit(10_000_000)
    socket.setdefaulttimeout(3.0)
    logging.disable(logging.CRITICAL)
    try:
        ecarts = eg.ecarts_et_remise(avant, dec)
    finally:
        csv.field_size_limit(limite)
        socket.setdefaulttimeout(None)
        logging.disable(logging.NOTSET)
    noms = {e.split(" :")[0] for e in ecarts}
    assert noms == {"csv.field_size_limit", "socket.getdefaulttimeout", "logging.disable"}
    assert eg.releve() == avant


def test_sys_path_est_remis_sans_etre_reproche():
    avant, dec = eg.releve(), eg.contexte_decimal()
    sys.path.insert(0, ".")
    assert eg.ecarts_et_remise(avant, dec) == []
    assert tuple(sys.path) == avant["sys.path"]


def test_un_etat_inchange_ne_dit_rien():
    avant, dec = eg.releve(), eg.contexte_decimal()
    assert eg.ecarts_et_remise(avant, dec) == []


def test_la_garde_est_installee_avant_monkeypatch(request, monkeypatch):
    """pytest range les fixtures autouse par ordre ALPHABÉTIQUE : si la garde passait après
    `monkeypatch`, elle prendrait ses remises (chdir, syspath_prepend) pour des fuites."""
    noms = request.fixturenames
    assert "_0_etat_global_rendu" in noms
    assert noms.index("_0_etat_global_rendu") < noms.index("monkeypatch")


def test_un_chdir_par_monkeypatch_n_est_pas_une_fuite(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # rendu par monkeypatch avant que la garde ne compare
