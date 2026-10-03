"""Les emails d'ACTIVATION d'un tenant : la personne avance d'une étape à la fois.

Un tenant qui sert la plateforme sous sa marque peut DÉCLARER qu'il écrit à ses propres
comptes pour les faire avancer (`OTO_ACTIVATION`, un objet par slug de tenant). Trois
emails, en anglais, chacun envoyé UNE fois, quand la personne est à l'étape qu'il
fait franchir (`db/activation.py` lit l'étape dans le journal) :

- `connect` — jamais branchée : les étapes exactes pour ajouter le connecteur à
  l'agent (celles que l'écran d'accueil affiche), le lien du connecteur ;
- `first-process` — branchée, sans premier processus terminé et silencieuse depuis
  48 h : la phrase à dire à l'agent pour qu'il en propose et en lance un ;
- `recurring` — un processus a tourné une fois : la phrase pour le programmer.

`connect` couvre les inscrits des `window_days` derniers jours (le rattrapage) ; les
deux suivants, les `sequence_days` premiers jours. 48 h au moins entre deux emails.
Aucun ne cite ce que la personne a fait : le jalon choisit le message, le message ne
raconte pas le jalon.

Un travail de maintenance, pas un événement : `oto-mcp maintenance activation`,
tiré par le timer quotidien (PROD seulement — la base est partagée). Chaque passage
relit l'état ; l'audience est dans `db/activation.py`.

**Trois verrous, tous mécaniques :**

1. **Fermé par défaut** (`OTO_ACTIVATION_ENVOI`, même dispositif que
   `OTO_DIGEST_LECTEURS`) : sans le drapeau, le passage DIT ce qu'il enverrait et
   n'écrit rien. Écrire à des comptes est une décision, pas l'effet d'un déploiement.
2. **Pas d'envoi sans essai reçu** : comme la relance (`capabilities/outreach.py`,
   garde-fou 3), un envoi réel exige une ligne `kind='test'` portant l'EMPREINTE du
   contenu servi. L'essai se fait par `python -m oto_mcp.activation essai`, vers la
   boîte de l'opérateur ; retoucher le texte invalide l'essai. C'est ce qui attrape un
   relais qui refuse l'expéditeur, ou un message qui arrive en indésirables, avant
   qu'un client ne le reçoive.
3. **Une fois par personne** : la trace `outreach_sends` est écrite AVANT l'envoi
   (index unique `(campagne, sub)`), et retirée si le relais refuse — un échec de
   transport ne condamne pas la personne, il la remet au passage suivant.

**Le relais et l'expéditeur sont ceux du TENANT.** `from` doit être sur un domaine que
le relais déclaré a vérifié ; sinon le relais refuse (403) et l'envoi est compté
`refuses`, la personne restant due. `mailer_url` désigne l'instance du mailer du tenant,
et son jeton est lu dans `OTO_ACTIVATION_MAILER_BEARER` ; sans `mailer_url`, c'est le
relais de l'instance (`OTO_MAILER_URL`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("oto_mcp.activation")

LOCALE = "en"
ETAPES = ("connect", "first-process", "recurring")


def _ouvert() -> bool:
    return os.environ.get("OTO_ACTIVATION_ENVOI", "").strip() in ("1", "true", "on")


@dataclass(frozen=True)
class Reglage:
    """Ce qu'un tenant déclare pour son email d'activation."""
    tenant: str
    sender: str                    # « Nom <adresse> », sur un domaine vérifié du relais
    reply_to: str
    cc: tuple = ()
    app_url: str = ""              # la page d'accueil (« onboarding ») du tenant
    mcp_url: str = ""              # le lien du connecteur, tel que l'écran le montre
    help_url: Optional[str] = None
    exclude_domains: tuple = ()
    delay_hours: int = 24
    window_days: int = 30
    sequence_days: int = 14
    max_per_run: int = 50
    mailer_url: Optional[str] = None
    link_base: Optional[str] = None  # l'hôte du lien de refus (celui du tenant)
    agent_label: str = "Claude"
    steps: tuple = field(default_factory=tuple)

    def campagne(self, etape: str) -> str:
        return f"activation-{etape}:{self.tenant}"


class ReglageInvalide(ValueError):
    """`OTO_ACTIVATION` illisible ou incomplet : on refuse plutôt que d'envoyer à moitié."""


_REQUIS = ("sender", "reply_to", "app_url", "mcp_url")


def reglages() -> list[Reglage]:
    """Les tenants qui ont déclaré l'activation. Aucun ⟹ liste vide (rien à faire)."""
    brut = os.environ.get("OTO_ACTIVATION", "").strip()
    if not brut:
        return []
    try:
        data = json.loads(brut)
    except ValueError as e:
        raise ReglageInvalide(f"OTO_ACTIVATION n'est pas du JSON : {e}") from e
    if not isinstance(data, dict):
        raise ReglageInvalide("OTO_ACTIVATION doit être un objet {slug: réglage}.")
    out = []
    for slug, r in data.items():
        if not isinstance(r, dict):
            raise ReglageInvalide(f"OTO_ACTIVATION[{slug!r}] doit être un objet.")
        manquants = [k for k in _REQUIS if not str(r.get(k) or "").strip()]
        if manquants:
            raise ReglageInvalide(
                f"OTO_ACTIVATION[{slug!r}] : champ(s) requis absent(s) : "
                + ", ".join(manquants))
        steps = tuple(str(s) for s in (r.get("steps") or ()))
        out.append(Reglage(
            tenant=str(slug), sender=str(r["sender"]), reply_to=str(r["reply_to"]),
            cc=tuple(str(a) for a in (r.get("cc") or ())),
            app_url=str(r["app_url"]).rstrip("/"), mcp_url=str(r["mcp_url"]),
            help_url=(str(r["help_url"]) if r.get("help_url") else None),
            exclude_domains=tuple(str(d).lower() for d in (r.get("exclude_domains") or ())),
            delay_hours=int(r.get("delay_hours", 24)),
            window_days=int(r.get("window_days", 30)),
            sequence_days=int(r.get("sequence_days", 14)),
            max_per_run=int(r.get("max_per_run", 50)),
            mailer_url=(str(r["mailer_url"]) if r.get("mailer_url") else None),
            link_base=(str(r["link_base"]).rstrip("/") if r.get("link_base") else None),
            agent_label=str(r.get("agent_label") or "Claude"),
            steps=steps or _etapes_par_defaut(),
        ))
    return out


def _etapes_par_defaut() -> tuple:
    """Les étapes d'ajout d'un connecteur personnalisé dans Claude (web ou bureau).
    `{mcp_url}` est remplacé au rendu. Un tenant peut déclarer les siennes (`steps`)."""
    return (
        "On claude.ai, select Customize in the left sidebar.",
        "Go to Connectors, click the plus icon (+) and select Add custom connector.",
        "Enter the name {name} and this link: {mcp_url} — then click Add.",
        "Click Connect and sign in to {name}.",
    )


# ── Le contenu ───────────────────────────────────────────────────────────────

def contenu(r: Reglage, nom_produit: str, etape: str = "connect") -> dict:
    """Sujet, corps et bouton de l'étape — en anglais, à la marque du tenant. Sans
    donnée de la personne : le même texte pour tous, ce qui permet d'en prendre
    l'empreinte et de l'essayer une fois pour toutes."""
    if etape == "first-process":
        return _contenu_premier_processus(r, nom_produit)
    if etape == "recurring":
        return _contenu_recurrent(r, nom_produit)
    if etape != "connect":
        raise ValueError(f"étape inconnue : {etape!r}")
    etapes = "\n".join(
        f"{i}. " + e.format(name=nom_produit, mcp_url=r.mcp_url)
        for i, e in enumerate(r.steps, 1))
    autres = (f"Using ChatGPT, Le Chat or Claude Code instead? The steps for each are "
              f"on your onboarding page: {r.app_url}")
    if r.help_url:
        autres += f"\n\nStep-by-step guide: {r.help_url}"
    corps = "\n\n".join([
        "Hi,",
        f"Your {nom_produit} account is ready. One step is left before it can do "
        f"anything for you: adding {nom_produit} to {r.agent_label}.",
        etapes,
        f"Then ask {r.agent_label}: \"What can {nom_produit} do for my company?\"",
        autres,
        f"If anything blocks you, reply to this email.\n\nThe {nom_produit} team",
    ])
    return {"subject": f"One step left to start using {nom_produit}",
            "body": corps,
            "cta_label": "Open my onboarding page",
            "cta_url": r.app_url}


def _contenu_premier_processus(r: Reglage, nom: str) -> dict:
    a = r.agent_label
    corps = "\n\n".join([
        "Hi,",
        f"{nom} is connected. The next step is your first process: a task "
        f"{nom} runs for you the same way every time, like a weekly list of accounts "
        "to follow up, or a morning brief built from your inbox.",
        f"In {a} (or whichever AI tool you connected), ask:",
        f"\"Use {nom}: read the onboarding guide and set up my first process.\"",
        "It will suggest processes that fit your company and the tools you've "
        "connected, then run the one you pick with you.",
        f"If anything blocks you, reply to this email.\n\nThe {nom} team",
    ])
    return {"subject": f"Your first {nom} process",
            "body": corps, "cta_label": f"Open {nom}", "cta_url": r.app_url}


def _contenu_recurrent(r: Reglage, nom: str) -> dict:
    a = r.agent_label
    corps = "\n\n".join([
        "Hi,",
        f"Your first {nom} process has run. A process is most useful when it runs "
        "without you having to ask.",
        f"In {a} (or whichever AI tool you connected), ask:",
        f"\"Use {nom}: schedule the process I ran last to run every week.\"",
        "It will set it up with you: the day, the time, and where the result should "
        "land (your inbox, Slack, or a table).",
        f"If anything blocks you, reply to this email.\n\nThe {nom} team",
    ])
    return {"subject": f"Make your {nom} process run on its own",
            "body": corps, "cta_label": f"Open {nom}", "cta_url": r.app_url}


def empreinte(c: dict, r: Reglage) -> str:
    """sha256 de ce que le destinataire reçoit ET de qui l'envoie : changer le texte,
    l'expéditeur ou les copies invalide l'essai."""
    porte = {**c, "sender": r.sender, "reply_to": r.reply_to, "cc": list(r.cc)}
    return hashlib.sha256(
        json.dumps(porte, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _bearer(r: Reglage) -> Optional[str]:
    if r.mailer_url:
        return os.environ.get("OTO_ACTIVATION_MAILER_BEARER") or None
    return None


def _envoyer(r: Reglage, to: str, c: dict, unsubscribe_url: Optional[str]) -> bool:
    from . import email as mailer
    html = mailer.render_composed_email(
        c["body"], cta_text=c["cta_label"], cta_url=c["cta_url"], brand=r.tenant,
        locale=LOCALE, unsubscribe_url=unsubscribe_url)
    return mailer._send(
        to, c["subject"], html, reply_to=r.reply_to, from_email=r.sender,
        cc=list(r.cc) or None, mailer_url=r.mailer_url, bearer=_bearer(r))


def _lien_refus(r: Reglage, sub: str) -> str:
    """Le lien de désinscription de la relance, page en ANGLAIS : le mail l'est.

    Sur l'hôte du TENANT quand il le déclare (`link_base`) : un lien vers notre domaine
    dans un mail parti du sien est une fuite de marque, et un signal d'indésirable. La
    route `/o/u/` est servie sur tous les hôtes de l'instance."""
    from . import outreach_optout
    if r.link_base:
        return f"{r.link_base}/o/u/{outreach_optout.sign(sub)}?lang={LOCALE}"
    return outreach_optout.lien(sub) + f"?lang={LOCALE}"


def _nom_produit(r: Reglage) -> Optional[str]:
    """Le nom que le tenant DÉCLARE (`tenants.brand.nom`), ou None.

    Le registre des tenants n'est posé qu'au boot du serveur : un travail de maintenance
    tourne dans son propre process et le pose lui-même, sans quoi chaque tenant y
    passerait pour inconnu et le mail partirait au gabarit neutre, signé du slug. Sans
    marque déclarée, on n'envoie PAS : « your acme account » n'est pas un mail."""
    from . import db, email_brand, server, tenancy
    if tenancy.current().entry_for_slug(r.tenant) is None:
        db.list_tenant_issuers()   # une base illisible doit échouer ici, pas plus loin
        registre, _ = server._registry_and_issuers()
        tenancy.install(registre)
    m = email_brand._declaree(r.tenant)
    return m.nom if m is not None else None


# ── Le passage ───────────────────────────────────────────────────────────────

def balayer(*, dry_run: bool = False) -> dict:
    """Envoie les emails dus, tenant par tenant, étape par étape. À blanc si
    `dry_run` OU drapeau fermé. Les étapes sont disjointes (un jalon exclut l'autre) :
    une personne ne reçoit jamais deux emails dans le même passage."""
    from .db import activation as db_act
    from .db import outreach as db_outreach

    a_blanc = dry_run or not _ouvert()
    rapport: dict = {"a_blanc": a_blanc, "tenants": []}
    for r in reglages():
        nom = _nom_produit(r)
        if not nom:
            rapport["tenants"].append({"tenant": r.tenant, "bloque": (
                "marque du tenant non déclarée ou incomplète (tenants.brand) : "
                "rien n'est envoyé au gabarit neutre")})
            continue
        budget = r.max_per_run
        for etape in ETAPES:
            c = contenu(r, nom, etape)
            fp = empreinte(c, r)
            camp = r.campagne(etape)
            crit = _criteres(r, etape)
            total = db_act.taille(**crit)
            lot = db_act.audience(**crit, cap=max(1, budget)) if budget > 0 else []
            essaye = LOCALE in db_outreach.locales_essayees(campaign=camp, fingerprint=fp)
            ligne = {"tenant": r.tenant, "etape": etape, "dus": total, "lot": len(lot),
                     "empreinte": fp[:12], "essai_recu": essaye, "envoyes": 0,
                     "refuses": 0}
            rapport["tenants"].append(ligne)
            if a_blanc:
                continue
            if not essaye:
                ligne["bloque"] = ("aucun essai reçu pour ce contenu : "
                                   f"`python -m oto_mcp.activation essai --etape {etape}`")
                log.warning("activation %s/%s : envoi refusé, aucun essai pour "
                            "l'empreinte %s", r.tenant, etape, fp[:12])
                continue
            for p in lot:
                if not db_outreach.enregistre_envoi(
                        campaign=camp, sub=p["sub"], to_email=p["email"],
                        locale=LOCALE, fingerprint=fp, sent_by="activation"):
                    continue
                budget -= 1
                ok = _envoyer(r, p["email"], c, _lien_refus(r, p["sub"]))
                if ok:
                    ligne["envoyes"] += 1
                else:
                    db_outreach.annule_envoi(campaign=camp, sub=p["sub"])
                    ligne["refuses"] += 1
                    log.warning("activation %s/%s : envoi refusé par le relais — la "
                                "personne reste due au passage suivant", r.tenant, etape)
    return rapport


def _criteres(r: Reglage, etape: str) -> dict:
    return dict(etape=etape, tenant=r.tenant, delay_hours=r.delay_hours,
                window_days=r.window_days, sequence_days=r.sequence_days,
                exclude_domains=list(r.exclude_domains))


def essai(tenant: str, operateur_sub: str, etape: Optional[str] = None) -> dict:
    """Envoie le message de l'étape (toutes si `etape` est omise) tel quel à
    l'OPÉRATEUR (sa boîte, copies comprises) et enregistre l'essai pour chaque
    empreinte. C'est ce qui ouvre l'envoi réel de l'étape."""
    from . import db
    from .db import outreach as db_outreach

    r = next((x for x in reglages() if x.tenant == tenant), None)
    if r is None:
        raise ReglageInvalide(f"aucun réglage d'activation pour le tenant {tenant!r}")
    if etape is not None and etape not in ETAPES:
        raise ReglageInvalide(f"étape inconnue : {etape!r} ({', '.join(ETAPES)})")
    op = db.get_user(operateur_sub) or {}
    if op.get("role") not in ("admin", "super_admin") or not op.get("email"):
        raise PermissionError("l'essai part vers un opérateur de plateforme, avec email")
    nom = _nom_produit(r)
    if not nom:
        raise ReglageInvalide(f"marque du tenant {tenant!r} non déclarée (tenants.brand)")
    out = []
    for e in ([etape] if etape else ETAPES):
        c = contenu(r, nom, e)
        fp = empreinte(c, r)
        ok = _envoyer(r, op["email"], c, _lien_refus(r, operateur_sub))
        if ok:
            db_outreach.enregistre_envoi(
                campaign=r.campagne(e), sub=operateur_sub, to_email=op["email"],
                locale=LOCALE, fingerprint=fp, kind="test", sent_by=operateur_sub)
        out.append({"etape": e, "envoye": ok, "empreinte": fp[:12]})
    return {"tenant": tenant, "to": op["email"], "essais": out}


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m oto_mcp.activation",
                                description="Emails d'activation d'un tenant.")
    sous = p.add_subparsers(dest="cmd", required=True)
    e = sous.add_parser("essai", help="s'envoyer les messages, ce qui ouvre l'envoi réel")
    e.add_argument("--tenant", required=True)
    e.add_argument("--operateur", required=True, help="le sub de l'opérateur destinataire")
    e.add_argument("--etape", choices=ETAPES, help="une seule étape (défaut : toutes)")
    sous.add_parser("apercu", help="les contenus et les destinataires, sans rien envoyer")
    args = p.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if args.cmd == "essai":
        print(json.dumps(essai(args.tenant, args.operateur, args.etape),
                         ensure_ascii=False, indent=2))
        return 0
    from .db import activation as db_act
    out = balayer(dry_run=True)
    for r in reglages():
        nom = _nom_produit(r)
        if not nom:
            continue
        for etape in ETAPES:
            out.setdefault("contenus", {}).setdefault(r.tenant, {})[etape] = \
                contenu(r, nom, etape)
            out.setdefault("destinataires", {}).setdefault(r.tenant, {})[etape] = [
                {"email": p["email"], "inscrit": str(p["created_at"])[:10]}
                for p in db_act.audience(**_criteres(r, etape), cap=500)]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
