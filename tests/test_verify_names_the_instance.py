"""Un `ok` de sonde doit dire QUELLE instance a répondu.

En niveau `auto`, la sonde teste le credential qui résout par la cascade — clé perso,
puis équipe, puis org, puis plateforme. Un `ok: true` nu est donc ambigu : impossible de
distinguer « ma clé perso marche » de « ma clé perso a échoué et c'est celle de l'org qui
répond ». C'est précisément le cas où la confirmation compte, puisque la perso gagne en
proximité et masque les autres.

Signalé le 03/08 en inspectant deux instances Salesforce d'une même org : les deux
passaient le verify, sans qu'on puisse dire laquelle avait été jointe.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities.connectors import verify as cv


class _Ctx:
    sub, org_id = "sub-x", 2


class _Inp:
    provider, level = "salesforce", "auto"


class _RC:
    """Substitut de `ResolvedCredential` — seuls l'entité gagnante et le mode comptent."""

    def __init__(self, mode, entity_type, entity_id):
        self.mode, self.entity_type, self.entity_id = mode, entity_type, entity_id
        self.fields, self.config = {"client_id": "ci"}, {}


# --- le nommage de l'instance --------------------------------------------------

@pytest.mark.parametrize("mode,etype,eid,niveau,attendu", [
    # ⚠️ `mode` vaut « user » là où l'entité dit « member » : c'est le level de l'ENTITÉ
    # qu'on expose, celui que parlent `ref` et `oto_instance op=list`.
    ("user", "member", "2:sub-x", "member", "member:2:sub-x:salesforce"),
    ("group", "group", "7", "group", "group:7:salesforce"),
    ("org", "org", "2", "org", "org:2:salesforce"),
    # Un grant plateforme n'a PAS de ligne de coffre — il faut quand même le nommer,
    # sinon le cas le plus ambigu de la cascade est justement celui qu'on ne voit pas.
    ("platform", None, None, "platform", "platform:salesforce"),
])
def test_la_sonde_nomme_linstance_jointe(monkeypatch, mode, etype, eid, niveau, attendu):
    from oto_mcp import access
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: _RC(mode, etype, eid))
    _, _, _, instance, _ = cv._fields_config_scope(_Ctx(), _Inp())
    assert instance == {"level": niveau, "ref": attendu}


def test_level_et_ref_ne_peuvent_pas_se_contredire(monkeypatch):
    """Ils sont dérivés de la MÊME source. Exposer `rc.mode` (« user ») à côté d'un ref
    en « member: » donnait deux mots pour le même objet dans une seule réponse, et
    cassait tout code comparant ce level à celui d'`oto_instance op=list`."""
    from oto_mcp import access
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: _RC("user", "member", "2:sub-x"))
    _, _, _, instance, _ = cv._fields_config_scope(_Ctx(), _Inp())
    assert instance["ref"].startswith(instance["level"] + ":")


def test_le_niveau_org_se_nomme_sans_passer_par_la_cascade(monkeypatch):
    """`level='org'` vise la clé de l'org SPÉCIFIQUEMENT (une clé perso la masquerait) :
    le ref rendu doit donc désigner l'org, pas ce que la cascade aurait choisi."""
    from oto_mcp import credentials_store
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda *a, **k: {"secret": "s", "meta": {}})
    monkeypatch.setattr(credentials_store, "unpack_secret", lambda *a: {"client_id": "ci"})
    monkeypatch.setattr(credentials_store, "public_meta", lambda m: {})

    class _InpOrg:
        provider, level = "salesforce", "org"

    _, _, _, instance, _ = cv._fields_config_scope(_Ctx(), _InpOrg())
    assert instance == {"level": "org", "ref": "org:2:salesforce"}


# --- la remontée jusqu'à la réponse --------------------------------------------

def test_le_ref_arrive_dans_la_reponse():
    """TRIPWIRE. Nommer l'instance en interne ne sert à rien si la réponse la perd —
    c'est exactement ce qui se passait : la résolution CONNAISSAIT l'entité gagnante et
    la jetait."""
    import inspect
    src = inspect.getsource(cv._verify)
    assert src.count("**instance") >= 2, (
        "l'instance sondée n'est pas jointe aux DEUX sorties (nominale et `pending`)")


def test_un_credential_incomplet_nomme_aussi_son_instance():
    """Le cas « il reste le consentement » renvoie tôt : sans l'instance, on saurait
    qu'il manque une étape mais pas SUR QUELLE clé."""
    import inspect
    src = inspect.getsource(cv._verify)
    bloc = src[src.index('"pending": True'):src.index("started = time.monotonic()")]
    assert "**instance" in bloc


# --- la cible d'écriture, sous rotation ----------------------------------------

@pytest.mark.parametrize("mode,etype,eid,cible_attendue", [
    ("user", "member", "2:sub-x", ("member", "2:sub-x", "")),
    ("org", "org", "2", ("org", "2", "")),
    # Grant plateforme : aucune ligne de coffre à réécrire.
    ("platform", None, None, None),
])
def test_la_sonde_sait_ou_reecrire(monkeypatch, mode, etype, eid, cible_attendue):
    """LE bug du 03/08. Sous rotation, sonder CONSOMME le jeton — le remplaçant doit
    être réécrit sur la ligne TESTÉE. Sans cette cible, la sonde ne peut que deviner via
    la cascade, qui désigne la clé la plus PROCHE : un `verify level=org` chez quelqu'un
    ayant aussi une clé perso comparait le jeton d'org au jeton perso, ne reconnaissait
    rien, ne persistait rien — et tuait donc le jeton d'org en le validant."""
    from oto_mcp import access
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _RC(mode, etype, eid))
    *_, cible = cv._fields_config_scope(_Ctx(), _Inp())
    assert cible == cible_attendue


def test_la_cible_est_transmise_a_la_sonde():
    """TRIPWIRE. La calculer sans la passer ne servirait à rien — c'est exactement
    l'état d'avant, où l'information existait et se perdait.

    Il OBSERVE ce que la sonde reçoit, au lieu de lire le texte de la fonction :
    la version d'avant cherchait `kw["instance"] = cible` dans la source, et
    tombait dès que le geste changeait de place (oto-backend#867) alors que la
    garantie tenait toujours. Un tripwire qui asserte une FORME de code se périme
    au premier déplacement ; celui-ci ne tombera que si la cible cesse d'arriver.
    """
    import asyncio

    from oto_mcp.connectors import verify as connector_verify

    vu = {}

    def _sonde(fields, config, instance=None):
        vu["instance"] = instance

    asyncio.run(connector_verify.executer(_sonde, {}, {}, ("org", "2", "")))
    assert vu["instance"] == ("org", "2", ""), (
        "la sonde n'a pas reçu la cible : sous rotation, le jeton rafraîchi serait "
        "réécrit sur la mauvaise ligne — le bug du 03/08.")


def test_une_sonde_sans_le_parametre_garde_sa_signature():
    """Les ~15 sondes à deux arguments ne doivent pas recevoir `instance` : le
    passer à toutes les casserait d'un coup."""
    import asyncio

    from oto_mcp.connectors import verify as connector_verify

    appels = []

    def _sonde_courte(fields, config):
        appels.append((fields, config))

    asyncio.run(connector_verify.executer(_sonde_courte, {"k": 1}, {}, ("org", "2", "")))
    assert appels == [({"k": 1}, {})]
