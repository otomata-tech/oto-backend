"""Ce qu'une écriture a le DROIT de poser, ce qu'on en écarte, et le relevé rendu.

Extrait de `core.py` (déplacement pur, 07/09/2026) — un mixin que `DatastorePg`
compose, sur le modèle de `SchemaOpsMixin`. Il ne redéfinit ni `_resolve`, ni
`_ns_of`, ni `_schema_of`, ni `_declared_key_of` : le noyau les fournit, comme il
fournit les six relevés (`off_schema`, `off_geles`, `off_erased`, `off_ignored`,
`off_rejected`, `off_forced`) que ces contrôles remplissent et que
`off_schema_report` sert.

⚠️ Ce module LIT des attributs de colonne (`champ`, `options`, `destination`,
`key`…) : il est listé dans `vocabulaire._read_keys`, sans quoi le vocabulaire
dérivé rétrécit et l'avertissement accuse des clés parfaitement lues.
"""
from __future__ import annotations

import copy
from typing import Optional

from .. import db, ownership, session_org
from . import ecartes as dsec
from . import schema as dsv2
from .columns import effacements_report, ignores_report
from .errors import RowValidationError
from .forcage import Forcage


class ControlesMixin:
    """Les contrôles d'écriture du store. Composé par `DatastorePg`."""

    def _colonnes_de_la_ligne_visee(self, ns_id: int, schema: Optional[dict],
                                    user_data: dict,
                                    key: Optional[str] = None) -> set:
        """Les colonnes DÉJÀ en place sur la ligne que cette écriture vise, si elle en
        vise une par sa clé métier. Sinon l'ensemble vide.

        ⚠️ **Appelée PARESSEUSEMENT** (`ranger_les_couches(colonnes_en_place=…)`) : seul
        un nom pointé encore irrésolu la déclenche. Le chemin nominal — l'immense
        majorité des écritures, dont les lots de huit mille lignes — ne paie donc aucun
        aller-retour SQL de plus. Deux clés sont interrogées, la clé explicite du lot
        puis la clé DÉCLARÉE, parce qu'un lot peut dédoubler sur une autre que celle
        qui porte l'index."""
        return set(self._donnees_de_la_ligne_visee(ns_id, schema, user_data, key))

    def _donnees_de_la_ligne_visee(self, ns_id: int, schema: Optional[dict],
                                   user_data: dict,
                                   key: Optional[str] = None) -> dict:
        """Les DONNÉES de la ligne que cette écriture vise par sa clé métier, ou `{}`.

        Sert les colonnes en place (ci-dessus) et le filtre des `null` sans effet
        (oto#182), qui doit savoir si une colonne porte une valeur. Même recherche, clé
        DÉBALLÉE comme dans les lots : une clé annotée désigne la même ligne qu'une clé
        nue. Appelée paresseusement par ses deux lecteurs."""
        vues = []
        for k in (key, self._declared_key_of(schema)):
            if not k or k in vues:
                continue
            vues.append(k)
            v = dsv2.unwrap(user_data.get(k))
            if v is None or str(v) == "":
                continue
            rid = db.datastore_find_row_id_by_key(ns_id, k, v)
            if rid is not None:
                return (db.datastore_get_row(ns_id, rid) or {}).get("data") or {}
        return {}

    # --- forçage d'une colonne verrouillée (#658) -----------------------------

    def _forcage_readonly(self, ns_id: int, schema: Optional[dict],
                          demande: bool,
                          chemins: Optional[frozenset] = None) -> Optional[Forcage]:
        """Le forçage de CET appel — `None` quand rien ne le demande.

        ⚠️ Le palier est tranché ICI, **une fois par appel et hors de toute
        transaction**. Il coûte une lecture d'ownership : l'évaluer dans le `_apply`
        du verrou de ligne prendrait une seconde connexion du pool pendant qu'on tient
        un `FOR UPDATE` — la forme exacte du gel de production du 02/09/2026.

        Deux courts-circuits avant la moindre requête, donc **zéro SQL de plus sur le
        chemin nominal** : personne ne demande le forçage, ou le tableau ne déclare
        aucune colonne verrouillée — il n'y a alors rien à forcer, et le demander ne
        doit rien coûter."""
        if not demande:
            return None
        if not dsv2.readonly_fields(schema):
            return Forcage(demande=True, autorise=False, chemins=chemins)
        return Forcage(demande=True, autorise=self._peut_forcer(ns_id),
                       chemins=chemins)

    def _peut_forcer(self, ns_id: int) -> bool:
        """Le PALIER : propriétaire du tableau ∪ qui le gouverne. L'un des deux suffit.

        Les deux ensembles se croisent sans s'inclure — un membre de l'org
        propriétaire POSSÈDE sans gouverner, un gérant (`role='manager'`, ADR 0048)
        GOUVERNE sans posséder — d'où l'union, et non l'un des deux seul.

        ⚠️ Ce qui reste dehors est exactement ce qui doit rester dehors : le tiers à
        qui le tableau a été PARTAGÉ en écriture (`data_share`, permission `write`).
        Il écrit — c'est le droit qu'on lui a donné — et il ne force pas. Sinon le
        verrou ne protégerait de personne : quiconque peut écrire pourrait le lever,
        ce qui est la définition d'une colonne ouverte.

        Endpoint agissant-org (sub-less) : pas de gouvernance par cette porte —
        `_entry` pose déjà la même règle — reste l'owner-match de l'org elle-même."""
        if self.acting_org is not None:
            owner = ownership.owner_of(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id))
            return (owner is not None
                    and (str(owner[0]), str(owner[1])) == ("org", str(self.acting_org)))
        if not self.sub:
            return False
        return (ownership.owns(self.sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id))
                or ownership.can_govern(self.sub, ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id)))

    def _relever_forcage(self, forcage: Optional[Forcage],
                         row_id: Optional[str]) -> None:
        """Agrafe la ligne aux substitutions, puis les verse aux DEUX journaux.

        Appelée seulement quand l'écriture a ABOUTI : un geste refusé n'a rien forcé,
        et le journaliser ferait chercher une valeur qui n'a pas bougé.

        Deux dépôts parce qu'il y a deux faces et deux journaux : `note_call_trace`
        pour la face MCP (versé dans les `args` de la ligne `tool_calls` par
        `server._calllog_sink`, via l'allowlist `_TRACED_ARGS` ; no-op hors appel
        MCP), et `self.off_forced` que la face REST relit pour SA ligne. Le « qui »
        n'est répété ni dans l'un ni dans l'autre : les deux journaux stampent déjà
        le `sub` et l'org de l'appelant."""
        if forcage is None or not forcage.forcees:
            return
        forcage.rattacher(row_id)
        releve = forcage.releve()
        if not releve:
            return
        self.off_forced = list(releve)
        session_org.note_call_trace(readonly_forced=list(releve))

    def _ecarter(self, schema: Optional[dict], merged: dict, errors: list,
                 hors: list, *, prev_status=None,
                 written: Optional[set] = None) -> Optional[list]:
        """Écarte les valeurs hors options et rend leur relevé — ou None pour
        refuser tout, comme avant (#667).

        Trois conditions, et chacune ferme un trou :

        1. **Les refus sont TOUS des valeurs hors options.** Un requis manquant ou
           une transition interdite portent sur la COHÉRENCE de la ligne : les
           écarter écrirait une fiche fausse. Le partage est là, pas ailleurs.
        2. **Chaque champ fautif est posé par CE geste.** Un patch ne se fait pas
           amputer d'une valeur qu'il n'a pas écrite : ce serait un effacement
           silencieux de la base, exactement ce que `valeurs_effacees` existe pour
           empêcher. Le cran juge le geste, pas le passé qu'il hérite.
        3. **La ligne amputée REPASSE la validation entière.** Retirer une valeur
           peut en défaire une autre — une colonne-aiguillage écartée cesse de
           rendre requis ce qu'elle gardait. Sans ce second tour, on écrirait une
           ligne incomplète sur la foi d'un contrôle qui n'a pas vu sa forme
           finale. Elle échoue ⇒ on refuse tout, avec le message d'origine.

        ⚠️ L'amputation se joue d'abord sur une COPIE : `merged` n'est touché que
        si le second tour est propre. Un écartement à moitié appliqué sur un
        refus final laisserait l'appelant avec un dict trafiqué et une exception.
        """
        if not hors or len(hors) != len(errors):
            return None
        # Une valeur dont le schéma déclare la DESTINATION est mal rangée, pas
        # indésirable (#545/#667) : le refus dit où l'écrire, et 27 agents sur 27
        # se corrigent. L'écarter écrirait une fiche qui prétend ne pas avoir été
        # qualifiée, sous un `ok: true` — corrompre en silence, pas sauver.
        if any(h.get("destination") for h in hors):
            return None
        essai = copy.deepcopy(merged)
        releve: list = []
        for h in hors:
            champ = str(h.get("champ") or "")
            tete = dsec.tete(champ)
            if not tete or (written is not None and tete not in written):
                return None
            if not dsec.retirer(essai, champ):
                return None
            options = ", ".join(str(o) for o in (h.get("options") or []))
            releve.append({
                "champ": champ,
                "motif": f"valeur hors options ({options})",
                "valeur_rejetee": h.get("valeur"),
            })
        # Rien à sauver ⇒ rien à écarter. Quand la valeur fautive est TOUT ce que
        # le geste pose, l'amputer ne sauve pas une fiche : elle en crée une VIDE,
        # sous un `ok`. Le motif de ce lot est de préserver un travail déjà fait —
        # là où il n'y en a pas, le refus reste la bonne réponse, et c'est ce que
        # le banc du régime strict (#319) a rappelé.
        reste = (essai if written is None
                 else {k: v for k, v in essai.items() if k in written})
        if not any(not dsv2.est_vide(v) for v in reste.values()):
            return None
        if dsv2.validate_row(schema, essai, prev_status=prev_status,
                             written=written):
            return None
        for h in hors:
            dsec.retirer(merged, str(h.get("champ") or ""))
        return releve

    def _check_row(self, schema: Optional[dict], merged: dict, *,
                   prev_status=None, written: Optional[set] = None,
                   lot: bool = False, creation: bool = False) -> None:
        """Valide la row TELLE QU'ÉCRITE (résultat mergé). No-op si le schéma ne
        déclare ni strict/required/max_length ni lifecycle (défaut 0016 soft).

        `written` = les clés que le geste réécrit (None sur un insert/remplacement,
        où tout est écrit) : borne `max_length` restreinte à celles-là, cf.
        `dsv2.validate_row`.

        C'est aussi LE seam d'écriture — tous les chemins (append, batch, merge de
        clé métier, upsert, patch) y passent — donc l'endroit unique où relever les
        champs HORS SCHÉMA du geste (#294), sur les seules clés posées. Un schéma
        `strict` active la validation, donc l'appel a bien lieu."""
        # #545 : le refus STRUCTURÉ se remplit PENDANT la validation — c'est le seul
        # endroit qui voit à la fois la colonne fautive et la colonne attendue. Le
        # récupérer après coup imposerait de reparser le message, ce que la face REST
        # ne doit jamais avoir à faire.
        details: dict = {}
        hors: list = []
        gelees: list = []
        errors = dsv2.validate_row(schema, merged, prev_status=prev_status,
                                   written=written, details=details, hors=hors,
                                   gelees=gelees)
        # Ce que ce geste n'écrit pas et qui ne passe plus le format déclaré. Relevé
        # même quand l'écriture réussit — c'est justement le cas normal : l'appelant
        # touche une autre colonne, et il est le seul à passer par cette ligne.
        for g in gelees:
            self.off_geles.setdefault(str(g["champ"]), str(g["refus"]))
        if errors:
            # #667 : une valeur hors options s'ÉCARTE, la fiche s'écrit. Tout autre
            # refus — et toute combinaison avec un autre refus — retombe ici.
            ecartes = self._ecarter(schema, merged, errors, hors,
                                    prev_status=prev_status, written=written)
            if ecartes is None:
                raise RowValidationError(errors, details=details)  # rien à relever
            self.off_rejected.extend(ecartes)
        posed = merged if written is None else {k: merged[k] for k in written
                                                if k in merged}
        # #354 : un `id` NU posé par le geste, qu'aucun field ne déclare, est un
        # identifiant de ligne égaré — pas une donnée. Écrit comme donnée, il a
        # produit des lignes fantômes (4 en une nuit de flotte : l'agent recopie
        # l'`id` que le claim lui a servi, la fusion ne matche rien, une ligne
        # SANS clé métier naît avec tout l'enrichissement). Reconnaissance par
        # DÉCLARATION : une vraie colonne `id` (CSV importé) se déclare au schéma
        # et passe ; sans déclaration, refus nommé — jamais une devinette, jamais
        # un silence. Posé ICI parce que ce seam voit TOUS les chemins d'écriture.
        if "id" in posed and not dsv2.declares_field(schema, "id"):
            # ⚠️ **La conduite DÉPEND du mode, et c'est tout l'objet de #72.** Les deux
            # gestes conseillés ci-dessous — reposer l'`_id` dans la ligne, ou passer
            # `id=` — aboutissent sur une écriture UNITAIRE et échouent tous les deux
            # sur un LOT : le premier tombe sur `_reject_misplaced_id(batch=True)`, le
            # second sur le refus de dispatch « `rows` OU `row`/`id`, pas les deux ».
            #
            # Mesuré sur les comptes clients : 87 erreurs sur 132 (65,9 %) viennent de
            # cette famille, dont 29 « `_id` dans une row du LOT » — et **22 d'entre
            # elles (76 %) suivent IMMÉDIATEMENT le conseil du refus précédent**. Le
            # défaut est donc auto-entretenu : notre propre texte produit le refus
            # suivant, et l'agent paie deux allers-retours pour une seule ligne.
            #
            # Un refus qui nomme un geste qui échoue est pire qu'un refus muet : il
            # est CRU, et il fait dépenser.
            if lot:
                conduite = (
                    "Un LOT ne cible aucune ligne par son identifiant, sous aucune "
                    "forme : sa seule cible est la clé métier (`key`). Ne repose donc "
                    "PAS l'`_id` dans la ligne — il serait refusé lui aussi — et ne "
                    "passe pas `id=`, qui est incompatible avec `rows`. Deux voies "
                    "aboutissent : laisser la clé métier dédoublonner "
                    "(`data_write(datastore=…, rows=[…], key=\"<colonne>\")`), ou "
                    "sortir du lot pour cette ligne "
                    "(`data_write(datastore=…, id=\"<_id>\", row={…})`).")
            else:
                conduite = ("Pour cibler une ligne : garde son `_id` tel que servi "
                            "dans la ligne, ou passe le paramètre id=.")
            raise ValueError(
                f"`id` ({posed['id']!r}) posé dans `row` sans être une colonne "
                "déclarée du tableau : un identifiant de ligne ne s'écrit pas "
                "comme une donnée — l'écriture viserait à côté (ligne fantôme). "
                f"{conduite} Si `id` est une vraie colonne de TES données, "
                "déclare-la au schéma (data_set_schema) puis réécris.")
        # #614/#678 : le TROISIÈME état de `strict` au premier niveau — refuser la
        # colonne non déclarée, opt-in table par table (`unknown_fields: "reject"`).
        #
        # Posé ICI, contre `posed`, et les deux points comptent autant que le refus :
        #   • ici, parce que c'est le seam qui calcule DÉJÀ le relevé, deux lignes
        #     plus bas. Le rapporteur et le refuseur partagent donc le prédicat
        #     (`_unknown_subkeys`), pas seulement l'intention — la divergence entre
        #     un signal et un refus qui se veulent d'accord se paie chez l'appelant ;
        #   • contre `posed`, jamais contre la ligne mergée : un tableau de
        #     production porte 162 colonnes hors schéma accumulées avant la pose du
        #     cran. Les juger rendrait la ligne inécrivable pour un patch sans
        #     rapport — la faute de #284, sur une autre règle. Le cran juge le GESTE,
        #     pas le passé qu'il hérite.
        #
        # Après le refus de l'`id` nu, qui est plus spécifique et plus utile : sur un
        # tableau fermé, `id` serait sinon rendu comme une colonne inventée de plus.
        # ⚠️ **L'ENVELOPPE (#117)** — le corps EST la ligne. Qui suit la convention
        # habituelle envoie `{"row": {…}}` et fabrique une colonne réellement appelée
        # `row`, contenant toute la ligne. Aucune garde ne mordait : sur un tableau
        # souple — le régime par DÉFAUT — le relevé hors-schéma lui-même reste muet,
        # et la réponse est indiscernable d'une écriture réussie. Le piège a produit
        # deux verdicts faux au cours d'une seule mesure.
        #
        # ⚠️ Refusé à la CRÉATION seulement, et le critère est l'ABSENCE TOTALE de
        # correspondance : ajouter une colonne libre à un tableau schématisé reste un
        # droit du contrat 0016. Ce qui n'a aucun sens, c'est une ligne qui ne touche
        # pas une SEULE des colonnes déclarées.
        #
        # Mesuré avant de poser : 67 980 lignes de tableaux à colonnes déclarées, UNE
        # seule ne correspondait à rien. La garde ne décrit aucun régime normal.
        if creation and dsv2.enveloppe_probable(schema, posed):
            dispo = [f["key"] for f in dsv2._fields(schema)
                     if isinstance(f.get("key"), str) and f["key"]]
            cite = ", ".join(f"`{k}`" for k in dispo[:6])
            if len(dispo) > 6:
                cite += f" (+{len(dispo) - 6} autres, `data_get_schema`)"
            raise ValueError(
                f"aucune des clés posées ({', '.join(repr(k) for k in sorted(posed))}) "
                f"n'est une colonne de ce tableau — rien n'a été écrit. ⚠️ Le corps EST "
                f"la ligne : ne l'enveloppe pas dans un objet qui la nomme. Écris "
                f"`{{\"{dispo[0]}\": …}}` directement, pas "
                f"`{{\"row\": {{\"{dispo[0]}\": …}}}}`. Les colonnes déclarées sont "
                f"{cite}. Si ces clés sont VRAIMENT tes données, déclare-les au schéma "
                f"(`data_set_schema`) — sinon elles naîtraient en colonnes que "
                f"personne n'a voulues.")
        hs_errors, hs_details = dsv2.off_schema_refusal(schema, posed)
        if hs_errors:
            raise RowValidationError(hs_errors, details=hs_details)
        self.off_schema.update(dsv2.off_schema_keys(schema, posed))
        # Valeurs hors des options DÉCLARÉES quand rien ne les fait respecter (#319) :
        # écrites quand même — le tableau est en régime souple — mais plus en silence.
        # Vide dès que la validation est armée : là, `validate_row` ci-dessus a déjà
        # refusé, et le redire serait un doublon sur un chemin qui ne passe pas.
        self.off_options.update(dsv2.unenforced_options(schema, posed))

    @staticmethod
    def _reject_misplaced_id(data: dict, row_id: Optional[str], *,
                             batch: bool = False) -> None:
        """REFUSE un `_id` posé DANS le payload au lieu du paramètre `id` (#390).

        `_id` est géré par le datastore : il vit dans la colonne `row_id`, jamais
        dans le blob. Il était donc filtré des données écrites — en SILENCE, et c'est
        ce silence qui coûte : une écriture `row={"_id": "019f…", "statut": …}` sans
        `id=` a INSÉRÉ une ligne neuve portant tout le travail d'un enrichissement,
        la ligne visée restant vide, sans une erreur. 28 champs repris à la main.

        Refuser ne casse aucun appelant légitime : personne n'écrit `_id` comme
        DONNÉE, puisque le faire n'avait déjà aucun effet. Un `_id` cohérent avec le
        `id=` fourni passe en revanche — c'est le round-trip normal (relire une ligne
        entière, la modifier, la repousser), et le refuser n'apprendrait rien à
        personne. Les autres colonnes de plateforme (`_created_at`, `_claimed_by`…)
        restent ignorées sans bruit pour la même raison : leur présence dans un
        round-trip est bénigne, elles ne DÉSIGNENT pas la cible de l'écriture."""
        if not isinstance(data, dict) or "_id" not in data:
            return
        posed = data.get("_id")
        if row_id is not None and str(posed) == str(row_id):
            return   # round-trip cohérent : l'intention est claire
        if batch:
            raise ValueError(
                f"`_id` ({posed!r}) dans une row du LOT : un batch dédouble par clé "
                "métier (`key`), il ne cible pas une ligne par son `_id`. Pour "
                "modifier UNE ligne précise, appelle data_write(id=…, row={…}) ; "
                "pour un lot, déclare la clé métier et laisse-la dédoublonner.")
        if row_id is None:
            # Chemin normalement inatteignable depuis append_row depuis #354 (la
            # promotion capte `_id` en amont et route vers update) — conservé en
            # défense en profondeur pour tout futur appelant.
            raise ValueError(
                f"`_id` ({posed!r}) posé DANS `row` : il y serait ignoré et ton "
                "écriture INSÉRERAIT une nouvelle ligne au lieu de modifier "
                "celle-là. L'identifiant est un paramètre : data_write(id=" +
                f"{posed!r}, row={{…}}).")
        raise ValueError(
            f"`_id` ({posed!r}) dans `row` ne correspond pas au `id` visé "
            f"({row_id!r}) — deux cibles pour une écriture. Retire `_id` du corps : "
            "seul le paramètre `id` désigne la ligne.")

    def off_schema_report(self) -> dict:
        """Le relevé « hors schéma » du geste, prêt à fusionner dans une réponse
        d'écriture : `{}` quand tout est dans le format (le cas normal — pas de clé
        parasite dans la réponse), sinon la liste des champs + la phrase qui dit
        quoi en faire. Union sur un lot : un renommage fautif se voit une fois,
        pas une par row."""
        out: dict = {}
        # oto#70 lot 2, premier temps : l'avertissement part avec CHAQUE écriture qui
        # pose une origine, pas une seule fois. Un écrivain qui repasse sur une ligne
        # par semaine ne verrait jamais un message servi une fois — et c'est précisément
        # ce profil-là que la mesure a trouvé (52 lignes touchées sur une semaine, toutes
        # réécrites après leur création).
        if self._origine_posee:
            out["origine_warning"] = dsv2.avertissement_origine(
                sorted(self._origine_posee))
        keys = sorted(self.off_schema)
        if keys:
            out["hors_schema"] = keys
            out["hors_schema_hint"] = dsv2.off_schema_warning(keys)
        # #319 : les options déclarées mais inertes. Clé DISTINCTE de `hors_schema` —
        # ce n'est pas la même faute : là une colonne inconnue, ici une valeur hors
        # d'une liste que le schéma laissait croire fermée.
        if self.off_options:
            out["hors_options"] = dict(sorted(self.off_options.items()))
            out["hors_options_hint"] = dsv2.unenforced_options_warning(self.off_options)
        # #733 : le type ne gèle plus une ligne pour une colonne qu'on n'écrit pas —
        # il la SIGNALE. Clé distincte des deux précédentes : là une colonne inconnue,
        # là une valeur hors d'une liste, ici une valeur déjà en base devenue non
        # conforme au format que le tableau déclare aujourd'hui.
        if self.off_geles:
            out["hors_type"] = dict(sorted(self.off_geles.items()))
            out["hors_type_hint"] = dsv2.types_geles_warning(
                [{"champ": k} for k in sorted(self.off_geles)])
        # #317 étape B : le changement de comportement, dit à l'instant où il joue.
        # Union sur un lot (comme `hors_schema`) — un batch de 500 lignes finies ne
        # répète pas 500 fois la même phrase.
        # ⚠️ AVANT `notices`, et au premier niveau : une ligne que rien ne pourra
        # rapprocher n'est pas un succès ordinaire. Le nom dit le FAIT — `notices`
        # sonnait comme « informations diverses », et c'est précisément pour ça qu'il
        # n'a pas été lu le 09/09/2026.
        if self.off_non_rapprochables:
            out["non_rapprochable"] = sorted(self.off_non_rapprochables)
            out["non_rapprochable_hint"] = " ".join(
                v for _, v in sorted(self.off_non_rapprochables.items()))
        if self.off_notices:
            out["notices"] = sorted(self.off_notices)
        # Ce que le geste a VIDÉ (#407/#408/#409). Clé DISTINCTE des précédentes : ce
        # n'est ni une colonne inconnue ni une valeur hors d'une liste, c'est une
        # valeur qui N'EST PLUS — la seule des quatre qui ait détruit quelque chose.
        out.update(effacements_report(self.off_erased))
        # #608 : ce que le geste aurait détruit et qu'on a préservé. CINQUIÈME clé,
        # distincte des quatre autres — les valeurs qu'elle nomme sont ENCORE en
        # base, et c'est toute la différence avec `valeurs_effacees`.
        out.update(ignores_report(self.off_ignored))
        # #667 : SIXIÈME clé — ce que le schéma a refusé et que le geste a écarté
        # pour écrire le reste. Ni en base (à la différence de `hors_options`), ni
        # détruite (à la différence de `valeurs_effacees`) : jamais entrée.
        out.update(dsec.rapport(self.off_rejected))
        return out

    # Le message que voient les tableaux dont l'écriture d'un état final libérait la
    # ligne (#317 étape B). Rendu à l'INSTANT où l'ancien comportement aurait joué :
    # c'est le seul moment où l'information est actionnable, et son lecteur est le
    # seul qui puisse agir. Trois autres emplacements ont été écartés — à la pose du
    # schéma (les tableaux concernés ne le reposent pas, le message n'arriverait
    # jamais), au claim (trop tôt, et relu à chaque ligne), une annonce hors produit
    # (hors du geste, donc oubliée).
    #
    # ⚠️ La promesse finale est volontairement PLUS PETITE que la première rédaction :
    # elle disait « couvre le cas où votre agent s'arrête en route », ce qui est FAUX
    # — un agent qui meurt n'appelle pas `run_finish`. Ce que la fin de run couvre,
    # c'est l'OUBLI de relâcher. Une promesse doit suivre le code, pas l'inverse.
    _TERMINAL_RELEASE_RETIRED = (
        "La ligne reste réservée. Écrire un état final ne libère plus la ligne "
        "automatiquement — ce comportement a été retiré. Ce qui la libère "
        "maintenant : la fin du traitement en cours (`run_finish`, quelle que soit "
        "son issue) rend toutes les lignes qu'il avait prises ; ou un `data_release` "
        "explicite si vous travaillez hors traitement. Si vous dépendiez de la "
        "libération automatique : appelez `data_release` après avoir écrit l'état "
        "final, ou encadrez votre travail par `run_start` / `run_finish` — la "
        "libération devient alors automatique si vous oubliez de relâcher vos lignes."
    )

    def _terminal_write_notice(self, schema: Optional[dict], ns_id: int, row_id: str,
                               merged: dict) -> None:
        """Dit, UNE fois par écriture concernée, que la libération automatique est
        retirée — et ne libère plus rien.

        Ne parle qu'aux tableaux réellement touchés : un cycle de vie déclaré, un état
        final écrit, ET une ligne effectivement sous bail. Les autres ne voient rien.
        """
        sf = dsv2.status_field(schema)
        if not sf:
            return
        if not dsv2.is_terminal_status(schema, merged.get(sf.get("key"))):
            return
        if db.datastore_active_lease(ns_id, row_id):
            self.off_notices.add(self._TERMINAL_RELEASE_RETIRED)


def _relever_origine_module(store, ns_id, payload, avant=None,
                            schema=None, declare: bool = False) -> None:
    """Relève les colonnes dont CET appel pose la couche `origine`, et REFUSE l'appel
    qui ne les déclare pas une fois la date passée (oto#70 lot 2).

    ⚠️ **Ce qui est refusé n'est pas l'écriture, c'est le SILENCE.** `declare` porte le
    paramètre de l'appelant (`origine_override`) : avec, l'écriture passe et laisse une
    trace distincte ; sans, elle passe aussi tant que la date n'est pas atteinte, avec
    l'avertissement pour toute réponse. Après, elle est refusée par un message qui dit
    exactement ce que l'avertissement disait.

    ⚠️ **La date arme la garde toute seule**, sans déploiement (`dsv2.refus_arme()`).
    C'est ce qui a été annoncé aux écrivains à chaque écriture depuis le barreau 1 ; un
    refus qui attendrait qu'on y pense ne serait pas le préavis qu'on leur a promis.

    ⚠️ **La trace distingue les deux populations** (`declare`), et c'est elle qui dira,
    après la date, si un écrivain s'est ADAPTÉ ou a DISPARU. Deux faits que le même
    compteur confondrait : dans les deux cas les écritures non déclarées tombent à zéro.

    ⚠️ **`face` reste NULL** : le store ne connaît pas le canal d'appel — il voit un
    `sub` et une org. `ResolvedCtx.channel` vit à l'adaptateur, deux couches plus haut.
    Le DDL l'accepte, et une face inconnue vaut mieux qu'une face devinée.
    """
    from ..db import origine_ecritures as db_origine

    colonnes = dsv2.origine_posee(payload, avant)
    if not colonnes:
        return
    if not declare and dsv2.refus_arme():
        # Rien n'est relevé : rien n'a été écrit. Un refus n'est pas une écriture, et
        # le compter gonflerait la population de gens qui, précisément, n'ont pas
        # réussi à écrire.
        raise ValueError(dsv2.refus_origine(colonnes))
    store._origine_posee.update(colonnes)
    # ⚠️ Les deux populations sont relevées SÉPARÉMENT. Sur une colonne qui ne déclare
    # pas le format, la plateforme ne pose JAMAIS d'origine : celle-ci vient donc
    # forcément de l'écrivain — c'est le cas que la définition interdit, et le seul
    # qu'on cherche. Les fondre dans un total ferait disparaître la population visée
    # dans celle qui l'entoure (mesuré : 64 cellules contre 15 688).
    declarees = dsv2.system_origin_fields(schema)
    for format_declare, lot in ((False, [c for c in colonnes if c not in declarees]),
                                (True, [c for c in colonnes if c in declarees])):
        db_origine.relever(sub=getattr(store, "sub", None),
                          org_id=getattr(store, "acting_org", None),
                          ns_id=ns_id, colonnes=lot,
                          format_declare=format_declare, declare=declare)
