"""`oto_procedure_app` reçoit `_org=` et `_group=`, comme `oto_procedure op=get`.

Écrit à la main (`@mcp.tool` brut), il n'est ni une capacité (qui reçoit `_org=` par
`_mcp_adapter`) ni un tool de travail : sans entrée explicite, aucun axe d'org ne lui
était advertisé, et un compte multi-org ne pouvait montrer que les procédures de son
org maison — alors qu'`oto_procedure op=get` venait de les lire sous `_org=`.
"""
from oto_mcp import call_axes


def _params(name: str) -> set[str]:
    return {a.param for a in call_axes.axes_for_call(name)}


def test_procedure_app_takes_org_and_group():
    params = _params("oto_procedure_app")
    assert "_org" in params
    assert "_group" in params


def test_procedure_app_advertises_org_in_listing():
    params = {a.param for a in call_axes.axes_for_listing("oto_procedure_app", {})}
    assert "_org" in params


def test_other_meta_tools_unchanged():
    # L'entrée est nominative : elle n'ouvre pas `_group` à tout le spine `oto_*`
    # (`_org`, lui, est accepté partout — `tests/test_jetons_de_contexte_acceptes.py`).
    assert "_group" not in _params("oto_create_org")


# ── #1355 : la notice exige `_run_id` sur chaque appel d'un run ───────────────

def test_procedure_et_email_send_prennent_le_run():
    """Créer une procédure ou envoyer un mail pendant un run : l'agent qui suit la
    notice passait `_run_id` et se faisait refuser une « propriété en trop »."""
    assert "_run_id" in _params("oto_procedure")
    assert "_run_id" in _params("email_send")


def test_email_send_prend_lorg_de_lappel():
    """Il envoie depuis une adresse de l'org ACTIVE : sans l'axe, l'org maison."""
    assert "_org" in _params("email_send")
    assert "_org" in {a.param for a in call_axes.axes_for_listing("email_send", {})}
