"""`ToolAliasMiddleware` — les noms d'outils au nom du PRODUIT du tenant."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware
from ..mcp_errors import McpError
from mcp.types import ErrorData

from .. import deprecations, handshake_log, session_org, tool_alias
from ..auth.hooks import current_user_sub_from_token
from . import _handshake
from .rappel_contexte import avec_lignes_en_tete

logger = logging.getLogger(__name__)


class ToolAliasMiddleware(handshake_log.JournalListesMixin, Middleware):
    """Traduit les noms d'outils de la plateforme au nom du PRODUIT du tenant.

    OUTERMOST, et c'est tout l'intérêt : le nom canonique (`oto_doc`) est rétabli
    AVANT que quoi que ce soit d'autre ne le lise — gates de contexte d'appel
    (`call_axes.axes_for`), politique de rédaction (`namespace_of`), visibilité de
    session, journal `tool_calls`. Rien en aval n'apprend qu'un alias existe, donc
    rien en aval ne peut diverger : les toggles, l'audit et les références
    `<tool:slug>` des procédures restent écrits en canonique, hier comme demain.

    En SORTIE, `on_list_tools` est au contraire le DERNIER à retoucher la liste (une
    chaîne extern→interne se déroule en sens inverse au retour) : le renommage
    s'applique après le filtrage de visibilité et l'enrichissement des descriptions,
    sur la liste réellement servie. Le TITRE de chaque outil (posé au montage,
    `tool_alias.poser_les_titres`) suit le nom ici : un alias déprécié reçoit le sien,
    un outil renommé au préfixe du tenant voit `Oto:` devenir le nom du produit.

    Deux crans de prudence :

    - un alias qui écraserait un outil RÉEL est abandonné (le nom canonique reste
      servi). Ça ne devrait pas arriver — `tool_alias.normalize_prefix` refuse un
      préfixe qui est un namespace déclaré — mais la liste est ici, la vérification
      est gratuite, et la conséquence d'une collision serait qu'un outil en éclipse
      un autre sans que personne ne le voie ;
    - aucun accès DB : `tool_alias.prefix_for` lit le registre d'émetteurs en
      mémoire. Ce hook s'exécute DANS la boucle (serveur MONO-LOOP).

    Fail-open partout : pas de sub (endpoint anonyme, découverte), pas de préfixe
    déclaré, ou erreur ⟹ les noms canoniques, à l'octet près.

    **Deuxième traduction, même bord : les noms DÉPRÉCIÉS** (`deprecations.TOOLS`,
    #519). Un outil renommé est servi sous ses DEUX noms — le nouveau, et l'ancien
    avec un avis de retrait daté en tête de sa description — et les deux s'appellent.
    La réponse d'un appel fait sous l'ancien nom porte le même avis en tête de son
    canal texte (`deprecations.avis_d_appel`), et le journal compte l'usage de l'alias
    (`session_org.nom_deprecie_de_l_appel`) sans changer le nom qu'il écrit.
    Ce n'est pas la même règle que le préfixe de tenant (celle-là dépend de QUI
    appelle, celle-ci de rien), mais c'est le même endroit : le seul où un nom peut
    être traduit sans que le reste du serveur l'apprenne. Le mettre ailleurs
    obligerait chaque gate, la denylist, le journal et les refs `<tool:slug>` à
    connaître les deux noms — et à diverger le jour où l'un d'eux l'oublie.
    """

    @staticmethod
    def _prefix() -> str:
        # `prefix_for` ne lève pas (registre en mémoire, fail-open journalisé chez
        # lui) : seul l'échec d'IDENTITÉ pouvait tomber ici. Il monte — le rattraper
        # servait nos noms canoniques, donc notre produit, à un compte dont on ignorait
        # l'identité, et la requête continuait anonyme sans un mot (#464).
        return tool_alias.prefix_for(current_user_sub_from_token())

    @staticmethod
    def _avec_alias_deprecies(tools):
        """L'ancien nom d'un outil renommé, servi À CÔTÉ du nouveau (#519).

        Dérivé de la liste RÉELLEMENT servie, jamais du registre : l'alias hérite
        donc du filtrage de visibilité de son outil — un outil masqué pour ce compte
        ne réapparaît pas par son ancien nom. Un alias qui tomberait sur un nom déjà
        pris est abandonné (le nom réel gagne) : la conséquence d'une collision
        serait qu'un outil en éclipse un autre sans que personne ne le voie.
        """
        if not deprecations.TOOLS:
            return tools
        pris = {t.name for t in tools}
        out = list(tools)
        for t in tools:
            for ancien in deprecations.tools_deprecies_de(t.name):
                if ancien in pris:
                    continue
                pris.add(ancien)
                # Son propre titre : copié tel quel, l'ancien nom porterait le titre
                # du nouveau, et l'annuaire afficherait deux fois le même outil.
                titre = f"{tool_alias.titre_pose(t)} (deprecated name: {ancien})"
                out.append(t.model_copy(update={
                    "name": ancien,
                    "description": (deprecations.avis(t.name, ancien)
                                    + (t.description or "")),
                    **tool_alias.champs_titre(t, titre),
                }))
        return out

    async def on_list_tools(self, context, call_next):
        """La liste d'outils servie — et sa JOURNALISATION (`handshake_log`) : ce
        middleware est le dernier à retoucher la liste (alias dépréciés ajoutés,
        préfixe de tenant appliqué), donc son compte est celui que le client reçoit."""
        tools = self._liste_servie(await call_next(context))
        handshake_log.log_list("tools/list", context, tools)
        return tools

    def _liste_servie(self, tools):
        try:
            tools = self._avec_alias_deprecies(tools)
        except Exception:  # noqa: BLE001 — un avis de dépréciation ne casse pas une liste
            logger.warning("alias dépréciés non servis (fail-open, noms canoniques)",
                           exc_info=True)
        prefix = self._prefix()
        if not prefix:
            return tools
        try:
            pris = {t.name for t in tools}
            out = []
            for t in tools:
                maj = {}
                nom = tool_alias.public(t.name, prefix)
                if nom != t.name and nom not in pris:
                    maj["name"] = nom
                # Le TITRE au nom du produit : `Oto: Doc` → `Acme: Doc`, y compris pour
                # `data_*`/`run_*`/`feedback`, dont le NOM reste (nom de capacité) mais
                # dont le titre portait notre marque.
                titre = tool_alias.titre_pose(t)
                public = tool_alias.titre_public(titre, prefix)
                if public != titre:
                    maj.update(tool_alias.champs_titre(t, public))
                # La DESCRIPTION aussi : c'est sur elle que le modèle choisit, et 27
                # d'entre elles renvoient à un outil voisin (« resolve it with
                # oto_kb »). Laissée en canonique, elle fait rappeler un nom que le
                # client réaffiche sous notre marque — le défaut qu'on corrige, par
                # la porte de derrière. Mesuré à ~2 ms pour les 200 Ko servis, et nul
                # pour un tenant sans préfixe (on sort avant).
                desc = tool_alias.rewrite_prose(t.description or "", prefix)
                if desc != (t.description or ""):
                    maj["description"] = desc
                out.append(t.model_copy(update=maj) if maj else t)
            return out
        except Exception:
            logger.warning("renommage des outils échoué (fail-open, noms canoniques)",
                           exc_info=True)
            return tools

    async def on_initialize(self, context, call_next):
        """`serverInfo` au nom du PRODUIT — le dernier recoin de la classe de défaut
        que ce middleware ferme : les outils disaient `acme_…` mais le handshake
        annonçait encore `oto`. `name` suit le `tool_prefix` déclaré (l'identifiant,
        cohérent avec les noms d'outils), `title` le nom du tenant (le libellé
        humain). Rien de déclaré ⟹ l'annonce d'avant, à l'octet près (fail-open).

        ⚠️ Résolu AVANT `call_next` et posé sur la session (`_handshake`) : la réponse
        part PENDANT `call_next`, la modifier ensuite ne change rien à ce que le client
        reçoit. `title`, lui, n'a pas de champ dans les options de session de la
        bibliothèque MCP (`InitializationOptions`) — il ne peut pas partir par cette
        voie ; on le garde sur `result` pour ce qui le relit côté serveur."""
        try:
            name, title = tool_alias.server_identity_for(current_user_sub_from_token())
        except Exception:  # noqa: BLE001 — une identité d'affichage ne casse pas un handshake
            logger.warning("renommage du serverInfo échoué (fail-open)", exc_info=True)
            name, title = None, None
        if name:
            _handshake.poser_avant_reponse(context, server_name=name)
        result = await call_next(context)
        handshake_log.log_initialize(context, result)
        if result is None or getattr(result, "serverInfo", None) is None:
            return result
        if not name and not title:
            return result
        maj = {}
        if name:
            maj["name"] = name
        if title:
            maj["title"] = title
        return result.model_copy(
            update={"serverInfo": result.serverInfo.model_copy(update=maj)})

    async def on_call_tool(self, context, call_next):
        prefix = self._prefix()
        name = getattr(context.message, "name", "") or ""
        # Les deux traductions se composent, dans cet ordre : un ancien nom porté
        # par le préfixe d'un tenant (`acme_admin_…`) revient d'abord au canonique de
        # plateforme, puis au nom d'aujourd'hui.
        plateforme = tool_alias.canonical(name, prefix)
        canonical = deprecations.tool_canonique(plateforme)
        if canonical != name:
            context = context.copy(
                message=context.message.model_copy(update={"name": canonical}))
        # Appelé sous un ancien nom : le nom DEMANDÉ reste lisible du journal, qui
        # compte l'usage de l'alias, et la réponse le dit en tête — même place que les
        # avis de l'appel (`session_org.noter_avis`).
        ancien = plateforme if canonical != plateforme else None
        jeton = session_org.set_nom_deprecie(ancien) if ancien else None
        try:
            result = await call_next(context)
        except McpError as e:
            if not prefix:
                raise
            raise self._erreur_traduite(e, prefix) from e
        finally:
            if jeton is not None:
                session_org.reset_nom_deprecie(jeton)
        if ancien:
            return avec_lignes_en_tete(result, [deprecations.avis_d_appel(ancien)])
        return result

    @staticmethod
    def _erreur_traduite(erreur: McpError, prefix: str) -> McpError:
        """Le contrat d'erreur (`ErrorEnvelopeMiddleware`, plus interne) NOMME des
        outils dans son message et son `hint` — « appelle-le via oto_call(…) »,
        « installe le connecteur — oto_connector(op='select') ». C'est le PREMIER mur
        que rencontre un agent, donc le texte le plus lu après le socle : le laisser
        en canonique ferait suivre la consigne au nom près, et réafficher notre marque
        à l'écran de quelqu'un qui n'est pas notre client.

        Fail-open : toute difficulté rend l'erreur d'ORIGINE — une erreur à traduire
        ne devient jamais une erreur de traduction.
        """
        try:
            data = getattr(erreur.error, "data", None)
            message = tool_alias.rewrite_prose(erreur.error.message or "", prefix)
            # `data` porte ce que le tool y a mis : l'enveloppe `{oto: {hint}}` du
            # contrat d'erreur, ou tout autre chose (`oto_tool_schema` y met un
            # schéma). On ne touche QUE le hint, et seulement s'il est là.
            oto = data.get("oto") if isinstance(data, dict) else None
            hint = oto.get("hint") if isinstance(oto, dict) else None
            traduit = tool_alias.rewrite_prose(hint, prefix) if hint else hint
            if message == erreur.error.message and traduit == hint:
                return erreur
            if traduit != hint:
                data = {**data, "oto": {**oto, "hint": traduit}}
            return McpError(ErrorData(code=erreur.error.code, message=message, data=data))
        except Exception:  # noqa: BLE001
            logger.warning("traduction du message d'erreur échouée (fail-open)",
                           exc_info=True)
            return erreur
