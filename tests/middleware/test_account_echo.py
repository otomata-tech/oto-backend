"""L'agent doit savoir SOUS QUEL compte son appel est parti.

Avec deux workspaces Slack, un agent en visait un — défaut posé, épinglage de projet
ou `_account=` — sans que rien ne le lui confirme : l'identité effective ne vivait que
dans le journal, qu'il ne lit pas. Un message envoyé sous la mauvaise identité ne se
rattrape pas.

L'écho est volontairement DISCRET : il n'apparaît que là où il y a un choix (un compte
NOMMÉ), et jamais pour un compte auxiliaire résolu par un outil composite.
"""
from __future__ import annotations

import pytest

from oto_mcp import redaction, session_org
from oto_mcp.middleware import call_context


class _Result:
    """Ce que rend un tool FastMCP, réduit aux deux canaux qui portent la donnée."""

    def __init__(self, payload, is_error: bool = False):
        self.structured_content = payload
        self.content = []
        self.is_error = is_error


@pytest.fixture()
def trace():
    tok = session_org.set_call_trace({})
    yield session_org.current_call_trace()
    session_org.reset_call_trace(tok)


def _payload(result):
    return redaction.extract_payload(result)


def test_le_compte_nomme_est_annonce(trace):
    session_org.note_call_trace(resolved_connector="slack", resolved_account="client-x")
    out = call_context._echo_account(_Result({"ok": True, "ts": "1.2"}), "slack_post_message")
    assert _payload(out) == {"ok": True, "ts": "1.2", "_account": "client-x"}


def test_mono_compte_aucun_echo(trace):
    """La ligne du coffre est anonyme tant qu'il n'y a qu'un compte : rien à choisir,
    donc rien à annoncer — 99 % des réponses ne changent pas d'un octet."""
    session_org.note_call_trace(resolved_connector="slack", resolved_account="")
    out = call_context._echo_account(_Result({"ok": True}), "slack_post_message")
    assert _payload(out) == {"ok": True}


def test_un_credential_auxiliaire_n_est_pas_annonce(trace):
    """Un outil composite peut résoudre un AUTRE connecteur en chemin. Annoncer ce
    compte-là ferait croire que l'appel est parti sous lui."""
    session_org.note_call_trace(resolved_connector="google", resolved_account="perso")
    out = call_context._echo_account(_Result({"ok": True}), "slack_post_message")
    assert _payload(out) == {"ok": True}


def test_une_erreur_est_rendue_telle_quelle(trace):
    session_org.note_call_trace(resolved_connector="slack", resolved_account="client-x")
    err = _Result({"error": "channel_not_found"}, is_error=True)
    assert call_context._echo_account(err, "slack_post_message") is err


def test_un_payload_non_dict_est_rendu_tel_quel(trace):
    session_org.note_call_trace(resolved_connector="slack", resolved_account="client-x")
    liste = _Result([{"id": 1}, {"id": 2}])
    assert call_context._echo_account(liste, "slack_read_history") is liste


def test_hors_appel_mcp_aucun_relevé():
    """Le relevé n'existe pas en REST/stdio : l'écho doit être inerte, pas lever."""
    out = call_context._echo_account(_Result({"ok": True}), "slack_post_message")
    assert _payload(out) == {"ok": True}


def test_un_champ_metier_du_meme_nom_n_est_jamais_ecrase(trace):
    session_org.note_call_trace(resolved_connector="slack", resolved_account="client-x")
    out = call_context._echo_account(_Result({"_account": "à moi"}), "slack_post_message")
    assert _payload(out) == {"_account": "à moi"}


# --- Le dispatch universel (`oto_call`) -------------------------------------

def test_l_echo_suit_l_outil_VISE_par_oto_call(trace):
    """`oto_call` dispatche un autre outil : c'est SON `name` qui dit le connecteur.

    Sans ça, l'écho se taisait là où il sert le plus — le schéma étant figé au
    handshake, une session ouverte avant la pose d'un second compte n'a que
    `oto_call` pour viser un workspace."""
    session_org.note_call_trace(resolved_connector="slack", resolved_account="client-x")
    assert call_context._cible("oto_call", {"name": "slack_list_channels"}) == "slack_list_channels"
    out = call_context._echo_account(_Result({"channels": []}),
                                   call_context._cible("oto_call", {"name": "slack_list_channels"}))
    assert _payload(out) == {"channels": [], "_account": "client-x"}


def test_oto_call_sans_cible_lisible_reste_lui_meme(trace):
    """Un appel malformé ne doit pas faire deviner un connecteur au hasard."""
    assert call_context._cible("oto_call", {}) == "oto_call"
    assert call_context._cible("oto_call", {"name": 42}) == "oto_call"


def test_un_outil_direct_n_est_pas_reinterprete(trace):
    assert call_context._cible("slack_post_message", {"name": "autre_chose"}) == "slack_post_message"


# --- Le porteur et le compte PRÊTÉ (Microsoft) --------------------------------
#
# Mesuré : une emprunteuse opérait la boîte d'une collègue sans le savoir. Le compte
# propre se résout sous la carte du service (`outlook`), le prêt sous la ligne du porteur
# (`microsoft`) : l'égalité des noms nus taisait l'écho précisément pour le prêt.

def test_un_compte_resolu_sous_le_porteur_est_annonce_sur_l_outil_du_service(trace):
    session_org.note_call_trace(resolved_connector="microsoft",
                                resolved_account="jane@contoso.example")
    out = call_context._echo_account(_Result({"ok": True}), "outlook",
                                     rappel={"_account": "jane@contoso.example",
                                             "lent_by": "preteuse@contoso.example"})
    assert _payload(out) == {"ok": True, "_account": "jane@contoso.example",
                             "lent_by": "preteuse@contoso.example"}


def test_un_compte_propre_sous_la_carte_n_a_pas_de_lent_by(trace):
    session_org.note_call_trace(resolved_connector="outlook",
                                resolved_account="moi@fabrikam.example")
    out = call_context._echo_account(_Result({"ok": True}), "outlook_calendar_event")
    assert _payload(out) == {"ok": True, "_account": "moi@fabrikam.example"}


def test_un_porteur_etranger_reste_tu(trace):
    session_org.note_call_trace(resolved_connector="microsoft", resolved_account="jane")
    out = call_context._echo_account(_Result({"ok": True}), "gmail",
                                     rappel={"_account": "jane@contoso.example",
                                             "lent_by": "preteuse@contoso.example"})
    assert _payload(out) == {"ok": True}


def test_le_preteur_est_lu_dans_la_ligne_qui_a_servi():
    """La ligne MEMBRE d'un autre sub = un prêt ; la sienne (même dans une autre org),
    une clé d'équipe ou d'org = pas de prêteur."""
    from oto_mcp import access
    pret = ("member", "8:preteuse", "microsoft", "j")
    assert access.preteur_de_la_ligne("emprunteuse", pret) == "preteuse"
    assert access.preteur_de_la_ligne("preteuse", pret) is None
    assert access.preteur_de_la_ligne("emprunteuse",
                                      ("member", "9:emprunteuse", "unipile", "")) is None
    assert access.preteur_de_la_ligne("emprunteuse", ("org", "8", "slack", "x")) is None


def _rappel(monkeypatch, sub):
    import asyncio

    from oto_mcp import db
    monkeypatch.setattr(call_context, "current_user_sub_from_token", lambda: sub)
    monkeypatch.setattr(db, "emails_by_subs",
                        lambda subs: {"titulaire": "titulaire@contoso.example"})
    return asyncio.run(call_context._rappel_de_l_appel())


def test_le_rappel_du_compte_d_une_autre_personne_la_nomme_par_son_email(trace, monkeypatch):
    """Another person's account served (named, at its rule's level, #1499): the response
    says whose it is. No `lent_via_group` any more: the loan to a team is gone, the
    level comes from a rule and the echo names the owner only."""
    session_org.note_call_trace(resolved_account="j",
                                credential_row=("member", "8:titulaire", "microsoft", "j"))
    assert _rappel(monkeypatch, "collegue") == {
        "_account": "j", "lent_by": "titulaire@contoso.example"}


def test_le_rappel_d_un_compte_propre_ne_lit_rien(trace, monkeypatch):
    import asyncio

    from oto_mcp import db
    monkeypatch.setattr(call_context, "current_user_sub_from_token", lambda: "moi")
    monkeypatch.setattr(db, "emails_by_subs", lambda subs: pytest.fail("requête inutile"))
    session_org.note_call_trace(resolved_account="j",
                                credential_row=("member", "8:moi", "microsoft", "j"))
    assert asyncio.run(call_context._rappel_de_l_appel()) == {"_account": "j"}


def test_une_cle_d_org_ne_fait_aucun_saut(trace, monkeypatch):
    import asyncio
    monkeypatch.setattr(call_context, "run_in_threadpool",
                        lambda *a, **k: pytest.fail("saut inutile"))
    session_org.note_call_trace(resolved_account="client-x",
                                credential_row=("org", "8", "slack", "client-x"))
    assert asyncio.run(call_context._rappel_de_l_appel()) == {"_account": "client-x"}
