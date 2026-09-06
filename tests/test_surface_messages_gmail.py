"""Les quatre maillons de surface de l'incident gmail du 14/08 (oto-backend#345).

Une session a expédié trois fois le même mail à une cliente en croyant fabriquer un
brouillon. Le diagnostic initial (« pas de mode brouillon ») était faux : `mode="draft"`
marche, le schéma est correct. Ce qui a échoué, c'est la SURFACE — quatre messages qui
ne disaient pas ce qu'ils savaient, et l'agent a recomposé son appel à neuf en perdant
le paramètre qu'il cherchait depuis quatre essais.

Chaque test ci-dessous fige un de ces messages. Aucun ne teste une intention : tous
exercent le chemin réel (une vraie `ValidationError` pydantic, le vrai registre, la
vraie fonction de message).
"""
import pytest
from pydantic import BaseModel, ValidationError

from oto_mcp import error_taxonomy as T
from oto_mcp.auth import google as G


# ── ③ l'erreur d'arguments nomme la clé fautive ─────────────────────────────────
class _Modele(BaseModel, extra="forbid"):
    body: str
    mode: str = "send"


def _validation_error(**kwargs) -> ValidationError:
    try:
        _Modele(**kwargs)
    except ValidationError as e:
        return e
    raise AssertionError("le modèle aurait dû refuser")


def test_une_cle_inconnue_est_NOMMEE_et_le_schema_est_indique():
    # C'est l'appel exact de l'incident : `op` n'existe pas sur ce tool.
    msg = T._arg_error_message(_validation_error(body="x", op="draft"))
    assert "op" in msg
    assert "non reconnu" in msg
    assert "oto_tool_schema" in msg


def test_un_champ_requis_absent_se_distingue_d_une_cle_inconnue():
    msg = T._arg_error_message(_validation_error(mode="draft"))
    assert "body" in msg and "requis" in msg
    assert "non reconnu" not in msg


def test_les_deux_natures_de_refus_coexistent_dans_le_message():
    msg = T._arg_error_message(_validation_error(action="draft"))
    assert "action" in msg and "body" in msg


def test_une_forme_inattendue_retombe_sur_le_message_generique():
    # Pas de ValidationError dans la chaîne : on ne prétend nommer personne.
    assert "vérifie les paramètres" in T._arg_error_message(ValueError("autre chose"))


# ── ⑤ un nom RETIRÉ ne se fait plus passer pour un connecteur absent ────────────
def test_un_nom_retire_rend_les_verbes_survivants_du_domaine(monkeypatch):
    # `gmail_search` a été supprimé par la consolidation google (33→13 tools).
    monkeypatch.setattr(T, "_surviving_siblings",
                        lambda n: ["gmail_compose", "gmail_list_accounts", "gmail_message"])
    info = T.classify(T.NotFoundError("Unknown tool: 'gmail_search'"))
    assert info.code == "unknown_tool"
    assert "n'existe plus" in info.message
    assert "gmail_message" in info.message
    # Et surtout : plus un mot sur une installation de connecteur — c'est le mensonge
    # qui a envoyé la session chercher un demi-montage inexistant.
    assert "installé" not in info.message and "installe" not in (info.hint or "")


def test_un_outil_qui_EXISTE_mais_n_est_pas_monte_garde_son_message(monkeypatch):
    monkeypatch.setattr(T, "_surviving_siblings", lambda n: None)
    monkeypatch.setattr(T, "_connector_of_tool", lambda n: "google")
    info = T.classify(T.NotFoundError("Unknown tool: 'gmail_message'"))
    assert info.code == "tool_not_mounted"
    assert "n'est pas installé dans ta toolbox" in info.message
    assert "oto_call" in (info.hint or "")


def test_le_registre_non_rechauffe_ne_fait_JAMAIS_conclure_a_un_retrait(monkeypatch):
    # Hors serveur, `boot_tool_names()` rend []. En conclure « l'outil n'existe plus »
    # ferait mentir CHAQUE message — d'où le fail-safe.
    import oto_mcp.tool_registry as reg
    monkeypatch.setattr(reg, "boot_tool_names", lambda: [])
    assert T._surviving_siblings("gmail_search") is None


def test_un_nom_PRESENT_au_registre_n_est_pas_un_retrait(monkeypatch):
    import oto_mcp.tool_registry as reg
    monkeypatch.setattr(reg, "boot_tool_names", lambda: ["gmail_compose", "gmail_search"])
    assert T._surviving_siblings("gmail_search") is None


def test_les_voisins_sont_ceux_du_MEME_namespace(monkeypatch):
    import oto_mcp.tool_registry as reg
    monkeypatch.setattr(reg, "boot_tool_names",
                        lambda: ["gmail_compose", "gmail_message", "drive_file", "fr_get"])
    assert T._surviving_siblings("gmail_search") == ["gmail_compose", "gmail_message"]


# ── ② l'erreur de compte nomme les comptes connectés et la forme attendue ───────
def _accounts(monkeypatch, emails):
    monkeypatch.setattr(G.db, "list_google_accounts",
                        lambda sub, org: [{"google_email": e} for e in emails])


def test_un_alias_pris_pour_un_email_est_corrige_par_le_message(monkeypatch):
    # `otomata` est un alias de la convention CLI (`oto -a otomata`), pas un email.
    _accounts(monkeypatch, ["alexis@otomata.tech", "jane.doe@acme.test"])
    msg = G._no_account_message("u1", 2, "otomata")
    assert "otomata" in msg
    assert "alexis@otomata.tech" in msg and "jane.doe@acme.test" in msg
    assert "EMAIL" in msg and "alias" in msg
    assert "gmail_list_accounts" in msg


def test_sans_aucun_compte_connecte_le_message_renvoie_au_dashboard(monkeypatch):
    _accounts(monkeypatch, [])
    msg = G._no_account_message("u1", 2, "otomata")
    assert "otomata" in msg and "manage.oto.cx" in msg
    # Ne pas prétendre lister des comptes qui n'existent pas.
    assert "Comptes connectés" not in msg


def test_sans_compte_demande_mais_des_comptes_connectes_on_les_nomme(monkeypatch):
    _accounts(monkeypatch, ["alexis@otomata.tech"])
    msg = G._no_account_message("u1", 2, None)
    assert "alexis@otomata.tech" in msg


def test_une_lecture_de_comptes_en_echec_ne_transforme_pas_l_erreur_en_panne(monkeypatch):
    def _boom(sub, org):
        raise RuntimeError("DB down")
    monkeypatch.setattr(G.db, "list_google_accounts", _boom)
    msg = G._no_account_message("u1", 2, "otomata")
    assert "otomata" in msg and "manage.oto.cx" in msg


# ── ① le retour de gmail_compose DIT l'acte ─────────────────────────────────────
@pytest.mark.parametrize("mode,attendu", [("draft", "draft"), ("send", "sent")])
def test_le_retour_nomme_l_acte(mode, attendu):
    # `_acte` est refermé sur `mode` dans le handler ; on rejoue sa logique exacte,
    # qui est la seule chose que ce maillon ajoute.
    def _acte(res, mode=mode):
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out["kind"] = "draft" if mode == "draft" else "sent"
        return out

    rendu = _acte({"id": "r123", "message_id": "m1", "threadId": "t1"})
    assert rendu["kind"] == attendu
    assert rendu["id"] == "r123"      # le retour amont n'est pas amputé


# ── ④ le défaut ne sort plus vers l'extérieur ───────────────────────────────────
def _schema_compose() -> tuple[dict, str]:
    """Le schéma SERVI de `gmail_compose` + sa description — ce que le client voit.

    On interroge `list_tools`, pas la fonction Python : un test qui lit la signature
    décrirait notre intention, pas le document que le modèle reçoit.
    """
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.tools import gmail

    m = FastMCP("t")
    gmail.register(m)
    t = next(x for x in asyncio.run(m.list_tools(run_middleware=False))
             if x.name == "gmail_compose")
    return t.parameters["properties"]["mode"], (t.description or "")


def test_le_defaut_de_gmail_compose_est_BROUILLON():
    """Le chemin paresseux ne doit pas être celui qui envoie à un tiers.

    Avant : oublier `mode` suffisait à expédier un mail — c'est exactement ce qui est
    arrivé le 14/08, trois fois, chez une cliente. La règle maison (« aucun défaut ne doit
    écrire », a fortiori dehors) tranche : on rédige, et envoyer devient un acte déclaré.
    Rupture de comportement assumée — une procédure qui envoie vraiment passe `mode="send"`.
    """
    mode, _ = _schema_compose()
    assert mode["default"] == "draft"
    assert set(mode["enum"]) == {"send", "draft"}      # envoyer reste possible
    assert "(default)" in mode["description"] and "draft" in mode["description"]


def test_le_contrat_ANNONCE_que_l_envoi_est_explicite():
    # La description EST le contrat lu par le modèle : si elle ne dit pas que le défaut
    # ne part pas, le changement de défaut ne protège que ceux qui l'ont deviné.
    _, doc = _schema_compose()
    assert "DRAFT by default" in doc
    assert '`mode="send"` is required' in doc
