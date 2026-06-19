# Billing — credits d'appel par org (paiement Stripe)

Deux modèles **cumulables** : (a) **credits d'appel** (1 appel MCP = 1 credit, débité du
wallet de l'**org active** ; stock de base gratuit `OTO_MCP_FREE_CALLS` déf. 1000 + recharge
par **packs Stripe ponctuels** `mode=payment`) ; (b) **abonnements récurrents par option**
(`mode=subscription`, ex. option LinkedIn — cf. dernière puce). Les credits paient les
**appels**, l'abonnement paie l'**accès** à une option.

- **Modèle** : portefeuille **par org** (`credits_store.py`, couche backend-core). `balance` =
  compteur entier d'appels restants, **peut passer négatif** — **soft enforcement, on ne bloque
  JAMAIS un appel**. Drapeau `low` (alerte UI) seulement. Don de base posé **paresseusement**
  (`ensure_wallet`, au 1er débit OU 1re lecture), idempotent (`base_granted`).
- **Débit** : greffé sur `server._calllog_sink` (le hook calllog, **point d'interception unique**)
  → `credits_store.debit_for_call(sub)`. S'exécute **après** l'exécution du tool (non-bloquant par
  construction), best-effort (avale tout), no-op sans sub / sans org active. **Tous les appels
  comptent** (méta-tools + échecs inclus). Le débit n'écrit QUE `org_credits.balance` (pas de ligne
  ledger — volumétrie ; le détail par appel vit dans `tool_calls`).
- **Tables** (`db._SCHEMA`) : `org_credits(org_id PK, balance, base_granted, …)` + ledger
  `credit_transactions(id, org_id, delta, reason, stripe_event_id UNIQUE, …)` — le ledger ne porte
  que les mouvements **monétaires** (`stripe`/`base_grant`/`admin_adjust`).
- **Stripe** (`billing.py`, SDK **lazy-import** — absent = seuls les endpoints billing cassent, pas
  le boot) : catalogue `PACKS` en code (prix ad-hoc `price_data`, remise volume = la dégressivité
  1ct→0,1ct). `create_checkout_session(org_id, pack_id, sub)` (`metadata={org_id,calls,…}`) →
  `{checkout_url}`. Webhook `POST /api/billing/webhook` (**route brute** dans `make_routes`, NON
  authentifié mais **signature-vérifié** sur le **corps brut**, pas de capability/CORS) → sur
  `checkout.session.completed`, `credits_store.credit(...)`. **Idempotent** sur `event["id"]`
  (`UNIQUE` + `ON CONFLICT`) ; renvoie 500 sur erreur interne → Stripe rejoue sans double-crédit.
- **Surfaces** (capacités `capabilities/billing.py`, montage auto MCP+REST) : `billing.balance`
  (`ORG_MEMBER`, MCP `billing_balance` + `GET /api/me/billing`), `billing.transactions`
  (`GET /api/me/billing/transactions`), `billing.packs` (`SUB_ONLY`, `GET /api/billing/packs`),
  `billing.checkout` (`ORG_MEMBER`, `POST /api/me/billing/checkout`). **Qui paie = tout membre**
  de l'org (recharge le wallet partagé, bénin). `/api/me` expose un bloc `billing` (`{balance, low,
  base_granted}`, `null` si pas d'org active). Front : dashboard `/console/billing`.
- **Env** (`/opt/oto-mcp/.env`, cf. DEPLOY.md) : `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `OTO_MCP_FREE_CALLS` (déf. 1000), `OTO_DASHBOARD_URL`, `OTO_MCP_LOW_BALANCE_THRESHOLD` (déf. 50).
- **Gotcha** : un caller **sans org active** n'est **pas facturé** (no-op) — le metering exige
  l'appartenance org (cas limite : user tout neuf avant sa 1re org). Ne jamais déduire « a eu le
  stock de base » du solde (il peut être négatif) → lire `base_granted`.
- **Abonnements récurrents** (`mode=subscription`) — l'**option LinkedIn (unipile)** = €15/mois/**siège**
  (= compte LinkedIn connecté ; env `OTO_MCP_UNIPILE_SEAT_PRICE_CENTS=1500`). Miroir local
  `org_subscriptions(org_id, product, stripe_*, status, quantity)` tenu par les webhooks (lu pour le
  gate, sans appel Stripe par requête). `billing.create_unipile_subscription_checkout` /
  `sync_unipile_seats` (quantité = nb comptes, Stripe prorate) / `has_active_unipile_subscription`.
  `handle_event` **dispatche** : `checkout.session.completed` (packs **vs** subscription) +
  `customer.subscription.*` + `invoice.payment_failed`. `connect` est **gaté** sur abonnement actif
  (402 `unipile_subscription_required`) ; `POST /api/me/unipile/subscribe` → `{checkout_url}`.
  ⚠️ **L'endpoint webhook Stripe doit être ABONNÉ aux event types côté Stripe** (dashboard/API), pas
  seulement codé : `mcp.oto.ninja/api/billing/webhook` n'écoutait au départ que
  `checkout.session.completed` → les events d'abonnement (annulation/échec) n'arrivaient pas. Ajoutés
  via l'API Stripe à `enabled_events`.
