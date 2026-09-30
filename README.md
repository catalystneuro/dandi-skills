# DANDI Skills

Agent skills for working with the [DANDI Archive](https://dandiarchive.org). Each
skill is a folder with a `SKILL.md` in the [Agent Skills](https://agentskills.io)
format, so it works in Claude Code and in other agents that read that format,
plus any scripts and reference files it needs.

## Skills

### dandiset-metadata

Populates or improves the metadata of a dandiset: contributors with ORCIDs and
ROR-linked affiliations, the contact person, funders with award numbers, related
publications and code, subject terms (brain regions, cell types, disorders,
cognitive concepts), keywords, license, and ethics approval.

The skill starts from the associated paper when there is one, because a single
OpenAlex lookup of its DOI gives the authors, ORCIDs, institutions and funding.
It asks the user for what only they know (the contact person, the ethics
protocol, the license) and never fills a gap with a guess. Every DOI, ORCID,
ROR identifier and ontology term has to come from a lookup made during the
session; the validation step flags any that did not. Changes are shown as a
diff and written to the draft only after the user approves them.

The rules come from the DANDI metadata editor's assistant
([medit](https://medit.dandiarchive.org)) and from proposing metadata
improvements for a few hundred existing dandisets, where each rule was added
after a specific mistake (a paper linked because it matched the lab and topic,
an ORCID that OpenAlex attached to a namesake).

`scripts/dandiset_metadata.py` is a single-file command-line helper with no
dependencies beyond `requests` and `dandischema`:

```bash
python dandiset-metadata/scripts/dandiset_metadata.py fetch 000123
python dandiset-metadata/scripts/dandiset_metadata.py audit 000123.original.json
python dandiset-metadata/scripts/dandiset_metadata.py paper 10.1016/j.cell.2021.05.026
```

Run it with `--help` for the full list of commands.

`dandiset-metadata/tests` has offline tests of the parts that need no network
(name handling, award grouping, the edit operations, validation, and the diff):
`pytest dandiset-metadata/tests`.

## Installation

### Claude Code

```
/plugin marketplace add dandi/dandi-skills
/plugin install dandiset-metadata@dandi-skills
```

The skill is also listed in the [CatalystNeuro marketplace](https://github.com/catalystneuro/claude-skills),
where the `nwb-convert` skill uses it after uploading a conversion to DANDI.

### Other Agents

Copy or symlink the `dandiset-metadata` folder into the agent's skills directory
(for example `~/.claude/skills/`, `.agents/skills/`, or wherever your agent looks),
or point the agent at `dandiset-metadata/SKILL.md` directly.

## License

Apache 2.0
