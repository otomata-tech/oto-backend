## usage — empreinte numérique d'un domaine

recon **passif** / osint d'un domaine (rien d'intrusif, sources publiques) — open data, sans clé.
un seul outil, `infosec_domain(domain, aspect=…)` — l'aspect choisit la lecture :
- `whois` / `dns` — immatriculation rdap et enregistrements dns (avec indices de stack mail/saas)
- `email_security` — posture spf/dmarc/dkim, signal de maturité it d'un prospect
- `subdomains` — sous-domaines connus via les logs certificate transparency (crt.sh)
- `tls` / `headers` — certificat tls et en-têtes http de sécurité

## note — ce que ça sert à qualifier, au-delà de la sécurité

le nom du connecteur dit « sécurité », mais l'usage le plus courant ici est
**commercial** : reconnaître l'outillage d'une cible pour la qualifier avant de
l'aborder. c'est ce que la ligne « indices de stack mail/saas » recouvre sans le dire.

- les enregistrements `dns` (`MX`, `TXT`) nomment le fournisseur de messagerie et
  souvent les saas branchés dessus — savoir qu'un prospect est chez tel hébergeur, tel
  crm ou tel outil de signature dit avec quoi ton offre devra coexister, ou ce qu'elle
  remplacerait ;
- `email_security` (spf/dmarc/dkim) se lit comme un **signal de maturité it** : une
  posture stricte et complète ne décrit pas la même organisation qu'un domaine sans
  dmarc ;
- `subdomains` révèle des produits, des environnements et des marques annexes qu'aucune
  page d'accueil ne montre.

⚠️ tout est **passif** et vient de sources publiques : aucune sollicitation de la
cible, rien d'intrusif. c'est ce qui rend l'usage commercial acceptable — on lit ce que
le domaine publie de lui-même.
