# Dandiset Metadata Fields

Shapes checked against dandischema 0.14; they hold for drafts at schema versions 0.6 and 0.7. The editable
fields are listed in the order the DANDI metadata editor shows them. Fields the
server computes (`id`, `citation`, `assetsSummary`, `manifestLocation`,
`version`, `url`, `dateCreated`, `doi`, `datePublished`) are left alone: the
archive overwrites them.

Always start from the fetched draft and edit the fields below in place. Never
rebuild the document from scratch, and never drop a field you did not mean to
change.

## Title and Description

`name` is a string, about as descriptive as the title of a journal article. If
the dataset belongs to one paper, the paper's title is a good default.

`description` is a string. The paper's abstract is an acceptable start, but a
good description also names the data streams (for example "Neuropixels
recordings from ALM and striatum, spike-sorted units, licking and video"), the
species and number of subjects, and a sentence or two on the task and methods.
Funding does not belong in the description; it goes in the contributors.

`acknowledgement` is a string: the paper's acknowledgments, copied verbatim.

## Contributors

`contributor` is one list holding people, organizations, and funders.

A person:

```json
{
  "schemaKey": "Person",
  "name": "Li, Nuo",
  "identifier": "0000-0002-6613-5018",
  "email": "nuo.li@example.edu",
  "roleName": ["dcite:Author", "dcite:ContactPerson"],
  "includeInCitation": true,
  "affiliation": [
    {"schemaKey": "Affiliation", "name": "Baylor College of Medicine", "identifier": "https://ror.org/02pttbw34"}
  ]
}
```

- `name` must match `Family, Given`: a comma and a space, and only letters,
  spaces, hyphens, periods and apostrophes (no parentheses or digits).
  Particles stay with the family name ("van der Meer, Matthijs").
- `identifier` is the bare ORCID (`0000-0002-1825-0097`), never the
  `https://orcid.org/` URL. Leave it out when there is none; do not write an
  empty string.
- Affiliations use `schemaKey: "Affiliation"` (not `Organization`), with the
  ROR URL `https://ror.org/xxxxxxx` as `identifier` when one is known. An
  affiliation without a ROR is allowed; keep the name and say so to the user.
- At least one person needs `dcite:ContactPerson`, and every contact person
  needs an `email`. This is required before the dandiset can be published.
- `dcite:Author` puts the person in the citation shown on the landing page.
  Others who helped (a data curator who ran the NWB conversion, a technician)
  get the fitting role (`dcite:DataCurator`, `dcite:DataCollector`,
  `dcite:Software`, and so on) and usually `includeInCitation: false`.
- Order matters: the citation lists people in contributor order. Follow the
  paper's author order unless the user says otherwise.

A funder, one entry per award:

```json
{
  "schemaKey": "Organization",
  "name": "National Institute of Neurological Disorders and Stroke",
  "identifier": "https://ror.org/01s5ya894",
  "roleName": ["dcite:Funder"],
  "awardNumber": "R01 NS112312",
  "includeInCitation": false
}
```

- The same agency with two awards is two entries. A funder the paper names
  without an award number is one entry without `awardNumber`.
- `awardNumber` holds only the number, with no "Grant", "Award No.", or
  "project number" prefix. Copy it as the source gives it; do not reformat it
  beyond removing such a prefix.
- Prefer the most specific funder with a ROR (the NIH institute, such as NINDS
  or NEI, when the paper names it) over the umbrella agency.
- Other organizations (a consortium, a core facility) use
  `schemaKey: "Organization"` with a role such as `dcite:Sponsor` or
  `dcite:Other`.

## Subject Matter

`about` holds ontology terms, each with a full IRI:

```json
[
  {"schemaKey": "Anatomy", "name": "Secondary motor area",
   "identifier": "https://purl.brain-bican.org/ontology/mbao/MBA_993"},
  {"schemaKey": "Anatomy", "name": "hippocampal formation",
   "identifier": "http://purl.obolibrary.org/obo/UBERON_0002421"},
  {"schemaKey": "Disorder", "name": "Parkinson's disease",
   "identifier": "http://purl.obolibrary.org/obo/DOID_14330"},
  {"schemaKey": "GenericType", "name": "working memory",
   "identifier": "https://www.cognitiveatlas.org/concept/id/trm_4a3fd79d0b5a7"}
]
```

| What | Ontology | schemaKey |
|------|----------|-----------|
| Mouse brain region | Allen Mouse Brain Atlas (BICAN purl `mbao/MBA_<id>`) | Anatomy |
| Brain region in other species, other anatomy | UBERON | Anatomy |
| Cell type | CL | Anatomy |
| Disease | DOID, or NCIT | Disorder |
| Phenotype or symptom | HP | Disorder |
| Cognitive concept, task construct | Cognitive Atlas | GenericType |

Use the full IRI (`http://purl.obolibrary.org/obo/UBERON_0002421`), not the
CURIE (`UBERON:0002421`). Two to five terms the description or paper clearly
supports is typical. Species and approach are computed from the NWB files into
`assetsSummary`; do not add them here.

## Keywords

`keywords` is a list of strings. Each should add search value that the title,
species, anatomy and approach do not already carry: "grid cells", "optogenetic
silencing", "delayed response task", "sharp-wave ripples". Leave out generic
words such as "neuroscience", "brain", "data", "electrophysiology", "NWB".

## License

`license` is a list with one of `spdx:CC-BY-4.0` or `spdx:CC0-1.0`. It is
required for publishing. CC-BY-4.0 asks reusers to credit the authors and is
the common choice; CC0 waives that. It is the owner's decision; recommend
CC-BY-4.0 when they have no preference.

## Protocols and Ethics

`protocol` is a list of URLs, such as a protocols.io page.

`ethicsApproval` records the IACUC or IRB protocol:

```json
[{"schemaKey": "EthicsApproval", "identifier": "IACUC AN-7152 (Baylor College of Medicine)",
  "contactPoint": {"schemaKey": "ContactPoint", "url": "https://www.bcm.edu/research/..."}}]
```

`identifier` is free text: the protocol number and the approving body.
`contactPoint` is optional and only takes `email` and `url` (there is no
`name`). The protocol number comes from the user or from the paper's methods
section (the `fulltext` command finds it); never invent one.

## Related Resources

`relatedResource` links publications, code, notebooks, and other datasets. The
relation reads from the dandiset to the resource: dandiset *IsDescribedBy*
paper.

```json
{
  "schemaKey": "Resource",
  "identifier": "doi:10.1016/j.cell.2021.05.026",
  "url": "https://doi.org/10.1016/j.cell.2021.05.026",
  "name": "Modularity and robustness of frontal cortical networks",
  "repository": "Cell",
  "relation": "dcite:IsDescribedBy",
  "resourceType": "dcite:JournalArticle"
}
```

| Resource | relation | resourceType |
|----------|----------|--------------|
| The paper or preprint that is the primary account of this data | `dcite:IsDescribedBy` | `dcite:JournalArticle` or `dcite:Preprint` |
| A data descriptor (for example in Scientific Data) | `dcite:IsDocumentedBy` | `dcite:DataPaper` |
| A later paper that reuses or analyzes this data | `dcite:IsCitedBy` | `dcite:JournalArticle` |
| NWB conversion code | `dcite:IsSupplementedBy` | `dcite:Software` |
| Analysis code for the paper | `dcite:IsSupplementedBy` | `dcite:Software` |
| Example notebook (github.com/dandi/example-notebooks) | `dcite:IsSupplementedBy` | `dcite:ComputationalNotebook` |
| Another dataset this was derived from | `dcite:IsDerivedFrom` | `dcite:Dataset` |
| A companion dataset | `dcite:IsPartOf` or `dcite:HasPart` | `dcite:Dataset` |

`repository` is the journal or preprint server for papers, and the host
(GitHub, Zenodo) for code. Give a DOI as `doi:10...` in `identifier` and
`https://doi.org/10...` in `url`. When a preprint was later published, list the
journal version as `IsDescribedBy`; the preprint can stay as a second entry.

## Was Generated By

`wasGeneratedBy` describes the activities that produced the data. Empty entries
(nothing but a `schemaKey`) make the draft invalid; remove them. Otherwise
leave this field to the owner.
