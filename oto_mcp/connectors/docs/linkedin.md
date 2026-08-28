## prerequisite — connecte ton compte LinkedIn

connecte TON compte LinkedIn depuis le dashboard oto — pas de cookie à coller ni d'extension. la session tourne sur un vrai navigateur hébergé (proxy résidentiel), ce qui évite les blocages d'empreinte.
- la clé d'abonnement vit sur le connecteur **Compte Unipile** (ta clé BYO, ou celle de la plateforme avec l'option **messagerie hébergée** accordée par un admin) ;
- puis « Connecter mon compte LinkedIn » ici.

## usage — prospection et messagerie LinkedIn

recherche, profils, posts, réseau, offres d'emploi et messagerie — tu agis comme toi-même, sous ton propre compte.
- « recherche LinkedIn des DAF en région lyonnaise dans mon réseau N1 »
- « ouvre le profil LinkedIn de ce slug et résume sa carrière »
- « envoie une invitation à ce prospect avec une note » puis « réponds dans le fil quand il accepte »
- « montre ma home LinkedIn récente » ou « commente ce post »

## note — c'est TA session, pas une base de données

les résultats viennent de ce que TON compte voit (réseau, abonnements, produits premium) et sont soumis aux limites de LinkedIn — pas d'un fichier acheté. pour un email ou un mobile qu'un profil ne publie pas, passe par un connecteur d'enrichissement (dropcontact, fullenrich, kaspr, lusha).

## note — Recruiter et Sales Navigator s'activent à la connexion

un produit premium (`recruiter` ou `sales_navigator`, **exclusifs** — un seul par compte) s'attache **au moment de connecter**. sans lui, les endpoints premium répondent 403 « out of your scope ». sur un compte déjà connecté, c'est une **reconnexion** qui l'attache, pas une seconde connexion.

## note — le feed est servi en vue de tri

`linkedin_post(op="feed")` rend un extrait par défaut (texte coupé à 600 caractères, colonnes de tri seulement) : une page entière dépassait le plafond d'un résultat MCP. `fields=["*"]` et `text_max_chars=None` rendent le brut ; la réponse dit toujours ce qu'elle a rogné.
