"""L'ordre des middlewares MCP est un CONTRAT, pas un détail (2026-08-02).

fastmcp exécute `instance.middleware` dans l'ordre de la liste : premier ajouté =
plus EXTERNE (`_run_middleware` wrap en reversed(), vérifié empiriquement). Les
invariants gardés ici :

- `ToolAliasMiddleware` OUTERMOST absolu — il rétablit le nom CANONIQUE d'un outil
  avant que quoi que ce soit d'autre ne le lise (gates `_org=`, rédaction par
  namespace, visibilité, journal `tool_calls`), et renomme la liste servie en dernier
  (au RETOUR), donc après le filtrage de visibilité. Plus interne, une partie de la
  chaîne verrait le nom du tenant : le journal se scinderait en deux noms pour un
  seul outil, et un gate par namespace tomberait fail-open sur un namespace inconnu.
- `EmptyResultMiddleware` juste dessous — il sert un résultat VIDE en PHRASE, et doit
  donc tourner APRÈS tout ce qui réémet le payload en JSON dans le canal texte (la
  rédaction, l'écho de compte). Plus interne, la structure qu'il vient de retirer du
  texte y serait rétablie par le middleware suivant (oto#32).
- `CallContextMiddleware` sous lui — sa ContextVar `_CALL_ORG` doit rester posée
  pendant que la rédaction ET le calllog (plus internes… donc ajoutés après) relisent
  `current_org`. Ajouté ailleurs, un appel `_org=` est rédigé/audité sous l'org MAISON
  (bug vécu : il était innermost jusqu'au 2026-08-02).
- `FieldRedactionMiddleware` avant le reste : retouche le résultat final en sortie.
- `ErrorEnvelopeMiddleware` plus externe que calllog + Sentry : eux voient l'erreur
  brute, l'enveloppe scrubbe en dernier.
- `UserDisabledToolsMiddleware` plus externe que `ToolCallLogger` : un refus de gate
  n'est pas journalisé.
- `SentryToolErrorMiddleware` INNERMOST : capture le vrai traceback en premier et
  pose `last_event_id` que le calllog (plus externe) stampe sur la ligne tool_calls.
"""
from oto_mcp import server

from _mcp_app import static_mcp as _test_mcp


OURS = [
    "ToolAliasMiddleware",
    "EmptyResultMiddleware",
    "CallContextMiddleware",
    "FieldRedactionMiddleware",
    "ErrorEnvelopeMiddleware",
    "UserDisabledToolsMiddleware",
    "DynamicInstructionsMiddleware",
    "ToolCallLogger",
    "SentryToolErrorMiddleware",
]


def test_mcp_middleware_order_contract():
    # fastmcp préfixe les siens (ex. DereferenceRefsMiddleware) — on fige l'ordre
    # relatif des NÔTRES, pas la liste brute.
    names = [type(m).__name__ for m in _test_mcp().middleware if type(m).__name__ in OURS]
    assert names == OURS, (
        f"Ordre des middlewares modifié : {names}. Premier ajouté = plus EXTERNE — "
        "relire les invariants du docstring avant de changer quoi que ce soit."
    )
