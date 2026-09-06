"""Salesforce — generic CRUD over sObjects (Contact, Account…) via REST + SOQL.

Credential = OAuth2 Connected App à 3 secrets (client_id/client_secret/refresh_token)
+ `login_url` non-secret (login.salesforce.com prod, test.salesforce.com sandbox, ou
My Domain) → modèle générique multi-champs (ADR 0011), résolu par appel via
`access.resolve_credential_fields("salesforce")`. byo_user OU byo_org (pas de quota
plateforme : le credential EST le grant). Contrairement à Zoho, pas de table de
région fixe : le refresh Salesforce renvoie l'`instance_url`, mis en cache en mémoire
côté client avec l'access token.

"Companies" = l'sObject standard **Account** ; contacts = **Contact**. Surface
générique par `sobject` (comme hubspot/zoho) plutôt que des tools contact/account
dédiés — couvre aussi Lead/Opportunity/objets custom sans code supplémentaire.

**Surface consolidée (ADR 0047 §Amendement, appliqué au connecteur salesforce)** :
un tool par OBJET métier, le verbe en paramètre `op` — `salesforce_record`
(list/get/create/update/delete/upsert/bulk_create/bulk_update, tous scopés par
`sobject`), `salesforce_query` (soql/sosl) et `salesforce_note` (list/create sur
un enregistrement). Les deux décisions de périmètre :

- **les bulk sont des `op` de `salesforce_record`**, pas un tool à part : `items`
  est le pluriel de `data`, tout est scopé par le même `sobject`, et l'avertissement
  « prends-moi plutôt que N create » se lit alors À CÔTÉ de `op="create"`, là où
  l'agent le cherche. Coût réel de la fusion : deux paramètres (`items`,
  `all_or_none`), pas une variante disjointe.
- **`salesforce_query` porte SOQL *et* SOSL** : deux langages, mais un seul
  paramètre, de même forme (une chaîne) et de même retour — le critère est
  l'homogénéité des paramètres, et ici elle est totale. `op` nomme le langage.

`salesforce_describe` reste **SEUL** : il décrit un TYPE, pas un enregistrement ;
sa sortie est un schéma projeté (pas des lignes), son `verbose` n'a de contrepartie
nulle part, et c'est lui qui énumère les `fields` que les autres consomment — même
rôle de découverte que `zoho_modules` / `gmail_list_accounts`.

⚠️ **Ce module ÉCRIT dans le CRM du client** : `salesforce_record` op=create /
update / delete / upsert / bulk_create / bulk_update, et `salesforce_note`
op=create. Tous les défauts d'`op` sont des LECTURES (`salesforce_record`
op="list", `salesforce_note` op="list", `salesforce_query` op="soql") : un appel
sans `op` ne peut ni écrire, ni supprimer.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, egress, status_hints
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import verify as connector_verify


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument obligatoire pour CET op — erreur actionnable qui NOMME l'op et
    l'argument, jamais un fallback.

    Une chaîne VIDE compte comme absente : `record_id=""` ne désigne aucun
    enregistrement, et l'URL construite viserait la COLLECTION — donc un autre
    enregistrement que celui voulu, ou une suppression qui rate sa cible."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _bad(f"op='{op}' requiert {name}")
    return value


def _login_url(login_url: Optional[str]) -> str:
    """Le serveur d'auth à appeler — point de passage UNIQUE des deux chemins
    (sonde et tools), donc l'endroit où la garde d'egress mord une seule fois.

    Le défaut est une constante de ce module : rien à contrôler. Une valeur
    SAISIE, elle, vient de la carte d'une organisation, et un `login_url` qui
    résout vers l'intérieur enverrait le refresh token — donc un secret — vers
    un service de la machine (`oto_mcp/egress.py`)."""
    valeur = (login_url or "").strip().rstrip("/")
    if not valeur:
        return "https://login.salesforce.com"
    egress.check_url(valeur, connector="salesforce", field="login_url")
    return valeur


# Ce qu'il faut d'un champ pour le LIRE ou l'ÉCRIRE — le reste des 57 clés que
# Salesforce renvoie par champ (aggregatable, byteLength, compoundFieldName, mask…)
# ne sert à personne côté agent.
_DESCRIBE_FIELD_KEYS = ("name", "label", "type", "length", "nillable",
                        "createable", "updateable", "referenceTo", "defaultValue")
_DESCRIBE_OBJECT_KEYS = ("name", "label", "labelPlural", "custom", "createable",
                         "updateable", "deletable", "queryable", "searchable", "keyPrefix")


def _project_describe(raw: dict) -> dict:
    """Projection resserrée d'un describe sObject (signal #339).

    Le payload brut d'un Account standard fait ~220 Ko / 45 clés (127
    childRelationships, actionOverrides, recordTypeInfos…) : trop gros pour le
    contexte d'un agent, donc tronqué et déporté en fichier par le client — donc
    inchaînable, alors que seuls 51 champs comptent. On garde l'objet + ses champs,
    `verbose=True` rend le brut à qui en a besoin."""
    fields = []
    for f in (raw.get("fields") or []):
        out = {k: f.get(k) for k in _DESCRIBE_FIELD_KEYS if f.get(k) not in (None, [], "")}
        out["name"] = f.get("name")          # toujours présent, même vide
        # Un picklist n'est utile qu'en VALEURS d'API actives (l'objet complet porte
        # label/validFor/defaultValue par entrée = 4× le poids pour rien).
        picks = [p.get("value") for p in (f.get("picklistValues") or []) if p.get("active")]
        if picks:
            out["picklistValues"] = picks
        fields.append(out)
    obj = {k: raw.get(k) for k in _DESCRIBE_OBJECT_KEYS if raw.get(k) is not None}
    obj["fields"] = fields
    obj["field_count"] = len(fields)
    obj["_note"] = ("Projection (name/label/type/length/nillable/createable/updateable/"
                    "referenceTo/picklistValues). verbose=true pour le payload Salesforce brut.")
    return obj


def _salesforce_credential_state(fields: dict) -> status_hints.CredentialState:
    """SOURCE UNIQUE de « ce credential Salesforce est-il utilisable ? ».

    Connexion en DEUX temps, comme Zoho : on pose la Connected App (Consumer Key +
    Secret + Login URL), puis on consent — et c'est le consentement qui produit le
    refresh_token. L'état intermédiaire est NORMAL, pas une panne : sans cette
    déclaration, `api_key_save` sonderait un credential incomplet par construction,
    refuserait la pose, et le bouton Connecter deviendrait injoignable (le blocage
    circulaire vécu sur Zoho le 28/07). Un seul libellé, rendu tel quel par toutes
    les surfaces."""
    if (fields.get("client_id") and fields.get("client_secret")
            and not fields.get("refresh_token")):
        return status_hints.CredentialState(
            complete=False, missing=("refresh_token",),
            next_action=("Connected App enregistrée, mais l'autorisation n'a pas "
                         "encore été donnée — clique « Connecter » sur la fiche du "
                         "connecteur pour ouvrir le consentement Salesforce."))
    return status_hints.CredentialState(complete=True)


def _salesforce_pending_action(sub: str, org, group, entry: dict):  # noqa: ARG001
    """Étape qui manque, pour le verdict de la fiche — le PENDANT d'affichage de
    `_salesforce_credential_state`.

    Les deux hooks sont nécessaires et ne servent pas au même moment : `register_state`
    dit à la POSE si l'incomplétude est attendue (sinon la sonde refuse d'écrire),
    celui-ci dit à la LECTURE ce qu'il reste à faire. Sans lui, la carte paraît
    configurée — l'app est bien posée — et échoue au premier appel d'outil. Calqué sur
    `zoho._pending_action_for` : même seam, même fail-open, même libellé unique rendu
    tel quel par toutes les surfaces."""
    if entry.get("mode") == "forbidden":
        return None   # rien de posé → le verdict « à connecter » suffit
    try:
        # `resolve_credential(sub=…)` : le hook tourne depuis /api/me (REST), hors
        # contexte MCP → le sub doit être EXPLICITE. `emit_on_failure=False` : sonde
        # d'affichage, elle ne doit pas fausser le signal d'usage.
        fields = access.resolve_credential(
            "salesforce", want="byo", sub=sub, emit_on_failure=False).fields
    # noqa: SILENT — sonde d'affichage : sans credential, pas d'action en attente à proposer
    except Exception:  # noqa: BLE001 — fail-open, jamais /api/me en erreur
        return None
    st = _salesforce_credential_state(fields)
    return None if st.complete else "Autorise oto chez Salesforce"


status_hints.register_state("salesforce", _salesforce_credential_state)
status_hints.register("salesforce", _salesforce_pending_action)


def _start_flow(ctx, values: dict) -> dict:
    """Point d'entrée du flux générique — délègue au MÊME handler que la capacité
    `me.salesforce_connect`, pour qu'il n'existe qu'une façon de démarrer.

    `app` (comme `scope`) est une clé cachée, pas un `FlowParam` déclaré : le
    dashboard/front la passe hors formulaire (le client sait qui il est), elle ne
    doit jamais devenir un champ visible à l'utilisateur."""
    from ..capabilities import salesforce_connect
    return salesforce_connect.start_for(
        ctx, (values.get("scope") or "member"), values.get("app"))


# Le flux de consentement, déclaré comme celui de Zoho — c'est ce qui fait apparaître le
# bouton sur la fiche, SANS que le dashboard ait à connaître le nom « salesforce ».
# ⚠️ PAS de paramètre « Pour qui ? ». Il a existé, et c'était un pansement : la surface
# ORG n'avait pas de bouton de connexion, donc consentir pour l'org ne pouvait se faire
# que depuis la fiche PERSONNELLE, en le déclarant dans un menu. Le levier manquant a été
# posé (02/08) — le sélecteur est alors devenu une question absurde : on est sur sa fiche,
# on autorise pour soi ; on est sur la fiche de l'org, on autorise pour l'org. Le scope se
# DÉDUIT de la surface, l'appelant le passe (`values["scope"]`), on ne le demande plus.
connector_flow.declare(
    "salesforce",
    start=_start_flow,
    label="Autoriser oto chez Salesforce",
    callback_path="/api/salesforce/oauth/callback",
)


def _sf_error_hint(exc: Exception) -> str:
    """Traduit l'erreur OAuth Salesforce brute en message actionnable. Utilisée
    par la sonde `_verify` (credential déjà posé) ET par le flow OAuth live
    (`salesforce_oauth.exchange_code`, échec de l'échange authorization_code) —
    les deux surfaces d'erreur Salesforce partagent le même vocabulaire brut,
    donc les mêmes branches de correspondance s'appliquent.

    ⚠️ Une traduction AJOUTE, elle ne REMPLACE jamais. La version précédente
    substituait sa supposition au dire du fournisseur : un `invalid_grant` était
    systématiquement rendu « refresh token périmé, ou login_url incorrect », alors
    que Salesforce disait autre chose (code d'autorisation expiré, appel depuis
    une IP non autorisée…). Le message accusait la mauvaise pièce et envoyait
    corriger ce qui marchait — une heure perdue le 31/07."""
    raw = " ".join(str(exc).split())[:220]
    low = raw.lower()
    hint = _sf_hint_for(low)
    return f"{hint} (Salesforce dit : {raw})" if hint else (
        f"échec de connexion Salesforce : {raw}")


def _sf_hint_for(low: str) -> str:
    """La correspondance seule — sans le dire du fournisseur, que l'appelant joint."""
    if "invalid_client" in low or "invalid_client_id" in low:
        return ("client_id / client_secret incorrect — vérifie la Connected App "
                "Salesforce (Consumer Key / Consumer Secret).")
    if "invalid_grant" in low:
        return ("le grant a été refusé — jeton révoqué ou expiré, code d'autorisation "
                "déjà consommé, ou appel bloqué par les restrictions IP de l'app "
                "(le rafraîchissement part de NOTRE serveur, pas de ton navigateur). "
                "Le motif exact est entre parenthèses ci-dessous.")
    if "invalid_scope" in low:
        return ("les OAuth Scopes de la Connected App n'incluent pas `api` et "
                "`refresh_token` (ou `offline_access`) — Setup → App Manager → "
                "ton app → Edit Policies → OAuth Scopes, puis réessaie.")
    if "redirect_uri_mismatch" in low:
        # DÉRIVÉE, jamais écrite : ce message est lu depuis la prod ET la preprod, et
        # chacune envoie sa propre redirect_uri. Une URL en dur y désignait toujours la
        # prod — donc un utilisateur de preprod lisait « doit être exactement <prod> »
        # alors que son backend envoyait autre chose. Le message accusait la victime.
        from ..connectors import flow as connector_flow
        attendue = connector_flow.callback_url("salesforce") or "l'URL affichée sur la fiche"
        return ("Callback URL de la Connected App incorrecte — doit être exactement "
                f"{attendue} (vérifie qu'il n'y a pas d'espace ni de slash final en trop).")
    return ""


def _verify(fields: dict, config: dict | None = None,
            instance: tuple | None = None) -> None:  # noqa: ARG001 (config: contrat de sonde, non utilisé ici)
    """Sonde en deux temps (auth PUIS accès réel) :

    1. **refresh du token OAuth** : valide client_id + client_secret + refresh_token +
       login_url d'un coup (échec → message actionnable via `_sf_error_hint`) ;
    2. **lecture réelle** (`SELECT Id FROM Contact LIMIT 1`) : un token peut
       authentifier mais le profil/permission set de la Connected App peut ne pas
       donner accès à l'objet Contact — capté ici plutôt qu'au premier appel agent.

    ⚠️ **Elle n'est plus « sans effet de bord », et ne peut pas l'être.** Sous rotation
    (RTR, imposée par Salesforce), l'étape 1 consomme le refresh token et en reçoit un
    neuf : une sonde qui ne persiste pas ce remplaçant DÉTRUIT la connexion qu'elle
    prétend vérifier. C'est ce qui s'est produit le 31/07 — la sonde post-écriture de
    `persist_token` tuait le jeton 500 ms après sa pose. Elle branche donc la même
    persistance que le chemin des outils, quand elle porte sur un credential déjà
    stocké.
    """
    from oto.tools.salesforce.client import SalesforceClient

    client = SalesforceClient(
        client_id=fields.get("client_id"),
        client_secret=fields.get("client_secret"),
        refresh_token=fields.get("refresh_token"),
        login_url=_login_url(fields.get("login_url")),
        # `instance` = la clé RÉELLEMENT sondée, fournie par l'appelant. Sans elle on
        # ne peut que deviner via la cascade — qui désigne la plus proche, pas celle
        # qu'on teste : un `verify level=org` chez quelqu'un qui a AUSSI une clé perso
        # comparait le jeton d'org au jeton perso, ne reconnaissait pas, ne persistait
        # rien, et tuait donc le jeton d'org en le rafraîchissant. Vécu 03/08.
        on_refresh=_rotation_writer_for(fields.get("refresh_token") or "", instance),
    )
    try:
        client.query("SELECT Id FROM Contact LIMIT 1")
    except Exception as e:  # noqa: BLE001 — l'erreur provider EST le retour de la sonde
        raise ValueError(_sf_error_hint(e)) from e


class _Cible:
    """L'entité sondée, sous la forme attendue par `_rotation_writer`."""

    def __init__(self, entity_type, entity_id, account=""):
        self.entity_type, self.entity_id, self.account = entity_type, entity_id, account


def _rotation_writer_for(jeton_lu: str, instance: tuple | None = None):
    """Le writer de rotation pour la SONDE.

    Quand l'appelant DIT quelle entité il teste (`instance`), on écrit là — sans
    deviner. C'est le cas nominal, et le seul correct dès qu'il existe plusieurs clés
    pour un même connecteur : la cascade désigne la plus PROCHE, pas celle qu'on sonde.

    Sinon (appelants qui ne le fournissent pas encore) on retombe sur la cascade, et
    on ne branche l'écriture que si le credential résolu porte bien le jeton qu'on
    s'apprête à consommer. Deux cas où l'on ne persiste rien, volontairement :

    - **sonde avant persistance** (`api_key_save`) : les champs testés sont des
      candidats, aucune ligne ne les porte encore — il n'y a rien à mettre à jour ;
    - **hors contexte de requête** (CLI, test) : pas d'org, donc pas de cascade.

    Dans les deux cas on retombe sur l'ancien comportement (aucune écriture), ce qui
    est correct : on ne peut pas corrompre ce qu'on n'a pas identifié.
    """
    from .. import access

    if instance is not None:
        etype, eid, *reste = instance
        return _rotation_writer(_Cible(etype, eid, reste[0] if reste else ""), jeton_lu)
    try:
        rc = access.resolve_credential("salesforce", emit_on_failure=False)
    # noqa: SILENT — dette déclarée : le jeton rafraîchi n'est pas persisté (#424, verdict C)
    except Exception:  # noqa: BLE001 — pas de credential résolu = rien à persister
        return None
    if rc.entity_type is None or (rc.fields or {}).get("refresh_token") != jeton_lu:
        return None
    return _rotation_writer(rc, jeton_lu)


def _rotation_writer(rc, jeton_lu: str):
    """Persiste le refresh token RENOUVELÉ, là où l'ancien a été lu.

    Salesforce impose la rotation (RTR) sur les External Client Apps : chaque
    rafraîchissement invalide le jeton utilisé et en renvoie un neuf. Ne pas
    l'écrire revient à révoquer la connexion au premier appel — et à la faire
    révoquer *complètement* au second, Salesforce traitant la réutilisation d'un
    jeton consommé comme une compromission (révocation du jeton courant ET des
    access tokens associés).

    ⚠️ **Écriture conditionnelle**, pas un écrasement : on ne réécrit que si le
    jeton stocké est toujours celui qu'on a lu. Deux appels concurrents (ou la
    preprod, qui partage cette base avec la prod) peuvent avoir tourné entre-temps ;
    écraser aveuglément remettrait en place un jeton déjà consommé, c'est-à-dire
    exactement le geste que Salesforce interprète comme une attaque.
    """
    from .. import credentials_store

    def _write(token_data: dict) -> None:
        # `on_refresh` n'est invoqué qu'APRÈS un refresh d'access token RÉUSSI (jamais
        # sur échec — cf. `oto.tools.salesforce.client`) : c'est le déclencheur
        # "refresh réussi" du démarquage (oto#25 lot b3), inconditionnel — qu'il y ait
        # ROTATION du refresh token ou non, contrairement à la persistance ci-dessous.
        if rc.entity_type is not None:
            connector_health.record_health(
                "salesforce", (rc.entity_type, rc.entity_id, rc.account), True, None)
        nouveau = token_data.get("refresh_token")
        # Pas de rotation, ou grant plateforme (pas de ligne de coffre à réécrire).
        if not nouveau or nouveau == jeton_lu or rc.entity_type is None:
            return
        row = credentials_store.get_credential_with_meta(
            rc.entity_type, rc.entity_id, "salesforce", account=rc.account)
        if not row or not row.get("secret"):
            return
        champs = credentials_store.unpack_secret("salesforce", row["secret"])
        if champs.get("refresh_token") != jeton_lu:
            return  # quelqu'un d'autre a déjà tourné : sa valeur est plus récente
        # ⚠️ `meta` DOIT être repassé. L'upsert fait `meta = EXCLUDED.meta` avec
        # `json.dumps(meta or {})` : omettre l'argument n'est pas « ne pas toucher au
        # meta », c'est l'ÉCRASER par {}. Comme la rotation réécrit à chaque appel
        # d'outil, la version précédente effaçait `instance_url`/`identity_url`/
        # `connected_at` dès le premier usage — on ne savait alors plus sur quelle org
        # Salesforce la clé pointait. Repéré le 03/08, sur une clé qui avait tourné
        # depuis la veille pendant qu'une clé fraîche avait encore son meta intact.
        credentials_store.set_credential(
            rc.entity_type, rc.entity_id, "salesforce",
            credentials_store.pack_secret("salesforce",
                                          {**champs, "refresh_token": nouveau}),
            account=rc.account, meta=row.get("meta") or {})

    return _write


# sObject Collections plafonne à 200 enregistrements par appel — limite DURE
# Salesforce (vérifiée sur la doc officielle), pas une politique oto : on la
# fait échouer tôt côté tool plutôt que de laisser Salesforce renvoyer un 400.
_MAX_COLLECTION_RECORDS = 200


def _validate_bulk_items(items: list) -> None:
    if not items:
        raise _bad("items : au moins un enregistrement requis.")
    if len(items) > _MAX_COLLECTION_RECORDS:
        raise _bad(
            f"{len(items)} éléments — sObject Collections plafonne à "
            f"{_MAX_COLLECTION_RECORDS} par appel, découper en plusieurs appels."
        )


def _validate_update_items_have_id(items: list) -> None:
    for i, item in enumerate(items):
        if not item.get("Id"):
            raise _bad(f"items[{i}] : \"Id\" requis pour mettre à jour un enregistrement.")


def _bulk_receipt(raw: list[dict]) -> dict:
    """Normalise la réponse sObject Collections (liste de {id, success, errors},
    même ordre que les items envoyés) en un reçu indexé — même esprit que le
    reçu bulk de folk_create, jamais N corps de réponse complets."""
    results = [{"index": i, **r} for i, r in enumerate(raw)]
    return {"total": len(raw),
            "succeeded": sum(1 for r in raw if r.get("success")),
            "results": results}


# Ops de chaque tool, lectures d'abord. SOURCE UNIQUE : la garde d'entrée, le
# message de refus ET l'enum du schéma (`Literal[…]` en signature) en dérivent — une
# op ajoutée ne peut donc pas être acceptée sans être annoncée (ni annoncée sans être
# acceptée). Le découpage read/write n'est pas décoratif : il documente ce qu'un
# défaut d'`op` peut atteindre (jamais une écriture).
_RECORD_READ_OPS = ("list", "get")
_RECORD_WRITE_OPS = ("create", "update", "delete", "upsert",
                     "bulk_create", "bulk_update")
_RECORD_OPS = _RECORD_READ_OPS + _RECORD_WRITE_OPS
_RECORD_OPS_ERROR = ("op doit être 'list', 'get', 'create', 'update', 'delete', "
                     "'upsert', 'bulk_create' ou 'bulk_update'")

_QUERY_OPS = ("soql", "sosl")            # les deux sont des LECTURES
_QUERY_OPS_ERROR = "op doit être 'soql' ou 'sosl'"

_NOTE_READ_OPS = ("list",)
_NOTE_WRITE_OPS = ("create",)
_NOTE_OPS = _NOTE_READ_OPS + _NOTE_WRITE_OPS
_NOTE_OPS_ERROR = "op doit être 'list' ou 'create'"


def register(mcp: FastMCP) -> None:
    connector_verify.register("salesforce", _verify)
    from oto.tools.salesforce import SalesforceAuthError
    from oto.tools.salesforce.client import SalesforceClient

    def _client() -> tuple[SalesforceClient, "access.ResolvedCredential"]:
        # On passe par `resolve_credential` (et non `resolve_credential_fields`) parce
        # qu'on a besoin de l'ENTITÉ gagnante de la cascade : sous rotation, il faut
        # réécrire le jeton renouvelé exactement là où il a été lu — clé membre, clé
        # d'équipe ou clé d'org — sinon on le range au mauvais niveau. La même entité
        # sert aussi à marquer une ligne rejetée (oto#25 lot b2, `rc` rendu à l'appelant).
        rc = access.resolve_credential("salesforce")
        creds = rc.fields
        client = SalesforceClient(
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
            refresh_token=creds.get("refresh_token"),
            login_url=_login_url(creds.get("login_url")),
            on_refresh=_rotation_writer(rc, creds.get("refresh_token") or ""),
        )
        return client, rc

    @contextmanager
    def _marks_rejection(rc):
        """Sur `SalesforceAuthError` (refus du REFRESH — grant mort ; jamais un 401 nu
        d'un geste applicatif sur un enregistrement précis, avec une clé par ailleurs
        saine — cf. `oto.tools.salesforce.client`, seul point qui la lève), marque la
        ligne DE COFFRE réellement servie rejetée (`connectors.health.mark_rejected`,
        même garde de portée que `verify`), PUIS RE-LÈVE — marquer n'est jamais un
        fallback qui avale l'erreur réelle (oto#25 lot b2)."""
        try:
            yield
        except SalesforceAuthError as e:
            connector_health.mark_rejected(
                rc.entity_type, rc.entity_id, "salesforce", rc.account, str(e))
            raise

    @mcp.tool()
    def salesforce_describe(sobject: str, verbose: bool = False) -> dict:
        """Field metadata for an sObject type (e.g. "Account", "Contact", or custom).

        Returns a TIGHT projection: the object's own flags + one entry per field with
        what you need to read or write it (name, label, type, length, nillable,
        createable, updateable, referenceTo, picklist values). Salesforce's raw
        describe is ~220 KB for a standard Account (127 childRelationships,
        actionOverrides, recordTypeInfos, 57 keys per field) — too big to chain on.

        Args:
            sobject: e.g. "Account", "Contact", or a custom "Foo__c".
            verbose: True → the RAW Salesforce payload, unprojected. Only when you
                need something the projection drops (child relationships, layouts);
                expect it to be truncated by the client.
        """
        client, rc = _client()
        with _marks_rejection(rc):
            raw = client.describe(sobject)
        return raw if verbose else _project_describe(raw)

    @mcp.tool()
    def salesforce_record(
        sobject: str,
        op: Literal[_RECORD_OPS] = "list",
        record_id: Optional[str] = None,
        data: Optional[dict] = None,
        fields: Optional[str] = None,
        where: Optional[str] = None,
        limit: int = 200,
        external_id_field: Optional[str] = None,
        external_id: Optional[str] = None,
        items: Optional[list[dict]] = None,
        all_or_none: bool = False,
    ) -> dict:
        """A record of an sObject type — list, read, create, update, delete,
        upsert, or write up to 200 in one call.

        "Companies" are the standard **Account** sObject; contacts are
        **Contact**. The same surface covers Lead / Opportunity / any custom
        "Foo__c".

        `op`:
        - **"list"** (default): list records of `sobject`, built as a SOQL SELECT.
          Filter with `where`, pick columns with `fields`, cap with `limit`.
        - **"get"**: get one record by id (`record_id`).
        - **"create"** — ⚠️ WRITES: create a record (`data` = field → value), e.g.
          sobject="Contact", data={"FirstName": "Ada", "LastName": "Lovelace",
          "Email": "ada@example.com"}; sobject="Account", data={"Name": "Acme Corp"}.
        - **"update"** — ⚠️ WRITES: update a record's fields (`record_id` + `data`).
        - **"delete"** — ⚠️ WRITES: delete a record. Irreversible.
        - **"upsert"** — ⚠️ WRITES: create-or-update a record keyed on an external
          id field (idempotent).
        - **"bulk_create"** — ⚠️ WRITES: create up to 200 records of the SAME
          sObject type in ONE Salesforce call (sObject Collections) — instead of N
          separate op="create" calls. ⚠️ **Never lean on Salesforce's own
          de-duplication — here or on op="create"**: whether a duplicate rule fires
          at all is that org's Setup, not something oto applies or checks (measured:
          `success: true` on exact duplicates, same name and Account). A rule that
          DOES fire lands as a per-record `DUPLICATES_DETECTED` in `results`, not an
          exception — and records sent together are compared only with what is
          ALREADY in Salesforce, never with each other. Check existence yourself,
          and de-duplicate `items` against itself.
        - **"bulk_update"** — ⚠️ WRITES: update up to 200 records of the SAME
          sObject type in ONE Salesforce call (sObject Collections) — instead of N
          separate op="update" calls.

        Returns —
            The Salesforce payload for the single-record ops. For
            op="bulk_create"/"bulk_update": {"total", "succeeded", "results":
            [{"index", "id", "success", "errors"}, ...]} — ALWAYS inspect
            `results`: unlike op="create", a record can fail here with NO
            exception raised (the call itself succeeded, that one record didn't).

        Field metadata — what fields exist on an sObject, their type, whether they
        are createable/updateable, their picklist values — is a different tool:
        salesforce_describe.

        Args:
            sobject: e.g. "Contact", "Account" (companies), "Lead", "Opportunity",
                or a custom "Foo__c". For the bulk ops, EVERY record in `items`
                must be this type.
            op: list (default) | get | create | update | delete | upsert |
                bulk_create | bulk_update.
            record_id: op="get"/"update"/"delete" — the record id.
            data: op="create"/"update"/"upsert" — field → value.
            fields: op="list"/"get" — comma-separated field names. Optional — a
                sensible default set is used per known sObject if omitted.
            where: op="list" — SOQL WHERE clause without the "WHERE" keyword,
                e.g. "Industry = 'Technology'".
            limit: op="list" — maximum number of records (default 200).
            external_id_field: op="upsert" — name of the external id FIELD to key
                the create-or-update on.
            external_id: op="upsert" — the external id VALUE.
            items: op="bulk_create"/"bulk_update" — field→value dicts, same shape
                as `data`, one per record (max 200 — split into several calls
                above that). For op="bulk_update" each item MUST include "Id"
                (the record to update) plus the fields to change.
            all_or_none: op="bulk_create"/"bulk_update" — if true, the WHOLE batch
                rolls back when any record fails. Default false (Salesforce's own
                default): successes are kept, failures are reported per-record
                below.
        """
        # Refus AVANT toute résolution de credential : une op inconnue n'atteint
        # jamais le client — donc jamais, par un chemin dérivé, une écriture.
        if op not in _RECORD_OPS:
            raise _bad(_RECORD_OPS_ERROR)
        client, rc = _client()

        with _marks_rejection(rc):
            # ---- lectures ------------------------------------------------------
            if op == "list":
                return client.list_records(sobject, fields=fields, where=where,
                                           limit=limit)
            if op == "get":
                return client.get_record(sobject, _need(record_id, "record_id", op),
                                         fields=fields)

            # ---- écritures -----------------------------------------------------
            if op == "create":
                return client.create_record(sobject, _need(data, "data", op))
            if op == "update":
                return client.update_record(sobject,
                                            _need(record_id, "record_id", op),
                                            _need(data, "data", op))
            if op == "delete":
                return client.delete_record(sobject, _need(record_id, "record_id", op))
            if op == "upsert":
                return client.upsert_record(
                    sobject,
                    _need(external_id_field, "external_id_field", op),
                    _need(external_id, "external_id", op),
                    _need(data, "data", op))
            if op == "bulk_create":
                _validate_bulk_items(_need(items, "items", op))
                raw = client.create_records(sobject, items, all_or_none=all_or_none)
                return _bulk_receipt(raw)
            if op == "bulk_update":
                _validate_bulk_items(_need(items, "items", op))
                _validate_update_items_have_id(items)
                raw = client.update_records(sobject, items, all_or_none=all_or_none)
                return _bulk_receipt(raw)

            # Structurellement inatteignable (garde d'entrée ci-dessus) — filet contre
            # un `return None` implicite si une op était ajoutée à `_RECORD_OPS` sans
            # sa branche : mieux vaut refuser que rendre « rien » pour un succès.
            raise _bad(_RECORD_OPS_ERROR)

    @mcp.tool()
    def salesforce_query(query: str, op: Literal[_QUERY_OPS] = "soql") -> dict:
        """Run a raw statement against the org — SOQL query or SOSL search.

        `op`:
        - **"soql"** (default): a SOQL query, e.g.
          "SELECT Id, Name FROM Account WHERE Industry = 'Technology'".
        - **"sosl"**: a SOSL search, e.g.
          "FIND {Acme} IN ALL FIELDS RETURNING Account(Id, Name)".

        Both are reads. For a plain listing of one sObject you do not need to write
        SOQL at all: salesforce_record(op="list", sobject=…, where=…) builds it.

        Args:
            query: the statement itself — SOQL under op="soql", SOSL (a FIND …
                statement) under op="sosl". The two languages are NOT
                interchangeable: a FIND statement sent with the default op is
                rejected by Salesforce as a malformed query.
            op: soql (default) | sosl.
        """
        if op not in _QUERY_OPS:
            raise _bad(_QUERY_OPS_ERROR)
        client, rc = _client()
        with _marks_rejection(rc):
            if op == "soql":
                return client.query(_need(query, "query", op))
            if op == "sosl":
                return client.search(_need(query, "query", op))
            raise _bad(_QUERY_OPS_ERROR)   # inatteignable, cf. salesforce_record

    @mcp.tool()
    def salesforce_note(
        record_id: str,
        op: Literal[_NOTE_OPS] = "list",
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> dict:
        """The Enhanced Notes attached to a record.

        Enhanced Notes = the **ContentNote** object, the Lightning default — NOT
        supported on orgs still on classic Notes.

        `op`:
        - **"list"** (default): list the notes attached to `record_id`.
        - **"create"** — ⚠️ WRITES: add an Enhanced Note to the record.

        Args:
            record_id: the record the notes are attached to (any sObject).
            op: list (default) | create.
            title: op="create" — the note title.
            body: op="create" — the note body.
        """
        if op not in _NOTE_OPS:
            raise _bad(_NOTE_OPS_ERROR)
        client, rc = _client()
        with _marks_rejection(rc):
            if op == "list":
                return {"notes": client.list_notes(record_id)}
            if op == "create":
                return client.create_note(record_id, _need(title, "title", op),
                                          _need(body, "body", op))
            raise _bad(_NOTE_OPS_ERROR)    # inatteignable, cf. salesforce_record
