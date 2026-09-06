"""Outils GitHub — dépôts et code, issues, pull requests, organisations, Actions.

Wrappe `oto.tools.github.client.GitHubClient` (REST v3, Bearer). Sept outils, un
par famille de l'API amont.

Quatre pièges de cette API sont traités ici plutôt que laissés à l'agent, parce
qu'aucun ne se manifeste par une erreur :

- ⚠️ **Un 404 sur une ressource privée veut presque toujours dire « le jeton n'a
  pas le droit »**, pas « n'existe pas » : GitHub masque l'existence exprès. Le
  message de refus le dit, sinon on cherche une faute de frappe pendant une heure.
- ⚠️ **Une pull request EST une issue** côté GitHub : `github_issues op='search'`
  écarte donc les PR par défaut, faute de filtre amont. Sans ça, compter les
  tickets d'un dépôt donne un nombre faux, souvent de beaucoup.
- ⚠️ **`per_page` plafonne à 100 et GitHub rabote en silence** au-delà : le
  client refuse localement, en nommant la borne, plutôt que de rendre 100 lignes
  là où l'agent en croyait 500.
- ⚠️ **La recherche s'arrête à 1 000 résultats** quel que soit `total_count` :
  `github_search` remonte un drapeau de troncature plutôt que de laisser lire
  « 12 000 résultats » comme une promesse.

⚠️ **`github_actions op='dispatch'` déclenche une exécution réelle** — donc
potentiellement un déploiement. C'est le seul geste de ce connecteur qui agit
hors de GitHub, et il est en **dry-run par défaut**, comme l'envoi d'email de
`lightfield` et le lancement de campagne d'`origami`.

Les appels au client sont écrits en clair (`_client().list_issues(…)`) : c'est
ce qui les rend vérifiables par la sonde version-skew
(`test_tools_client_methods_exist`).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, egress
from ..connectors import verify as connector_verify


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    """Traduit un refus de GitHub en message actionnable.

    Le cas qui compte est le **404** : sur une ressource privée hors portée du
    jeton, GitHub répond 404 plutôt que 403 pour ne pas divulguer son existence.
    Rendre « introuvable » tel quel enverrait chercher une faute de frappe là où
    il manque un scope.
    """
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    msg = body.get("message") or ""
    if status == 401:
        return ("GitHub a rejeté le jeton (401) — il est invalide, expiré ou "
                "révoqué. Repose-le sur ce connecteur.")
    if status == 403:
        return (f"GitHub a refusé (403) : {msg or 'accès non autorisé'}. Soit il "
                "manque un scope au jeton (jeton classique) ou une permission / "
                "le dépôt dans sa liste (jeton fine-grained), soit c'est une "
                "limite d'usage secondaire — dans ce cas, réessaie plus tard.")
    if status == 404:
        return ("GitHub : introuvable (404). ⚠️ Sur une ressource PRIVÉE, GitHub "
                "répond 404 quand le jeton n'a pas le droit de la voir, exprès, "
                "pour ne pas divulguer son existence. Avant de suspecter le nom, "
                "vérifie que le jeton couvre bien ce dépôt / cette organisation.")
    if status == 405:
        return (f"GitHub : opération impossible en l'état (405) : {msg}. Sur une "
                "fusion, cela veut dire que la PR n'est pas fusionnable — "
                "conflits, ou contrôles de branche en échec.")
    if status == 409:
        return (f"GitHub : conflit (409) : {msg}. La référence a bougé depuis la "
                "lecture — relis l'état frais et réessaie. Sur une écriture de "
                "fichier, c'est le `sha` du blob qui est périmé ou absent.")
    if status == 422:
        return (f"GitHub a refusé la requête (422) : {msg or e.body}. C'est une "
                "validation : champ manquant, valeur hors bornes, ou — sur la "
                "recherche — au-delà des 1 000 résultats accessibles.")
    if status == 429:
        return ("GitHub : trop de requêtes (429) — limite d'usage atteinte. "
                "Réessaie dans un instant.")
    if status in (500, 502, 503, 504):
        return f"GitHub est momentanément indisponible (HTTP {status}) — réessaie plus tard."
    return f"GitHub a refusé la requête (HTTP {status}): {e.body}"


def _check_base_url(base_url) -> None:
    """Garde d'egress sur l'URL d'API Enterprise Server, quand elle est posée.

    Vide = github.com, une constante de la lib : rien à contrôler. Renseignée,
    elle désigne un serveur auto-hébergé — donc, potentiellement, un hôte du
    réseau interne de la plateforme (`oto_mcp/egress.py`)."""
    valeur = (base_url or "").strip()
    if valeur:
        egress.check_url(valeur, connector="github")


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Sonde « tester la connexion » : `GET /user`, puis l'état des quotas.

    `/user` n'exige aucun scope particulier : il sépare « jeton invalide » (401)
    de « jeton valide mais restreint » (403/404 ailleurs). Sonder un dépôt
    confondrait les deux — et pire, un dépôt hors portée répondrait 404, ce qui
    ferait afficher rouge sur un jeton parfaitement sain.

    On ne PEUT PAS vérifier plus : les scopes d'un jeton classique ne sont
    lisibles que dans un en-tête de réponse, et un jeton fine-grained n'expose
    pas sa liste de dépôts. La sonde dit donc « ce jeton est vivant et voici
    qui il est » — et c'est exactement ce qu'elle promet.
    """
    from oto.tools.github.client import GitHubClient
    _check_base_url(fields.get("base_url"))
    client = GitHubClient(token=fields["token"],
                          base_url=(fields.get("base_url") or None))
    who = client.me()
    if not isinstance(who, dict) or not who.get("login"):
        raise ValueError(
            "GitHub a répondu sans identifier le compte — jeton inattendu, ou "
            "URL d'API qui ne pointe pas vers une instance GitHub.")


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.github.client import GitHubClient

    connector_verify.register("github", _verify)

    def _client() -> GitHubClient:
        creds = access.resolve_credential_fields("github")
        _check_base_url(creds.get("base_url"))
        return GitHubClient(token=creds["token"],
                            base_url=(creds.get("base_url") or None))

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    def _need(value, nom: str, op: str):
        if value in (None, "", [], {}):
            raise _bad(f"op='{op}' : `{nom}` requis.")
        return value

    def _repo(owner: Optional[str], repo: Optional[str], op: str):
        _need(owner, "owner", op)
        _need(repo, "repo", op)
        return owner, repo

    def _bad_op(op: str, attendus: str):
        return _bad(f"`op` invalide : {op!r} (attendu : {attendus}).")

    # --- dépôts ---------------------------------------------------------------

    @mcp.tool()
    def github_repos(
        op: Literal["mine", "org", "user", "get", "branches", "commits",
                    "commit", "compare", "tags", "contributors", "languages",
                    "topics", "releases", "release", "latest_release",
                    "create_release", "update_release"] = "mine",
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        org: Optional[str] = None,
        username: Optional[str] = None,
        ref: Optional[str] = None,
        base: Optional[str] = None,
        head: Optional[str] = None,
        path: Optional[str] = None,
        author: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        release_id: Optional[str] = None,
        sort: Optional[str] = None,
        fields: Optional[dict] = None,
        page: Optional[int] = None,
        per_page: int = 30,
    ) -> Any:
        """GitHub — les dépôts : fiche, branches, commits, tags, releases.

        Le point d'entrée pour se repérer dans un dépôt avant d'en lire le code
        (`github_files`) ou les tickets (`github_issues`).

        ⚠️ Un 404 sur un dépôt privé signale presque toujours un jeton sans le
        droit, pas un nom erroné.

        ⚠️ `op='commit'` rend le commit AVEC son diff : GitHub plafonne à 300
        fichiers et tronque au-delà sans le dire dans `files` — comparer à
        `stats` pour s'en apercevoir.

        ⚠️ `op='latest_release'` ignore les brouillons ET les préversions : ce
        n'est pas le dernier tag créé.

        `op`: `mine` | `org` | `user` (listes de dépôts) · `get` · `branches` ·
        `commits` · `commit` (avec diff) · `compare` (base…head) · `tags` ·
        `contributors` · `languages` · `topics` · `releases` · `release` ·
        `latest_release` · `create_release` · `update_release`.

        Args:
            op: l'opération, cf. ci-dessus.
            owner: propriétaire du dépôt.
            repo: nom du dépôt.
            org: op='org' — l'organisation dont on liste les dépôts.
            username: op='user' — le compte dont on liste les dépôts publics.
            ref: op='commit' — branche, tag ou SHA.
            base: op='compare' — la référence de départ.
            head: op='compare' — la référence d'arrivée.
            path: op='commits' — ne garder que les commits touchant ce chemin.
            author: op='commits' — filtre par auteur.
            since: op='commits' — borne basse (ISO 8601).
            until: op='commits' — borne haute (ISO 8601).
            release_id: op='release'/'update_release' — la release visée.
            sort: op='mine'/'org'/'user' — created | updated | pushed | full_name.
            fields: op='create_release'/'update_release' — le corps (tag_name requis).
            page: numéro de page.
            per_page: lignes par page (1-100, défaut 30).
        """
        c = _client()
        if op == "mine":
            return _run(lambda: c.list_my_repos(sort=sort, per_page=per_page,
                                                page=page))
        if op == "org":
            _need(org, "org", op)
            return _run(lambda: c.list_org_repos(org, sort=sort,
                                                 per_page=per_page, page=page))
        if op == "user":
            _need(username, "username", op)
            return _run(lambda: c.list_user_repos(username, sort=sort,
                                                  per_page=per_page, page=page))
        o, r = _repo(owner, repo, op)
        if op == "get":
            return _run(lambda: c.get_repo(o, r))
        if op == "branches":
            return _run(lambda: c.list_branches(o, r, per_page=per_page, page=page))
        if op == "commits":
            return _run(lambda: c.list_commits(o, r, sha=ref, path=path,
                                               author=author, since=since,
                                               until=until, per_page=per_page,
                                               page=page))
        if op == "commit":
            _need(ref, "ref", op)
            return _run(lambda: c.get_commit(o, r, ref))
        if op == "compare":
            _need(base, "base", op)
            _need(head, "head", op)
            return _run(lambda: c.compare_commits(o, r, base, head,
                                                  per_page=per_page, page=page))
        if op == "tags":
            return _run(lambda: c.list_tags(o, r, per_page=per_page, page=page))
        if op == "contributors":
            return _run(lambda: c.list_contributors(o, r, per_page=per_page,
                                                    page=page))
        if op == "languages":
            return _run(lambda: c.list_languages(o, r))
        if op == "topics":
            return _run(lambda: c.list_topics(o, r))
        if op == "releases":
            return _run(lambda: c.list_releases(o, r, per_page=per_page, page=page))
        if op == "release":
            _need(release_id, "release_id", op)
            return _run(lambda: c.get_release(o, r, release_id))
        if op == "latest_release":
            return _run(lambda: c.get_latest_release(o, r))
        if op == "create_release":
            _need(fields, "fields", op)
            return _run(lambda: c.create_release(o, r, fields))
        if op == "update_release":
            _need(release_id, "release_id", op)
            _need(fields, "fields", op)
            return _run(lambda: c.update_release(o, r, release_id, fields))
        raise _bad_op(op, "mine | org | user | get | branches | commits | commit | "
                          "compare | tags | contributors | languages | topics | "
                          "releases | release | latest_release | create_release | "
                          "update_release")

    # --- fichiers -------------------------------------------------------------

    @mcp.tool()
    def github_files(
        op: Literal["read", "list", "readme", "write", "delete"] = "read",
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        path: Optional[str] = None,
        ref: Optional[str] = None,
        content: Optional[str] = None,
        message: Optional[str] = None,
        sha: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> Any:
        """GitHub — lire et écrire des fichiers du dépôt.

        `op='read'` rend le TEXTE décodé ; `op='list'` rend le contenu d'un
        dossier ; `op='readme'` trouve le README quel que soit son nom.

        ⚠️ **Au-delà de 1 Mo, GitHub sert la métadonnée sans le contenu** : la
        lecture le dit alors nommément plutôt que de rendre une chaîne vide qui
        se lirait comme un fichier vide.

        ⚠️ **Écrire par-dessus un fichier existant EXIGE son `sha`** (celui du
        blob, rendu par `op='list'` ou par une lecture). Sans lui, GitHub répond
        409 : c'est son contrôle de concurrence, qui garantit qu'on remplace bien
        la version qu'on a lue et non une modification arrivée entre-temps.
        L'omettre est normal pour une CRÉATION.

        ⚠️ Chaque écriture est un **commit réel** sur la branche visée (`branch`,
        ou la branche par défaut) — visible dans l'historique, et attribué au
        porteur du jeton.

        Args:
            op: read | list | readme | write | delete.
            owner: propriétaire du dépôt.
            repo: nom du dépôt.
            path: chemin du fichier ou du dossier.
            ref: branche, tag ou SHA à lire (défaut : branche par défaut).
            content: op='write' — le contenu texte à écrire.
            message: op='write'/'delete' — le message de commit.
            sha: op='write' (mise à jour) / 'delete' — le sha du blob existant.
            branch: op='write'/'delete' — la branche cible.
        """
        c = _client()
        o, r = _repo(owner, repo, op)
        if op == "read":
            _need(path, "path", op)
            return _run(lambda: {"path": path, "ref": ref,
                                 "text": c.read_text_file(o, r, path, ref)})
        if op == "list":
            _need(path, "path", op)
            return _run(lambda: c.get_content(o, r, path, ref))
        if op == "readme":
            return _run(lambda: c.get_readme(o, r, ref))
        if op == "write":
            _need(path, "path", op)
            _need(message, "message", op)
            if content is None:
                raise _bad("op='write' : `content` requis (le texte à écrire).")
            return _run(lambda: c.create_or_update_file(
                o, r, path, message, content, sha=sha, branch=branch))
        if op == "delete":
            _need(path, "path", op)
            _need(message, "message", op)
            _need(sha, "sha", op)
            return _run(lambda: c.delete_file(o, r, path, message, sha,
                                              branch=branch))
        raise _bad_op(op, "read | list | readme | write | delete")

    # --- issues ---------------------------------------------------------------

    @mcp.tool()
    def github_issues(
        op: Literal["search", "get", "create", "update", "comments", "comment",
                    "update_comment", "delete_comment", "labels", "add_labels",
                    "set_labels", "remove_label", "create_label",
                    "assign", "unassign", "lock", "unlock",
                    "milestones", "create_milestone"] = "search",
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        number: Optional[int] = None,
        comment_id: Optional[str] = None,
        state: Optional[str] = None,
        labels: Optional[list[str]] = None,
        label: Optional[str] = None,
        assignees: Optional[list[str]] = None,
        assignee: Optional[str] = None,
        creator: Optional[str] = None,
        milestone: Optional[str] = None,
        since: Optional[str] = None,
        sort: Optional[str] = None,
        body: Optional[str] = None,
        lock_reason: Optional[str] = None,
        include_pull_requests: bool = False,
        fields: Optional[dict] = None,
        page: Optional[int] = None,
        per_page: int = 30,
    ) -> Any:
        """GitHub — les issues d'un dépôt, leurs commentaires et leurs étiquettes.

        ⚠️ **Chez GitHub, une pull request EST une issue** : l'API rend les deux
        mélangées. `op='search'` écarte donc les PR par défaut — sans quoi
        « combien de tickets ouverts ? » donne un nombre faux, souvent de
        beaucoup. `include_pull_requests=true` rend la réponse brute de l'API.
        Ce tri se fait après pagination : une page de 30 dont 12 sont des PR en
        rend 18, ce qui est normal.

        Réciproquement, et c'est utile : ces mêmes opérations marchent sur une PR
        en passant son numéro — commentaires de fil, étiquettes, assignations et
        jalons sont communs aux deux.

        ⚠️ `op='update'` avec `labels` ou `assignees` **REMPLACE** la liste. Pour
        ajouter sans écraser : `add_labels` / `assign`.

        ⚠️ `op='assign'` **ignore en silence** un compte sans accès en écriture
        au dépôt : la réponse revient en succès sans l'avoir assigné. Comparer la
        liste rendue à celle demandée.

        ⚠️ Créer une issue ou un commentaire **notifie** les abonnés du dépôt et
        toute personne mentionnée. Il n'y a pas de brouillon d'issue chez GitHub.

        Args:
            op: l'opération, cf. ci-dessus.
            owner: propriétaire du dépôt.
            repo: nom du dépôt.
            number: le numéro de l'issue (ou de la PR).
            comment_id: le commentaire visé (update_comment, delete_comment).
            state: op='search' — open | closed | all.
            labels: op='search' (filtre) ou add_labels / set_labels (écriture).
            label: op='remove_label' — l'étiquette à retirer. op='create_label' — son nom.
            assignees: op='assign'/'unassign' — les comptes visés.
            assignee: op='search' — filtre par assigné.
            creator: op='search' — filtre par auteur.
            milestone: op='search' — filtre par jalon.
            since: op='search'/'comments' — modifiés depuis (ISO 8601).
            sort: op='search' — created | updated | comments.
            body: op='comment'/'update_comment' — le texte du commentaire.
            lock_reason: op='lock' — off-topic | too heated | resolved | spam.
            include_pull_requests: op='search' — inclure les PR (défaut false).
            fields: op='create'/'update'/'create_label'/'create_milestone' — le corps.
            page: numéro de page.
            per_page: lignes par page (1-100, défaut 30).
        """
        c = _client()
        o, r = _repo(owner, repo, op)
        if op == "search":
            return _run(lambda: c.list_issues(
                o, r, state=state, labels=labels, assignee=assignee,
                creator=creator, milestone=milestone, since=since, sort=sort,
                include_pull_requests=include_pull_requests,
                per_page=per_page, page=page))
        if op == "get":
            _need(number, "number", op)
            return _run(lambda: c.get_issue(o, r, number))
        if op == "create":
            _need(fields, "fields", op)
            return _run(lambda: c.create_issue(o, r, fields))
        if op == "update":
            _need(number, "number", op)
            _need(fields, "fields", op)
            return _run(lambda: c.update_issue(o, r, number, fields))
        if op == "comments":
            _need(number, "number", op)
            return _run(lambda: c.list_issue_comments(o, r, number, since=since,
                                                      per_page=per_page,
                                                      page=page))
        if op == "comment":
            _need(number, "number", op)
            _need(body, "body", op)
            return _run(lambda: c.create_issue_comment(o, r, number, body))
        if op == "update_comment":
            _need(comment_id, "comment_id", op)
            _need(body, "body", op)
            return _run(lambda: c.update_issue_comment(o, r, comment_id, body))
        if op == "delete_comment":
            _need(comment_id, "comment_id", op)
            return _run(lambda: c.delete_issue_comment(o, r, comment_id))
        if op == "labels":
            return _run(lambda: c.list_labels(o, r, per_page=per_page, page=page))
        if op == "add_labels":
            _need(number, "number", op)
            _need(labels, "labels", op)
            return _run(lambda: c.add_labels(o, r, number, labels))
        if op == "set_labels":
            _need(number, "number", op)
            if labels is None:
                raise _bad("op='set_labels' : `labels` requis — une liste vide "
                           "retire toutes les étiquettes, ce qui est une "
                           "intention, mais elle doit être écrite.")
            return _run(lambda: c.set_labels(o, r, number, labels))
        if op == "remove_label":
            _need(number, "number", op)
            _need(label, "label", op)
            return _run(lambda: c.remove_label(o, r, number, label))
        if op == "create_label":
            _need(fields, "fields", op)
            return _run(lambda: c.create_label(
                o, r, fields.get("name"), fields.get("color"),
                description=fields.get("description")))
        if op == "assign":
            _need(number, "number", op)
            _need(assignees, "assignees", op)
            return _run(lambda: c.add_assignees(o, r, number, assignees))
        if op == "unassign":
            _need(number, "number", op)
            _need(assignees, "assignees", op)
            return _run(lambda: c.remove_assignees(o, r, number, assignees))
        if op == "lock":
            _need(number, "number", op)
            return _run(lambda: c.lock_issue(o, r, number, lock_reason))
        if op == "unlock":
            _need(number, "number", op)
            return _run(lambda: c.unlock_issue(o, r, number))
        if op == "milestones":
            return _run(lambda: c.list_milestones(o, r, state=state,
                                                  per_page=per_page, page=page))
        if op == "create_milestone":
            _need(fields, "fields", op)
            return _run(lambda: c.create_milestone(o, r, fields.get("title"),
                                                   fields))
        raise _bad_op(op, "search | get | create | update | comments | comment | "
                          "update_comment | delete_comment | labels | add_labels | "
                          "set_labels | remove_label | create_label | assign | "
                          "unassign | lock | unlock | milestones | create_milestone")

    # --- pull requests --------------------------------------------------------

    @mcp.tool()
    def github_pulls(
        op: Literal["search", "get", "create", "update", "files", "commits",
                    "merged", "merge", "update_branch",
                    "reviews", "review", "submit_review",
                    "review_comments", "review_comment",
                    "reviewers", "request_review", "remove_reviewers"] = "search",
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        number: Optional[int] = None,
        review_id: Optional[str] = None,
        state: Optional[str] = None,
        base: Optional[str] = None,
        head: Optional[str] = None,
        sort: Optional[str] = None,
        event: Optional[str] = None,
        body: Optional[str] = None,
        merge_method: Optional[str] = None,
        commit_title: Optional[str] = None,
        sha: Optional[str] = None,
        reviewers: Optional[list[str]] = None,
        team_reviewers: Optional[list[str]] = None,
        fields: Optional[dict] = None,
        page: Optional[int] = None,
        per_page: int = 30,
    ) -> Any:
        """GitHub — les pull requests : diff, revues, relecteurs, fusion.

        Les commentaires de FIL, étiquettes et assignations d'une PR passent par
        `github_issues` avec le même numéro — c'est voulu côté GitHub. Ici vit ce
        qui est propre à une PR : le diff, les revues, les commentaires ligne à
        ligne, et la fusion.

        ⚠️ **Une PR fusionnée est `closed`** : il n'existe pas d'état `merged`.
        Pour les distinguer, lire `merged_at` (nul = fermée sans fusion), ou
        `op='merged'`.

        ⚠️ **`mergeable` peut valoir `null`** sur `op='get'` : GitHub le calcule
        en tâche de fond au premier appel. `null` veut dire « pas encore su » —
        redemander, surtout ne pas le lire comme « non fusionnable ».

        ⚠️ **`op='merge'` écrit sur la branche cible et n'est pas annulable d'un
        clic.** Les trois méthodes diffèrent : `merge` ajoute un commit de
        fusion, `squash` écrase la branche en un seul commit, `rebase` réécrit
        les commits. Passer `sha` protège de la course : si la tête a bougé
        depuis la lecture, GitHub refuse au lieu de fusionner autre chose.

        ⚠️ **Une revue sans `event` reste en ATTENTE** (`PENDING`) : rien n'est
        publié, personne n'est notifié, et elle n'est visible que de son auteur.
        C'est utile pour préparer, et c'est un piège quand on croyait approuver.
        `APPROVE` peut débloquer une fusion protégée : c'est un acte de
        gouvernance, pas un commentaire.

        ⚠️ `op='files'` est plafonné à 3 000 fichiers et omet les gros `patch` ;
        `op='commits'` à 250. Une PR massive est rendue incomplète, sans erreur.

        Args:
            op: l'opération, cf. ci-dessus.
            owner: propriétaire du dépôt.
            repo: nom du dépôt.
            number: le numéro de la PR.
            review_id: op='submit_review' — la revue en attente à publier.
            state: op='search' — open | closed | all.
            base: op='search'/'create' — la branche cible.
            head: op='search'/'create' — la branche source.
            sort: op='search' — created | updated | popularity | long-running.
            event: op='review'/'submit_review' — APPROVE | REQUEST_CHANGES | COMMENT.
            body: op='review'/'submit_review' — le texte de la revue.
            merge_method: op='merge' — merge | squash | rebase.
            commit_title: op='merge' — titre du commit de fusion.
            sha: op='merge' — la tête attendue (protection contre la course).
            reviewers: op='request_review'/'remove_reviewers' — des comptes.
            team_reviewers: op='request_review'/'remove_reviewers' — des slugs d'équipe.
            fields: op='create'/'update'/'review_comment' — le corps.
            page: numéro de page.
            per_page: lignes par page (1-100, défaut 30).
        """
        c = _client()
        o, r = _repo(owner, repo, op)
        if op == "search":
            return _run(lambda: c.list_pulls(o, r, state=state, head=head,
                                             base=base, sort=sort,
                                             per_page=per_page, page=page))
        if op == "get":
            _need(number, "number", op)
            return _run(lambda: c.get_pull(o, r, number))
        if op == "create":
            _need(fields, "fields", op)
            return _run(lambda: c.create_pull(o, r, fields))
        if op == "update":
            _need(number, "number", op)
            _need(fields, "fields", op)
            return _run(lambda: c.update_pull(o, r, number, fields))
        if op == "files":
            _need(number, "number", op)
            return _run(lambda: c.list_pull_files(o, r, number,
                                                  per_page=per_page, page=page))
        if op == "commits":
            _need(number, "number", op)
            return _run(lambda: c.list_pull_commits(o, r, number,
                                                    per_page=per_page, page=page))
        if op == "merged":
            _need(number, "number", op)
            return _run(lambda: {"number": number,
                                 "merged": c.check_pull_merged(o, r, number)})
        if op == "merge":
            _need(number, "number", op)
            return _run(lambda: c.merge_pull(o, r, number,
                                             commit_title=commit_title,
                                             commit_message=body, sha=sha,
                                             merge_method=merge_method))
        if op == "update_branch":
            _need(number, "number", op)
            return _run(lambda: c.update_pull_branch(o, r, number, sha))
        if op == "reviews":
            _need(number, "number", op)
            return _run(lambda: c.list_reviews(o, r, number, per_page=per_page,
                                               page=page))
        if op == "review":
            _need(number, "number", op)
            payload = dict(fields or {})
            if body is not None:
                payload["body"] = body
            if event is not None:
                payload["event"] = event
            return _run(lambda: c.create_review(o, r, number, payload))
        if op == "submit_review":
            _need(number, "number", op)
            _need(review_id, "review_id", op)
            _need(event, "event", op)
            return _run(lambda: c.submit_review(o, r, number, review_id, event,
                                                body))
        if op == "review_comments":
            _need(number, "number", op)
            return _run(lambda: c.list_review_comments(o, r, number,
                                                       per_page=per_page,
                                                       page=page))
        if op == "review_comment":
            _need(number, "number", op)
            _need(fields, "fields", op)
            return _run(lambda: c.create_review_comment(o, r, number, fields))
        if op == "reviewers":
            _need(number, "number", op)
            return _run(lambda: c.list_requested_reviewers(o, r, number))
        if op == "request_review":
            _need(number, "number", op)
            return _run(lambda: c.request_reviewers(o, r, number,
                                                    reviewers=reviewers,
                                                    team_reviewers=team_reviewers))
        if op == "remove_reviewers":
            _need(number, "number", op)
            return _run(lambda: c.remove_requested_reviewers(
                o, r, number, reviewers=reviewers,
                team_reviewers=team_reviewers))
        raise _bad_op(op, "search | get | create | update | files | commits | "
                          "merged | merge | update_branch | reviews | review | "
                          "submit_review | review_comments | review_comment | "
                          "reviewers | request_review | remove_reviewers")

    # --- organisations --------------------------------------------------------

    @mcp.tool()
    def github_orgs(
        op: Literal["me", "my_orgs", "rate_limit", "get", "members",
                    "is_member", "membership", "set_membership", "remove_member",
                    "teams", "team", "team_members", "team_repos",
                    "add_team_member", "remove_team_member",
                    "collaborators", "is_collaborator", "permission",
                    "add_collaborator", "remove_collaborator"] = "me",
        org: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        team_slug: Optional[str] = None,
        username: Optional[str] = None,
        role: Optional[str] = None,
        permission: Optional[str] = None,
        filter: Optional[str] = None,
        affiliation: Optional[str] = None,
        page: Optional[int] = None,
        per_page: int = 30,
    ) -> Any:
        """GitHub — qui est dans l'organisation, dans une équipe, sur un dépôt.

        ⚠️ **Trois appartenances se ressemblent et ne sont pas la même chose** :
        membre de l'ORGANISATION, membre d'une ÉQUIPE, collaborateur d'un DÉPÔT.
        Retirer quelqu'un de l'un ne le retire pas des autres — c'est l'erreur la
        plus fréquente ici. `remove_member` sort de l'organisation (et de toutes
        ses équipes) ; `remove_team_member` ne touche que l'équipe ;
        `remove_collaborator` ne touche qu'un dépôt, **et ne retire pas un accès
        hérité d'une équipe**.

        ⚠️ **`members` ne montre que ce que le jeton a le droit de voir** : sans
        le scope d'organisation, seuls les membres PUBLICS sortent — une liste
        plus courte, sans erreur. Ce n'est donc pas un recensement.

        ⚠️ `set_membership` et `add_collaborator` **envoient une invitation** :
        l'accès n'est effectif qu'une fois acceptée (`pending` d'ici là).
        `role='admin'` sur une organisation donne les droits de propriétaire.

        ⚠️ `permission` rend le niveau EFFECTIF (héritages d'équipe compris), ce
        que la liste des collaborateurs directs ne dit pas.

        `op='me'` (le compte du jeton) et `op='rate_limit'` (l'état des quotas,
        sans les consommer) servent à diagnostiquer avant d'accuser un nom.

        Args:
            op: l'opération, cf. ci-dessus.
            org: l'organisation visée.
            owner: propriétaire du dépôt (opérations de collaborateur).
            repo: nom du dépôt (opérations de collaborateur).
            team_slug: le slug de l'équipe (pas son nom affiché).
            username: le compte visé.
            role: admin | member (organisation) ; member | maintainer (équipe).
            permission: pull | triage | push | maintain | admin (collaborateur).
            filter: op='members' — 2fa_disabled | all.
            affiliation: op='collaborators' — outside | direct | all.
            page: numéro de page.
            per_page: lignes par page (1-100, défaut 30).
        """
        c = _client()
        if op == "me":
            return _run(lambda: c.me())
        if op == "my_orgs":
            return _run(lambda: c.list_my_orgs(per_page=per_page, page=page))
        if op == "rate_limit":
            return _run(lambda: c.rate_limit())
        if op in ("collaborators", "is_collaborator", "permission",
                  "add_collaborator", "remove_collaborator"):
            o, r = _repo(owner, repo, op)
            if op == "collaborators":
                return _run(lambda: c.list_collaborators(
                    o, r, affiliation=affiliation, permission=permission,
                    per_page=per_page, page=page))
            _need(username, "username", op)
            if op == "is_collaborator":
                return _run(lambda: {"username": username,
                                     "collaborator": c.check_collaborator(o, r, username)})
            if op == "permission":
                return _run(lambda: c.get_collaborator_permission(o, r, username))
            if op == "add_collaborator":
                return _run(lambda: c.add_collaborator(o, r, username, permission))
            return _run(lambda: c.remove_collaborator(o, r, username))
        _need(org, "org", op)
        if op == "get":
            return _run(lambda: c.get_org(org))
        if op == "members":
            return _run(lambda: c.list_org_members(org, filter=filter, role=role,
                                                   per_page=per_page, page=page))
        if op == "is_member":
            _need(username, "username", op)
            return _run(lambda: {"username": username,
                                 "member": c.check_org_membership(org, username)})
        if op == "membership":
            _need(username, "username", op)
            return _run(lambda: c.get_org_membership(org, username))
        if op == "set_membership":
            _need(username, "username", op)
            return _run(lambda: c.set_org_membership(org, username, role))
        if op == "remove_member":
            _need(username, "username", op)
            return _run(lambda: c.remove_org_member(org, username))
        if op == "teams":
            return _run(lambda: c.list_teams(org, per_page=per_page, page=page))
        if op == "team":
            _need(team_slug, "team_slug", op)
            return _run(lambda: c.get_team(org, team_slug))
        if op == "team_members":
            _need(team_slug, "team_slug", op)
            return _run(lambda: c.list_team_members(org, team_slug, role=role,
                                                    per_page=per_page, page=page))
        if op == "team_repos":
            _need(team_slug, "team_slug", op)
            return _run(lambda: c.list_team_repos(org, team_slug,
                                                  per_page=per_page, page=page))
        if op == "add_team_member":
            _need(team_slug, "team_slug", op)
            _need(username, "username", op)
            return _run(lambda: c.add_team_member(org, team_slug, username, role))
        if op == "remove_team_member":
            _need(team_slug, "team_slug", op)
            _need(username, "username", op)
            return _run(lambda: c.remove_team_member(org, team_slug, username))
        raise _bad_op(op, "me | my_orgs | rate_limit | get | members | is_member | "
                          "membership | set_membership | remove_member | teams | "
                          "team | team_members | team_repos | add_team_member | "
                          "remove_team_member | collaborators | is_collaborator | "
                          "permission | add_collaborator | remove_collaborator")

    # --- Actions --------------------------------------------------------------

    @mcp.tool()
    def github_actions(
        op: Literal["workflows", "workflow", "dispatch", "runs", "run",
                    "cancel", "rerun", "rerun_failed", "delete_run",
                    "jobs", "job", "logs",
                    "artifacts", "artifact", "download",
                    "delete_artifact"] = "runs",
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        workflow: Optional[str] = None,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        ref: Optional[str] = None,
        branch: Optional[str] = None,
        event: Optional[str] = None,
        status: Optional[str] = None,
        actor: Optional[str] = None,
        inputs: Optional[dict] = None,
        dry_run: bool = True,
        page: Optional[int] = None,
        per_page: int = 30,
    ) -> Any:
        """GitHub Actions — workflows, exécutions, jobs, logs et artefacts.

        Lecture d'abord : `runs` puis `jobs` puis `logs` est le chemin normal
        pour comprendre pourquoi un pipeline a échoué.

        ⚠️ Lire `status` ET `conclusion` : une exécution `completed` peut avoir
        échoué, et `conclusion` reste nul tant que le travail n'est pas fini.

        ⚠️ **`op='dispatch'` déclenche une exécution réelle** — donc
        potentiellement un build, une publication ou un **déploiement**. Il est
        en **dry-run par défaut** : `dry_run=false` pour déclencher vraiment. Le
        workflow doit déclarer `workflow_dispatch`, sinon GitHub répond 404 — ici
        un 404 veut dire « pas déclenchable », pas « n'existe pas ». La réponse
        ne rend PAS l'exécution créée : la retrouver en listant les runs juste
        après (il y a un court délai).

        ⚠️ `rerun` relance TOUTE l'exécution (minutes facturées, redéploiement
        possible) ; `rerun_failed` ne rejoue que les jobs en échec — moins cher
        et moins risqué. `cancel` interrompt un travail en cours.

        ⚠️ `logs` et `download` rendent une **URL signée éphémère** (~1 minute),
        à télécharger **sans en-tête d'authentification** — le stockage refuse une
        requête doublement authentifiée. `None` signale des logs expirés ou un
        artefact périmé (90 jours par défaut).

        Args:
            op: l'opération, cf. ci-dessus.
            owner: propriétaire du dépôt.
            repo: nom du dépôt.
            workflow: id numérique ou nom de fichier (ci.yml) — le nom est plus stable.
            run_id: l'exécution visée.
            job_id: le job visé (job, logs).
            artifact_id: l'artefact visé (artifact, download, delete_artifact).
            ref: op='dispatch' — la branche ou le tag sur lequel tourner.
            branch: op='runs' — filtre par branche.
            event: op='runs' — filtre par événement déclencheur.
            status: op='runs' — queued | in_progress | completed | success | failure…
            actor: op='runs' — filtre par déclencheur.
            inputs: op='dispatch' — les entrées du workflow.
            dry_run: op='dispatch' — True (défaut) décrit sans déclencher.
            page: numéro de page.
            per_page: lignes par page (1-100, défaut 30).
        """
        c = _client()
        o, r = _repo(owner, repo, op)
        if op == "workflows":
            return _run(lambda: c.list_workflows(o, r, per_page=per_page, page=page))
        if op == "workflow":
            _need(workflow, "workflow", op)
            return _run(lambda: c.get_workflow(o, r, workflow))
        if op == "dispatch":
            _need(workflow, "workflow", op)
            _need(ref, "ref", op)
            if dry_run:
                return {
                    "dry_run": True,
                    "would": "déclencher une exécution réelle de ce workflow",
                    "repo": f"{o}/{r}", "workflow": workflow, "ref": ref,
                    "inputs": inputs or {},
                    "avertissement": ("une exécution peut construire, publier "
                                      "ou DÉPLOYER, et consomme des minutes "
                                      "facturées"),
                    "pour_declencher": "rappeler avec dry_run=false",
                }
            return _run(lambda: c.dispatch_workflow(o, r, workflow, ref, inputs))
        if op == "runs":
            return _run(lambda: c.list_workflow_runs(
                o, r, workflow=workflow, actor=actor, branch=branch,
                event=event, status=status, per_page=per_page, page=page))
        if op == "run":
            _need(run_id, "run_id", op)
            return _run(lambda: c.get_workflow_run(o, r, run_id))
        if op == "cancel":
            _need(run_id, "run_id", op)
            return _run(lambda: c.cancel_workflow_run(o, r, run_id))
        if op == "rerun":
            _need(run_id, "run_id", op)
            return _run(lambda: c.rerun_workflow_run(o, r, run_id))
        if op == "rerun_failed":
            _need(run_id, "run_id", op)
            return _run(lambda: c.rerun_failed_jobs(o, r, run_id))
        if op == "delete_run":
            _need(run_id, "run_id", op)
            return _run(lambda: c.delete_workflow_run(o, r, run_id))
        if op == "jobs":
            _need(run_id, "run_id", op)
            return _run(lambda: c.list_run_jobs(o, r, run_id, per_page=per_page,
                                                page=page))
        if op == "job":
            _need(job_id, "job_id", op)
            return _run(lambda: c.get_job(o, r, job_id))
        if op == "logs":
            _need(job_id, "job_id", op)
            return _run(lambda: {"job_id": job_id,
                                 "url": c.get_job_logs_url(o, r, job_id),
                                 "note": ("URL signée valable ~1 minute, à "
                                          "télécharger SANS en-tête "
                                          "d'authentification ; null = logs "
                                          "expirés ou absents")})
        if op == "artifacts":
            return _run(lambda: c.list_artifacts(o, r, run_id=run_id,
                                                 per_page=per_page, page=page))
        if op == "artifact":
            _need(artifact_id, "artifact_id", op)
            return _run(lambda: c.get_artifact(o, r, artifact_id))
        if op == "download":
            _need(artifact_id, "artifact_id", op)
            return _run(lambda: {"artifact_id": artifact_id,
                                 "url": c.get_artifact_download_url(o, r, artifact_id),
                                 "note": ("URL signée éphémère, sans en-tête "
                                          "d'authentification ; null = artefact "
                                          "expiré (90 jours par défaut)")})
        if op == "delete_artifact":
            _need(artifact_id, "artifact_id", op)
            return _run(lambda: c.delete_artifact(o, r, artifact_id))
        raise _bad_op(op, "workflows | workflow | dispatch | runs | run | cancel | "
                          "rerun | rerun_failed | delete_run | jobs | job | logs | "
                          "artifacts | artifact | download | delete_artifact")

    # --- recherche ------------------------------------------------------------

    @mcp.tool()
    def github_search(
        op: Literal["repos", "code", "issues", "users", "commits"] = "repos",
        q: Optional[str] = None,
        sort: Optional[str] = None,
        order: Optional[str] = None,
        page: Optional[int] = None,
        per_page: int = 30,
    ) -> Any:
        """GitHub — la recherche : dépôts, code, issues/PR, comptes, commits.

        `q` prend la syntaxe de qualificateurs de GitHub, passée telle quelle :
        `repo:`, `org:`, `language:`, `is:issue` / `is:pr`, `state:`, `in:file`…

        ⚠️ **Plafond de 1 000 résultats, quoi qu'annonce `total_count`.** Ce
        compteur est une estimation du corpus, PAS le nombre de lignes
        récupérables : au-delà, GitHub répond 422. La réponse porte donc un
        `troncature` calculé, qui vaut vrai aussi quand GitHub a **abandonné la
        recherche en cours de route** (`incomplete_results`) — deux causes
        invisibles autrement.

        ⚠️ **La recherche de code a ses propres règles**, et elles expliquent la
        plupart des « pourquoi ne trouve-t-il pas ? » : seule la branche par
        défaut est indexée, les fichiers de plus de 384 Ko ne le sont pas, et il
        faut au moins un terme réel — `repo:x` seul ne suffit pas. Elle ne rend
        pas le contenu du fichier : le lire ensuite avec `github_files op='read'`.

        ⚠️ Limite d'usage propre et basse (~30 requêtes/minute), un ordre de
        grandeur sous le reste de l'API.

        Args:
            op: repos | code | issues | users | commits.
            q: la requête, syntaxe GitHub (requise).
            sort: dépend de op — stars/forks/updated (repos), indexed (code),
                comments/created/updated (issues). Absent = par pertinence.
            order: asc | desc.
            page: numéro de page.
            per_page: lignes par page (1-100, défaut 30).
        """
        c = _client()
        _need(q, "q", op)
        fns = {"repos": c.search_repositories, "code": c.search_code,
               "issues": c.search_issues, "users": c.search_users,
               "commits": c.search_commits}
        fn = fns.get(op)
        if fn is None:
            raise _bad_op(op, "repos | code | issues | users | commits")
        payload = _run(lambda: fn(q, sort=sort, order=order,
                                  per_page=per_page, page=page))
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["troncature"] = {
                "tronque": c.search_is_truncated(payload),
                "pourquoi": ("GitHub ne sert que les 1 000 premiers résultats, "
                             "et abandonne parfois la recherche en route "
                             "(incomplete_results) — total_count n'est donc pas "
                             "un nombre de lignes récupérables"),
            }
        return payload
