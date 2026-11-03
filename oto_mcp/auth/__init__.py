"""L'authentification : qui parle au serveur, et comment un credential s'ACQUIERT.

Package sans surface propre. Deux sujets qui se répondent :
- **entrant** — `hooks` (le `sub` du jeton courant), `decouverte` (la métadonnée de
  ressource protégée, qui annonce Logto), `inscription_dynamique` (la DCR RFC 7591 des
  clients sans CIMD, sous le domaine de l'émetteur), `annuaire` (l'administration de
  l'annuaire Logto), `token_scopes` (la portée d'un jeton `oto_…`), `anon` (le shim OAuth des
  endpoints publics) ;
- **sortant** — `flow` (la danse `authorization_code`, écrite UNE fois), `pkce` (les
  helpers state/PKCE), puis un module par fournisseur :
  `google`, `salesforce`, `zoho`, `microsoft`, `ubersuggest`, `jefacture`.

Une famille se reconnaît ici au SUFFIXE (`*_oauth`) autant qu'au préfixe — cf.
`docs/conventions.md` §« Où vit un fichier ».
"""
