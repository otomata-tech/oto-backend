"""Le journal des prêts de compte d'AVANT la bascule : `connector_loan_events`.

Les prêts (`share_side`) sont retirés par la bascule des comptes (#1499, révision
`20261105_1000_bascule_comptes`) : plus rien n'y écrit. La table reste, en LECTURE —
l'historique des prêts d'avant ; le journal des partages d'aujourd'hui est
`connector_account_share_events` (`db/regles_de_comptes`). Une fusion de comptes y fait
encore suivre sa ligne du coffre (`suivre_la_ligne`).
"""
from __future__ import annotations

def suivre_la_ligne(conn, entity_type: str, ancien: str, nouveau: str, connector: str,
                    account: str) -> None:
    """Le journal suit SA ligne du coffre quand une fusion de comptes la rechiffre sous un
    nouveau nom (`credentials_store.rekey_personal_credentials`) : la lecture par ligne
    (`pour_la_ligne`) garde son historique. Une ligne laissée en place garde le sien."""
    conn.execute(
        "UPDATE connector_loan_events SET entity_id = %s WHERE entity_type = %s "
        "AND entity_id = %s AND connector = %s AND account = %s",
        (nouveau, entity_type, ancien, connector, account or ""))


def pour_la_ligne(entity_type: str, entity_id: str, connector: str,
                  account: str = "", limit: int = 50) -> list[dict]:
    """Les gestes sur UNE ligne du coffre, du plus récent au plus ancien."""
    from ._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT at, actor_sub, org_id, entity_type, entity_id, connector, account, "
            "target, action FROM connector_loan_events WHERE entity_type = %s "
            "AND entity_id = %s AND connector = %s AND account = %s "
            "ORDER BY id DESC LIMIT %s",
            (entity_type, entity_id, connector, account or "", int(limit))).fetchall()
