"""Les deux mails de l'effacement : le lien de confirmation, et « c'est fait ».

Sous la marque de l'org (`orgs.front_brand`), comme une invitation (`email_templates`).
Le mail de confirmation dit ce qui va disparaître, quand, et comment l'arrêter : un
lien qui détruit tout ne doit jamais ressembler à un lien qu'on clique par réflexe.
"""
from __future__ import annotations

from .. import email as _email
from .. import email_brand as _charte


def _envoyer(m: _charte.Marque, to: str, subject: str, html: str) -> bool:
    return _email._send(to, subject, html, from_email=m.expediteur or None)


def send_org_erasure_confirm_email(to: str, *, org_name: str, confirm_url: str,
                                   delai_jours: int, par_operateur: bool,
                                   brand: str | None = None, locale: str | None = None) -> bool:
    m = _charte.marque(brand)
    locale = locale or m.langue or None
    org = _email._esc(org_name)
    if locale == "en":
        qui = ("our support team, at your request," if par_operateur
               else "an administrator of the workspace")
        subject = f"confirm the permanent deletion of {org_name}"
        contenu = (
            f'<p style="{_charte.PARA}">{qui} asked to <strong>permanently delete</strong> '
            f'the workspace <strong>{org}</strong> on {_email._esc(m.nom)}: its projects, '
            "tables, processes, agents, connected accounts, files and history, and the "
            "accounts of members who belong to no other workspace.</p>"
            f'<p style="{_charte.PARA}">once you confirm, the workspace is suspended at once '
            f"and erased {delai_jours} days later. Until then, an administrator can still "
            "cancel it from the workspace settings.</p>"
            + _email._bouton(confirm_url, "review and confirm", brand)
            + f'<p style="{_charte.PARA}">not expecting this? ignore this email: nothing is '
            "deleted without your confirmation, and this link expires in 72 hours.</p>")
    else:
        qui = ("notre support, à votre demande," if par_operateur
               else "un administrateur de l'espace")
        subject = f"confirmez la suppression définitive de {org_name}"
        contenu = (
            f'<p style="{_charte.PARA}">{qui} a demandé la <strong>suppression '
            f"définitive</strong> de l'espace <strong>{org}</strong> sur "
            f"{_email._esc(m.nom)} : ses projets, tableaux, processus, agents, comptes "
            "connectés, fichiers et son historique, ainsi que les comptes des membres qui "
            "n'appartiennent à aucun autre espace.</p>"
            f'<p style="{_charte.PARA}">dès votre confirmation, l\'espace est suspendu, puis '
            f"effacé {delai_jours} jours plus tard. D'ici là, un administrateur peut encore "
            "annuler depuis les réglages de l'espace.</p>"
            + _email._bouton(confirm_url, "vérifier et confirmer", brand)
            + f'<p style="{_charte.PARA}">vous n\'attendiez pas ce mail ? ignorez-le : rien '
            "n'est supprimé sans votre confirmation, et ce lien expire dans 72 heures.</p>")
    return _envoyer(m, to, subject, _charte.page(
        m, contenu, preheader=subject,
        mention=_charte.mention_transactionnelle(m, locale), locale=locale))


def send_org_erasure_done_email(to: str, *, org_name: str | None, brand: str | None = None,
                                locale: str | None = None) -> bool:
    m = _charte.marque(brand)
    locale = locale or m.langue or None
    org = _email._esc(org_name or "")
    if locale == "en":
        subject = f"{org_name or 'your workspace'} has been deleted"
        contenu = (f'<p style="{_charte.PARA}">the workspace <strong>{org}</strong> and its '
                   f"data have been permanently erased from {_email._esc(m.nom)}. Invoices "
                   "already issued are kept for the period required by law.</p>"
                   f'<p style="{_charte.PARA}">this is the last email you will receive about '
                   "it.</p>")
    else:
        subject = f"{org_name or 'votre espace'} a été supprimé"
        contenu = (f'<p style="{_charte.PARA}">l\'espace <strong>{org}</strong> et ses '
                   f"données ont été définitivement effacés de {_email._esc(m.nom)}. Les "
                   "factures déjà émises sont conservées le temps que la loi impose.</p>"
                   f'<p style="{_charte.PARA}">c\'est le dernier mail que vous recevrez à son '
                   "sujet.</p>")
    return _envoyer(m, to, subject, _charte.page(
        m, contenu, preheader=subject,
        mention=_charte.mention_transactionnelle(m, locale), locale=locale))
