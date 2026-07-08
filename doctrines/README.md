# Doctrines partagées (bibliothèque publique)

Doctrines **versionnées au repo** publiées dans la bibliothèque publique
(`doctrine_library`) sous l'auteur **Otomata** — un catalogue de skills que
n'importe quelle org peut **forker** (`oto_procedure(op='fork')`) dans son espace.

Diffère de `scripts/seed_doctrine_library.py` (qui publie les skills d'une org
existante) : ici les doctrines sont des fichiers markdown, pas besoin d'org source.

## Format

Un fichier `<slug>.md` par doctrine, avec un front-matter `---` :

```markdown
---
slug: mon-skill
title: Titre lisible
description: Une phrase de résumé (affichée au catalogue).
category: Recrutement
tags: tag1, tag2, tag3
---

# Titre

Le corps markdown de la doctrine…
```

## Publier / mettre à jour

Idempotent (upsert par slug, incrémente la version) :

```bash
# sur la box (DB accessible)
cd /opt/oto-mcp && ./.venv/bin/python -m scripts.seed_talent_doctrines
```

## Jeux de doctrines

- `talent-sourcing/` — RH / sourcing de talents / ATS : workflow de bout en bout
  (`talent-sourcing`) + skills `boolean-search`, `candidate-screening`,
  `ats-hygiene`, `recruiter-outreach`. Tirent parti des connecteurs ATS
  (greenhouse / lever / ashby / teamtailor / recruitee), LinkedIn (unipile),
  enrichissement (hunter / fullenrich / zerobounce) et outreach (lemlist).
