"""DDL du domaine « alertes de credential » — fragment assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul.

**Une table, et son courrier ne part pas encore.**

`credential_disparitions` enregistre les moments où une clé retirée laisse derrière elle
des **agents programmés actifs** qui en dépendent (oto#59).

Le 03/09/2026, une clé a disparu d'une org : une douzaine de passages programmés ont
tourné à l'aveugle pendant **36 heures**. ⚠️ Et le canal qui aurait annoncé la panne
tournait sur le credential tombé — six fois par jour, un run découvrait qu'il était
cassé, l'inscrivait sur une ligne que personne ne regardait, et se taisait,
**correctement, selon ses propres règles**. La panne était silencieuse *par
construction*.

⚠️ **C'est pourquoi le courrier part par le canal de PLATEFORME**, jamais par un
connecteur de l'org : le canal qui prévient ne doit pas pouvoir mourir avec ce dont il
annonce la mort. C'est la seule propriété qui distingue cette alerte du registre qu'elle
remplace.

⚠️ **`notifie_at` NULL = rien n'est parti.** L'envoi est derrière un interrupteur de
plateforme à OFF par défaut : le mécanisme se déploie, l'effet attend une décision. Un
canal qu'on ouvre en devinant son volume est un canal qu'on referme au bout d'une
semaine, après avoir appris à ses destinataires à l'ignorer — la même prudence que
`portee_elargissements`, et pour la même raison.

⚠️ **Aucune FK, ni vers `orgs` ni vers les déclencheurs.** Une alerte documente un état
passé : elle doit survivre à la suppression de ce qu'elle décrit, sinon elle disparaît
exactement quand elle devient intéressante.
"""
from __future__ import annotations

ALERTES = """
-- Une clé a été retirée alors que des agents programmés actifs en dépendaient (oto#59).
CREATE TABLE IF NOT EXISTS credential_disparitions (
    id BIGSERIAL PRIMARY KEY,
    -- L'org qui subit : c'est elle qui porte les déclencheurs, et c'est son titulaire
    -- qu'on prévient.
    org_id BIGINT NOT NULL,
    connector TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    -- Qui a retiré. Pas pour blâmer : pour que le courriel puisse dire « retiré par
    -- untel » plutôt que « la clé a disparu », qui n'aide personne à comprendre.
    acteur_sub TEXT,
    -- Combien d'agents programmés actifs en dépendaient, et lesquels (libellés bornés).
    -- ⚠️ Le COMPTE est la donnée qui décide ; les libellés servent à écrire un courriel
    -- lisible, et ils sont un instantané — un déclencheur renommé depuis ne l'est pas ici.
    agents_count INTEGER NOT NULL DEFAULT 0,
    agents JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Posé le jour où le courriel part vraiment. NULL pendant que l'interrupteur est
    -- fermé — et c'est ce NULL qui dit qu'aucun message n'est parti.
    notifie_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Le drain : « qu'est-ce qui n'a pas encore été annoncé, par org ». C'est la seule
-- lecture du travail de maintenance, et elle porte sa condition dans l'index.
CREATE INDEX IF NOT EXISTS idx_cred_disparitions_a_notifier
    ON credential_disparitions (org_id, created_at) WHERE notifie_at IS NULL;
"""
