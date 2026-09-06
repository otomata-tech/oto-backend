"""Les attributs de colonne : une déclaration, deux clients, et un garde-fou (oto#56).

Le validateur acceptait n'importe quelle clé sur une colonne. `editable` passe,
`zorglub` passe, et surtout `read_only` passe — une faute de frappe sur un attribut de
garde est silencieuse **et** elle désarme la garde.

⚠️ **Ce banc existe surtout pour empêcher l'avertissement de MENTIR.** Sa première forme
dérivait la liste des clés vivantes en observant le validateur ; c'était faux, parce que
le schéma est aussi un contrat SERVI — cinq attributs (`label`, `help`, `placeholder`,
`hint`, `description`) n'existent que pour le front. L'avertissement aurait crié sur
presque tous les tableaux existants, et on aurait appris à l'ignorer.

L'observation n'a pas disparu : elle a changé de rôle. Elle ne fabrique plus la liste,
elle la GARDE — tout ce que le validateur lit doit être déclaré.
"""
from __future__ import annotations

from oto_mcp.datastore import cles_inconnues as C
from oto_mcp.datastore import schema as S
from oto_mcp.datastore import schema_keys as K


class _Espion(dict):
    """Un champ qui note ce qu'on lui demande — `get`, `[]` et `in` sont relevés."""

    def __init__(self, source, vues):
        super().__init__(source)
        self._vues = vues

    def get(self, cle, defaut=None):
        self._vues.add(cle)
        return super().get(cle, defaut)

    def __getitem__(self, cle):
        self._vues.add(cle)
        return super().__getitem__(cle)

    def __contains__(self, cle):
        self._vues.add(cle)
        return super().__contains__(cle)


#: ⚠️ **Six sondes fixes, et c'est ainsi que la garde a menti.** Cette liste était
#: écrite à la main ; aucune de ses entrées ne déclenchait les branches qui lisent
#: `required`, `options` ou `max_items`. Le contrôle ne les voyait donc pas, restait
#: VERT, et portait pourtant le nom de la classe entière — « tout ce que le validateur
#: lit est déclaré ». Une garde par échantillon ne garde que son échantillon, et le
#: danger vient de ce qu'elle s'appelle autrement.
#:
#: Les sondes sont désormais DÉRIVÉES de la déclaration : une par clé déclarée, donc
#: aucune clé déclarée ne peut plus manquer d'être exercée. La limite qui subsiste est
#: nommée dans le test ci-dessous, parce que la taire serait refaire la même faute.
_TYPES = ({"type": "text"}, {"type": "number"}, {"type": "list", "of": "text"},
          {"type": "object", "fields": []}, {"type": "enum", "options": ["a"]})
_VALEURS = {"readonly": True, "system": "now", "role": "status", "required": True,
            "max_items": 3, "options": ["a"], "max_length": 5, "pattern": "^a",
            "required_when": {"field": "x", "equals": 1}, "display": "title",
            "lifecycle": {"states": ["a"], "transitions": {}}, "flat_alias": "x",
            "origine": "manuel", "of": "text", "fields": [],
            "required_layers": ["comment"]}


def _sondes():
    """Une sonde par clé déclarée, plus un jeu de types — dérivé, jamais recopié."""
    yield {}
    yield from _TYPES
    for cle in sorted(K.RECONNUES):
        if cle == "key":
            continue
        base = ({"type": "list", "of": "text"} if cle == "max_items"
                else {"type": "enum"} if cle == "options" else {"type": "text"})
        yield {**base, cle: _VALEURS.get(cle, "x")}


def _cles_lues_par_le_validateur() -> set[str]:
    vues: set = set()
    for sonde in _sondes():
        for nom in ("colonne", "colonne.comment"):
            try:
                S.validate_schema_def({"fields": [_Espion({"key": nom, **sonde}, vues)]})
            except Exception:  # noqa: BLE001 — une sonde qui casse n'apprend rien
                continue
    return {v for v in vues if isinstance(v, str)}


def test_tout_ce_que_le_validateur_LIT_est_declare():
    """Le premier sens. Un `f.get("nouveau")` ajouté sans déclaration ferait dire à
    l'avertissement qu'une clé vivante n'est lue par personne.

    ⚠️ **Ce que ce test ne garde PAS**, et il faut le lire avant de s'y fier : il
    observe le validateur en l'exerçant. Une clé lue sur une branche qu'aucune sonde
    n'atteint reste invisible — c'est très exactement ce qui est arrivé à `required`,
    `options` et `max_items`. Les sondes sont maintenant dérivées de la déclaration,
    donc ce trou ne peut plus s'ouvrir sur une clé DÉCLARÉE ; il reste ouvert sur une
    clé qu'on lirait sans jamais l'avoir déclarée. Le second sens, lui, est exact."""
    non_declarees = _cles_lues_par_le_validateur() - K.RECONNUES
    assert not non_declarees, (
        f"le validateur lit des clés qui ne sont pas déclarées : {sorted(non_declarees)}. "
        "Déclare-les dans `datastore/schema_keys.py` avec leur lecteur, sinon "
        "`unknown_keys_warning` les dénoncera comme mortes.")


def test_le_validateur_DERIVE_ses_crans_de_la_declaration():
    """Premier client, pas documentation : si les deux divergeaient, la déclaration
    ne serait qu'un commentaire."""
    assert S._COLUMN_ONLY_KEYS == K.COLONNE_SEULEMENT


def test_une_faute_de_frappe_sur_un_cran_est_nommee():
    """`read_only` désarme le verrou en silence : c'est le cas le plus coûteux, parce
    que l'auteur croit avoir protégé sa colonne."""
    w = C.check({"fields": [{"key": "k", "type": "text", "read_only": True}]})
    assert w["unknown_keys_warning"] and "read_only" in w["unknown_keys_warning"]


def test_le_cran_correctement_ecrit_ne_dit_rien():
    """L'autre moitié : un avertissement qui parle toujours ne se lit plus."""
    assert C.check({"fields": [{"key": "k", "type": "text", "readonly": True}]})[
        "unknown_keys_warning"] is None


def test_le_cas_fondateur_est_nomme():
    """`editable` n'existe nulle part — l'agent l'a découvert en comparant deux refus
    strictement identiques, mot pour mot."""
    w = C.check({"fields": [{"key": "k", "type": "text",
                             "readonly": True, "editable": True}]})
    assert w["unknown_keys_warning"] and "editable" in w["unknown_keys_warning"]


def test_les_attributs_du_FRONT_ne_sont_jamais_denonces():
    """La moitié qui a failli couler le lot : ces cinq-là sont invisibles au
    validateur et parfaitement vivants. `label` est lu 40 fois côté dashboard."""
    champs = [{"key": "k", "type": "text", "label": "Nom", "description": "d",
               "help": "h", "hint": "i", "placeholder": "p"}]
    assert C.check({"fields": champs})["unknown_keys_warning"] is None


def test_chaque_cle_declaree_nomme_au_moins_un_lecteur():
    """Une clé sans lecteur n'aurait aucune raison d'être reconnue — et c'est
    précisément ce que le lot cherche à rendre impossible."""
    for c in K.CLES:
        assert c.lecteurs, c.nom
        assert set(c.lecteurs) <= {"validateur", "front"}, c.nom
        assert c.quoi.strip(), c.nom


def test_la_declaration_est_SERVIE():
    """Elle est déclarée à la main pour la moitié `front` : le seul chemin qui rendra
    cette moitié sûre est un contrôle côté dashboard, et il lui faut cette route."""
    servie = K.servie()
    assert {e["key"] for e in servie} == K.RECONNUES
    assert all(e["readers"] and "what" in e for e in servie)


# ── le second sens : ce qu'on DÉCLARE appliqué doit l'être ───────────────────

def test_tout_ce_qui_est_declare_APPLIQUE_est_reellement_lu():
    """⚠️ Le sens que personne ne pose jamais. On vérifie qu'on n'oublie rien ; on ne
    vérifie pas qu'on ne PROMET rien en trop. Et c'est celui-ci qui fait tomber la clé
    fantôme : `enum` était déclarée « lue par le validateur » alors que le code ne la
    lit nulle part — c'est une valeur de `type`, pas une clé.

    Cette liste est SERVIE (`GET /api/datastore/schema/keys`). Une clé déclarée mais
    morte y est pire qu'une clé manquante : elle est lue AVANT d'écrire, par quelqu'un
    qui cherche justement quoi écrire. Ici, elle recommandait exactement ce que la
    table des fautes de frappe corrige ensuite (`enum` → `options`).

    Exact, pas échantillonné : la dérivation surestime (elle ramasse aussi des clés de
    ligne), donc une clé déclarée et absente du dérivé n'est lue nulle part."""
    fantomes = sorted(K.LUES_PAR_LE_VALIDATEUR - S.interpreted_keys())
    assert not fantomes, (
        f"ces clés sont déclarées appliquées par le validateur, qui ne les lit "
        f"nulle part : {fantomes}. Elles sont SERVIES à qui cherche quoi écrire. "
        "Retire-les, ou fais-les lire.")


def test_la_derivation_suit_les_CONSTANTES_pas_seulement_les_litteraux():
    """`flat_alias` n'est jamais écrite en toutes lettres : elle est lue par sa
    constante, en `f.get(FLAT_ALIAS)` ET en `f[FLAT_ALIAS]`. Une dérivation qui ne
    regarde que les littéraux l'aurait accusée d'être morte — et le test du second
    sens, bâti dessus, aurait exigé qu'on retire une clé parfaitement vivante. Un
    garde-fou faux ne se contente pas de manquer un défaut : il fait supprimer du
    code juste.

    Les deux formes de lecture comptent : n'en résoudre qu'une laisserait le trou
    ouvert sur l'autre, et `flat_alias` — lue par les deux — le masquerait."""
    assert "flat_alias" in S.interpreted_keys()
    assert "flat_alias" in K.LUES_PAR_LE_VALIDATEUR


#: Les clés qu'`enforced_keys()` applique AILLEURS que sur une colonne : à la racine
#: du schéma, ou sous `lifecycle`. La liste servie ne décrit que les attributs de
#: COLONNE, donc elles n'y ont pas leur place. Écrite à la main, et c'est voulu : le
#: jour où le validateur applique une clé de plus, ce test rougit et force à dire de
#: quel NIVEAU elle est, au lieu de la laisser tomber dans un trou entre deux listes.
_APPLIQUEES_HORS_COLONNE = {"strict", "key_required", "unknown_fields", "claimable"}


def test_tout_ce_que_le_validateur_APPLIQUE_est_dans_la_liste_SERVIE():
    """⚠️ Le troisième sens, et celui qui garde ce lot. `required`, `options` et
    `max_items` sont exécutés par le validateur et ne figuraient PAS dans la liste
    servie sur `GET /api/datastore/schema/keys` — l'endroit exact où un agent regarde
    pour savoir quoi écrire. Les trois crans les plus utiles étaient invisibles là où
    on les cherche, pendant qu'une clé morte (`enum`) y était recommandée.

    Ce sens ne recouvre pas les deux autres : une clé peut être lue quelque part
    (premier sens) sans être un attribut de colonne servi. C'est le seul qui relie ce
    que le serveur APPLIQUE à ce qu'il ANNONCE."""
    manquantes = sorted(set(S.enforced_keys()) - K.RECONNUES - _APPLIQUEES_HORS_COLONNE)
    assert not manquantes, (
        f"le validateur applique ces clés et la liste servie ne les annonce pas : "
        f"{manquantes}. Déclare-les dans `schema_keys.CLES`, ou — si elles ne portent "
        "pas sur une colonne — range-les dans `_APPLIQUEES_HORS_COLONNE` en disant "
        "sur quoi elles portent.")


# ── la référence unique : les deux avertissements s'accordent ────────────────

_EPREUVE = {"fields": [
    {"key": "ref", "type": "text", "label": "Référence", "required": True},
    {"key": "statut", "type": "enum", "options": ["a"], "enum": ["a"], "help": "x"},
    {"key": "tags", "type": "list", "of": "text", "max_items": 5,
     "hint": "h", "placeholder": "p", "description": "d"},
    {"key": "note", "type": "text", "read_only": True},
]}


def test_les_deux_avertissements_denoncent_EXACTEMENT_la_meme_chose():
    """⚠️ Il y en avait deux, sur deux inventaires, et ils se trompaient sur des
    ensembles DISJOINTS — dans la même réponse. Celui de la pose dénonçait `options`,
    `required`, `max_items` (que le validateur applique) ; celui de la lecture
    dénonçait `label`, `help`, `hint`, `placeholder`, `description` (que le front
    lit). Chacun était aveugle exactement là où l'autre voyait. L'auteur d'un schéma
    recevait donc deux verdicts contradictoires sur le sien — et deux signaux qui se
    contredisent apprennent à être ignorés deux fois plus vite qu'un seul faux.

    Ils partagent désormais `schema.vocabulaire_vivant()`. Ce test compare leurs
    verdicts plutôt que de les figer séparément : c'est l'ACCORD qui est l'invariant,
    et deux constantes recopiées se remettraient à diverger.

    ⚠️ **Le schéma d'épreuve porte une clé prise dans l'ÉCART entre les deux
    inventaires, et elle est dérivée, pas choisie.** Sans elle, ce test passait alors
    même qu'on rebranchait l'un des deux sur son ancienne référence : les huit clés
    litigieuses ayant été déclarées, les deux listes coïncidaient sur cet
    échantillon-là — et le test aurait été une garde par échantillon, exactement le
    défaut que ce lot corrige. Un banc qui compare deux références doit exercer ce qui
    les sépare, sinon il compare deux fois la même chose."""
    ecart = sorted(S.vocabulaire_vivant() - K.RECONNUES)
    assert ecart, "les deux inventaires ne se recouvrent plus : ce test perd son objet"
    champs = _EPREUVE["fields"] + [{"key": "ecart", "type": "text", ecart[0]: 1}]

    pose = C.inconnues(champs)
    lecture = {e["field"]: e["keys"]
               for e in S.unknown_declaration_keys({"fields": champs})}
    assert pose == lecture, (pose, lecture)
    assert pose == {"statut": ["enum"], "note": ["read_only"]}, (
        f"`{ecart[0]}` est lue par le validateur : aucun des deux ne doit la dénoncer")


def test_les_trois_crans_APPLIQUES_ne_sont_plus_denonces():
    """`required`, `options` et `max_items` sont exécutés par le validateur. Les
    dénoncer envoyait retirer ce qui contraint."""
    for cle, champ in (("required", {"key": "k", "type": "text", "required": True}),
                       ("options", {"key": "k", "type": "enum", "options": ["a"]}),
                       ("max_items", {"key": "k", "type": "list", "of": "text",
                                      "max_items": 3})):
        assert C.check({"fields": [champ]})["unknown_keys_warning"] is None, cle


def test_le_cas_fondateur_est_dénoncé_DANS_LE_BON_SENS():
    """⚠️ L'avertissement de pose se trompait de sens sur le cas même qui l'a fait
    naître : un `enum` posé à côté d'un `options` — la faute qui a laissé 504 valeurs
    libres sur un tableau qui se croyait contraint — passait en SILENCE, tandis
    qu'`options`, la clé qui fait foi, était accusée. Le message envoyait retirer la
    bonne et garder la mauvaise."""
    w = C.check({"fields": [{"key": "s", "type": "enum",
                             "options": ["a"], "enum": ["a"]}]})["unknown_keys_warning"]
    assert w and "`enum`" in w
    assert "`options`" not in w, "la clé qui contraint ne doit JAMAIS être accusée"
