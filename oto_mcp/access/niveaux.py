"""The LEVEL of a caller on a connector account, and what a call requires (#1499, step 3).

The model is oto-core's (`droits-d-acces.md`, `connecteurs-et-comptes.md`), transposed:
an account is an instance (`connector_instances`) owned by the org, a team or a person;
its rules (`connector_account_rules`) give a level to a person, a team or the whole org,
on the scale `none` < `read` < `write` < `sensitive` < `manage`
(`docs/partage-de-compte.md` § Les règles de comptes).

**The level of a caller on an account** (`niveau`), in this order:
- an account of ANOTHER org than the call's: D1 (`niveau_entre_orgs`) — its owner
  manages it, anyone else only what a rule naming her gives;
- a personal account: `manage` for its owner, before any rule; for anyone else, nothing
  once the owner has left the account's org or is paused, else its rules;
- a rule that names the person wins; else a rule on her ACTIVE team (or on the team that
  owns the account, for one of its members); else the account's default — `sensitive`
  for a member of the owning team (D2 for a non-member, `niveau_equipe_non_membre`),
  `sensitive` for a member of the owning org (deviation (a) from oto-core) — on an org
  account, a rule on the whole org replaces it; on a team's, it raises it;
- under the level a call needs, the org admin manages the org's and the teams' accounts,
  and a team lead manages his team's accounts unless a rule names him.
A rule with `services` (a Google or Microsoft account) covers only those services and the
account itself (its identity); for any other service it counts as `none`.

**What a call requires** (`besoin`): the class of its op (`tools/effets`) — reading needs
`read`, writing `write`, sending or destroying `sensitive`. The edge of the call
(`middleware/call_context`, `tools/meta.executer_cible`) notes the tool and its arguments
(`noter_l_appel`); the resolver reads the class from them, so the level is judged on the
account REALLY resolved. An op without a declared class raises. Outside a tool call (a
REST route that configures an account), the resolution needs `read` (`NIVEAU_HORS_APPEL`).

**The decisions of #1499**, all settled by the owner, are isolated here and only here: D1
(`niveau_entre_orgs`: sharing across orgs kept, to a person, by a rule), D2
(`niveau_equipe_non_membre`: cut), D3 (`defaut_qui_tranche`: the default, framed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from mcp.types import ErrorData

from ..mcp_errors import McpError

NIVEAUX = ("none", "read", "write", "sensitive", "manage")
RANG = {n: i for i, n in enumerate(NIVEAUX)}
#: The level each class of `tools/effets.CLASSES` requires.
NIVEAU_DE_LA_CLASSE = {"lire": "read", "ecrire": "write", "envoyer": "sensitive",
                       "detruire": "sensitive"}
#: Outside a tool call (a REST route that configures or tests an account): reading.
NIVEAU_HORS_APPEL = "read"
#: The defaults: a member of the owning team, a member of the owning org. oto-core gives
#: a member of the org `read`; here `sensitive` (decided 05/11/2026): an org account
#: serves its members as it did before the rules, and a rule — of the org or of the
#: person — restricts it, `none` included.
DEFAUT_MEMBRE_EQUIPE = "sensitive"
DEFAUT_MEMBRE_ORG = "sensitive"

#: The carriers whose account AUTHORIZES its services one by one: a rule on such an
#: account may carry a LIST of services (`connector_account_rules.services`).
PORTEURS_A_SERVICES = frozenset({"google", "microsoft"})

CODE_NIVEAU_INSUFFISANT = "insufficient_level"
CODE_COMPTE_AMBIGU = "ambiguous_account"
CODE_COMPTE_SUSPENDU = "account_suspended"
CODE_EFFET_NON_DECLARE = "effect_undeclared"



@dataclass(frozen=True)
class Regle:
    """One rule of an account: exactly one subject (`user`, `group` or `org`)."""
    user: Optional[str]
    group: Optional[int]
    org: bool
    level: str
    services: Optional[tuple] = None

    def couvre(self, service: Optional[str], porteur: str) -> bool:
        """Does the rule cover `service`? No list = everything; the account itself (its
        identity, `service == porteur`) is always covered."""
        if self.services is None or service is None or service == porteur:
            return True
        return service in self.services

    def niveau_pour(self, service: Optional[str], porteur: str) -> str:
        return self.level if self.couvre(service, porteur) else "none"


@dataclass(frozen=True)
class Compte:
    """An account (an instance) as the level reads it. `owner_type`/`owner_id` are the
    instance's (`member` `<org>:<sub>`, `group` `<id>`, `org` `<id>`); `org_id` is the
    org the account lives in (read from the team for a team account)."""
    instance_id: Optional[int]
    owner_type: str
    owner_id: str
    connector: str
    account: str
    org_id: Optional[int]
    regles: tuple = ()
    is_default: bool = False
    #: Who set the default (`meta.default_set_by`), None before #1499.
    default_par: Optional[str] = None
    #: When (`meta.default_set_at`), None before #1499.
    default_le: Optional[str] = None
    #: The name of the account's org — how an account of ANOTHER org is introduced
    #: (« lent by <org> »), never anything else of that org.
    org_nom: Optional[str] = None
    #: When its key was set (`connector_credentials.set_at`, ISO): the most recent of
    #: several accounts of other orgs serves at step 5. None without a vault row.
    pose_le: Optional[str] = None
    #: When the most recent rule that opens it to the caller was set (ISO): the most
    #: recent LOAN serves at step 5. None when no rule opens it to her (or not read).
    prete_le: Optional[str] = None
    label: Optional[str] = None

    @property
    def proprietaire(self) -> Optional[str]:
        if self.owner_type == "member":
            return self.owner_id.partition(":")[2] or None
        return None

    @property
    def equipe(self) -> Optional[int]:
        if self.owner_type == "group" and str(self.owner_id).isdigit():
            return int(self.owner_id)
        return None

    @property
    def libelle(self) -> str:
        """How a refusal names it: its label, else its account name, else its connector."""
        return self.label or self.account or self.connector


@dataclass
class Appelant:
    """Who calls, in which context. The role reads are LAZY (`roles`, `group_store`) and
    memoized for one resolution: an account whose rules already suffice reads none."""
    sub: str
    org: Optional[int]
    #: The active team, or a zero-arg callable that reads it — LAZY: a call served by
    #: its own key never reads it.
    equipe: "Optional[int] | Callable[[], Optional[int]]" = None
    _memo: dict = field(default_factory=dict)

    @property
    def equipe_active(self) -> Optional[int]:
        if callable(self.equipe):
            self.equipe = self.equipe()
        return self.equipe

    def _une_fois(self, cle: tuple, lire: Callable[[], bool]) -> bool:
        if cle not in self._memo:
            self._memo[cle] = bool(lire())
        return self._memo[cle]

    def membre_de_l_equipe(self, gid: int) -> bool:
        if gid == self.equipe_active:
            return True
        from .. import group_store
        return self._une_fois(("equipe", gid),
                              lambda: group_store.is_group_member(self.sub, gid))

    def admin_de_l_org(self, org: int) -> bool:
        from .. import roles
        return self._une_fois(("admin", org), lambda: roles.is_org_admin(self.sub, org))

    def chef_de_l_equipe(self, gid: int) -> bool:
        from .. import roles
        return self._une_fois(("chef", gid), lambda: roles.can_admin_group(self.sub, gid))

    def _hors_de_l_org_de_l_appel(self) -> bool:
        """`_project=` set the org of a shared project she is NOT a member of as the
        call's org (`session_org.current_call_hors_org`): there, she is not a member."""
        from .. import session_org
        return session_org.current_call_hors_org() == (self.sub, self.org)

    def membre_de_l_org(self, org: int) -> bool:
        if org == self.org and not self._hors_de_l_org_de_l_appel():
            return True   # the call's org: membership is guarded where it is set
        from .. import roles
        return self._une_fois(("org", org), lambda: roles.is_org_member(self.sub, org))


# ── The decisions of #1499 (settled) ──────────────────────────────────────────────

def niveau_entre_orgs(appelant: Appelant, compte: Compte, service: Optional[str],
                      porteur: str) -> str:
    """D1 — settled by the owner (#1499): an account of ANOTHER org than the call's. Its
    owner (a person's own key set in another org, #172) manages it; anyone else holds only
    what a rule NAMING her gives (a person outside the account's org — never a team or an
    org from outside); no team rule, org rule, default or role reaches across orgs. Such
    an account serves NAMED, or unnamed at step 5 when nothing local serves
    (`access/comptes`); its owner must still be a member of its org."""
    proprietaire = compte.proprietaire
    if proprietaire is not None:
        if compte.org_id is None or not _proprietaire_present(proprietaire, compte.org_id):
            return "none"
        if proprietaire == appelant.sub:
            return "manage"
    nominative = next((r for r in compte.regles if r.user == appelant.sub), None)
    return "none" if nominative is None else nominative.niveau_pour(service, porteur)


def niveau_equipe_non_membre(appelant: Appelant, compte: Compte) -> str:
    """D2 — settled by the owner (#1499): a team account used by someone who is not a
    member of that team (a shared project, #480; someone who left the team) is CUT —
    `none` without a rule that names her (or her active team, or the org)."""
    return "none"


def defaut_qui_tranche(candidats: list, appelant: "Optional[Appelant]" = None
                       ) -> Optional[Compte]:
    """D3 — settled by the owner (#1499), option A: two accounts usable at the same step
    (the org's, or the active team's) are refused `ambiguous_account`, UNLESS a persistent
    default (`meta.is_default` of the vault row) designates exactly one of them AND was set
    by someone who manages that account. `candidats` are already the accounts at the level
    the call requires: a default under that level never settles. It never passes a named
    account nor the caller's own (earlier steps).

    The author (`meta.default_set_by`) is recorded on every path since #1499. A default
    without author predates it: every path that could set an org or team default was
    guarded by its management (`capabilities/connectors/identities._require_scope`,
    `auth/google.scope_target`, the Google connect at those scopes) — it counts as set
    by a manager."""
    defauts = [c for c in candidats if c.is_default]
    if len(defauts) != 1:
        return None
    elu = defauts[0]
    if elu.default_par is None or appelant is None:
        return elu
    auteur = Appelant(elu.default_par, elu.org_id)
    return elu if niveau(auteur, elu, besoin="manage") == "manage" else None


# ── The level ────────────────────────────────────────────────────────────────────

def _plus_haut(*les_niveaux: str) -> str:
    return max(les_niveaux, key=RANG.__getitem__)


def suffit(niveau_detenu: str, niveau_exige: str) -> bool:
    return RANG[niveau_detenu] >= RANG[niveau_exige]


def niveau(appelant: Appelant, compte: Compte, service: Optional[str] = None, *,
           besoin: str = "manage") -> str:
    """The level of `appelant` on `compte` for a call to `service` (a service of the
    account, or None = the account itself). `besoin`: under it, the roles that MANAGE
    (org admin, team lead) are read; at or above it, they are not (a level that already
    suffices reads no role)."""
    porteur = compte.connector
    if compte.org_id is not None and appelant.org is not None and compte.org_id != appelant.org:
        return niveau_entre_orgs(appelant, compte, service, porteur)
    proprietaire = compte.proprietaire
    if proprietaire is not None:
        if proprietaire == appelant.sub:
            return "manage"
        if compte.org_id is None or not _proprietaire_present(proprietaire, compte.org_id):
            return "none"   # the owner left the org, or is paused: no longer serves others
    nominative = next((r for r in compte.regles if r.user == appelant.sub), None)
    equipe = compte.equipe
    regle_equipe = next((r for r in compte.regles if r.group is not None and (
        r.group == appelant.equipe_active
        or (r.group == equipe and appelant.membre_de_l_equipe(equipe)))), None)
    regle_org = next((r for r in compte.regles if r.org), None)
    if nominative is not None:
        detenu = nominative.niveau_pour(service, porteur)
    elif regle_equipe is not None:
        detenu = regle_equipe.niveau_pour(service, porteur)
    else:
        detenu = _defaut(appelant, compte)
        if regle_org is not None and compte.org_id is not None \
                and appelant.membre_de_l_org(compte.org_id):
            # On an ORG account the org's rule REPLACES the members' default (it may
            # restrict it); on a team's, it opens the account to the rest of the org.
            par_regle = regle_org.niveau_pour(service, porteur)
            detenu = (par_regle if compte.owner_type == "org"
                      else _plus_haut(detenu, par_regle))
    if suffit(detenu, besoin) or proprietaire is not None:
        return detenu
    if compte.org_id is not None and appelant.admin_de_l_org(compte.org_id):
        return "manage"
    if equipe is not None and nominative is None and appelant.chef_de_l_equipe(equipe):
        return "manage"
    return detenu


def _defaut(appelant: Appelant, compte: Compte) -> str:
    equipe = compte.equipe
    if equipe is not None:
        return (DEFAUT_MEMBRE_EQUIPE if appelant.membre_de_l_equipe(equipe)
                else niveau_equipe_non_membre(appelant, compte))
    if compte.owner_type == "org" and compte.org_id is not None \
            and appelant.membre_de_l_org(compte.org_id):
        return DEFAUT_MEMBRE_ORG
    return "none"


def _proprietaire_present(sub: str, org: int) -> bool:
    """Is the owner of a personal account still a member of its org, and awake? Her
    account stops serving the others the moment she leaves or is paused, and serves again
    when she comes back — read on every call, nothing is detached (ADR 0062-D4)."""
    from .. import account_suspension, org_store
    # Her REAL membership (`org_store.get_org_role`), not a role read for the caller: no
    # platform escalation, and a delegated job's org lock (`verrou_org`) — which bounds
    # what its bearer does, not whose account it may name — does not blind it.
    return (org_store.get_org_role(org, sub) is not None
            and account_suspension.etat(sub) is None)


# ── What a call requires ─────────────────────────────────────────────────────────

def noter_l_appel(outil: str, args: Optional[dict], en_lecture: bool = False):
    """Called by the edge of a tool call (`middleware/call_context`,
    `tools/meta.executer_cible`) before the tool runs: what the call does, judged at
    resolution on the account really resolved (`besoin`). `en_lecture`: the tool declares
    itself read-only (`tools/lecture.en_lecture` on its FastMCP object) — then it has no
    line in `tools/effets` and requires `read`. Returns the token the edge hands back to `oublier_l_appel` once the tool is done."""
    from .. import session_org
    return session_org.set_call_exigence(
        {"outil": outil, "args": dict(args or {}), "en_lecture": bool(en_lecture)})


def oublier_l_appel(token) -> None:
    from .. import session_org
    session_org.reset_call_exigence(token)


def besoin() -> str:
    """The level the current call requires: `read` for a read-only tool, else the class
    of its op (`tools/effets`); `NIVEAU_HORS_APPEL` outside a tool call. An op without a
    declared class RAISES (`effect_undeclared`): it is never presumed to read."""
    from .. import session_org
    appel = session_org.current_call_exigence()
    if appel is None:
        return NIVEAU_HORS_APPEL
    if "besoin" in appel:
        return appel["besoin"]
    from ..tools import effets
    outil, args, en_lecture = appel["outil"], appel["args"], appel["en_lecture"]
    try:
        exige = ("read" if en_lecture
                 else NIVEAU_DE_LA_CLASSE[effets.effet(outil, args)[0]])
    except effets.EffetNonDeclare as e:
        raise McpError(ErrorData(
            code=-32603,
            message=(f"`{outil}`: {e}. Every op a connector serves declares what it does "
                     "(`tools/effets.EFFETS`); without it, no account is chosen. Nothing "
                     "was done."),
            data={"code": CODE_EFFET_NON_DECLARE, "retryable": False})) from e
    appel["besoin"] = exige
    return exige


# ── The refusals ─────────────────────────────────────────────────────────────────

class NiveauInsuffisant(McpError):
    """403 `insufficient_level`: the account resolved does not give the caller the level
    the call requires. Names the account, the level held, the level required and who
    manages the account. Typed so that a REST face maps it to a declared 403
    (`capabilities/connectors/verify.py`)."""


def libelle_de_personne(sub: str) -> str:
    """How a person is named to someone else: her e-mail, else her name, else the sub."""
    from .. import db
    email = (db.emails_by_subs([sub]) or {}).get(sub)
    if email:
        return email
    return (db.get_user(sub) or {}).get("name") or sub


def preteur(compte: Compte) -> str:
    """Who lends an account of the call's org served unnamed (step 5, `lent_by`): its
    owner's e-mail, or the team that owns it."""
    if compte.proprietaire is not None:
        return libelle_de_personne(compte.proprietaire)
    from .. import group_store
    equipe = compte.equipe
    return f"team {(group_store.get_group(equipe) or {}).get('name') or f'#{equipe}'}"


def qui_gere(compte: Compte, org_appel: Optional[int] = None) -> str:
    """Who manages an account, in words a refusal can carry. An account of ANOTHER org
    than the call's (`org_appel`) says only that org's name — nothing else of it."""
    from .. import group_store, org_store
    if org_appel is not None and compte.org_id is not None and compte.org_id != org_appel:
        return f"whoever manages it in the org {compte.org_nom or '#' + str(compte.org_id)}"
    proprietaire = compte.proprietaire
    if proprietaire is not None:
        return libelle_de_personne(proprietaire)
    equipe = compte.equipe
    if equipe is not None:
        nom = (group_store.get_group(equipe) or {}).get("name") or f"#{equipe}"
        return f"the lead of the team {nom} (or an admin of the org)"
    nom = (org_store.get_org(compte.org_id) or {}).get("name") if compte.org_id else None
    return f"an admin of the org {nom or '#' + str(compte.org_id)}"


def refus_niveau(compte: Compte, detenu: str, exige: str, *,
                 a_nommer: Optional[list] = None,
                 org_appel: Optional[int] = None) -> NiveauInsuffisant:
    gere = qui_gere(compte, org_appel)
    a_nommer = list(a_nommer or [])
    return NiveauInsuffisant(ErrorData(
        code=-32602,
        message=(f"The account `{compte.libelle}` ({compte.connector}) gives you the level "
                 f"`{detenu}`; this call requires `{exige}`. Ask {gere} to raise your "
                 f"level (oto_instance op='set_rule').{phrase_des_candidats(a_nommer)} "
                 "Nothing was done."),
        data={"code": CODE_NIVEAU_INSUFFISANT, "status": 403, "retryable": False,
              "account": compte.libelle, "instance_id": compte.instance_id,
              "level": detenu, "required": exige, "managed_by": gere,
              "to_name": a_nommer}))


def a_nommer(compte: Compte, detenu: str, *, propre: bool) -> dict:
    """A candidate a refusal lists so the agent can NAME it: an account of another org —
    the caller's own key there (`propre`, #172) or one a rule opens to her (D1). It serves
    unnamed only when nothing local does and it is the only one at the level required
    (step 5); listed when it did not serve. Of the other org it says its name, nothing
    else."""
    org = compte.org_nom or f"#{compte.org_id}"
    out = {"account": compte.account, "label": compte.libelle,
           "instance": f"inst:{compte.instance_id}", "level": detenu}
    out["your_account_in_org" if propre else "lent_by_org"] = org
    return out


def _phrase_a_nommer(c: dict) -> str:
    nom = (f"`_account=\"{c['account']}\"`" if c["account"]
           else f"`_instance=\"{c['instance']}\"`")
    if "your_account_in_org" in c:
        return (f"your own account `{c['label']}` in the org {c['your_account_in_org']} "
                f"({nom}) — name it, or set the key in this org")
    return f"`{c['label']}` lent by {c['lent_by_org']}, level {c['level']} ({nom})"


def phrase_des_candidats(candidats: list[dict]) -> str:
    """The sentence a refusal adds for the accounts to NAME (empty without any)."""
    if not candidats:
        return ""
    return (" Accounts of other orgs you can name: "
            + "; ".join(_phrase_a_nommer(c) for c in candidats) + ".")


def refus_ambigu(candidats: list, *, ou: str, a_nommer: Optional[list] = None):
    """`ambiguous_account`: several accounts usable at the same step, no default to
    settle it (D3). Names them by their label and says how to name one; `a_nommer`: the
    accounts of other orgs the caller can name, listed too."""
    from .cascade import CompteAmbigu
    noms = ", ".join(f"`{c.libelle}`" for c in candidats)
    nommables = [c for c in candidats if c.account]
    comment = ("pass `_account=\"<name>\"` with one of them" if len(nommables) == len(candidats)
               else "pass `_instance=` with the ref of one of them (oto_instance op='list')")
    a_nommer = list(a_nommer or [])
    portee = {"org": "org", "group": "group"}.get(candidats[0].owner_type)
    sc = f"scope='{portee}', " if portee else ""
    return CompteAmbigu(ErrorData(
        code=-32602,
        message=(f"Several {candidats[0].connector} accounts {ou} can serve this call "
                 f"({noms}): {comment}, or set a default with oto_identity(op='set', "
                 f"{sc}connector='{candidats[0].connector}', identity_id='<name>')."
                 f"{phrase_des_candidats(a_nommer)} Nothing was done."),
        data={"code": CODE_COMPTE_AMBIGU, "retryable": False,
              "accounts": [{"account": c.account, "label": c.libelle,
                            "instance_id": c.instance_id} for c in candidats],
              "to_name": a_nommer}))
