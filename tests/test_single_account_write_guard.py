"""oto-backend#409 — « accepté-et-ignoré » est exclu à la pose d'un compte nommé.

Le coffre stocke N lignes par (entité, connecteur, **compte**) pour TOUS les
connecteurs, mais seule la résolution d'un connecteur MULTI-compte va les lire.
Poser un compte nommé sur un mono-compte écrivait donc une ligne parfaitement
valide que rien n'irait jamais chercher — écrite, inerte, sans refus ni
avertissement. Deux volets, tous deux ici :

1. **la garde de pose** — un connecteur qui ne résout pas les comptes nommés
   REFUSE la déclaration d'un compte nommé, en se nommant ;
2. **la règle de cardinalité** — un credential MULTI-CHAMPS est multi-compte au
   même titre qu'une clé simple (cas Slack : un token par installation dans un
   workspace, N installations = N tokens), sauf exclusion explicite et motivée
   **par connecteur**. Jamais « toute forme à champs ⟹ multi ».

Les cas nominaux tranchent contre le REGISTRE RÉEL (`crunchbase` = cookie, donc
mono par construction ; `zoho` = multi) : c'est le montage servi qui décide, pas
une fixture qui reproduirait la règle qu'on veut prouver.
"""
import pytest

from oto_mcp import credentials_store, providers


def _vault_forbidden(monkeypatch):
    """Le coffre ne doit PAS être touché par un refus : la garde tranche sur le
    registre, avant toute lecture des comptes déjà posés."""
    def _boom(*a, **k):
        raise AssertionError("la garde a lu le coffre avant de refuser")
    monkeypatch.setattr(credentials_store, "list_accounts", _boom)


# --- 1. La garde de pose -----------------------------------------------------

def test_named_account_on_single_account_connector_is_refused(monkeypatch):
    assert providers.REGISTRY["crunchbase"].auth_multi_account is False
    _vault_forbidden(monkeypatch)
    with pytest.raises(credentials_store.SingleAccountConnector) as e:
        credentials_store.guard_account_write(
            credentials_store.MEMBER, "1:u1", "crunchbase", "second-compte")
    # Refus NOMMÉ : le connecteur et le compte refusé sont dans le message —
    # sans eux, l'appelant ne sait pas quoi corriger.
    assert "crunchbase" in str(e.value) and "second-compte" in str(e.value)


def test_unnamed_account_on_single_account_connector_passes(monkeypatch):
    """La pose ordinaire (sans compte nommé) reste intouchée — c'est le cas de
    tous les credentials posés jusqu'ici."""
    _vault_forbidden(monkeypatch)
    credentials_store.guard_account_write(
        credentials_store.MEMBER, "1:u1", "crunchbase", "")


def test_multi_account_connector_delegates_to_coexistence(monkeypatch):
    """Multi-compte : la garde ne change rien au contrat existant — elle passe la
    main à la cohérence des noms ('' et comptes nommés ne cohabitent pas)."""
    assert providers.REGISTRY["zoho"].auth_multi_account is True
    seen = []
    monkeypatch.setattr(credentials_store, "ensure_named_coexistence",
                        lambda *a: seen.append(a))
    credentials_store.guard_account_write("org", "35", "zoho", "zoho-us")
    assert seen == [("org", "35", "zoho", "zoho-us")]


def test_unknown_connector_named_account_is_refused(monkeypatch):
    """Un connecteur hors registre ne résout aucun compte nommé : même refus que
    le mono-compte, jamais un laissez-passer."""
    _vault_forbidden(monkeypatch)
    with pytest.raises(credentials_store.SingleAccountConnector):
        credentials_store.guard_account_write("org", "35", "pas-au-registre", "compte-x")


# --- 2. La règle de cardinalité ---------------------------------------------

@pytest.mark.parametrize("name", ["slack", "silae", "stripe", "salesforce",
                                  "zohodesk", "posthog", "http"])
def test_multi_field_credential_is_multi_account(name):
    """Un credential multi-champs porte N comptes comme une clé simple : la forme
    du credential n'est pas une raison de fournisseur."""
    con = providers.REGISTRY[name]
    assert con.secret_kind == "fields" and con.auth_multi_account is True


@pytest.mark.parametrize("name", ["unipile", "crunchbase", "culture"])
def test_excluded_families_stay_single_account(name):
    """Les deux familles exclues pour une raison qui n'est PAS la forme du
    credential — porteur d'identité cross-org (barreau de cascade mono par
    construction) et flux à consentement OAuth/cookie (N comptes = N
    consentements) — plus l'open-data, qui n'a pas de credential du tout.

    ⚠️ `atlassian` tenait la place du consentement **OAuth** mono-compte jusqu'au
    2026-09-09 (parti avec la fédération MCP, ADR 0069). Il n'a PAS de remplaçant :
    `crunchbase` couvre la moitié cookie, et le seul `secret_kind="oauth"` restant,
    `google`, est multi-compte par construction. Cette moitié-là n'est donc plus
    représentée — à re-couvrir le jour où un connecteur OAuth mono-compte arrive,
    plutôt que de faire semblant avec un connecteur qui ne l'est pas."""
    assert providers.REGISTRY[name].auth_multi_account is False


def test_la_cardinalite_declaree_prime_sur_la_derivation():
    """Elle se déclare DANS l'entrée du connecteur, jamais par appartenance à une
    liste transverse — et dans les DEUX sens : `mono` là où la dérivation dirait
    multi, `multi` là où elle dirait mono."""
    mono = providers._c("faux", ["faux"], auth_modes={"byo_user"},
                        secret_kind="fields", cardinality="mono",
                        credential_fields=(providers.CredentialField("a", "A"),))
    assert mono.auth_multi_account is False        # `fields` dérivait multi

    multi = providers._c("faux2", ["faux2"], auth_modes={"byo_user"},
                         secret_kind="cookie", cardinality="multi")
    assert multi.auth_multi_account is True        # `cookie` dérivait mono


def test_seuls_les_depots_de_cle_se_declarent_MONO_et_on_sait_pourquoi():
    """Le sens `mono` a eu ZÉRO porteur jusqu'au 04/09/2026, par tripwire : en
    ajouter un est une décision explicite, qui casse ce test et se motive en revue.
    Voici le motif des trois premiers.

    `anthropic` et `mistral` sont des DÉPÔTS DE CLÉ (`kind="credential"`) : ils ne
    portent aucun outil, seulement la clé de modèle sur laquelle les agents
    programmés de l'org tournent. La dérivation les rendrait multi (c'est une
    `api_key`), donc l'écran proposerait d'en poser une deuxième — et
    `runner.jobs` lit le COMPTE UNIQUE au moment de servir un travail. Une clé
    posée sous un nom de compte ne serait jamais lue : l'org paierait sur la clé
    de la plateforme en croyant payer sur la sienne, un écart qui ne se voit que
    sur une facture.

    Et c'est bien une raison de FOURNISSEUR au sens de ce fichier : un passage
    tourne sur une clé, deux dépôts pour la même org seraient deux factures pour
    un même travail, sans aucun critère pour trancher laquelle.

    `transcription` (ADR 0074, #674) est mono pour une raison DIFFÉRENTE : ce n'est
    pas un dépôt de clé (il porte deux outils), mais une instance y est « une clé ×
    une langue × un vocabulaire », rattachée à un projet par SLOT (ADR 0035) — pas
    par un compte nommé qu'on choisirait à l'appel. Deux instances sur la même org
    n'auraient aucun critère pour se départager au moment de transcrire (contrairement
    à Zoho ou Unipile, où l'agent NOMME le compte visé) ; la dérivation `fields` les
    rendrait pourtant multi, ce qui proposerait un second dépôt sans jamais dire
    lequel choisir."""
    assert sorted(c.name for c in providers._REGISTRY_LIST
                  if c.cardinality == "mono") == ["anthropic", "mistral", "transcription"]


def test_un_compte_nomme_sur_un_depot_de_cle_est_refuse(monkeypatch):
    """La conséquence de la déclaration ci-dessus, et la raison de la poser : la
    garde de pose refuse ce que la résolution n'irait jamais lire."""
    _vault_forbidden(monkeypatch)
    with pytest.raises(credentials_store.SingleAccountConnector):
        credentials_store.guard_account_write(
            credentials_store.ORG, "2", "anthropic", "equipe-data")


def test_seuls_deux_connecteurs_se_declarent_MULTI_et_on_sait_pourquoi():
    """Le sens `multi` a exactement deux porteurs, et c'est la MESURE qui les a
    désignés : ceux dont le descripteur d'auth dit faux. `zoho` et `folk`, longtemps
    dans la liste transverse, n'y sont PAS — la dérivation les rend multi toute seule
    depuis que la règle couvre les credentials multi-champs. Les garder déclarés
    aurait reconduit la liste sous un autre nom."""
    # Split google (2026-09-26) : les six services déclarent la cardinalité du compte
    # qu'ils empruntent — même raison de fournisseur, portée par `google.service`.
    assert sorted(c.name for c in providers._REGISTRY_LIST
                  if c.cardinality == "multi") == [
        "browser", "calendar", "chat", "drive", "gmail", "google", "sheets", "tasks"]
    for nom in ("zoho", "folk"):
        con = providers.REGISTRY[nom]
        assert con.cardinality == "" and con.auth_multi_account is True, nom


def test_la_liste_transverse_de_cardinalite_a_DISPARU():
    """⚠️ LE test du lot (tranché par Alexis le 27/08 : « ce qui gêne, c'est la LISTE
    elle-même »). Sonde le NOM dans tout le code servi : une constante réintroduite
    ailleurs, sous le même nom ou un autre, rouvrirait le même défaut — une propriété
    de connecteur qui vit loin du connecteur."""
    import ast
    import pathlib
    racine = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"
    porteurs = []
    for p in racine.rglob("*.py"):
        # Sonde AST, pas un `grep` de ligne : la prose a le DROIT de dire que la liste
        # a existé — c'est même ce qu'on veut y lire. Ce qu'on cherche est un ACCÈS,
        # un nom ou un attribut ; commentaires et docstrings disparaissent avec l'AST.
        arbre = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(arbre):
            nom = (n.id if isinstance(n, ast.Name)
                   else n.attr if isinstance(n, ast.Attribute)
                   else n.target.id if isinstance(n, ast.AnnAssign)
                   and isinstance(n.target, ast.Name) else None)
            if nom == "MULTI_ACCOUNT_PROVIDERS":
                porteurs.append(f"{p.name}:{getattr(n, 'lineno', '?')}")
    assert not porteurs, (
        f"la liste transverse de cardinalité est de retour : {porteurs}. Une propriété "
        "de connecteur se déclare dans l'entrée du connecteur (`cardinality`), à côté "
        "de ce qu'elle qualifie.")


def test_l_annonce_STATIQUE_de_l_axe_compte_n_est_plus_la_cardinalite():
    """Les deux rôles que la liste confondait sont séparés, et c'est ce qui la rendait
    indéboulonnable : l'annonce statique parle du SCHÉMA des tools (recopié à chaque
    handshake, donc curé), la cardinalité parle du COFFRE. Quatre connecteurs annoncent
    statiquement ; 74 sont multi-compte."""
    statiques = sorted(c.name for c in providers._REGISTRY_LIST if c.account_axis_static)
    assert statiques == ["browser", "calendar", "chat", "drive", "folk", "gmail", "google",
                         "sheets", "tasks", "zoho"]
    multi = [c.name for c in providers._REGISTRY_LIST if c.auth_multi_account]
    assert set(statiques) < set(multi) and len(multi) > len(statiques)
