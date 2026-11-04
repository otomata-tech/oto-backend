"""The ACCOUNTS a resolution may choose, read with their rules, and the choice at a step
(#1499, step 3 — `docs/partage-de-compte.md` § Les règles de comptes).

The resolver (`access/resolve`) walks its steps in oto-core's order — the account the
call NAMES, then the caller's own, then her ACTIVE team's, then the org's — and asks
here, at each step, which account serves (`ChoixDuParcours.a_l_etape`): the live
accounts of the step whose level (`access/niveaux.niveau`) covers what the call requires;
one serves; two are settled by D3 (`niveaux.defaut_qui_tranche`) or refused
`ambiguous_account`; none hands over to the next step, and if nothing at the right level
serves at all, the call is refused `insufficient_level` — never the platform key in their
place. Another person's account serves only NAMED (`autrui_nomme`).

`comptes_de`, `comptes_d_autrui` and `compte_de_la_ligne` are the only reads of the rules
on the hot path (one query each), and the seam the tests replace.

The echo of a call (`rappel_du_compte`) says which account served and, when it is another
person's, whose (`lent_by`).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .. import credentials_store, providers
from . import niveaux
from .niveaux import Compte, Regle


def _compte(row: dict) -> Compte:
    regles = tuple(Regle(user=r.get("user"), group=r.get("group"), org=bool(r.get("org")),
                         level=r["level"],
                         services=(tuple(r["services"]) if r.get("services") is not None
                                   else None))
                   for r in (row.get("regles") or []))
    return Compte(instance_id=row["instance_id"], owner_type=row["owner_type"],
                  owner_id=str(row["owner_id"]), connector=row["connector"],
                  account=row["account"] or "", org_id=row.get("org_id"), regles=regles,
                  is_default=bool(row.get("is_default")), label=row.get("label"),
                  default_par=row.get("default_par"),
                  default_le=(str(row["default_le"]) if row.get("default_le") else None),
                  org_nom=row.get("org_nom"))


def comptes_des_lignes(porteur: str, lignes: list) -> dict:
    """`{(entity_type, entity_id, account): Compte}` — the accounts (instances) of these
    vault rows with their rules, in one query. The seam the tests replace."""
    from ..db import regles_de_comptes
    return {(c.owner_type, c.owner_id, c.account): c
            for c in map(_compte, regles_de_comptes.comptes_des_lignes(porteur, lignes))}


def comptes_d_autrui(connector: str, org_id: int, sub: str, account: str) -> list:
    """Other persons' accounts of the org named `account`. The seam the tests replace."""
    from ..db import regles_de_comptes
    return [_compte(r) for r in regles_de_comptes.comptes_d_autrui(connector, org_id, sub,
                                                                    account)]


def comptes_hors_org(connector: Optional[str], org_id: int, sub: str,
                     account: Optional[str] = None) -> list:
    """The accounts of OTHER orgs than `org_id` that `sub` may name: her own key there
    (#172) and those a rule naming her opens (D1). The seam the tests replace."""
    from ..db import regles_de_comptes
    return [_compte(r) for r in regles_de_comptes.comptes_hors_org(connector, sub, org_id,
                                                                    account)]


def comptes_de_l_etape(porteur: str, entity_type: str, entity_id: str,
                       lignes: list[tuple[str, bool]]) -> list:
    """The accounts of one step: its vault rows (`(account, is_default)`, read by the
    resolver from the vault) joined to their instances and rules. A live vault row
    without its instance is a defect (the instance is born with the row,
    `db/connector_instances`): RAISES, never a level presumed."""
    cles = [(entity_type, str(entity_id), a or "") for a, _ in lignes]
    trouves = comptes_des_lignes(porteur, cles)
    out = []
    for (a, defaut), cle in zip(lignes, cles):
        compte = trouves.get(cle)
        if compte is None:
            raise RuntimeError(f"vault row {':'.join(cle[:2])}:{porteur}:{cle[2]!r} has no "
                               "live instance: its level cannot be judged")
        out.append(replace(compte, is_default=bool(defaut)) if defaut is not None
                   else compte)
    return out


class ChoixDuParcours:
    """The choice along ONE resolution: who calls, what the call requires, the accounts
    seen under that level, and the account each step served. Created per resolution."""

    def __init__(self, appelant: niveaux.Appelant, service: str, besoin: str):
        self.appelant = appelant
        self.service = service
        self.besoin = besoin
        self.sous_le_niveau: list[tuple[Compte, str]] = []
        self.servis: dict[tuple, Compte] = {}
        self._a_nommer: Optional[list] = None

    def niveau(self, compte: Compte) -> str:
        return niveaux.niveau(self.appelant, compte, self.service, besoin=self.besoin)

    def exiger(self, compte: Compte) -> None:
        """The account NAMED (or pinned) serves only at the level the call requires."""
        detenu = self.niveau(compte)
        if not niveaux.suffit(detenu, self.besoin):
            raise niveaux.refus_niveau(compte, detenu, self.besoin,
                                       org_appel=self.appelant.org)

    def a_l_etape(self, entity_type: str, entity_id: str, lignes: list, nom: Optional[str],
                  *, ou: str) -> Optional[Compte]:
        """The account the step `(entity_type, entity_id)` serves among its vault rows
        `lignes` (`(account, is_default)`), or None (hand over). `nom`: the account the
        call names — found here, it serves at the level required or is refused; absent
        here, the step hands over. Unnamed: the accounts at the level required; one
        serves, two are settled by D3 or refused `ambiguous_account`."""
        porteur = providers.credential_provider(self.service)
        if nom is not None:
            lignes = [(a, d) for a, d in lignes if (a or "") == nom]
        if not lignes:
            return None
        trouves = comptes_de_l_etape(porteur, entity_type, entity_id, lignes)
        if nom is not None:
            self.exiger(trouves[0])
            return self.servir(trouves[0])
        utilisables = []
        for c in trouves:
            detenu = self.niveau(c)
            if niveaux.suffit(detenu, self.besoin):
                utilisables.append(c)
            else:
                self.sous_le_niveau.append((c, detenu))
        if len(utilisables) == 1:
            return self.servir(utilisables[0])
        if not utilisables:
            return None
        tranche = niveaux.defaut_qui_tranche(utilisables, self.appelant)
        if tranche is None:
            raise niveaux.refus_ambigu(utilisables, ou=ou, a_nommer=self.a_nommer())
        return self.servir(tranche)

    def autrui_nomme(self, nom: str) -> Optional[Compte]:
        """An account the call NAMES that no step of the call's org holds: another
        person's account of that org first; else an account of ANOTHER org — the
        caller's own key there (#172) or one a rule naming her opens (D1, #1499). Each
        serves only named, at the level its rules give. Two of the same name at the same
        place: `ambiguous_account` (name it by `_instance=`). None: nothing bears it."""
        org = self.appelant.org
        if org is None:
            return None
        porteur = providers.credential_provider(self.service)
        for lire, ou in ((lambda: comptes_d_autrui(porteur, org, self.appelant.sub, nom),
                          "shared with you under that name"),
                         (lambda: comptes_hors_org(porteur, org, self.appelant.sub, nom),
                          "of other orgs under that name")):
            ouverts = [c for c in lire() if niveaux.RANG[self.niveau(c)] > 0]
            if len(ouverts) > 1:
                raise niveaux.refus_ambigu(ouverts, ou=ou)
            if ouverts:
                self.exiger(ouverts[0])
                return self.servir(ouverts[0])
        return None

    def a_nommer(self) -> list[dict]:
        """The accounts of other orgs the caller could NAME (her own key there, #172;
        those a rule opens to her, D1), for a refusal to list — read once, lazily."""
        if self._a_nommer is None:
            org = self.appelant.org
            porteur = providers.credential_provider(self.service)
            out = []
            for c in (comptes_hors_org(porteur, org, self.appelant.sub)
                      if org is not None else []):
                detenu = self.niveau(c)
                if niveaux.RANG[detenu] > 0:
                    out.append(niveaux.a_nommer(
                        c, detenu, propre=c.proprietaire == self.appelant.sub))
            self._a_nommer = out
        return self._a_nommer

    def servir(self, compte: Compte) -> Compte:
        self.servis[(compte.owner_type, compte.owner_id, compte.account)] = compte
        if compte.org_id is not None and compte.org_id != self.appelant.org:
            # An account of another org serves: the echo says whose org lent it, and at
            # which level (`rappel_du_compte`) — nothing else of that org.
            from .. import session_org
            session_org.note_call_trace(compte_d_une_autre_org={
                "org": compte.org_nom or f"#{compte.org_id}", "level": self.niveau(compte),
                "own": compte.proprietaire == self.appelant.sub})
        return compte

    def refuser_si_sous_le_niveau(self, win) -> None:
        """Accounts were seen at a step but none at the level the call requires, and no
        account of the right level won (nothing, or the platform key): refused, naming
        the first of them — never the platform key in their place."""
        if self.sous_le_niveau and (win is None or win.mode == "platform"):
            compte, detenu = self.sous_le_niveau[0]
            raise niveaux.refus_niveau(compte, detenu, self.besoin,
                                       a_nommer=self.a_nommer(),
                                       org_appel=self.appelant.org)


# ── What `oto_context` says of the accounts (#1499) ─────────────────────────────

def defauts_des_etapes(org: int, equipe: Optional[int]) -> list[dict]:
    """The defaults set at the org's and the team's step. The seam the tests replace."""
    from ..db import regles_de_comptes
    return regles_de_comptes.defauts_des_etapes(org, equipe)


def comptes_du_contexte(sub: str, org: Optional[int], equipe: Optional[int]) -> dict:
    """The accounts the context names: those of OTHER orgs the caller may name — her own
    key there (#172, `your_account_in_org`) and those a rule opens to her (D1:
    `lent_by_org` and the level, nothing else of that org) —, and the defaults that settle
    a tie at the org's and the active team's step (D3: the account, who set it, when)."""
    if org is None:
        return {"to_name": [], "defaults": []}
    appelant = niveaux.Appelant(sub, org, equipe)
    a_nommer = []
    for c in comptes_hors_org(None, org, sub):
        detenu = niveaux.niveau(appelant, c)
        if niveaux.RANG[detenu] > 0:
            a_nommer.append({"connector": c.connector, **niveaux.a_nommer(
                c, detenu, propre=c.proprietaire == sub)})
    defauts = [{"step": "org" if d["entity_type"] == "org" else "team",
                "connector": d["connector"], "account": d["account"],
                "set_by": (niveaux.libelle_de_personne(d["default_par"]) if d["default_par"]
                           else None),
                "set_at": d["default_le"]}
               for d in defauts_des_etapes(org, equipe)]
    return {"to_name": a_nommer, "defaults": defauts}


# ── The echo ─────────────────────────────────────────────────────────────────────

def ligne_d_un_membre(row) -> bool:
    """Is the vault row that served (`credential_row` of the call trace) a person's?
    Pure: the echo asks it on the event loop, and only such a row is worth a query."""
    return bool(row) and row[0] == credentials_store.MEMBER


def preteur_de_la_ligne(sub: Optional[str], row) -> Optional[str]:
    """The sub of the person whose row served, when it is not `sub`'s. Pure."""
    if not ligne_d_un_membre(row):
        return None
    _, _, proprietaire = str(row[1]).partition(":")
    return proprietaire if proprietaire and proprietaire != sub else None


def rappel_du_compte(sub: Optional[str], account: str, row,
                     autre_org: Optional[dict] = None) -> dict:
    """The descriptor a response carries: `{_account}`, plus `lent_by` (the owner's
    label) when another person's account served, plus — an account of ANOTHER org
    (`autre_org`, noted at resolution) — `lent_by_org` and `level`, or
    `your_account_in_org` for the caller's own key there (#172)."""
    out = {"_account": account}
    proprietaire = preteur_de_la_ligne(sub, row)
    if proprietaire is not None and not autre_org:
        out["lent_by"] = niveaux.libelle_de_personne(proprietaire)
    if autre_org:
        if autre_org.get("own"):
            out["your_account_in_org"] = autre_org["org"]
        else:
            out["lent_by_org"], out["level"] = autre_org["org"], autre_org["level"]
    return out
