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

The helper script prints JSON for every command. Run it as

```bash
DM="python ${CLAUDE_SKILL_DIR}/scripts/dandiset_metadata.py"   # or: uv run ${CLAUDE_SKILL_DIR}/scripts/...
```

where `${CLAUDE_SKILL_DIR}` is the directory that holds this file (use its
path directly if your agent does not substitute the variable). It needs
`requests` and `dandischema`; `uv run` installs them from the script header.
Run it from a working directory for this task: it keeps a log of verified
identifiers in `.dandiset-metadata/` there.

## Who You Are Working For

When the user owns the dandiset, or is working with the owners (for example
right after converting and uploading their data), every field is open,
including the title and description. Draft them, show them, and write only what
the user approves.

When the dandiset belongs to someone else (a curator's review, a batch audit),
limit changes to the structured fields: contributors' ORCIDs, affiliations and
roles, funders, related resources, subject terms, keywords, and a missing
license. Leave the title, description, and contact details alone, never remove
or reorder contributors, and hand the owner a diff to review. Owners can apply
and commit changes themselves in the metadata editor at
`https://medit.dandiarchive.org/?dandiset=NNNNNN`.

## Workflow

### 1. Fetch and Audit

```bash
$DM fetch 000123            # writes 000123.original.json; keep it unmodified as the base
cp 000123.original.json 000123.proposed.json
$DM audit 000123.original.json
```

The audit runs the rule-based half of DANDI's metadata quality checklist and
reports the species (from the NWB files), DOIs and paper links in the text,
and the server's own validation errors. Show the user what fails. The
checklist:

- Is the title informative, and does the description name the data streams and summarize the methods?
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
$DM paper 10.1016/j.cell.2021.05.026
$DM search-paper "Chen Kang Lindsey Druckmann Li 2021 frontal cortical networks"
```

`paper` also returns a ready `relatedResource` entry. Set the relation
deliberately: `dcite:IsDescribedBy` for the paper that presents this data,
`dcite:IsDocumentedBy` for a data descriptor, `dcite:IsCitedBy` for a paper
that reuses it. If there is no paper yet (common before publication), skip to
step 3 and gather everything from the user.

### 3. Contributors

```bash
$DM import-authors 10.1016/j.cell.2021.05.026 --metadata 000123.proposed.json --out 000123.proposed.json
```

This builds one entry per author in publication order, with ORCIDs and
affiliations resolved through ROR, and merges them with the existing
contributors (matched by ORCID, then by name) without duplicating anyone. Read
its report to the user: who was added, who was matched, authors without an
ORCID, and affiliations it could not resolve.

For people not on the paper, or when there is no paper:

- Write names as `Family, Given`.
- ORCID: `orcid-search --family F --given G`, then `check-orcid ORCID --name "Family, Given" --metadata 000123.proposed.json`. Attach it only when the verdict is `corroborated` or the user confirms it.
- Affiliation: `ror "Department of Neuroscience, Baylor College of Medicine"` and take the match marked `chosen`.
- Ask who the contact person is and for their email. Do not pick one yourself unless there is exactly one person with an email or the user tells you.
- People who converted or curated the data get `dcite:DataCurator` and usually `includeInCitation: false`.

### 4. Funders

Take funders and award numbers from the `paper` output, then read the paper's
acknowledgments and ask the user, since OpenAlex funding data is often
incomplete. Add one `Organization` entry with `roleName: ["dcite:Funder"]` per
award, with the funder's ROR (`ror "National Institute of Mental Health"`) and
the award number as the source gives it, minus any "Grant" or "Award No."
prefix. Fix existing funder entries the same way: add a missing ROR, strip a
prefix, and split an entry that lists several awards.

### 5. Related Resources

Besides the paper, add links the user or the description provides: the NWB
conversion code, analysis code, an example notebook, a protocols.io page (in
`protocol`), and related datasets. The relation reads from the dandiset to the
resource. `references/fields.md` has a table of relations and resource types.

### 6. Subject Terms and Keywords

For each brain region, cell type, disease, or cognitive concept the description
or paper supports, look up a term and pick from the results:

```bash
$DM ontology "anterior lateral motor cortex" --category anatomy --species mouse
$DM ontology "Parkinson's disease" --category disorder
$DM ontology "working memory" --category cognitive
```

Use `--species mouse` when the audit says the dataset is mouse-only; mouse
brain regions then come from the Allen Mouse Brain Atlas, which is what
dandisets on the archive already use. Two to five well-supported terms is
typical. When the regions differ by subject, see `references/asset-metadata.md`.

Add specific keywords (techniques, tasks, phenomena, cell types) and remove
generic ones ("neuroscience", "brain", "data", "NWB").

### 7. License, Ethics, Title, Description

Ask the user for the license (`spdx:CC-BY-4.0`, the common choice, or
`spdx:CC0-1.0`) and for the IACUC or IRB protocol number and approving
institution, unless the paper's methods state it. In owner mode, draft a title
and description that pass the checklist and show them for approval.

### 8. Validate, Review, Save

```bash
$DM validate 000123.proposed.json --base 000123.original.json
$DM diff 000123.original.json 000123.proposed.json
```

`validate` reports schema errors the proposal adds or fixes, placeholder
values, removed contributors, and `unverified_identifiers`: any new DOI, ORCID,
ROR, or ontology IRI that no lookup in this session returned. Fix everything it
flags before going on. A draft that was already invalid (no license, no contact
person) can stay invalid in those respects if the user cannot supply the
missing information yet; say what is still missing.

Show the user the diff and get an explicit yes. Then, with an owner's API key
in `DANDI_API_KEY`:

```bash
$DM save 000123 000123.proposed.json --base 000123.original.json --yes
```

`save` refuses if the live draft has changed since the fetch, and reports the
server's validation errors afterwards. The API key is on dandiarchive.org:
sign in, click your initials in the upper right, and copy the key. Without a
key, give the user the diff and point them to the metadata editor. Publishing
is the owner's step on the dandiset page and is not part of this skill.

## Rules

These hold in every mode. Most of them were learned by getting them wrong.

- Every DOI, ORCID, ROR identifier, and ontology IRI must come from a lookup in this session. Never write one from memory, even one you are sure of. An identifier the user gives you or that you read on a web page goes through the matching command first (`paper`, `check-orcid`, `ror`, `ontology`), which checks it and records it; `validate` flags any that did not.
- Never add placeholder values ("N/A", "unknown", "TBD", template text, all-zero ORCIDs). Leave the field empty and tell the user what is missing.
- Link a paper only when the dandiset names it or the user confirms it. A paper that merely matches the lab, topic, and grants may be a different paper.
- Attach an ORCID to an existing person only when `check-orcid` corroborates it or the user confirms it. OpenAlex sometimes links an author to a namesake's ORCID.
- Never invent or alter an award number beyond removing a prefix. If two sources disagree, ask.
- Never remove contributors, roles, emails, or existing related resources without the user asking for it.
- Do not set a contact person or license on the user's behalf in someone else's dandiset; list them as open items for the owner.
- Show the diff and get approval before `save`. Write only to the draft; never publish.

## Reference Files

- `references/fields.md`: the JSON shape of every field, with the relation and ontology tables.
- `references/sources.md`: what OpenAlex, Crossref, ROR, ORCID, and the ontology services are good for, and where each goes wrong.
- `references/asset-metadata.md`: setting brain regions per NWB file when they differ across subjects.
