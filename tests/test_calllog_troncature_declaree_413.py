"""Les arguments journalisés : la borne relevée, et toute coupe qui reste DÉCLARÉE
(oto-backend#413).

Décision d'Alexis du 06/09/2026 : « on garde tout, et on protège l'accès au journal »
(l'accès, c'est #563). Le plafond de 300 caractères coupait la valeur fautive d'un
`data_write` refusé, et la coupe était SILENCIEUSE : on croyait lire le corps entier.

Ce qui reste de borne est un arbitrage de COÛT DE STOCKAGE (`tool_calls` porte des
millions de lignes), jamais de confidentialité — et il se lit sur la ligne :
`args._truncated = {"at": <borne>, "sizes": {<argument>: <taille réelle>}}`.

Logique pure : aucune base (convention `CLAUDE.md` §Tests).
"""
from __future__ import annotations

from oto_mcp import calllog


def test_une_valeur_de_1000_caracteres_n_est_plus_coupee():
    """Le cas vécu : la valeur fautive d'un `data_write` était au-delà de 300."""
    corps = "x" * 1000
    args = calllog.truncated_args({"rows": corps}, tool="data_write")
    assert args["rows"] == corps
    assert calllog.ARGS_TRUNCATED_KEY not in args, "rien de coupé, rien à déclarer"


def test_une_coupe_restante_se_DECLARE_avec_la_taille_reelle():
    """« corps tronqué à N, taille réelle M » — sur la ligne, pas dans une doc."""
    borne = calllog.MAX_ARG_CHARS
    args = calllog.truncated_args({"rows": "y" * (borne + 500), "op": "upsert"},
                                  tool="data_write")
    assert args["rows"] == "y" * borne + "…"
    assert args[calllog.ARGS_TRUNCATED_KEY] == {"at": borne,
                                                "sizes": {"rows": borne + 500}}
    assert args["op"] == "upsert"


def test_la_declaration_vaut_pour_une_valeur_composee_stringifiee():
    """Une liste de lignes est stringifiée AVANT d'être mesurée : la taille déclarée est
    celle du texte qu'on aurait écrit, pas un nombre d'éléments."""
    rows = [{"k": i, "v": "z" * 100} for i in range(100)]
    args = calllog.truncated_args({"rows": rows}, tool="data_write")
    assert args[calllog.ARGS_TRUNCATED_KEY]["sizes"]["rows"] == len(str(rows))


def test_le_masquage_s_applique_a_la_valeur_longue():
    """Relever la borne ne rouvre aucun canal : un argument déclaré secret part en
    empreinte quelle que soit sa longueur, à la racine comme en profondeur — y compris
    dans une charge qui aurait été coupée AVANT le secret sous l'ancienne borne."""
    long_secret = "s3cr3t-" * 200
    args = calllog.truncated_args(
        {"op": "connect", "smtp_imap": {"filler": "f" * 350,
                                        "smtp_password": long_secret}},
        tool="lemlist_mailbox")
    assert "s3cr3t-" not in str(args)
    assert calllog.ARGS_TRUNCATED_KEY not in args


def test_la_declaration_ne_porte_que_des_noms_et_des_tailles():
    """Ce qui est ajouté à la ligne ne contient aucune valeur d'argument."""
    args = calllog.truncated_args({"body": "secret-in-body " * 1000}, tool="api_post")
    decl = args[calllog.ARGS_TRUNCATED_KEY]
    assert set(decl) == {"at", "sizes"}
    assert all(isinstance(v, int) for v in decl["sizes"].values())
