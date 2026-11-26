---
title: Datastore semantics — layers, faces, business key
description: valeur/comment/link/origine, what a write destroys, what readonly and the business key protect, what diverges between data_* and REST /api/datastore, and what a response does not contain
---

# Datastore semantics

Read this **before** writing to a table you did not fill yourself, or calling
the REST face (`/api/datastore/…`) instead of the `data_*` tools. Both faces talk
to the same storage: this guide says what that storage does with a write, where the two
faces diverge, and what a response does not say.

## 0. Addressing a table: by its NUMBER

A table carries a **number** (`ns_id`, e.g. `174`) and a **name** (`edition-vivier`).
**Only the number addresses it**: `datastore: 174`. A table name is **refused** (since
2026-11-08) — `datastore_by_name`, and nothing is read or written: a name is only unique
per owner, it changes on rename, and the number is what the platform records.

Where to find it: `data_list_datastores` gives it under **both names** (`ns_id`, and `id`
— the same number, kept for the dashboard's links), creation and renaming
do too, and above all **responses
return it** — `ns_id` in the claim (`data_claim_next`), the write (`data_write`),
the release (`data_release`), the reading of a page (`data_rows`) and the reading of the
schema (`data_get_schema`). Claim, note the `ns_id`, address by it afterwards.

⚠️ The `datastore` key of a response is the table's **canonical name**, never the echo of
what you sent: addressing `174` answers `datastore: "edition-vivier"`, not
`"174"`. That is how you read *which* table was touched. (`data_write` on a single
row is the exception and returns only `ns_id`: its body **is** the row, a `datastore`
key there would collide with a column.)

`slot:<name>` is still understood on both sides: it is a project reference, not a
table name, and it resolves to either one.

## 1. Every column has four layers

Closed vocabulary: `valeur` (the column itself) and three layers that describe it —
`comment` (what another source says about this value), `link` (the URL that supports it),
`origine` (where it comes from; or, when the platform sets it, the previous value).
A "flat" column is a column whose layers are empty.

A misspelled layer is **refused by name** on write, nothing is written.
A dict that mixes a known layer and an unknown key is refused likewise — unless the
column is declared `json` in the schema; a dict that carries **no** layer key
is ordinary `json` data, never interpreted.

## 2. Write nested, read back flat

Write — the layer goes **inside** the column:

    {"adresse": {"valeur": "12 rue X", "comment": "registry — 20 B AVENUE Y"}}
    {"adresse": "12 rue X"}                 # the value alone, layers intact
    {"adresse": {"comment": "…"}}           # the comment alone, value intact

Read (`data_rows`, `GET …/rows`, `GET …/rows/{row_id}`) — the **bare name always returns
the value**, and each filled layer arrives **flat**, under a `field.layer` key:

    {"_id": "…", "adresse": "12 rue X", "adresse.comment": "registry — 20 B AVENUE Y"}

There is never an `adresse.valeur`; an empty layer (`null`, `""`) is not served.
These flat keys are addressed like columns in `fields` and `filters`:
`[{"field": "email.origine", "op": "empty"}]` = the values without provenance.

The two forms are **asymmetric** (nested on write, flat on read by
default). The `layers` parameter of `data_rows`, `GET …/rows` and `GET …/rows/{row_id}`
lifts the asymmetry: `flat` (default) = the above; `nested` = `adresse` comes back as
`{"valeur": …, "comment": …}` — `valeur` always, filled layers only,
the form you write in; a cell without layers stays the same scalar, a
list column applies the rule within its items. Any other value is refused,
naming the parameter. In `nested`, `fields` names columns (`adresse.comment`
only exists in `flat`). `versions` applies to both forms: the `origine` layer is
only served when requested (`versions=["current","origine"]`), at any depth — list
items included (`item["email.origine"]` flat, `item["email"]["origine"]` nested).
**The default will switch to `nested`, with dated notice**:
name `layers` right now if you depend on a form.

In both forms the round trip holds: a flat key `adresse.comment` re-emitted on write is
**filed** under `adresse` as soon as the column exists somewhere — in the write, on
the target row or in the schema. If it exists nowhere, the write is refused,
naming the key and the three places looked at — never a literal `adresse.comment`
column created silently.

## 2 bis. A list element is written at its INDEX

To touch ONE element of a list column, you do not have to send the list back: write to
the address you read — the same one as in `filters` and `group_by`.

| what you want | what you write |
|---|---|
| modify an attribute | `{"contacts[1].email": "d@x.fr"}` — or `{"valeur": …, "comment": …}` |
| annotate an attribute without rewriting it | `{"contacts[1].email.comment": "official site"}` |
| clear an attribute | `{"contacts[1].email": null}` |
| add an element at the end of the list | `{"contacts[+]": {"nom": "Cy", "email": "c@x.fr"}}` — a complete record |
| delete an element | `{"contacts[0]": null}` |
| add WITHOUT reading back, one or several elements | `{"tags[+]": ["relance", "chaud"]}` — in order, duplicates kept |
| remove WITHOUT reading back, by value | `{"tags[-]": "chaud"}` or a list — all occurrences |
| add a line to a TEXT column, without reading it back | `{"journal[+]": "06/10 follow-up done"}` — or a list of lines |

The attribute follows the rule of a column (§3): `comment`/`link` drop with a value
that changes, the origin stays, the other attributes and the other elements do not move.
The index starts at 0 and **designates the list as you read it**: in a single call,
`{"contacts[0]": null, "contacts[2].email": …}` targets the third element read; the addition
happens last. Deleting the last element clears the column. Only the element
you write is judged by the schema.

Adding (`[+]`) and removing by value (`[-]`) require no read: they are
resolved under the row's lock, so two simultaneous additions on the same row
BOTH arrive — it is the operation for a log kept by several writers. Order within a
call: indexes, then removals, then additions. Removal only targets a list
of VALUES (a record is removed at its index); an absent value is refused, nothing
is written. To count: `group_by: "tags[]"` (one group per value).

On a `text` column, `[+]` appends at the end of the cell, on a new line, under the
same lock: no need to send back a 25,000-character text to add a
line to it, and two simultaneous additions both arrive. The resulting text respects
`max_length`. But a log kept by several writers lives better in a TABLE, one row per
pass: it can be filtered, counted and read in pieces; the lengthened cell remains a
stopgap.

Refused, with the form that works: an index that does not exist ("`contacts` has 2 items;
index 5 does not exist. To append: `contacts[+]`"), `{"contacts[0]": {…}}` (write its
attributes), `contacts[].email` (read address), `contacts[+].email` (an element
is added whole), `contacts[role=DAF].email` (target the index), and the whole column with
one of its indexes in the same call.

## 3. What a write does — and destroys

A write touches **only what it names**. On an open column there is no
undo: the previous value leaves the row when yours arrives. It only
survives in the revision log — `data_row_history` returns the before and after
of each write, 90 days by default — and nothing puts it back for you.

| you write | effect |
|---|---|
| `{"champ": Y}` or `{"champ": {"valeur": Y}}` | value replaced; `origine` intact; `comment` and `link` **drop** (they described the old value) |
| `{"champ": Y}` with Y identical to the value in place | **no-op**: all layers stay |
| `{"champ": null}` | value cleared; a filled `origine` survives; the clearing comes back in `valeurs_effacees` (field, row, lost value) |
| `{"champ": ""}` or `{"champ": []}` on a value in place | value **replaced**, as by any value; the replaced value comes back in `valeurs_effacees` |
| `{"champ": {}}` on a value in place | **ignored**: the value stays, the `valeurs_ignorees` report says so; if that was all the write set, the call is **refused**, naming `null` |
| `{"champ": {"comment": C}}` | comment set; value and other layers intact |
| `{"champ": {"valeur": Y_identique, "comment": C}}` | comment set, nothing drops |
| `{"champ": {"origine": null}}` | origin cleared; the column becomes flat again |
| field not named | intact |

Not naming a field leaves it intact; naming it with `null` clears it — a `null`
slipped into a half-filled template destroys a value in place.

**Two writes on different columns never overwrite each other**, even started at the
same instant. **Only when what you write was COMPUTED from a row you read**
(a list or a text you send back whole, a status chosen from the status in
place): pass the `_revision` of that read — `data_write(id=…, expected_revision=…)`,
or `PATCH …/rows/{row_id}?expected_revision=…` (as a query parameter, never in the
body). If the row has changed since — any column, or its claim —,
nothing is written and the call is refused with `revision_conflict` and the current revision
(REST: `409`, `details.current_revision`): read again, recompute, rewrite. Otherwise, do not
pass it.

**Deleting and releasing take the same precondition** — `data_delete_row(id=…,
expected_revision=…)`, `DELETE …/rows/{row_id}?expected_revision=…`, and
`POST …/rows/{row_id}/release` — when it was a row you READ that made you decide. A
deletion on a stale state takes away the change someone else had just made; a
release on a stale state removes the lease someone else has taken back since. Same
refusal, same course of action: read again, decide again, replay. Deleting a row claimed by
another job is refused (`row_locked`), like writing to it.

**Several rows to delete = ONE call**: `data_delete_row(ids=["r1", {"id": "r2",
"expected_revision": "3"}, …])` (500 at most). Each row keeps its precondition; a
refused row does not stop the others and comes back in `refused` with its reason (and
`current_revision` on a conflict); a row already absent comes back in `not_found`.
A malformed element refuses the whole call, before any deletion.

## 4. ⚠️ `origine: "system"` is REMOVED

This setting armed an automatic capture: on the first write that changed a
value, the platform froze the previous one as origin. **It no longer exists** (8 September 2026).

What replaces it: **`donnees_d_origine`** (next section), which freezes the original
version **at the moment the value comes in**. A declared operation, instead of a safety net that
depended on the order in which the setting was declared and the data imported — it is
that reversed order that had produced 837 "origine inconnue" cells on a
production table.

⚠️ **What the removal changes for you, and you need to know it**: nothing captures
automatically anymore. A value an agent overwrites is no longer retained by anyone. If you
want to keep what the client handed over, **declare it at import**.

⚠️ **The `origine` layers already in the database do not move** — 28,799 cells carry one
at the time of the removal. They remain read, served and never rewritten. It is the
mechanism that goes, not the data. One single exception: the text "origine inconnue"
that the sweep set IN PLACE of a value has been removed from the cells that carried it.
**A cell without an `origine` layer has no known starting value**: it is
the absence of the layer that says so, never a text in its place.

An `origine` declaration is **refused** since 1 October 2026, like any key its
level does not admit (§ 4 octies); those that remained are removed by the closing
migration.

## 4 bis. `donnees_d_origine: true` — when YOU bring the client's data

The format above is a safety net: it catches the previous value when someone
overwrites. This parameter is the opposite — **an operation, not a safety net**. You declare that this
call brings the data **as the client handed it over**, and each cell freezes its
original version at the moment the value comes in.

```
data_write(datastore="…", key="siren", donnees_d_origine=True,
           rows=[{"siren": "123456789",
                  "raison_sociale": {"valeur": "DUPONT",
                                     "comment": "client's file of 2026-08-05"}}])
```

**Provenance goes in `comment`** — there is no separate parameter for it. The
layers you write land in both versions, because they describe the
same fact on the day of the import.

⚠️ **Reserve it for IMPORT, never for enrichment.** What an agent establishes is the
**current** version. Marking your own finding as origin would present your work
as the client's data — exactly what the definition of origin forbids,
and the kind of error you only discover at delivery, in front of her.

Three rules that spare you precautions:

- an **origin already set is never rewritten**. A re-import of the same file (with
  `key=`, which designates the rows, cf. §6) updates the current version and leaves
  the origin of the first one — you can replay an import without destroying anything;
- an **empty cell receives nothing**. "The client handed over nothing" and "the client
  handed over an empty value" are two different facts; `0` and `false`, on the other hand, are handed-over
  values and keep their origin;
- the **default does not change**. Without this parameter, your write is ordinary and targets the
  current version, as before.

**Why this parameter exists**: before it, having an origin depended on an ORDER OF
OPERATIONS — `origine: "system"` had to be declared **before** the row
existed, otherwise the capture had nothing left to freeze. ⚠️ **This silence cost 837
cells out of 846** on a campaign table: setting declared after the fact, the client's
values already overwritten by agents, and a sweep that could only set an admission
of ignorance, since removed. An explicit operation cannot get the order wrong.

**On a bulk import** (`oto_upload_url` → `PUT /api/upload/{token}`), declare it
**at mint**, with the rest: the signed `PUT` carries no parameter, so whoever
delivers the bytes cannot decide that their file is the client's data.

## 4 ter. `versions` — which versions you want to READ

A cell exists in two versions: `current`, what has been established, and `origine`, what
the client handed over. A write always targets the current one and does not have to say so; a
read, on the other hand, names what it wants.

```
data_rows(datastore="…", versions=["current", "origine"])   # without `versions`: current only
```

⚠️ **Request BOTH in the SAME call when you compare them.** Two calls are
not atomic: a write in between would have you compare the before of one state with
the after of another, and you would announce "fixed" on a row nobody
touched.

⚠️ **The BARE name always carries the current version**, whatever your request.
`versions` only decides what is ADDED alongside (`field.origine` and its
sub-fields) — at any depth: without `origine`, the item of a list column does not
carry `contacts[0]["email.origine"]` either. Making `field` carry two meanings depending on a
parameter would be a trap, not a convenience.

**The response declares what it served**, in `versions_servies`. That is what makes
"I did not ask for it" and "this cell has none" distinguishable — otherwise you
would reinvent a marker, worse, since this time you would have guessed it.

**By default, the current value only**: the origin (the first version of the data)
is requested with `versions`.

## 4 quater. `force` — forcing what you NAME, not the whole call

```
data_write(datastore="…", id="…", force=["raison_sociale", "raison_sociale.origine"],
           row={"raison_sociale": {"valeur": "Dupont SAS"}})
```

Naming it is enough: no need for `readonly_override` on top. A path is a column
or one of its layers.

⚠️ **It changes the SCOPE, not the right.** Forcing remains reserved to the owner of the
table or whoever governs it — shared write access is not enough, otherwise the
lock would protect from nobody. What naming your targets changes is that a batch of
five hundred rows stops forcing everything it carries.

A locked column absent from your list is refused normally, **and the refusal tells
you that it is your list that does not name it** — not that you lack a right. The
distinction matters: without it, you would go looking for a permission you already have.

## 4 quinquies. Two operations on a cell: `null` erases, `@empty` says "searched, nothing"

| what you want | what you write |
|---|---|
| erase the cell | `{"champ": null}` — the lost value comes back once in `valeurs_effacees` |
| say "searched, nothing" | `{"champ": {"valeur": "@empty", "comment": "registry and legal notices: nothing"}}` — it satisfies a required field |
| leave it alone | **omit the field** |

`@empty` also works on the field of a list element (`{"contacts": [{"nom": …,
"fonction": "@empty"}]}`). Set on a layer (`comment`, `link`), it empties only that
layer. An assumed empty reads back as `""` by default, and as `"@empty"` with
`empties=sentinel`: that is the read to do before sending back a list without `of.key`,
which is replaced whole. It is refused on the identity of an element (`of.key`), in a
list of values and in an object.

⚠️ **`@empty` must be the ENTIRE value of the sub-field, alone.** Mixed into a sentence, it
is no longer anything but text, stored as is — `"@empty ; nothing on the legal notices"`
lands in the cell, and a client reads it in her deliverable. The reason goes in
`comment`. The word only bites as a whole word, and that is deliberate: a sentinel
recognised in the middle of a text would empty a value on the strength of a substring.

⚠️ **On a value in place, `null` and `@empty` alike remove it**, and it is kept
nowhere (the response returns it once, in `valeurs_effacees`); only the version
supplied by the client, if it was set at import, remains readable in
`champ.origine` (`versions`, 4 ter). **Omitting the field means "not mine"** (the
value is kept), and a `comment` layer alone does not say "searched, nothing".

⚠️ **`@keep` and `@clear` are not accepted**: a write that carries them, wherever it
may be (value, layer, list element, row of a batch), is refused WHOLE, and the
refusal names the columns and the operation to perform. Instead of `@clear`, write `null`.
Instead of `@keep`, omit the sub-field — or, for a `comment` or a `link` that
must survive a value that changes, send it back as is: writing a value drops
the `comment` and the `link` that accompanied it.

⚠️ **`""` and `[]` are values: they REPLACE the value in place**, and the replaced
value comes back in `valeurs_effacees`. A `""` that is set does not satisfy `required` and
counts as empty: to say "searched, nothing", use `@empty`. To keep a
value, omit the column; to erase it, write `null`. `{}` is not a value and is never stored.

`null` also erases a cell holding an assumed empty (`@empty`), marker included: it is the
operation that replaces `@clear` to unmark it.

## 4 sexies. The `lifecycle` DESIGNATES the state column

```json
{"key": "statut", "type": "enum", "lifecycle": {"states": [...], "terminal": [...]}}
```

**Nothing else to declare.** The state column is the one that carries the block — no
`role: "status"` label, no schema key to match.

**Transitions are always written as a LIST**, even for a single destination:
`"transitions": {"a_qualifier": ["a_contacter", "perdu"], "a_contacter": ["gagne"]}`.
`{"a_contacter": "gagne"}` (a string) is **refused when the schema is set and when it is patched**, and the
refusal gives the exact form (oto#63): every screen and every validator reads a list,
none guesses. Same rule for `terminal`: `"terminal": ["gagne"]`, never
`"terminal": "gagne"`. A block stored in the old form is refused on use
(state change, claim), with the patch that repairs it.

⚠️ **What this removes**: it is now IMPOSSIBLE to set a lifecycle that does not
apply. Before, a `lifecycle` on an unlabelled column was stored,
served… and never read — five tables were in that situation, four of them in production, and
their authors believed they had armed a work queue.

**The displayed name of a stage** is declared in the same block:
`"labels": {"a_qualifier": "À qualifier", "perdu": "Perdu"}`. This is
presentation: a row is always written with the CODE (`"statut": "a_qualifier"`),
never with the label. Each key must be a state of `states` (an unknown key is
refused, and named), each value a non-empty string of at most 60 characters; a
state without a label is allowed. To name a stage on an existing table:
`data_patch_schema(fields=[{"key": "statut", "lifecycle": {"labels": {"perdu":
"Perdu"}}}])` — the other labels and the rest of the lifecycle do not move.

Several columns may carry a block — a queue of agents and states tracked by
a human, for example. What is **refused when the schema is set** is two QUEUES: two
blocks that declare `claimable`, `max_claims`, `abandon_state` or `advance`. Otherwise the first
one found would win, and the order of declaration would decide silently. The queue is the
column whose block declares them (failing that, the first one that carries a block): it is the one
that `data_claim_next` claims from.

**A campaign in several passes: `advance`** (oto#95). The passes are states
(`a_traiter → societe → dirigeant → email`), and `"advance": {"societe": "dirigeant",
"dirigeant": "email"}` declares their sequence. Each pass claims by ITS state
(`data_claim_next(filter={"statut": "societe"})`), writes its work and releases
(`data_release`, or `run_finish`): if the row has been written since its claim, the
platform moves it to the next state — a revision in the journal, source `system`,
attached to the run of the pass. You do not write the state; if you write it anyway, your
write prevails and nothing more moves. Without a write, nothing advances (the cap on
retries applies). A lease that expires does not advance: the pass is redone. Each step
must be a declared transition (refused when the schema is set otherwise), never from a
terminal state; going backwards remains a transition that you write.

⚠️ **And if no column carries one, the table has NO lifecycle** — but it
always has a queue: `data_claim_next` claims without declaring anything. What is missing
is the guards (transitions, cap on retries, abandon state, claim
scope). A column with its `options`, or the old label, looks like a
state without being one — the response tells you so rather than letting you draw conclusions from its
silence.

## 4 sexies bis. A column is DECLARED before it is written

**A write to a column that the schema does not declare is REFUSED**
(`unknown_column`), on ALL tables — tables without a schema included (oto#124). Nothing is written; a batch or an
import is refused WHOLE, before its first row. To write to a new
column, declare it first, then write:

```
data_patch_schema(datastore=…, fields=[{"key": "score", "type": "number"}])
```

(REST: `PATCH /api/datastores/{datastore}/schema`, same body.) `fields` merges by
key: the column is added, the others do not move. The columns that a table
already carried when the rule came in were declared for it. A patch that touches a declared column of a row
carrying an old undeclared column goes through; writing `null` to the latter erases it.

**A new table is born without columns**: its first write is therefore refused
as long as nothing is declared. For a table you are going to fill, the
normal operation is to create it WITH its schema, in a single call:

```
data_create_datastore(datastore="prospects", schema={"fields": [
    {"key": "siren", "type": "text"}, {"key": "nom", "type": "text"}], "key": "siren"})
```

(REST: `POST /api/datastores`, body `{"datastore": …, "schema": {…}}`.) `schema` is
the SAME object as `data_set_schema` — same keys, same closed vocabulary, same
validation, business key and top-level setting included. The table is born with it; if the schema
is refused, the table is NOT created (nothing to clean up, the same name stays free).

`oto_import` itself declares the new columns of a file (CSV headers, NDJSON keys).

## 4 sexies ter. The declared format is a contract — no more settings

**The declared format is a contract on ALL tables with a schema** (oto#124: always
refuse, no more settings):

- the `options` of a column are applied, whatever its type;
- the SHAPE of a `text`, `url`, `object`, `list` and `enum` value is checked
  (a URL starts with `http(s)://`, an object is an object, a list a list);
- an unknown layer of a value is refused (`{"valeur": …, "commentaire": …}`);
- a declared sub-record (`object.fields`, `list.of.fields`) refuses an attribute it
  does not declare;
- when the schema is set, the column of a claim scope (`lifecycle.claimable`) must
  be declared.

A fault is refused (`row_invalid`), nothing is written — except a value outside
`options` that is alone in its fault: it is DISCARDED and the rest of the row is written
(`valeurs_ecartees`).

If the value is right, extend the format:

```
data_patch_schema(datastore=…, fields=[{"key": "statut", "options": ["a", "b", "c"]}])
```

**What is judged is what the operation WRITES, never what the row already carries.** A row in which
a value in place is out of format is written on its OTHER columns; the fault in place is
stated (`hors_type`), it refuses nothing. Existing
rows are not corrected by the platform.

**Only one top-level setting remains: `new_rows`** — does a new row have the right
to be born? `"create"` (default) or `"reject"` (a write that designates no
existing row is refused — requires `key`). `data_get_schema` serves it as it
applies (`reglages`).

`unknown_columns` is **retired** (2026-10-05): setting or patching it is refused,
with this reason — no more settings, columns and values are always
checked. `strict`, `unknown_fields` and
`key_required` have been refused since 2026-10-02 (`key_required: true` →
`new_rows: "reject"`).

## 4 septies. A declared requirement applies, at any depth

A table's validation is armed as soon as its schema declares a requirement:
`required`, `required_when`, `max_length`, `max_items` on a column, an
object sub-field or the attribute of a list element — and `options` in a
sub-field. "Each contact carries a name" is declared on `of.fields`, and it is
enforced. The rest of the format (the `options` of a top-level column, the shape of
values) is a contract everywhere (§4 sexies ter).

Two forms that would not apply are **refused when the schema is set**, with the right one:
`max_items` is set on the list (`{"type": "list", "max_items": 3, "of": {…}}`),
never in `of`; sub-fields (`fields`, `of`) require the `type` of their column
(`object` or `list`).

In a list with `of.key`, only the elements that your write **changes** are judged —
and, in any list, only those you write by their rank (§2 bis).
An element sent back as is, which did not meet a requirement set after it, does not
block your write: it is flagged in `hors_type`, to be corrected when you come
back to it.

## 4 octies. The vocabulary of a schema is CLOSED

Since 2026-10-01, each level of a schema declares the keys it admits — the
**top level**, a **column**, a **sub-field** (`fields` of an object or of an element), the **element
of a list** (`of`) and the **`lifecycle`** block — and a key absent from its level is
**refused**, when the schema is set (`data_set_schema`, `PUT …/schema`) as well as when it is patched
(`data_patch_schema`, `PATCH …/schema`). The lists, and who reads each key, are served
on `GET /api/datastore/schema/keys` (`levels`).

The refusal names the path, the key, and where it goes:

```
fields.statut: `enum` is not allowed on a column — did you mean `options`?
fields.etat: `states` is not allowed on a column — it goes INSIDE the column's `lifecycle` block (`lifecycle.states`) …
top level: `semantic_search` is not allowed at the top level of the schema — it is a PARAMETER of the call …
```

- **a help text** goes in `description` — the only one: `note`, `help`, `hint` and
  `placeholder` have been folded into it and are refused;
- **an annotation of your own** (a dependency, a value label, a provenance of the
  format) goes in `meta`: an object, admitted at every level, carried as is,
  **never read**, bounded in size (`meta_max_bytes`). Nothing in `meta`
  becomes active — a `readonly` stored there locks nothing;
- **a key already stored** and unchanged blocks nothing: the refusal applies to what the
  operation SETS or MODIFIES. The patch of another column goes through; the response and
  `data_get_schema` name it in `warning`. To remove it:
  `data_patch_schema(remove_attrs={"column": ["key"]})`.

## 5. What `readonly: true` protects — and does not protect

A `readonly` column (schema) locks a **set value**: a write that **changes** it (bare
value, `null`, or `{"valeur": …}`) is refused, naming the column and where the thing
goes instead — `champ.comment`, which stays open. An identical value passes (no-op);
`comment`, `link` and `origine` (unless the system sets it) remain writable.

`readonly` means **"no longer changes once set"**, not "can no longer be written
after creation":

- a cell **without a value** — absent key, `null` or `""` — **gets filled**, once: a
  column locked after its import is not empty forever;
- **`@empty` is a set value** ("searched, nothing"): replacing it is refused;
- **erasing** a set value (`null`) is a change: refused.

What this notch does **not** close: the **creation** of a row — nothing is overwritten; a
table that must not grow is closed with `new_rows: "reject"`. The key column cannot be
`readonly`: it is `new_rows: "reject"` that protects it. A **computed** column (§9), for
its part, stays closed even when empty.

To replace anyway: `readonly_override=true` **on the call** (argument of
`data_write`; query parameter on `POST`/`PATCH …/rows`). Reserved for the owner
of the table or whoever governs it — a table only shared for writing is refused —
and every forced replacement is logged (row, column, replaced value).

## 6. What the business key does on write

When the schema declares a `key` (`data_set_schema`), a write **without `id`** that
carries a key value **already present** does not create a duplicate: a unique index
guarantees it. What it does to that row depends on what **your call says** (oto#141):

- **You DESIGNATE** — by `id=`, or by naming the key with **`key=`** (a batch, or a single
  row with `key=` = the declared key; REST `?key=`; the minting of an upload): a
  value already present **modifies** its row (its `_id` is returned), a new value
  creates it. No need for `upsert`.
- **You ADD** — neither `id` nor `key=`: a value already present is a duplicate you
  did not see.

| | what happens |
|---|---|
| add, key already present, without `upsert` | **refused** `business_key_exists`: the refusal names the row in place and the operations |
| add, key already present, `upsert=true` | **merges** (its `_id` is returned) |
| two rows of the same call with the same key, without `upsert` (designation as well as add) | the batch is **refused WHOLE** before its first row, ranks named ("Rows 1 and 2: same siren '111'.") |

You do not designate the same row twice in one call: remove the duplicate, or pass
`upsert=true` if it must merge. `upsert` applies on every face — single row,
batch, REST, `oto_upload_url` (declared at minting, sealed in the token, like `key`) and
`oto_import` —, and it is refused where it would merge nothing: with `id=`, or on a
table without a key (and, for a batch, without `key=`).

A batch returns each merge in **`fusions`**: `{rang, dans_rang, id, cle}` — `rang` the
rank of the row in the batch (starting at 1), `dans_rang` the rank of the row of the **same**
batch that set the targeted row, `null` for a row already in the database, `cle`
`{column: value}`. **`ids` stays aligned rank for rank**: two entries can
designate the same row, and `fusions` says which. The receipt of a signed upload, read by
a link holder, returns `fusions` without `id`.

On a **closed** table (`new_rows: "reject"`, below), every write without `id` is a
designation by the key: it never creates, the key value is the way to target
its row.

A row created without a key value is created anyway and the response flags it
(`notices`): no later write will find it by its key.

`new_rows: "reject"` closes the table (it requires `key`): a write that designates no
existing row — neither `id`, nor a key value already carried; a merely **new** key
counts as unknown — is **refused** (`business_key_required`) instead of creating.
The refusal says whether the write CARRIED the key ("carries `siren` = …": the value is
unknown) or not ("does not carry `siren`"); REST: `details.cle_portee`, `details.valeur`,
`details.a_renvoyer`. A batch deduplicated by `key=` on another column is judged on
the DECLARED key.
Open, write, close again: `data_patch_schema(new_rows="create")`, the write, then
`data_patch_schema(new_rows="reject")`.

A **batch** (`data_write(rows=[…])`, `oto_upload_url`) is not atomic: it stops at
the first refused row, the previous ones stay written, the refusal names the row and
says how many landed. Two refusals are exceptions, judged on the WHOLE batch before its
first row: a refused word (`@keep`, `@clear`) and, without `upsert`, a key that is
duplicated or already carried by an add (above). `key=` on the batch designates by a column
other than the declared key if needed; on a single row, only the declared key
applies.

## 7. Two faces, one storage

| operation | MCP (`data_*`) | REST (`/api/datastores` = `DS`) |
|---|---|---|
| tables | `data_list_datastores`, `data_create_datastore`, `data_rename_datastore`, `data_delete_datastore`, `data_url` | `GET`/`POST DS` ; `PATCH`/`DELETE DS/{tableau}` ; `GET DS/{tableau}/url` |
| rows | `data_rows` (page, or `id`), `data_write`, `data_delete_row` | `GET`/`POST DS/{tableau}/rows` ; `GET`/`PATCH`/`DELETE …/rows/{row_id}` |
| schema | `data_get_schema`, `data_set_schema`, `data_patch_schema`, `data_drop_column` | `GET`/`PUT`/`PATCH …/schema` ; `POST …/drop_column` |
| work queue | `data_claim_next`, `data_release` | `POST …/claim_next` ; `POST …/rows/{row_id}/claim` ; `POST …/rows/{row_id}/release` ; `GET …/queue` |
| aggregate | `data_aggregate` | `GET …/aggregate` |
| sharing | `data_share` | `GET`/`POST`/`DELETE …/share` |
| history of a row | `data_row_history` | `GET …/rows/{row_id}/history` |
| activity | — | `GET …/activity` ; `GET …/rows/{row_id}/activity` |

`{tableau}` is the **number** of the table (a name is refused, `400 datastore_by_name` —
see §0); `slot:<nom>` is understood on both sides. The
complete description (inputs, responses, codes) is `GET /api/openapi.json`, without auth;
the REST face is called with the same token as `/mcp`, or an API token.

**Identical** on both faces: the storage, the layers on read (`layers`),
the business key, `readonly`, the schema refusals — a row created on one side reads on
the other, identically.

**Diverges**:

- **Batch.** `POST …/rows` writes **one** row: the body **is** the row. A body with a
  single key whose value is a list of objects (`{"data": [...]}`), or whose
  `rows` key carries one whatever the others (`{"rows": [...], "key":
  "siren"}`), is refused `400 batch_body`, nothing is written — unless a column with
  that name is declared in the schema. A body that is a JSON list is refused `400
  invalid_body`. The REST batch is `POST …/rows/batch`, body `{"rows": [...], "key":
  "siren", "donnees_d_origine": true}` (`key`, `upsert` and
  `donnees_d_origine` optional): the same operation as `data_write(rows=[…])` — same
  engine, same refusals naming the row, same notices, same response (`inserted`,
  `updated`, `count`, `ids`, `fusions`). `POST …/rows` takes `?key=` and `?upsert=true` as query
  (its body is the row). For volume, `oto_upload_url`.
- **Projection.** `fields` only exists on `data_rows`; REST returns the whole row.
- **Unknown parameters.** REST refuses any query or path parameter it does not
  know — 400 `unknown_fields`, which names the field and the expected ones. The body of
  `POST`/`PATCH …/rows` is free: those are the columns.
- **Pagination.** `data_rows`: `limit` (100) + `cursor` → `{rows, count, next_cursor}`.
  REST: `offset` + `limit` (50, max 500) → `{rows, total, offset, limit}`.
- **REST filters**: `filter`, `filters`, `metrics` are JSON **inside a query
  string** (`?filters=[{"field":…}]`), sent only once.
- **`group_by`.** A string `"a,b"` is refused on both faces: cross-grouping
  does not exist. The **list** form `["a", "b"]` only exists on `data_aggregate`, and
  it **merges** the values of the fields under the same key, it does not cross;
  REST takes one column.
- **`key=`**: on a batch, MCP and REST (`…/rows/batch`) take any
  column; on a single row (`data_write(row=)`, `POST …/rows?key=`), only the declared
  key.
- **Schema refusals: the payload to send back.** A validation refusal (missing required,
  type or format, unknown sub-field, required layer) carries the fragment of `row` to
  send back: only the fields to fix, a `<…>` template in place of each value
  (`"<text>"`, `"<number>"`, `"<a | b>"`), `| @empty` where that operation is allowed, and in
  a list the faulty element ALONE, designated by its `of.key` (otherwise its rank, in
  `a_renvoyer_elements`). MCP: at the end of the message. REST: `details.a_renvoyer`. Replace
  the templates and write again; a list is sent back whole, that element fixed in its place
  — or write only its attributes, at its rank (§2 bis).
- **Refusals.** MCP: `INVALID_PARAMS` error carrying the message. REST: named 400
  (`row_invalid`, `business_key_required`, `invalid_row_input`, `jeton_mal_place`,
  `invalid_filters`…), 403 `datastore_read_only` (table shared read-only),
  404 `datastore_not_found` (with the org where it lives, if it exists in another of
  yours) or `row_not_found`; 409 `row_locked` (row claimed by someone else),
  `revision_conflict` (the row has changed since the `_revision` passed) or
  `business_key_exists` (add on a key already carried, or duplicate within the call, without
  `upsert`, §6 — `details`: `id` of the
  row in place, or `doublons` and `existantes` of a batch).

## 8. What a response does not contain

A success is not an acknowledgement of what you believe you did; read what is missing.

- **The REST list cannot say "there are more".** `{rows, total, offset, limit}`
  without a cursor: the end is computed (`offset + len(rows) >= total`). An `offset` beyond
  the total returns `rows: []` with 200 — the same response as an empty table; a row
  deleted between two pages shifts the following ones without a word. On `data_rows`,
  `next_cursor: null` is the only end signal, and a stale cursor is refused.
- **`data_rows(fields=[…])` does not say a column is empty.** A column
  declared in the schema but filled nowhere returns `{_id}` rows **without a
  warning** — this is intended, so as not to flag a spelling mistake that is not
  one; the `warning` only comes for a name unknown everywhere.
- **A response does not name the table it did not resolve.** `ns_id` is present
  as soon as the table was reached; an `ns_id: null` signals a path that returned
  without resolving, not a table without a number.
- **A write `200`/`201` only carries what deviated.**
  `hors_type`, `valeurs_ecartees`, `valeurs_effacees`, `valeurs_ignorees`, `notices`
  are absent when
  everything is in format: their absence is the normal response, their presence is what
  you must read.
- **An aggregate on an absent field returns a group with key `null`**, not an error: a
  single group containing everything is the sign of a wrong column name, not of empty
  data.

## 9. A `type: "formula"` column is computed, never written

An OpenFormula formula set in the schema (`formula: "IFS(…)"`) computes the column from
the other columns **of the same row** — never another row, never another
computed column. Closed subset: `IFS`, `IF`, `SWITCH`, `AND`, `OR`, `NOT`,
`LEFT`, `MID`, `LEN`, `COUNTA`, `TRUE`, `FALSE`, comparisons; anything else, an unknown
column or a chain is refused **when the schema is set**, naming it. Complete grammar:
docstring of `oto_mcp/datastore/formule.py`.

- **Not writable.** A write is refused ("COMPUTED column") and `readonly_override`
  changes nothing: the value would be recomputed. Write the **input** columns.
  `champ.comment` carries the provenance (the winning `IFS` branch); `origine` is
  never touched.
- **A list is read as a range**: `contacts[].telephone` (with `[]` — a bare dot designates
  a layer) gives the sub-field of each element, and is only consumed inside
  `COUNTA(…)`.
- ⚠️ **`contacts<>""` is not an empty-list test.** Read as a scalar, a
  list is worth its text (`[]` → `"[]"`): `contacts<>""` is true even for `[]`. "At
  least one value" is written `COUNTA(contacts[].telephone)>0`.
- **The assumed empty `@empty` is an empty**, for comparisons as for `COUNTA`.
- **Recomputation**: on every write of the row; in bulk, as a background task, when the
  formula is set or its **text** changes (`data_set_schema`, `data_patch_schema`
  return `formules_recalcul_en_cours: true`; `get_schema` says `formules_a_recalculer`
  as long as some remain). Setting the same text again recomputes nothing; modifying it, even by one
  space, restarts the recomputation of every row.
- **Sorting, filtering, aggregation** read the **stored** value, as for any column. The
  column has no result type: sorting is **textual** — correct for a rank
  "1"…"5" or a name, wrong for multi-digit numbers (`"10"` before
  `"9"`, oto-backend#1034).

## 10. Credit the tool that FOUND a value: `via`

Each cell remembers which tool found the value it holds (`GET …/column-sources`), and the table header shows it ("Filled by Hunter 62% · other 38%"). Jev, the waterfalls and recipes are credited on their own. When YOU looked a value up with another tool and then write it yourself, say so:

```
apollo_match_person(...)            # found the email
data_write(datastore=…, id=…, via="apollo_match_person", row={"email": "…"})
```

- `via` counts only for a tool that SUCCEEDED in the same run (`run_start`, then `_run_id=` on every call). Otherwise the write still happens, unattributed, and the reply says why (`via_dropped`). It never blocks a write.
- Never a platform tool (`data_*`, `oto_*`, `run_*`). Omit it when you established the value yourself.
- Without `via`, the cell counts as "other".
