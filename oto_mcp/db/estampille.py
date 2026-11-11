"""LE point de passage des écritures de ligne : la transaction qui porte l'estampille
(oto#273, M2).

Toute écriture de `datastore_rows.data` du serveur s'ouvre ici. Une transaction, et
dedans, avant la première écriture, les six réglages que lit le déclencheur du
journal (`journal_revisions._FONCTION`) : `oto.acteur`, `oto.run_id`, `oto.source`,
`oto.geste_id`, `oto.outil`, `oto.fournisseur`. Leurs valeurs viennent du geste en cours (`oto_mcp.geste`).

**`set_config(…, true)`, l'équivalent de `SET LOCAL`** : la valeur meurt avec la
transaction. Une connexion du pool sert tout le monde ; un `SET` de session suivrait la
connexion chez l'appelant suivant.

**Une transaction explicite, même en autocommit** (`reuse_connection`) : sans elle, le
réglage local mourrait avec sa propre requête, avant l'écriture qu'il doit estampiller.

Ce que ce point ne couvre pas, et c'est voulu : une écriture qui ne passe pas par le
serveur (SQL à la main, migration, ancien code pendant une bascule bleu/vert) n'a aucun
réglage posé, et le journal porte `source` NULL — « écrit hors du serveur », pas
`system`. `tests/datastore/test_estampille_273.py` garde qu'aucune requête du paquet
qui écrit `data` n'échappe à ce point.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from .. import geste
from ._conn import _connect

_POSER = ("SELECT set_config('oto.acteur', %(acteur)s, true), "
          "set_config('oto.run_id', %(run_id)s, true), "
          "set_config('oto.source', %(source)s, true), "
          "set_config('oto.geste_id', %(geste_id)s, true), "
          "set_config('oto.outil', %(outil)s, true), "
          "set_config('oto.fournisseur', %(fournisseur)s, true)")


@contextmanager
def ecriture_de_lignes() -> Iterator[psycopg.Connection]:
    """Une connexion, UNE transaction, l'estampille posée dedans. Le code qui écrit
    des lignes n'ouvre ni `_connect()` ni `transaction()` lui-même : il ouvre ceci."""
    valeurs = {k: ("" if v is None else str(v)) for k, v in geste.estampille().items()}
    with _connect() as conn:
        with conn.transaction():
            conn.execute(_POSER, valeurs)
            yield conn
