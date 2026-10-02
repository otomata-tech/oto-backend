"""Les recettes : faire passer les données d'un outil de connecteur dans un tableau,
sans modèle (`oto_recipe`, `docs/recettes.md`).

`contrat` valide le corps d'une recette, `correspondance` lit un élément et fabrique
une ligne (chemins, gabarits, filtres), `moteur` appelle l'outil page par page et
écrit. Tout ce qui est pur vit dans les deux premiers ; le moteur seul touche le
réseau et la base.
"""
