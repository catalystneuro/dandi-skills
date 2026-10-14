---
name: dandiset-metadata
description: >
  Populate or improve the metadata of a dandiset on the DANDI Archive: contributors with
  ORCIDs and ROR affiliations, the contact person, funders with award numbers, related
  publications and code, subject terms (brain regions, cell types, disorders, cognitive
  concepts), keywords, license, and ethics approval. Use after uploading data to DANDI,
  when preparing a dandiset for publication, or when asked to review, complete, or fix
  dandiset metadata. Every identifier comes from a live lookup, never from memory.
license: Apache-2.0
compatibility: Needs network access and Python 3.10+ with requests and dandischema (or uv, which installs them).
metadata:
  author: DANDI Archive
  version: "0.1.0"
---

# Dandiset Metadata

This skill fills in the metadata that makes a dandiset citable and findable:
who made it, who paid for it, which paper describes it, what it is about, and
under what terms it can be reused. Most of this can be looked up from public
sources once the associated paper is known, and the rest has to come from the
people who collected the data. The job is to look up what can be looked up, ask
for what cannot, and never fill a gap with a guess.

The helper script prints JSON for every command. In the examples below, `dm`
stands for

```bash
python "${CLAUDE_SKILL_DIR}/scripts/dandiset_metadata.py"
```

`${CLAUDE_SKILL_DIR}` is the directory that holds this file; if your agent does
not substitute it, use that path directly. Shell functions and variables often
do not survive between an agent's tool calls, so write the command out, or
define `dm() { python ".../dandiset_metadata.py" "$@"; }` at the start of each
call (that form works in bash and zsh). The script needs `requests` and
`dandischema`; `uv run` in place of `python` installs them from the script
header. Work in a directory for this task: the script keeps a log of verified
identifiers in `.dandiset-metadata/` there. `dm --help` lists every command.

## Who You Are Working For

When the user owns the dandiset, or is working with the owners (for example
right after converting and uploading their data), every field is open,
including the title and description. Draft them, show them, and write only what
the user approves.

When the dandiset belongs to someone else (a curator's review, a batch audit),
limit changes to facts that come from sources: ORCIDs, affiliations, roles
(adding `dcite:Author` to the paper's authors is fine), funders, related
resources (including code and data links in the paper's data availability
statement), subject terms, keywords, an ethics protocol stated in the paper,
and removing empty `wasGeneratedBy` entries. Leave the title,
description, names, emails, contact person, and license alone, and never remove
or reorder contributors. Put those, and anything the judgment items of the
checklist turn up (a description that names no species, region, or technique; a
name with a typo or a trailing space; asset metadata that looks wrong, which
only re-uploading fixed NWB files can change), in a list of suggestions for the
owner. Hand the owner the diff; they can apply
and commit it in the metadata editor at
`https://medit.dandiarchive.org/?dandiset=NNNNNN`.

## Workflow

### 1. Fetch and Audit

```bash
dm fetch 000123                          # writes 000123.original.json; keep it unmodified as the base
cp 000123.original.json 000123.proposed.json
dm audit 000123.original.json
```

The fetched draft also carries fields the server fills in (`id`, `citation`,
`assetsSummary`, and on some drafts a placeholder `doi`, `publishedBy`, and
`datePublished`); leave them alone. Edits always go to the draft version. If the dandiset is already published,
the published versions stay as they are, and the changes appear in the next
version when the owner publishes again.

The audit runs the rule-based half of DANDI's metadata quality checklist and
reports the species (from the NWB files), DOIs and paper citations in the
text, and the server's validation errors. It checks the description only by
length, so read the title and description yourself against the judgment items.
The checklist:

- Is the title informative, and does the description name the data streams, species, and regions, and summarize the methods?
- Are associated publications in related resources, with DOIs, repository, and the correct relation?
- Are the authors listed as contributors with ORCIDs, and do they have affiliations with ROR identifiers?
- Is there a contact person with an email?
- Are funders listed, one entry per award, with bare award numbers and ROR identifiers?
- Are the relevant brain regions, cell types, diseases, and cognitive concepts in `about`?
- Are there specific keywords, a license, and the ethics protocol?

### 2. Find the Paper

A verified paper supplies authors, ORCIDs, affiliations, and funding in one
lookup, so settle this first. Use a DOI the description already contains, or
ask the user. With only a citation or title, `search-paper` returns
candidates; confirm the match with the user, because one lab often has several
papers with the same authors and topic.

```bash
dm paper 10.1016/j.cell.2021.05.026
dm search-paper "Gonzalez Giocomo 2022 parahippocampal neurons goal-directed navigation"
```

`paper` works for any DOI. For a paper it returns the authors, awards grouped by funder (with the different
spellings of each award number side by side), funders that have no award
number, and a `related_resource` entry ready for `apply`. For a preprint it
also looks for the published version (`published_version`). When there is one,
run `paper` on it too, list the journal article as `dcite:IsDescribedBy`, and
keep the preprint as a second entry. Set the relation deliberately:
`dcite:IsDescribedBy` for the paper that presents this data,
`dcite:IsDocumentedBy` for a data descriptor, `dcite:IsCitedBy` for a paper
that reuses it. For a code or data DOI (Zenodo, figshare, Dryad, OSF) it reads
the DataCite record instead; those uploads are often labeled with the paper's
type, so set `resourceType` and the relation from what the record holds (code
for this dataset is `dcite:Software`, `dcite:IsSupplementedBy`).

```bash
dm fulltext 10.7554/elife.85646
```

`fulltext` reads the open-access text from Europe PMC, or the PubMed Central
page of an author manuscript: the grant list, the funding statement or
acknowledgments, the data availability statement with its code and data links,
and the sentences that state ethics approval (usually with the IACUC or IRB
protocol number). Methods in a supplementary file are not covered; read the
publisher's page or ask the user for the protocol then.

If there is no paper yet (common before publication), go to step 3 and gather
everything from the user.

### 3. Contributors

```bash
dm import-authors 10.7554/elife.85646 --metadata 000123.proposed.json --out 000123.proposed.json
```

This builds one entry per author with ORCIDs and affiliations resolved through
ROR, and merges them with the existing contributors, matched by ORCID, then by
name, then by family name with a compatible given name ("Gonzalez, Alex" and
"Gonzalez, Alexander"). Existing contributors keep their place and new authors
follow the last listed person. In owner mode, `--order paper` puts the authors
first in publication order, which is what the citation should usually show.
It checks every ORCID against the ORCID registry before attaching it; any it
could not corroborate are listed in `orcids_not_attached` for you to raise with
the user. Report who was added and who was matched (confirm every similar-name
match), and resolve anything in `conflicts` by hand.

For people not on the paper, or when there is no paper:

- Write names as `Family, Given`.
- ORCID: `dm orcid-search --family F --given G`, then `dm check-orcid ORCID --name "Family, Given" --metadata 000123.proposed.json`. Attach it only when the verdict is `corroborated`. If the user confirms an `unconfirmed` one, rerun with `--confirmed-by-user`. This also applies to an ORCID you take from `paper` output by hand.
- Affiliation: `dm ror "Department of Neuroscience, Baylor College of Medicine"` and take the match marked `chosen`.
- Ask who the contact person is and for their email. Do not pick one yourself unless there is exactly one person with an email or the user tells you.
- People who converted or curated the data get `dcite:DataCurator` and usually `includeInCitation: false`.

### 4. Funders

Take the awards from `paper`, then read the funding statement from `fulltext`:
it often names foundations and fellowships that OpenAlex lacks, and each of
those needs its own `ror` lookup. When one award appears in several spellings
("1F32MH119766", "F32 MH119766", "MH119766"), use the complete form with the
activity code and serial (Europe PMC's grant list usually has it); the printed
acknowledgments sometimes give only the serial or contain a typo. An award that
appears only in grant metadata and not in the paper, or spellings that
disagree on the digits, go to the user as a question. Add one entry per award with the funder's ROR
(from `paper`'s `funder_ror`, or `dm ror "Office of Naval Research" --country "United States"`).
A funder the paper names without an award number gets one entry with no
`awardNumber`. A named program usually has no ROR record of its own: list it
under the program's name with the ROR of the organization that runs it when
that is clear (the Pew Scholars Program under The Pew Charitable Trusts, the
Simons Collaboration on the Global Brain under the Simons Foundation), and
with `"no_ror": true` otherwise (an endowed chair, the Searle Scholars
Program). Fix existing funder entries with `set_funder` (add a missing
ROR, strip a "Grant" or "Award No." prefix), and turn an entry that lists
several awards into one entry per award with `split_funder`. `apply` refuses
ROR identifiers that ROR marks inactive and names the successor; OpenAlex
still returns some retired ones.

### 5. Related Resources, Subject Terms, Keywords, Ethics

Add links the user or the description provides: the NWB conversion code,
analysis code, an example notebook, a protocols.io page (as `add_protocol`),
and related datasets. The relation reads from the dandiset to the resource;
`references/fields.md` has a table of relations and resource types.

For each brain region, cell type, disease, or cognitive concept the description
or paper supports, look up a term and pick from the results:

```bash
dm ontology "anterior lateral motor cortex" --category anatomy --species mouse
dm ontology "Parkinson's disease" --category disorder
dm ontology "working memory" --category cognitive
```

Use `--species mouse` when the audit says the dataset is mouse-only; mouse
brain regions then come from the Allen Mouse Brain Atlas, which is what
dandisets on the archive already use. When the atlas has no term for a
subregion (nucleus accumbens core), the search offers the parent region
(`parent_of_query`); use it and put the subregion in the keywords. Two to five well-supported terms is
typical. If nothing fits, leave the concept to a keyword. When regions differ
by subject, see `references/asset-metadata.md`.

Add specific keywords (techniques, tasks, phenomena, cell types) and remove
generic ones ("neuroscience", "brain", "data", "NWB"). Record the ethics
approval from `fulltext` or from the user. When the paper names the committee
but no protocol number, record the committee and list the number as an open
item for the owner.

All of these go in through `apply`, which takes a JSON list of operations,
checks each one (enum values, identifier forms, placeholders, duplicates), and
reports what it applied and skipped. It orders the operations itself (people,
then emails, then the rest, then roles), so a contact person and their email can
go in the same list.

```bash
cat > ops.json <<'EOF'
[
 {"op": "add_resource", "doi": "10.7554/elife.85646", "name": "Parahippocampal neurons encode ...",
  "repository": "eLife", "relation": "dcite:IsDescribedBy", "resourceType": "dcite:JournalArticle"},
 {"op": "add_funder", "name": "National Institute of Mental Health", "ror": "https://ror.org/04xeg9z08",
  "award_number": "MH126904"},
 {"op": "add_about", "schemaKey": "Anatomy", "name": "medial entorhinal cortex",
  "identifier": "http://purl.obolibrary.org/obo/UBERON_0007224"},
 {"op": "add_keywords", "keywords": ["goal-directed navigation", "head direction cells"]},
 {"op": "add_ethics", "identifier": "IACUC #27694 (Stanford University School of Medicine)"}
]
EOF
dm apply 000123.proposed.json ops.json
```

`dm apply --help` lists every operation, including `add_person`, `set_orcid`,
`add_affiliation`, `add_role`, `set_email`, `set_funder`, `split_funder`,
`set_license`, and, in owner mode, `set_text` for the title, description and
acknowledgement.

### 6. License, Title, Description, Acknowledgement

In owner mode, ask the user for the license (`spdx:CC-BY-4.0`, the common
choice, or `spdx:CC0-1.0`), and draft a title and description that pass the
checklist. When the paper has acknowledgments, copy them verbatim into the
`acknowledgement`. Keep the drafts as `set_text` operations in their own file,
show them, and apply that file with `dm apply ... --owner` once the user
approves. In a review, leave these as suggestions.

### 7. Validate, Review, Save

```bash
dm validate 000123.proposed.json --base 000123.original.json
dm diff 000123.original.json 000123.proposed.json
```

`validate` reports schema errors the proposal adds or fixes, placeholder
values, removed or reordered contributors, possible duplicate people, and
`unverified_identifiers`: any new DOI, ORCID, ROR, or ontology IRI that did not
pass a lookup in this session. Fix everything it flags before going on. A draft
that was already invalid (no license, no contact person) can stay invalid in
those respects if the user cannot supply the missing information yet; say what
is still missing.

The verified log is a backstop against identifiers written from memory, not
proof that a choice is right. ORCIDs count only after `check-orcid`
corroborates them (`import-authors` runs that check itself), ROR identifiers
only when `ror` marks them `verified`, and DOIs only after `paper`; ontology
results all count, so read their labels before choosing.

Show the user the diff and get an explicit yes. Then, with an owner's API key
in `DANDI_API_KEY`:

```bash
dm save 000123 000123.proposed.json --base 000123.original.json --yes
```

`save` refuses if the live draft has changed since the fetch, and reports the
server's validation errors afterwards. The API key is on dandiarchive.org:
sign in, click your initials in the upper right, and copy the key. Without a
key, give the user the diff and the list of suggestions, and point them to the
metadata editor. Publishing is the owner's step on the dandiset page and is not
part of this skill.

## Rules

These hold in every mode. Most of them were learned by getting them wrong.

- Every DOI, ORCID, ROR identifier, and ontology IRI must come from a lookup in this session. Never write one from memory, even one you are sure of. An identifier the user gives you or that you read on a web page goes through the matching command first (`paper`, `check-orcid`, `ror`, `ontology`), which checks it and records it.
- Never add placeholder values ("N/A", "unknown", "TBD", template text, all-zero ORCIDs). Leave the field empty and tell the user what is missing.
- Link a paper only when the dandiset names it (its DOI, most of its title, or a first-author citation) or the user confirms it. A paper that merely matches the lab, topic, and grants may be a different paper. The published version of a preprint the dandiset names counts as named.
- Attach an ORCID to an existing person only when `check-orcid` corroborates it or the user confirms it. OpenAlex sometimes links an author to a namesake's ORCID.
- Never invent or alter an award number beyond removing a prefix. When sources disagree, use the paper's printed form or ask.
- Never remove contributors, roles, emails, or existing related resources, and never rename a person, without the user asking for it.
- Show the diff and get approval before `save`. Write only to the draft; never publish.

## Reference Files

- `references/fields.md`: the JSON shape of every field, with the relation and ontology tables.
- `references/sources.md`: what OpenAlex, Crossref, Europe PMC, ROR, ORCID, and the ontology services are good for, and where each goes wrong.
- `references/asset-metadata.md`: setting brain regions per NWB file when they differ across subjects.
