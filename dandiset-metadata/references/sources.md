# Sources and Their Pitfalls

Every identifier in the metadata comes from one of these services during the
session. The script's commands wrap them; this file explains what each one is
good for and where it goes wrong, learned from proposing metadata for a few
hundred dandisets.

## OpenAlex (`paper`, `import-authors`, `search-paper`)

One lookup of a paper's DOI gives the author list in order, ORCIDs, raw
affiliation strings, institutions with ROR identifiers, corresponding authors,
funders with ROR identifiers, and award numbers. That is why finding the paper
comes first.

- OpenAlex assigns ORCIDs to authors by its own disambiguation, and it
  sometimes attaches the ORCID of a different researcher with the same name.
  Run `check-orcid` before attaching an ORCID to a person who is already a
  contributor. For a fresh import from the paper, an ORCID that the record
  itself lists under that paper is safe; `check-orcid` with `--paper` confirms
  this.
- OpenAlex's institution list for an author can include historical or
  secondary affiliations that are not on the paper. `import-authors` resolves
  each raw affiliation string from the paper through ROR first and accepts an
  OpenAlex institution only when its name appears in that string. Anything it
  cannot confirm is kept as a plain name and reported as unresolved.
- Funding in OpenAlex is often incomplete or empty. Read the paper's
  acknowledgments (the publisher page, PubMed Central, or the bioRxiv full
  text) and ask the user before concluding that the list is complete.
- bioRxiv and medRxiv DOIs carry no version suffix in OpenAlex
  (`10.1101/2023.01.01.522000`, not `...522000v2`).
- Set `OPENALEX_MAILTO` to an email address for the faster polite pool.

## Crossref (`paper`, `search-paper`)

Confirms that a DOI exists and gives its title, journal, type (use
`posted-content` to recognize a preprint), and sometimes funders with award
numbers that OpenAlex lacks. `search-paper` takes a free-text citation.

A search result is a candidate, not a match. The same lab often has several
papers with the same authors, grants, and topic, and linking the wrong one is
worse than linking none. Link a paper only when the dandiset's own text names
it (its DOI, most of its title, or a first-author citation such as "Chen et al.
2021"), or when the user confirms it.

## ROR (`ror`)

The affiliation matcher returns candidates and marks at most one as `chosen`
when it is confident. Use the chosen one. When none is chosen, look at name,
country and type before picking, and ask the user if it is still unclear: many
countries have a "National Institutes of Health", and universities have
separate records for their medical schools and hospitals.

For funders, search the specific agency ("National Institute of Mental
Health"), not the umbrella ("NIH"), when the source names it.

## ORCID Registry (`orcid-search`, `check-orcid`)

`orcid-search` finds candidates by name. Several people with the same name is
the normal case, and a name match alone is never enough. `check-orcid` reads
the ORCID record and reports `corroborated` only when the record carries a
matching name and also does one of the following: lists one of the dandiset's
papers, lists an institution that one of the contributors is affiliated with,
claims works co-authored with another contributor, or has an OpenAlex profile
of at least five works at one of those institutions. Anything else comes back
`unconfirmed`; ask the user or leave the ORCID out.

When the user gives an ORCID directly, still run `check-orcid` on it, to catch
a typo and to record it as verified.

## Ontologies (`ontology`)

- `anatomy` searches UBERON and CL through the EBI Ontology Lookup Service,
  and, with `--species mouse`, the Allen Mouse Brain Atlas first. Dandisets on
  the archive already use Allen atlas terms for mouse brain regions, in the
  BICAN form `https://purl.brain-bican.org/ontology/mbao/MBA_<id>`, so use them
  for mouse data. The atlas names regions differently from papers ("Secondary
  motor area" for ALM or M2, "Primary visual area" for V1); the search knows
  the common aliases.
- OLS often ranks a sub-region or a layer above the region itself (searching
  "primary motor cortex" can return "primary motor cortex layer 6" first).
  Results with an exact label match are marked `exact` and listed first; read
  the labels before choosing.
- `disorder` searches DOID, HP and NCIT. `cognitive` searches a bundled
  snapshot of the Cognitive Atlas concepts.
- If nothing fits, say so. Do not pick the closest wrong term.

## DANDI API (`fetch`, `save`)

`fetch` reads the draft version's metadata from
`/api/dandisets/{id}/versions/draft/info/` along with the server's own
validation errors. `save` writes with `PUT /api/dandisets/{id}/versions/draft/`
using the caller's API key, which must belong to an owner of the dandiset. It
first confirms the live draft still equals the file you started from, so it
cannot overwrite an edit made in the meantime.

The API key is on dandiarchive.org: sign in, click your initials in the upper
right, and copy the key. The sandbox (sandbox.dandiarchive.org) has separate
accounts and keys.

Without a key, or when the user would like to review the changes in a browser,
the DANDI metadata editor at https://medit.dandiarchive.org/?dandiset=NNNNNN
opens the draft, shows validation errors, and saves under the owner's own
account.
