---
slug: recruiter-outreach
title: Approche candidat (outreach recruteur)
description: Séquences d'approche personnalisées et conformes — InMail/LinkedIn, email, suivi, mise à jour ATS.
category: Recrutement
tags: recrutement, outreach, sequence, linkedin, email
---

# Approche candidat (outreach recruteur)

Skill de la doctrine `talent-sourcing`. Transformer un profil qualifié en
**conversation**. La personnalisation > le volume : un message ciblé bat dix
copier-coller.

## Anatomie d'un premier message

1. **Accroche personnalisée** (1 phrase) — une raison précise de SON profil (un
   projet, un repo, un parcours), pas un compliment générique.
2. **Le poste en une ligne** — rôle, équipe, ce qui le rend intéressant pour LUI.
3. **Le pourquoi maintenant** — contexte (levée, équipe qui se monte, scope).
4. **Un appel à l'action léger** — « ça vaut un échange de 15 min ? », jamais
   « envoie ton CV ».

Garde court (≤ 6 phrases). Une seule question. Pas de jargon RH.

## Canaux & outils

- **LinkedIn** (`unipile_send_invitation` + note, `unipile_send_message` une fois
  en relation). Le canal premier pour le passif.
- **Email** (`gmail_compose` pour du 1:1, **`lemlist`** pour une séquence cadencée :
  `lemlist_list_campaigns`, ajout de lead). N'envoie qu'à un email **vérifié**
  (`hunter_email_verify` / `zerobounce`).
- **Relances** : 2 à 3 max, espacées (J+3, J+7), chacune apporte un angle neuf —
  jamais un simple « petit up ».

## Boucle avec l'ATS

- Avant d'écrire : confirme le candidat loggé + la note de source (`ats-hygiene`).
- Après chaque réponse : **mets à jour le stage** et note la teneur de l'échange
  (`*_add_note`). L'ATS doit refléter l'état réel de la conversation.

## Garde-fous

- **Consentement & ton.** Approche pro, opt-out facile, pas de relance après un
  « non ». Respecte les règles de la plateforme (pas de spam LinkedIn — invitations
  mesurées, sous peine de restriction du compte).
- **Honnêteté.** Ne survends pas le poste ni la rémunération ; ce qui est promis en
  approche est tenu en process. Pas de fausse urgence.
- **Pas d'envoi en masse non supervisé.** Une séquence se déclenche après revue
  humaine de la liste et du copy.
