"""Fédération de compte oto → memento (otomata#16).

Un compte créé sur oto = un compte créé sur memento. oto (identité Logto) et
memento (identité Supabase) ne partagent pas d'IdP : la **jointure est l'email**.
Quand un compte oto naît (1er `db.upsert_user`), on demande à memento de
provisionner le compte correspondant via son endpoint serveur-à-serveur
`POST /api/federation/provision` (secret partagé), qui appelle en interne son
`ensureAccount(email)` (GoTrue `/invite` : crée `auth.users` + org perso, ou
no-op si l'email existe déjà). memento reste **propriétaire** de la création de
ses comptes — oto ne fait que la demander (frontière nette, pas de service_role
côté oto).

Best-effort, **jamais bloquant** : la création du compte oto ne doit jamais
échouer ni ralentir parce que memento est indisponible — d'où le thread daemon
et les exceptions seulement loggées.

Config (fédération **désactivée** si le bearer est absent) :
- `MEMENTO_PROVISION_BEARER` — secret partagé avec memento (sans lui : no-op).
- `MEMENTO_PROVISION_URL`    — défaut `https://me.mento.cc/api/federation/provision`.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("oto_mcp.memento_federation")

_DEFAULT_URL = "https://me.mento.cc/api/federation/provision"
_TIMEOUT = 10  # s — court : le compte oto est déjà créé, ceci est un effet de bord


def _enabled() -> bool:
    return bool(os.environ.get("MEMENTO_PROVISION_BEARER"))


def _provision(email: str) -> None:
    import requests

    url = os.environ.get("MEMENTO_PROVISION_URL", _DEFAULT_URL)
    bearer = os.environ.get("MEMENTO_PROVISION_BEARER")
    try:
        r = requests.post(
            url,
            json={"email": email},
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            timeout=_TIMEOUT,
        )
        if r.ok:
            try:
                provisioned = r.json().get("provisioned")
            except Exception:
                provisioned = None
            log.info("memento: compte fédéré pour %s (provisioned=%s)", email, provisioned)
        else:
            log.warning("memento: provision %s → HTTP %s", email, r.status_code)
    except Exception as e:  # réseau, timeout, DNS… → jamais fatal
        log.warning("memento: provision %s échouée (%s)", email, e)


def provision_async(sub: str, email: str) -> None:
    """Provisionne le compte memento par email, en tâche de fond.

    No-op silencieux si la fédération n'est pas configurée (`MEMENTO_PROVISION_BEARER`
    absent) ou si l'email est vide. Ne lève jamais, ne bloque jamais l'appelant.
    """
    if not _enabled() or not email:
        return
    threading.Thread(
        target=_provision, args=(email,), daemon=True, name="memento-provision"
    ).start()
