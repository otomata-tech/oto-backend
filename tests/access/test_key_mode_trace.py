"""`tool_calls.key_mode` — sous quelle clé l'appel est passé.

Le fait est posé au RÉSOLVEUR UNIQUE (ADR 0024), pas dans les tools : c'est ce qui
le rend vrai pour les ~15 connecteurs keyed sans travail par connecteur, et vrai
gratuitement pour celui qu'on ajoutera demain. Ces bancs tiennent ce contrat.

Ce que le mode décide, côté consommateur (la facturation du partenaire) : seul `platform` est
facturé. Un client sur SA propre clé paie déjà le fournisseur — lui compter des
crédits en plus n'aurait pas de sens.
"""
from unittest.mock import patch

from oto_mcp.access import resolve
from oto_mcp.access.resolved_credential import ResolvedCredential


def _rc(mode: str, is_platform: bool) -> ResolvedCredential:
    return ResolvedCredential("fullenrich", "sk-test", is_platform, mode,
                              "org", "42", None)


def test_le_resolveur_verse_le_MODE_au_releve_de_l_appel():
    for mode, is_platform in [("platform", True), ("org", False),
                              ("user", False), ("group", False), ("tenant", False)]:
        with patch("oto_mcp.access.resolve.session_org.note_call_trace") as trace, \
             patch("oto_mcp.access.resolve.instance_refs.ref_for_credential",
                   return_value="ref-1"):
            resolve._note_resolved_instance(_rc(mode, is_platform))
        assert trace.call_args.kwargs["key_mode"] == mode, mode


def test_c_est_le_mode_qui_est_trace_PAS_le_booleen():
    """`is_platform` écrase user/group/org/tenant en un seul « non ». Une facture
    peut avoir à distinguer une clé d'ORG d'une clé de MEMBRE — la colonne garde
    donc l'origine, pas le résumé."""
    modes = set()
    for mode in ("user", "group", "org", "tenant"):
        with patch("oto_mcp.access.resolve.session_org.note_call_trace") as trace, \
             patch("oto_mcp.access.resolve.instance_refs.ref_for_credential",
                   return_value="ref-1"):
            resolve._note_resolved_instance(_rc(mode, False))
        modes.add(trace.call_args.kwargs["key_mode"])
    # quatre origines, quatre valeurs distinctes — pas quatre fois `False`
    assert modes == {"user", "group", "org", "tenant"}


def test_un_releve_qui_echoue_fait_echouer_la_resolution():
    """Le relevé DÉCIDE (`key_mode` facture, `credential_row` désigne qui paie un run
    Apify) : avalé, son échec rendait leur verdict aléatoire. Il lève."""
    import pytest
    with patch("oto_mcp.access.resolve.instance_refs.ref_for_credential",
               side_effect=RuntimeError("bug de projection")), \
            pytest.raises(RuntimeError, match="bug de projection"):
        resolve._note_resolved_instance(_rc("org", False))


def test_la_ligne_qui_a_servi_est_toujours_posee_meme_sans_ref_projetable():
    """Une ligne dont la ref ne se projette pas (résidu `user`) pose quand même son
    `credential_row` et son `key_mode` : leurs lecteurs ne dépendent pas de la ref."""
    from oto_mcp import session_org
    rc = ResolvedCredential("apify", "sk-test", False, "user", "user", "sub-x", "")
    tok = session_org.set_call_trace({})
    try:
        resolve._note_resolved_instance(rc)
        trace = session_org.current_call_trace()
    finally:
        session_org.reset_call_trace(tok)
    assert "instance" not in trace
    assert trace["key_mode"] == "user"
    assert trace["credential_row"] == ("user", "sub-x", "apify", "")


def test_hors_appel_mcp_le_releve_ne_pose_rien_et_ne_leve_pas():
    rc = _rc("org", False)
    assert resolve._note_resolved_instance(rc) is rc
