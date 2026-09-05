"""DDL du domaine « écritures d'origine » — fragment assemblé par `db/_schema.py`.

**Une table, et elle existe pour être lue une fois puis disparaître.**

`origine_ecritures` relève qui pose la couche `origine` d'une cellule, alors que la
poser SANS LE DÉCLARER va devenir refusé (oto#70 lot 2, premier temps du préavis).
Écrire l'origine restera possible : ce qui disparaît, c'est de le faire en silence.

⚠️ **Elle existe parce que le journal d'appels ne peut pas répondre.** `tool_calls` ne
garde que les clés de PREMIER NIVEAU d'un appel (`row`, `namespace`, `id`) et la fiche
d'un appel tronque ses arguments : la couche vit à l'intérieur, invisible aux deux. On
peut y lire qui écrit, jamais qui écrit une COUCHE. C'est le voisin d'oto-backend#882 —
le journal existe et ne porte pas la coordonnée qu'on lui demande.

Mesuré avant de l'écrire : **64 cellules** portent une origine sur une colonne sans
format déclaré, contre 15 688 avec — un pour 245, concentrées dans trois tableaux
entiers. Le plancher est bas, donc le refus à venir cassera peu de monde ; encore
faut-il savoir QUI, et c'est ce que cette table dira.

⚠️ **Une ligne par (écrivain, tableau, colonne), pas par écriture.** Un compteur et deux
dates plutôt que N lignes : le volume d'écritures est inconnu et peut être élevé, alors
que la population, elle, est bornée par construction. On veut savoir combien ils sont,
pas combien de fois ils ont écrit.

⚠️ **Aucune valeur de cellule.** On relève CE QUI a été touché — le tableau, la colonne
— jamais ce qui a été écrit. Une trace qui recopie la donnée qu'elle surveille est un
second exemplaire à protéger, et celui-ci n'aurait aucune raison d'exister.
"""
from __future__ import annotations

ORIGINE = """
-- Qui pose la couche `origine` d'une cellule (oto#70 lot 2, premier temps).
CREATE TABLE IF NOT EXISTS origine_ecritures (
    -- L'écrivain, tel que la plateforme le connaît. `NULL` est possible et voulu :
    -- un appel par jeton d'API n'en porte pas (oto-backend#882), et l'ignorer
    -- effacerait précisément la population la plus difficile à joindre.
    sub TEXT,
    org_id BIGINT,
    ns_id BIGINT NOT NULL,
    colonne TEXT NOT NULL,
    -- Par où : 'mcp' | 'rest' | NULL (appel interne). Un proxy d'intention, pas une
    -- preuve — un humain parle aussi par MCP, un intégrateur aussi par REST.
    face TEXT,
    -- ⚠️ LE discriminant du lot. `false` = la colonne ne DÉCLARE pas le format : la
    -- plateforme n'y pose jamais d'origine, donc celle-ci vient forcément d'un
    -- écrivain — c'est le cas suspect, celui que la définition d'Alexis interdit.
    -- `true` = la colonne déclare le format et l'écrivain a modifié une origine que la
    -- plateforme gère : moins grave, mais tout aussi refusé demain.
    -- Sans cette colonne, les deux populations se confondraient dans un même total, et
    -- c'est justement celle qu'on cherche qui disparaîtrait dans l'autre.
    format_declare BOOLEAN NOT NULL DEFAULT false,
    ecritures BIGINT NOT NULL DEFAULT 1,
    premiere_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    derniere_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Une ligne par (écrivain, tableau, colonne) : c'est la POPULATION qu'on compte, pas
-- le trafic. `COALESCE` sur `sub` parce qu'un NULL ne s'égale pas à lui-même dans un
-- index unique, et qu'un écrivain sans compte doit s'agréger comme les autres.
CREATE UNIQUE INDEX IF NOT EXISTS idx_origine_ecritures_qui
    ON origine_ecritures (COALESCE(sub, ''), ns_id, colonne);

-- « Qui écrit encore, et depuis quand » — la lecture qui décidera de la longueur du
-- préavis, et de qui il faut prévenir avant de refuser.
CREATE INDEX IF NOT EXISTS idx_origine_ecritures_fraicheur
    ON origine_ecritures (derniere_at DESC);
"""
