"""Another person's HOSTED account (Unipile channels): its sub-instance and its rules
(#1499, step 3 — `docs/unipile.md`).

A hosted account has no vault row: its rules target its SUB-INSTANCE
(`db/sous_instances`) — connector `unipile`, owner `member` `<org>:<sub>`, account
`canal:<PROVIDER>`. There is no pointer and no grant: another person's account serves
only NAMED by the call (`_account=<account_id>` or `_instance=inst:<id>`), at the level
the rules of its sub-instance give the caller (`access.niveau`), and is refused
below it (`insufficient_level`). The caller's own account is resolved by
`connectors/identities`, which asks here only for the others'.
"""
from __future__ import annotations

PREFIXE_CANAL = "canal:"


def compte_du_canal(provider: str) -> str:
    """The `account` of a hosted account's sub-instance: `LINKEDIN` → `canal:LINKEDIN`."""
    return PREFIXE_CANAL + provider.upper()


def canal_du_compte(account: str) -> str | None:
    """The channel a sub-instance's `account` names (`canal:LINKEDIN` → `LINKEDIN`),
    None for any other account."""
    if not (account or "").startswith(PREFIXE_CANAL):
        return None
    return account[len(PREFIXE_CANAL):].upper() or None


def connecteur_du_compte(account: str) -> str | None:
    """The channel CONNECTOR of a sub-instance (`canal:WHATSAPP` → `whatsapp`), the one
    that gates its calls; None for an account that names no registered channel."""
    from .. import providers
    con = providers.connector_for_hosted_channel(canal_du_compte(account) or "")
    return con.name if con else None


def _service_du_canal(provider: str) -> str:
    """The connector a call on this channel is made to (the rules' `services` name it)."""
    from .. import providers
    con = providers.connector_for_hosted_channel(provider)
    return con.name if con else "unipile"


def canaux() -> list[str]:
    from .. import providers
    return [c.hosted_channel for c in providers._REGISTRY_LIST if c.hosted_channel]


def _appelant(sub: str):
    from .. import access
    return access.Appelant(sub, access.current_org(sub),
                                   lambda: access.current_group(sub))


def comptes_d_autrui_du_canal(sub: str, provider: str) -> list[dict]:
    """The LIVE accounts of this channel of OTHER persons that a call may name:
    `[{compte, owner_sub, account_id, account_name, org_nom}]` (`compte` = their
    sub-instance with its rules). Those connected in the context org (`org_nom` None),
    then those of ANOTHER org opened to `sub` by a rule naming her (D1, #1499: `org_nom`
    = the lending org's name). Her own account linked in another org is not among them:
    it is adopted, never served from there (#1342). Their level is not judged here."""
    from .. import access
    org = access.current_org(sub)
    if org is None:
        return []
    canal = compte_du_canal(provider)
    ici = [(c, None) for c in access.comptes_d_autrui("unipile", org, sub, canal)]
    ailleurs = [(c, c.org_nom) for c in access.comptes_hors_org("unipile", org, sub, canal)
                if c.proprietaire != sub]
    out = []
    for compte, org_nom in ici + ailleurs:
        ligne = _ligne_du_compte(compte, provider)
        if ligne:
            out.append({"compte": compte, "owner_sub": compte.proprietaire,
                        "account_id": ligne["account_id"],
                        "account_name": ligne.get("account_name"), "org_nom": org_nom})
    return out


def _ligne_du_compte(compte, provider: str) -> "dict | None":
    """The live `unipile_accounts` row of a sub-instance, in ITS org."""
    from .. import db
    proprio = compte.proprietaire
    if not proprio or compte.org_id is None:
        return None
    return db.get_unipile_account(proprio, compte.org_id, provider.upper())


def _vise(ref, compte) -> bool:
    """Does the call's `_instance=` ref designate this sub-instance?"""
    if ref is None:
        return False
    if ref.level == "inst":
        return ref.instance_id == compte.instance_id
    return (ref.level == "member" and compte.owner_id == f"{ref.org_id}:{ref.sub}"
            and (ref.account or compte.account) == compte.account
            and ref.connector in ("unipile", connecteur_du_compte(compte.account)))


def autrui_nomme(sub: str, provider: str, *, account_id: str | None = None,
                  ref=None) -> str | None:
    """The `account_id` of another person's account of this channel NAMED by the call
    (`account_id` from `_account=`, or `ref` from `_instance=`), when its rules open it to
    the caller at the level the call requires. Below that level (but above `none`):
    refused `insufficient_level`, naming who manages it. No rule for the caller (`none`)
    or nobody's account by that name: None — the caller then refuses as for an unknown
    account (an account closed to you is not revealed)."""
    from .. import access
    appelant = _appelant(sub)
    exige = access.besoin()
    service = _service_du_canal(provider)
    for autre in comptes_d_autrui_du_canal(sub, provider):
        compte = autre["compte"]
        if not (autre["account_id"] == account_id or _vise(ref, compte)):
            continue
        detenu = access.niveau(appelant, compte, service, besoin=exige)
        if access.RANG[detenu] == 0:
            return None
        if not access.suffit(detenu, exige):
            raise access.refus_niveau(compte, detenu, exige)
        return autre["account_id"]
    return None


def ref_du_canal(sub: str, provider: str):
    """The call's `_instance=` ref when it names a hosted account of THIS channel
    (`inst:<id>` of a sub-instance, or a `member` ref of one); None otherwise — a ref
    for another connector did not target this resolution."""
    from .. import session_org
    ref = session_org.current_call_instance()
    if ref is None:
        return None
    if ref.level == "inst":
        from ..db import regles_de_comptes
        ligne = regles_de_comptes.compte_par_id(ref.instance_id) if ref.instance_id else None
        if ligne and ligne["connector"] == "unipile" \
                and canal_du_compte(ligne["account"]) == provider.upper():
            return ref
        return None
    if ref.level == "member" and (ref.connector == "unipile" and (
            canal_du_compte(ref.account) == provider.upper())
            or ref.connector == _service_du_canal(provider)):
        return ref
    return None


def autrui_listes(sub: str, canal: str | None) -> list[dict]:
    """Other members' accounts of the org (this channel, or all) whose rules give the
    caller a level above `none`, those of another org lent to her by a rule naming her
    included: `{id, label, is_default: False, channel, level, owner, instance_id}`, plus
    `lent_by_org` (the lending org's name) for the latter."""
    from .. import access
    appelant = _appelant(sub)
    out = []
    for ch in ([canal.upper()] if canal else canaux()):
        service = _service_du_canal(ch)
        for autre in comptes_d_autrui_du_canal(sub, ch):
            detenu = access.niveau(appelant, autre["compte"], service)
            if access.RANG[detenu] == 0:
                continue
            proprio = autre["owner_sub"]
            libelle = access.libelle_de_personne(proprio)
            ident = {
                "id": autre["account_id"],
                "label": f"{autre['account_name'] or autre['account_id']} — account of {libelle}",
                "is_default": False, "channel": ch, "level": detenu,
                "owner": {"type": "member", "id": autre["compte"].owner_id, "label": libelle},
                "instance_id": autre["compte"].instance_id,
            }
            if autre["org_nom"] is not None:
                # Lent from another org (D1): its name and the owner's label, nothing
                # else of that org.
                ident["lent_by_org"] = autre["org_nom"]
            out.append(ident)
    return out
