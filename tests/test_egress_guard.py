"""La garde d'egress des connecteurs : ce qu'elle refuse, et qu'elle est BRANCHÉE.

Sept connecteurs prennent leur destination dans le credential — une adresse
choisie par un administrateur d'organisation cliente, pas par l'agent qui
appelle. Le process qui l'appelle tourne en root, sans conteneur : sa boucle
locale porte l'administration du proxy web et le pilotage du navigateur. Le
filtrage réseau du service ne bloque qu'une plage (le lien-local) — tout le
reste était joignable.

Ce banc tient trois choses, et la troisième est celle qui manque d'habitude :

1. **La décision** — ce qui est interne, ce qui ne l'est pas, et le
   déguisement : un nom de domaine public qui RÉSOUT vers l'intérieur. C'est le
   contournement évident, et un contrôle textuel le laisse passer.
2. **Les exceptions nommées** — une destination interne déclarée passe, la même
   adresse sur un AUTRE port ne passe pas, et une déclaration illisible lève au
   lieu d'être lue « comme vide ».
3. **Le câblage** — que chaque connecteur à hôte libre appelle réellement la
   garde. Un banc qui n'éprouve que la décision reste vert quand personne ne
   l'appelle : c'est le vert creux classique. D'où deux crans ici — un
   inventaire DÉRIVÉ du registre (donc un huitième connecteur à hôte libre fait
   rougir ce fichier tant qu'il n'est pas câblé), et des appels réels sur les
   sondes, qui prouvent que le refus tombe AVANT le premier octet réseau.

Le détecteur du cran 3 s'éprouve lui-même, plus bas : un détecteur qu'on ne peut
pas faire rougir sur commande ne garde rien.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from oto_mcp import egress

_TOOLS = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp" / "tools"


def _resolution(**table):
    """Remplace la résolution DNS par une table hôte → adresses."""
    def faux(hote, port):  # noqa: ARG001
        return set(table[hote])
    return faux


# ── 1. la décision : ce qui est interne ──────────────────────────────────────

@pytest.mark.parametrize("adresse,fragment", [
    ("127.0.0.1", "boucle locale"),
    ("127.1.2.3", "boucle locale"),
    ("::1", "boucle locale"),
    ("10.0.0.7", "réseau privé"),
    ("172.20.4.9", "réseau privé"),
    ("192.168.1.10", "réseau privé"),
    ("fd00::1", "réseau privé"),
    ("169.254.169.254", "lien-local"),
    ("0.0.0.0", "non spécifiée"),
    ("224.0.0.1", "multicast"),
])
def test_ces_adresses_sont_internes_et_la_raison_est_nommee(adresse, fragment):
    raison = egress.internal_reason(adresse)
    assert raison is not None, f"{adresse} devrait être refusée"
    assert fragment in raison, f"{adresse} : raison inattendue « {raison} »"


@pytest.mark.parametrize("adresse", ["1.1.1.1", "93.184.216.34", "2001:4860:4860::8888"])
def test_ces_adresses_sont_publiques(adresse):
    assert egress.internal_reason(adresse) is None


def test_le_filet_rattrape_une_plage_qu_aucune_branche_ne_nomme():
    """`100.64.0.1` (CGNAT, RFC 6598) n'est ni privée, ni réservée, ni bouclée
    au sens d'`ipaddress` : SEUL `not is_global` la voit. Sans cette épreuve, le
    filet pouvait disparaître sans que rien ne bronche — mesuré à la chute.
    Vérifié identique sous Python 3.10 (la box) et 3.13 (ce banc)."""
    assert egress.internal_reason("100.64.0.1") == \
        "adresse non publique (plage à usage spécial)"


@pytest.mark.parametrize("adresse", [
    "::ffff:127.0.0.1",                       # v4-mapped
    "::ffff:10.0.0.7",
    "2002:7f00:0001::",                       # 6to4 sur 127.0.0.1
    "2002:0a00:0007::",                       # 6to4 sur 10.0.0.7
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",   # Teredo
])
def test_une_v4_encapsulee_dans_une_v6_est_refusee(adresse):
    """La DÉCISION, d'abord : quelle que soit l'encapsulation, c'est refusé.
    Elle ne dépend pas du déballage — `is_global` couvre déjà ces plages."""
    assert egress.internal_reason(adresse) is not None


def test_le_deballage_v4_dans_v6_precise_la_RAISON():
    """Ce que le déballage apporte VRAIMENT, mesuré : pas la décision (voir
    ci-dessus), mais le mot juste. Sans lui, `2002:7f00:0001::` — qui joint la
    boucle locale — serait annoncé « réseau privé », et l'opérateur chercherait
    la mauvaise chose. Un refus mal motivé se corrige mal."""
    assert "boucle locale" in (egress.internal_reason("2002:7f00:0001::") or "")
    assert "réseau privé" in (egress.internal_reason("2002:0a00:0007::") or "")


# ── 2. le contrôle d'une URL, résolution comprise ────────────────────────────

def test_une_destination_publique_passe(monkeypatch):
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"api.acme.test": ["93.184.216.34"]}))
    egress.check_url("https://api.acme.test/v1", connector="http")


def test_une_adresse_interne_ecrite_en_clair_est_refusee(monkeypatch):
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"127.0.0.1": ["127.0.0.1"]}))
    with pytest.raises(egress.EgressRefused):
        egress.check_url("http://127.0.0.1:9001/config", connector="http")


def test_le_deguisement_par_nom_de_domaine_est_refuse(monkeypatch):
    """LE contournement évident : l'hôte est un nom parfaitement public, et il
    pointe vers l'intérieur. Aucun contrôle sur la FORME du texte ne le voit."""
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"interne.acme.test": ["127.0.0.1"]}))
    with pytest.raises(egress.EgressRefused) as pris:
        egress.check_url("https://interne.acme.test/", connector="http")
    message = str(pris.value)
    # Le refus doit montrer les DEUX : le nom saisi ET l'adresse jointe. Sans le
    # nom, il paraît absurde ; sans l'adresse, il ne dit pas ce qui cloche.
    # ⚠️ On vise la phrase de DIAGNOSTIC, pas la simple présence de « 127.0.0.1 »
    # dans le message : l'entrée d'exemple à déclarer la contient déjà, si bien
    # qu'un refus muet sur la résolution restait vert (trou vu à la chute).
    assert "interne.acme.test" in message
    assert "qui résout vers 127.0.0.1" in message


def test_un_hote_qui_resout_public_ET_interne_est_refuse(monkeypatch):
    """Le montage type du contournement : une réponse DNS qui mélange les deux.
    Il suffit que le client tire la mauvaise pour que la garde n'ait rien servi.

    ⚠️ L'adresse interne est ici celle qui vient EN DERNIER dans l'ordre parcouru
    (`1.1.1.1` < `192.168.1.10`). C'est délibéré : la première rédaction plaçait
    l'interne en tête, si bien qu'une garde ne regardant que la première adresse
    résolue restait verte — trou vu à l'épreuve de chute."""
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"double.acme.test": ["1.1.1.1", "192.168.1.10"]}))
    with pytest.raises(egress.EgressRefused) as pris:
        egress.check_url("https://double.acme.test/", connector="n8n")
    assert "192.168.1.10" in str(pris.value)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://api.acme.test/",
    "ftp://api.acme.test/",
])
def test_un_schema_hors_http_est_refuse(monkeypatch, url):
    """⚠️ L'hôte résout PUBLIQUEMENT ici, exprès : sans ce montage, ces URL
    étaient refusées parce que leur hôte ne résolvait pas — le contrôle de
    schéma pouvait disparaître sans que le banc bronche (trou vu à la chute).
    Cela ôte aussi toute dépendance au DNS de la machine qui joue le banc."""
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"api.acme.test": ["93.184.216.34"]}))
    with pytest.raises(egress.EgressRefused):
        egress.check_url(url, connector="http")


def test_un_hote_qui_ne_resout_pas_n_est_pas_appele(monkeypatch):
    """Ne pas pouvoir contrôler une destination n'autorise pas à l'appeler."""
    def casse(hote, port):  # noqa: ARG001
        raise OSError("Name or service not known")
    monkeypatch.setattr(egress, "resolved_addresses", casse)
    with pytest.raises(egress.EgressRefused):
        egress.check_url("https://nulle-part.acme.test/", connector="make")


def test_le_refus_nomme_sa_destination(monkeypatch):
    """Un refus qui ne dit pas OÙ porter l'intention fait rejouer le même appel,
    ou chercher un chemin pire. Il doit nommer la variable, le format, et donner
    l'entrée exacte à déclarer."""
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"pont.acme.test": ["127.0.0.1"]}))
    with pytest.raises(egress.EgressRefused) as pris:
        egress.check_url("http://pont.acme.test:9000/healthz",
                         connector="http", field="base_url")
    message = str(pris.value)
    assert egress.ALLOW_VAR in message
    assert "nom=adresse:port" in message
    assert "127.0.0.1:9000" in message, "l'entrée à déclarer doit être donnée telle quelle"
    assert "base_url" in message and "http" in message, "quelle carte corriger"


# ── 3. les exceptions nommées ────────────────────────────────────────────────

def test_une_destination_interne_declaree_passe(monkeypatch):
    """C'est la raison d'être de la liste : un pont hébergé en boucle locale
    continue de fonctionner. Sans ce cran, la garde casse ce qu'elle protège."""
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"127.0.0.1": ["127.0.0.1"]}))
    monkeypatch.setenv(egress.ALLOW_VAR, "pont-local=127.0.0.1:9000")
    egress.check_url("http://127.0.0.1:9000/healthz", connector="http")


def test_une_exception_n_ouvre_QUE_son_port(monkeypatch):
    """Une exception déclarée par adresse seule ouvrirait toute la machine :
    le pont sur 9000 donnerait aussi l'administration du proxy sur 9001."""
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"127.0.0.1": ["127.0.0.1"]}))
    monkeypatch.setenv(egress.ALLOW_VAR, "pont-local=127.0.0.1:9000")
    with pytest.raises(egress.EgressRefused):
        egress.check_url("http://127.0.0.1:9001/config", connector="http")


def test_une_exception_ne_vaut_pas_pour_une_autre_adresse(monkeypatch):
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"10.0.0.7": ["10.0.0.7"]}))
    monkeypatch.setenv(egress.ALLOW_VAR, "pont-local=127.0.0.1:9000")
    with pytest.raises(egress.EgressRefused):
        egress.check_url("http://10.0.0.7:9000/", connector="http")


def test_le_defaut_est_vide():
    """Aucune destination réelle n'est écrite dans ce dépôt public : la liste se
    déclare au déploiement. Un défaut non vide ferait de ce fichier l'inventaire
    des services internes de la plateforme."""
    assert egress.declared_exceptions("") == {}


def test_la_liste_accepte_plusieurs_entrees_nommees():
    lue = egress.declared_exceptions(
        "pont-local=127.0.0.1:9000,\n service-interne=10.0.0.5:9002")
    assert lue == {("127.0.0.1", 9000): "pont-local",
                   ("10.0.0.5", 9002): "service-interne"}


@pytest.mark.parametrize("brut", [
    "127.0.0.1:9000",                 # sans nom : une exception se NOMME
    "pont-local=",                    # sans destination
    "pont-local=pont.acme.test:9000",  # un nom d'hôte peut se déplacer sous nous
    "pont-local=127.0.0.1",           # sans port : ouvrirait toute la machine
    "pont-local=127.0.0.1:huit",      # port illisible
])
def test_une_declaration_illisible_leve_au_lieu_d_etre_lue_comme_vide(brut):
    """Une politique de sécurité qu'on ne sait pas lire ne s'interprète pas."""
    with pytest.raises(ValueError) as pris:
        egress.declared_exceptions(brut)
    assert egress.ALLOW_VAR in str(pris.value), "le refus doit nommer la variable"


def test_une_declaration_illisible_n_atteint_pas_le_trafic_public(monkeypatch):
    """La lecture de la liste n'a lieu qu'au moment de refuser : une faute de
    frappe dans la variable ne doit pas couper les destinations publiques."""
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"api.acme.test": ["93.184.216.34"]}))
    monkeypatch.setenv(egress.ALLOW_VAR, "n'importe quoi")
    egress.check_url("https://api.acme.test/", connector="http")


# ── 4. le câblage, cran 1 : l'inventaire DÉRIVÉ du registre ──────────────────
#
# Rien n'est recopié à la main : la liste des connecteurs à hôte libre se lit
# dans le registre. Un huitième connecteur qui déclare une URL non secrète fait
# rougir ce fichier tant que sa garde n'est pas posée.

# Champ non secret qui désigne une DESTINATION, et pourquoi il n'en est pas une
# quand il est écarté. Écarter en silence, c'est rouvrir la porte sans le dire.
_HORS_PORTEE = {
    ("pipedrive", "company_domain"):
        "ce n'est pas une URL : le client compose `https://{valeur}.pipedrive.com`, "
        "donc l'hôte reste sous pipedrive.com quoi qu'on saisisse",
}


def _champs_de_destination() -> dict[str, set[str]]:
    """Connecteur → champs non secrets qui portent une destination."""
    from oto_mcp import providers
    trouve: dict[str, set[str]] = {}
    for connecteur in providers.REGISTRY.values():
        for champ in (connecteur.secret_fields or ()):
            nom = champ.name.lower()
            if champ.secret:
                continue
            if not any(m in nom for m in ("url", "host", "domain")):
                continue
            if (connecteur.name, champ.name) in _HORS_PORTEE:
                continue
            trouve.setdefault(connecteur.name, set()).add(champ.name)
    return trouve


def connecteurs_gardes(source: str) -> set[str]:
    """Les connecteurs qu'une source déclare garder — `egress.check_url(…,
    connector="X")`.

    Fonction publique : les épreuves du détecteur, plus bas, l'appellent sur des
    cas fabriqués dont on connaît la réponse."""
    try:
        arbre = ast.parse(source)
    except SyntaxError:
        return set()
    vus: set[str] = set()
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.Call):
            continue
        fonction = noeud.func
        if not (isinstance(fonction, ast.Attribute) and fonction.attr == "check_url"):
            continue
        if not (isinstance(fonction.value, ast.Name) and fonction.value.id == "egress"):
            continue
        for mot in noeud.keywords:
            if mot.arg == "connector" and isinstance(mot.value, ast.Constant):
                vus.add(mot.value.value)
    return vus


def _garde_directe(noeud) -> bool:
    """Ce sous-arbre appelle-t-il `egress.check_url` ?"""
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "check_url" and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "egress"
        for n in ast.walk(noeud))


def _nom_appele(appel: ast.Call) -> str:
    if isinstance(appel.func, ast.Name):
        return appel.func.id
    if isinstance(appel.func, ast.Attribute):
        return appel.func.attr
    return ""


def sites_non_gardes(source: str, chemin: str = "<mémoire>") -> list[str]:
    """Fonctions qui construisent un client SANS garde sur leur chemin.

    Compter les appels ne suffit pas : `github`, `lighton` et `posthog` rangent
    la garde dans un helper local qu'ils appellent à chaque site, et `salesforce`
    dans `_login_url`. Un compteur exigerait une forme d'écriture ; ce détecteur
    suit le CHEMIN — garde directe dans la fonction, ou appel à un helper du
    module qui la porte.

    C'est ce cran qui manquait : retirer la garde d'un seul des deux sites d'un
    module laissait le banc vert, puisque l'autre site la mentionnait encore
    (mesuré à l'épreuve de chute)."""
    try:
        arbre = ast.parse(source)
    except SyntaxError:
        return []
    fonctions = [f for f in ast.walk(arbre)
                 if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    porteurs = {f.name for f in fonctions if _garde_directe(f)}
    fautes = []
    for fn in fonctions:
        construit = any(_nom_appele(n).endswith("Client")
                        for n in ast.walk(fn) if isinstance(n, ast.Call))
        if not construit or _garde_directe(fn):
            continue
        if any(_nom_appele(n) in porteurs
               for n in ast.walk(fn) if isinstance(n, ast.Call)):
            continue
        fautes.append(f"{chemin}:{fn.lineno} {fn.name}()")
    return fautes


def test_chaque_connecteur_a_hote_libre_nomme_la_garde():
    manquants = []
    for nom in sorted(_champs_de_destination()):
        module = _TOOLS / f"{nom}.py"
        if not module.exists():
            continue        # pas de chemin d'appel sous tools/ : rien à garder ici
        if nom not in connecteurs_gardes(module.read_text(encoding="utf-8")):
            manquants.append(f"oto_mcp/tools/{nom}.py")
    assert not manquants, (
        "connecteur(s) à hôte libre sans garde d'egress — la destination y vient "
        "du credential d'une organisation :\n  " + "\n  ".join(manquants)
        + "\nAppeler `egress.check_url(<url>, connector=\"<nom>\")` avant de "
          "construire le client.")


def test_aucun_site_de_construction_n_echappe_a_la_garde():
    """Le cran qui manque à la version « le module mentionne la garde » : CHAQUE
    fonction qui construit un client doit l'avoir sur son chemin, pas seulement
    une fonction du fichier."""
    fautes = []
    for nom in sorted(_champs_de_destination()):
        module = _TOOLS / f"{nom}.py"
        if not module.exists():
            continue
        fautes += sites_non_gardes(module.read_text(encoding="utf-8"),
                                   f"oto_mcp/tools/{nom}.py")
    assert not fautes, (
        f"{len(fautes)} construction(s) de client hors garde d'egress :\n  "
        + "\n  ".join(fautes)
        + "\nAppeler `egress.check_url(...)` dans la fonction, ou passer par un "
          "helper du module qui le fait.")


def test_l_inventaire_derive_n_est_pas_vide():
    """Une dérivation qui ne trouve plus rien rendrait le test précédent vert
    pour la pire des raisons — c'est exactement la forme du vert creux."""
    inventaire = _champs_de_destination()
    assert len(inventaire) >= 7, f"inventaire suspect : {sorted(inventaire)}"
    assert "http" in inventaire and "base_url" in inventaire["http"]


def test_ce_qui_est_ecarte_de_l_inventaire_est_justifie():
    """Un écart sans raison écrite se transforme en trou six mois plus tard."""
    for cle, raison in _HORS_PORTEE.items():
        assert raison.strip(), f"{cle} écarté sans raison"


# ── 5. le câblage, cran 2 : l'APPEL, pas seulement sa présence ───────────────
#
# Le cran 1 lit du texte. Ces épreuves-ci appellent le vrai code des sondes avec
# une destination interne : si la garde n'était pas branchée sur la BONNE valeur,
# l'appel partirait sur le réseau au lieu de lever.

def _sonde(nom):
    import importlib
    return importlib.import_module(f"oto_mcp.tools.{nom}")._verify


@pytest.mark.parametrize("connecteur,fields,config", [
    ("n8n",     {"api_key": "k", "base_url": "http://10.0.0.7:5678"}, None),
    ("make",    {"api_token": "t", "base_url": "http://10.0.0.7/api/v2"}, None),
    ("github",  {"token": "t", "base_url": "http://10.0.0.7/api/v3"}, None),
    ("lighton", {"api_key": "k", "base_url": "http://10.0.0.7"}, None),
    ("posthog", {"api_key": "k"}, {"host": "http://10.0.0.7"}),
])
def test_la_sonde_refuse_avant_de_toucher_au_reseau(monkeypatch, connecteur, fields, config):
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"10.0.0.7": ["10.0.0.7"]}))
    monkeypatch.delenv(egress.ALLOW_VAR, raising=False)
    with pytest.raises(egress.EgressRefused):
        _sonde(connecteur)(fields, config)


def test_salesforce_garde_son_serveur_d_auth(monkeypatch):
    """`login_url` reçoit le refresh token : une destination interne y enverrait
    un SECRET vers un service de la machine, pas seulement une requête."""
    from oto_mcp.tools import salesforce
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"10.0.0.7": ["10.0.0.7"]}))
    monkeypatch.delenv(egress.ALLOW_VAR, raising=False)
    with pytest.raises(egress.EgressRefused):
        salesforce._login_url("http://10.0.0.7/services/oauth2")
    # Le défaut, lui, est une constante du module : rien à contrôler.
    assert salesforce._login_url("") == "https://login.salesforce.com"


def test_http_refuse_la_carte_et_le_dit_en_McpError(monkeypatch):
    """Le vecteur le plus large, de bout en bout : credential résolu → refus
    traduit pour l'appelant, sans qu'aucun client ne soit construit."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import http as tool_http

    monkeypatch.setattr(tool_http, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(tool_http, "_resolve_fields",
                        lambda: {"base_url": "https://pont.acme.test",
                                 "auth_mode": "bearer", "token": "t"})
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"pont.acme.test": ["127.0.0.1"]}))
    monkeypatch.delenv(egress.ALLOW_VAR, raising=False)
    with pytest.raises(McpError) as pris:
        tool_http._client()
    assert egress.ALLOW_VAR in str(pris.value)


def test_http_garde_aussi_le_serveur_de_jetons_oauth2(monkeypatch):
    """`token_url` part en POST depuis la lib sans repasser par `base_url` : ne
    garder que la base laisserait un chemin sortant entier hors de la garde."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import http as tool_http

    monkeypatch.setattr(tool_http, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(tool_http, "_resolve_fields",
                        lambda: {"base_url": "https://api.acme.test",
                                 "auth_mode": "oauth2",
                                 "token_url": "http://127.0.0.1:8200/token",
                                 "client_id": "c", "client_secret": "s"})
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"api.acme.test": ["93.184.216.34"],
                                       "127.0.0.1": ["127.0.0.1"]}))
    monkeypatch.delenv(egress.ALLOW_VAR, raising=False)
    with pytest.raises(McpError) as pris:
        tool_http._client()
    assert "token_url" in str(pris.value)


def test_http_laisse_passer_une_carte_publique(monkeypatch):
    """L'autre sens. Sans cette épreuve, une garde qui refuse TOUT serait verte
    partout ailleurs dans ce fichier."""
    from oto_mcp.tools import http as tool_http

    monkeypatch.setattr(tool_http, "current_user_sub_from_token", lambda: "u-1")
    monkeypatch.setattr(tool_http, "_resolve_fields",
                        lambda: {"base_url": "https://api.acme.test",
                                 "auth_mode": "bearer", "token": "t"})
    monkeypatch.setattr(egress, "resolved_addresses",
                        _resolution(**{"api.acme.test": ["93.184.216.34"]}))
    assert tool_http._client() is not None


# ── 6. les épreuves du détecteur du cran 1 ───────────────────────────────────
#
# Un détecteur qui ne peut pas rougir sur commande ne garde rien, et personne ne
# s'en aperçoit puisqu'il est vert.

def test_le_detecteur_voit_un_appel_direct():
    src = 'from .. import egress\ndef f(u):\n    egress.check_url(u, connector="n8n")\n'
    assert connecteurs_gardes(src) == {"n8n"}


def test_le_detecteur_voit_un_appel_range_dans_un_helper():
    """github/lighton/posthog passent par un `_check_base_url` local : le
    détecteur doit le voir aussi, sinon il exigerait une forme d'écriture."""
    src = ('from .. import egress\n'
           'def _check(v):\n'
           '    if v:\n'
           '        egress.check_url(v, connector="github")\n'
           'def _client(c):\n'
           '    return _check(c.get("base_url"))\n')
    assert connecteurs_gardes(src) == {"github"}


def test_le_detecteur_ne_voit_rien_dans_un_module_non_garde():
    src = ('from .. import access\n'
           'def _client(c):\n'
           '    return Client(base_url=c.get("base_url"))\n')
    assert connecteurs_gardes(src) == set()


def test_le_detecteur_ne_confond_pas_une_autre_check_url():
    """`web.py` porte un `check_url_public` et `file_source` un garde à lui : ni
    l'un ni l'autre n'est cette garde-ci."""
    src = ('from . import web\n'
           'def f(u):\n'
           '    web.check_url_public(u)\n'
           '    autre.check_url(u, connector="http")\n')
    assert connecteurs_gardes(src) == set()


def test_le_detecteur_exige_le_nom_du_connecteur():
    """Un appel sans `connector=` ne dit pas QUELLE carte il garde — et le
    message de refus ne saurait pas quoi nommer."""
    src = 'from .. import egress\ndef f(u):\n    egress.check_url(u)\n'
    assert connecteurs_gardes(src) == set()


# ── 7. les épreuves du détecteur de SITES ────────────────────────────────────
#
# Celui-ci est né d'un trou : la version d'avant ne regardait que le fichier, si
# bien que retirer la garde d'un site sur deux la laissait verte. Il s'éprouve
# donc dans les deux sens, et surtout sur ce cas précis.

def test_le_detecteur_de_sites_voit_une_fonction_non_gardee():
    src = ('from .. import egress\n'
           'def _client(c):\n'
           '    return N8nClient(base_url=c.get("base_url"))\n')
    assert sites_non_gardes(src) == ["<mémoire>:2 _client()"]


def test_le_detecteur_de_sites_accepte_une_garde_directe():
    src = ('from .. import egress\n'
           'def _client(c):\n'
           '    egress.check_url(c.get("base_url"), connector="n8n")\n'
           '    return N8nClient(base_url=c.get("base_url"))\n')
    assert sites_non_gardes(src) == []


def test_le_detecteur_de_sites_suit_un_helper_du_module():
    """github/lighton/posthog rangent la garde dans un helper, salesforce dans
    `_login_url`. Ne pas suivre le chemin exigerait une forme d'écriture."""
    src = ('from .. import egress\n'
           'def _check(v):\n'
           '    egress.check_url(v, connector="github")\n'
           'def _client(c):\n'
           '    _check(c.get("base_url"))\n'
           '    return GitHubClient(base_url=c.get("base_url"))\n')
    assert sites_non_gardes(src) == []


def test_le_detecteur_de_sites_voit_LE_site_retire_quand_l_autre_reste():
    """Le trou exact, reproduit : la sonde garde, le client ne garde plus. Le
    fichier mentionne toujours la garde — et pourtant un chemin est ouvert."""
    src = ('from .. import egress\n'
           'def _verify(f):\n'
           '    egress.check_url(f["base_url"], connector="n8n")\n'
           '    return N8nClient(base_url=f["base_url"]).ping()\n'
           'def _client(c):\n'
           '    return N8nClient(base_url=c.get("base_url"))\n')
    assert connecteurs_gardes(src) == {"n8n"}, "le détecteur de fichier, lui, ne voit rien"
    assert sites_non_gardes(src) == ["<mémoire>:5 _client()"]


def test_le_detecteur_de_sites_ignore_une_fonction_sans_client():
    src = 'def _excerpt(r):\n    return r.text[:500]\n'
    assert sites_non_gardes(src) == []
