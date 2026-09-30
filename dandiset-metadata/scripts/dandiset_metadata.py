#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "dandischema"]
# ///
"""Tools for populating the metadata of a dandiset on the DANDI Archive.

Every command prints JSON to stdout. Run with `uv run dandiset_metadata.py ...`
(dependencies are declared inline above) or with a Python that has `requests`
and `dandischema` installed.

    fetch ID [--out FILE]                       draft metadata of a dandiset
    audit FILE|ID                               quality checklist, species, paper hints
    paper DOI                                   authors, ORCIDs, institutions, funders, awards
    search-paper "citation or title"            candidate DOIs from Crossref and OpenAlex
    ror "institution or funder name"            ROR matches (take the one marked chosen)
    orcid-search --family F [--given G]         ORCID registry candidates
    check-orcid ORCID --name "Family, Given" --metadata FILE [--paper DOI ...]
                                                does the ORCID record tie this person to the dandiset?
    ontology TERM --category anatomy|disorder|cognitive [--species mouse]
    import-authors DOI --metadata FILE --out FILE [--order existing|paper]
                                                merge a paper's authors into the contributors
    fulltext DOI                                acknowledgments, grants, ethics sentences (Europe PMC)
    apply FILE OPS.json                         add funders, resources, terms, keywords, ethics, people
                                                (apply --help lists the operations)
    validate PROPOSED [--base ORIGINAL]         schema errors, new errors, unverified identifiers
    diff ORIGINAL PROPOSED                      what changed, path by path
    save ID PROPOSED --base ORIGINAL [--yes]    write the draft metadata (needs DANDI_API_KEY)

Identifiers that pass a lookup are appended to a session log,
.dandiset-metadata/verified.txt in the working directory (or
$DANDISET_METADATA_DIR): DOIs from `paper`, ORCIDs that `check-orcid` (or
import-authors) corroborates, ROR identifiers that ROR chose or matched by
exact name, and ontology terms. Search candidates (search-paper,
orcid-search, unchosen ROR matches) are not logged. `validate` flags any new
DOI, ORCID, ROR or ontology identifier in the proposal that is not in the log,
which is how an identifier recalled from memory gets caught.

Set DANDI_API_KEY for embargoed dandisets and for `save`. For the sandbox, pass
--instance sandbox (DANDI_SANDBOX_API_KEY is used when set). Set
OPENALEX_MAILTO to an email address to join the OpenAlex and Crossref polite
pools.
"""

import argparse
import copy
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
API = {"dandi": "https://api.dandiarchive.org/api", "sandbox": "https://api.sandbox.dandiarchive.org/api"}
WEB = {"dandi": "https://dandiarchive.org", "sandbox": "https://sandbox.dandiarchive.org"}
MAILTO = os.environ.get("OPENALEX_MAILTO", "")
USER_AGENT = "dandi-skills/dandiset-metadata" + (f" (mailto:{MAILTO})" if MAILTO else "")
SESSION_DIR = os.environ.get("DANDISET_METADATA_DIR", ".dandiset-metadata")

ORCID_PATTERN = re.compile(r"^\d{4}-\d{4}-\d{4}-(\d{3}X|\d{4})$")
ROR_PATTERN = re.compile(r"^https://ror\.org/[a-z0-9]+$")
DOI_IN_TEXT = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
GENERIC_KEYWORDS = {
    "neuroscience", "brain", "data", "dataset", "science", "biology", "research", "experiment",
    "experiments", "recording", "recordings", "nwb", "dandi", "dandiset", "neural data", "neural activity",
}
PLACEHOLDERS = {
    "n/a", "na", "n.a.", "none", "null", "nil", "undefined", "unknown", "not known", "not available",
    "not applicable", "not specified", "not provided", "not reported", "tbd", "tba", "tbc",
    "to be determined", "to be added", "todo", "pending", "placeholder", "xxx", "xxxx", "???", "...", "-",
}
TEMPLATE = re.compile(r"[\[<{](?:insert|add|your|enter|fill|todo|tbd)[^\]>}]*[\]>}]", re.I)
FUNDER_ROLES = {"dcite:Funder", "dcite:Sponsor"}
# Validation messages about fields the server sets; a draft always fails these.
SERVER_SET = ("id:", "url:", "version:", "doi:", "datePublished", "publishedBy", "citation",
              "manifestLocation", "assetsSummary", "identifier:")


# --- HTTP ----------------------------------------------------------------------

_last_call = {}


def _get(url, params=None, headers=None, host=None, min_interval=0.25):
    """GET JSON with a per-host throttle and three retries. None on 404."""
    host = host or urllib.parse.urlparse(url).netloc
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    last = None
    for attempt in range(3):
        wait = _last_call.get(host, 0) + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        try:
            r = requests.get(url, params=params, headers=hdrs, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{host}: {last}")


def _polite(params):
    if MAILTO:
        params["mailto"] = MAILTO
    return params


# --- Session log of verified identifiers ------------------------------------------

def _verified_path():
    return os.path.join(SESSION_DIR, "verified.txt")


def _note(*ids):
    ids = [i for i in ids if i]
    if not ids:
        return
    os.makedirs(SESSION_DIR, exist_ok=True)
    with open(_verified_path(), "a") as fh:
        for i in ids:
            fh.write(i + "\n")


def _verified():
    try:
        return {line.strip() for line in open(_verified_path()) if line.strip()}
    except OSError:
        return set()


# --- Normalizers -------------------------------------------------------------------

def norm_doi(s):
    s = (s or "").strip()
    s = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    return s.lower()


def extract_doi(text):
    m = DOI_IN_TEXT.search(text or "")
    if not m:
        return ""
    doi = m.group(0).rstrip(".,;:)]")
    if doi.lower().startswith("10.1101/"):
        doi = re.sub(r"v\d+$", "", doi)  # bioRxiv/medRxiv version suffix
    return doi.lower()


def norm_orcid(s):
    return re.sub(r"^(https?://)?(www\.)?orcid\.org/", "", (s or "").strip(), flags=re.I)


def fold(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", text)


def _norm_org(name):
    return " ".join(w for w in fold(name).split() if w not in ("the", "of", "at", "and"))


def _name_tokens(name):
    return {w for w in fold(name).split() if len(w) >= 3}


PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "do", "dos", "das",
             "la", "le", "du", "des", "ten", "ter", "af", "av", "el", "al", "bin", "ibn"}


def format_person_name(display):
    """'Given Family' -> 'Family, Given', keeping particles such as 'van' with the family name."""
    name = re.sub(r"\s+", " ", (display or "").strip())
    if not name or "," in name:
        return name
    parts = name.split(" ")
    if len(parts) == 1:
        return name
    start = len(parts) - 1
    while start > 1 and parts[start - 1] in PARTICLES:
        start -= 1
    return f"{' '.join(parts[start:])}, {' '.join(parts[:start])}"


def _name_key(name):
    formatted = format_person_name(name)
    if "," not in formatted:
        return None
    family, given = formatted.split(",", 1)
    family = " ".join(fold(family).split())
    given = (fold(given).split() or [""])[0]
    return f"{family}|{given}" if family else None


def is_placeholder(value):
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    return v in PLACEHOLDERS or bool(TEMPLATE.search(value)) or bool(re.fullmatch(r"0000-0000-0000-000[0X]", value.strip()))


# --- DANDI API -------------------------------------------------------------------

def _token(instance):
    if instance == "sandbox":
        return os.environ.get("DANDI_SANDBOX_API_KEY") or os.environ.get("DANDI_API_KEY")
    return os.environ.get("DANDI_API_KEY")


def _dandi_headers(instance):
    tok = _token(instance)
    return {"Authorization": "token " + tok} if tok else {}


def draft_info(identifier, instance="dandi"):
    info = _get(f"{API[instance]}/dandisets/{identifier}/versions/draft/info/", headers=_dandi_headers(instance))
    if info is None:
        raise RuntimeError(f"dandiset {identifier} not found on {instance} (embargoed dandisets need DANDI_API_KEY)")
    return info


def _load_metadata(ref, instance="dandi"):
    """A metadata dict from a JSON file path or a dandiset identifier."""
    if os.path.exists(ref):
        d = json.load(open(ref))
        return d.get("metadata", d) if isinstance(d, dict) and "metadata" in d and "schemaKey" not in d else d
    if re.fullmatch(r"\d{6}", ref):
        return draft_info(ref, instance)["metadata"]
    raise SystemExit(f"{ref} is neither a file nor a six-digit dandiset identifier")


# --- Audit -------------------------------------------------------------------------

def _arr(v):
    return v if isinstance(v, list) else []


def _s(v):
    return v.strip() if isinstance(v, str) else ""


def _roles(c):
    return [_s(r) for r in _arr(c.get("roleName"))] if isinstance(c, dict) else []


def _people(m):
    return [c for c in _arr(m.get("contributor")) if isinstance(c, dict) and c.get("schemaKey") == "Person"]


def _item(id_, ok, detail):
    return {"id": id_, "status": "pass" if ok else "fail", "detail": detail}


def audit(m):
    """The rule-based half of the DANDI metadata quality checklist (as in the metadata editor)."""
    out = []
    people = _people(m)
    title = _s(m.get("name"))
    desc = _s(m.get("description"))
    out.append(_item("title", len(title) >= 20, f"{len(title)} characters" if title else "No title."))
    out.append(_item("description", len(desc) >= 300,
                     f"{len(desc)} characters" + ("" if len(desc) >= 300 else "; aim for an abstract-length description")))
    lic = [_s(x) for x in _arr(m.get("license")) if _s(x)]
    out.append(_item("license", bool(lic), ", ".join(lic) or "No license is set."))
    contacts = [c for c in people if "dcite:ContactPerson" in _roles(c)]
    out.append(_item("contact", bool(contacts) and all(_s(c.get("email")) for c in contacts),
                     "; ".join(f"{c.get('name')} <{c.get('email') or 'NO EMAIL'}>" for c in contacts)
                     or "No contributor has the dcite:ContactPerson role."))
    authors = [c for c in people if "dcite:Author" in _roles(c)]
    out.append(_item("authors", bool(authors), f"{len(authors)} authors" if authors else
                     ("No contributor has the dcite:Author role." if people else "No people are listed.")))
    missing = [c.get("name") for c in people if not ORCID_PATTERN.match(_s(c.get("identifier")))]
    out.append(_item("orcids", bool(people) and not missing, "No people are listed." if not people else
                     f"Missing ORCID: {'; '.join(missing)}" if missing else f"All {len(people)} have an ORCID."))
    no_ror = [c.get("name") for c in people
              if not any(ROR_PATTERN.match(_s(a.get("identifier"))) for a in _arr(c.get("affiliation")) if isinstance(a, dict))]
    out.append(_item("affiliations", bool(people) and not no_ror, "No people are listed." if not people else
                     f"No ROR-linked affiliation: {'; '.join(no_ror)}" if no_ror else "All have a ROR-linked affiliation."))
    bad_names = [c.get("name") for c in people if "," not in _s(c.get("name"))]
    if bad_names:
        out.append(_item("names", False, f"Not in 'Family, Given' form: {'; '.join(bad_names)}"))
    funders = [c for c in _arr(m.get("contributor")) if isinstance(c, dict) and set(_roles(c)) & FUNDER_ROLES]
    problems = [f"{f.get('name')}: " + ", ".join(p for p, bad in (
        ("no ROR (fine only if ror.org has no record for it)", not ROR_PATTERN.match(_s(f.get("identifier")))),
        ("award number has a prefix", bool(re.match(r"(?i)\s*(grant|award|project|no\.?|number|#)", _s(f.get("awardNumber")))))) if bad)
        for f in funders]
    problems = [p for p in problems if not p.endswith(": ")]
    no_award = [f.get("name") for f in funders if not _s(f.get("awardNumber"))]
    out.append(_item("funders", bool(funders) and not problems,
                     ("; ".join(problems) + "." if problems else (f"{len(funders)} funder entries." if funders else "No funder is listed."))
                     + (f" Without an award number (fine if the source gives none): {'; '.join(no_award)}." if no_award else "")))
    about = [a for a in _arr(m.get("about")) if isinstance(a, dict)]
    no_id = [a.get("name") for a in about if not _s(a.get("identifier"))]
    out.append(_item("about", bool(about) and not no_id,
                     f"No ontology identifier: {'; '.join(map(str, no_id))}" if no_id else
                     (f"{len(about)} terms: " + "; ".join(str(a.get("name")) for a in about) if about else "The about field is empty.")))
    kws = [_s(k) for k in _arr(m.get("keywords")) if _s(k)]
    generic = [k for k in kws if k.lower() in GENERIC_KEYWORDS]
    out.append(_item("keywords", bool(kws) and not generic,
                     f"Too generic: {', '.join(generic)}" if generic else (", ".join(kws) if kws else "No keywords.")))
    res = [r for r in _arr(m.get("relatedResource")) if isinstance(r, dict)]
    pubs = [r for r in res if extract_doi(_s(r.get("identifier")) + " " + _s(r.get("url")))
            and r.get("resourceType") not in ("dcite:Software", "dcite:Dataset", "dcite:ComputationalNotebook")
            and not extract_doi(_s(r.get("identifier")) + " " + _s(r.get("url"))).startswith(DATACITE_PREFIXES)]
    out.append(_item("publication", any(_s(r.get("relation")) for r in pubs),
                     f"{len(pubs)} related publications with a DOI" if pubs else
                     ("No related publication with a DOI (code and data DOIs do not count)." if res else "No related resources.")))
    eth = [_s(e.get("identifier")) for e in _arr(m.get("ethicsApproval")) if isinstance(e, dict) and _s(e.get("identifier"))]
    out.append(_item("ethics", bool(eth), ", ".join(eth) or "No ethics approval (IACUC or IRB protocol) is recorded."))
    return out


def species_of(m):
    """'mouse' when every species in the assets summary is Mus musculus, else the names found."""
    species = (m.get("assetsSummary") or {}).get("species") or []
    names = [(sp.get("name") or "") for sp in species if isinstance(sp, dict)]
    if names and all("mus musculus" in (n + " " + (sp.get("identifier") or "")).lower() or "10090" in (sp.get("identifier") or "")
                     for n, sp in zip(names, species)):
        return "mouse"
    return ", ".join(names)


PAPER_URL = re.compile(r"https?://(?:www\.)?(?:doi\.org|biorxiv\.org|medrxiv\.org|pubmed\.ncbi\.nlm\.nih\.gov|"
                       r"ncbi\.nlm\.nih\.gov/pmc|arxiv\.org|elifesciences\.org|nature\.com|sciencedirect\.com|"
                       r"cell\.com|jneurosci\.org|pnas\.org)/\S+", re.I)


def paper_hints(m):
    text = " ".join(str(m.get(k) or "") for k in ("name", "description", "acknowledgement"))
    return {"dois": sorted({extract_doi(x) for x in DOI_IN_TEXT.findall(text)}),
            "urls": sorted(set(PAPER_URL.findall(text))),
            "citations": sorted(set(re.findall(r"\b[A-Z][A-Za-z'-]+ et al\.?,? \(?\d{4}\)?", text)))}


# --- OpenAlex and Crossref -----------------------------------------------------------

OPENALEX_SELECT = "id,doi,title,publication_year,type,primary_location,authorships,funders,awards"


def _openalex_compact(w):
    if not w:
        return None
    doi = norm_doi(w.get("doi"))
    src = ((w.get("primary_location") or {}).get("source") or {})
    authors = []
    for a in w.get("authorships") or []:
        person = a.get("author") or {}
        orcid = norm_orcid(person.get("orcid") or "") or None
        insts = [{"name": i.get("display_name"), "ror": i.get("ror")} for i in a.get("institutions") or []]
        _note(*[i["ror"] for i in insts])  # ORCIDs are verified only by check-orcid
        authors.append({"name": format_person_name(person.get("display_name") or ""), "orcid": orcid,
                        "position": a.get("author_position"), "corresponding": bool(a.get("is_corresponding")),
                        "raw_affiliations": a.get("raw_affiliation_strings") or [], "institutions": insts})
    funders = {f.get("id"): {"name": f.get("display_name"), "ror": f.get("ror")} for f in w.get("funders") or []}
    _note(*[f["ror"] for f in funders.values()])
    raw_awards = []
    for a in w.get("awards") or []:
        f = funders.get(a.get("funder_id")) or {}
        raw_awards.append((a.get("funder_display_name") or f.get("name"), f.get("ror"), a.get("funder_award_id")))
    awards = group_awards(raw_awards)
    with_award = {a["funder_ror"] for a in awards}
    _note(doi)
    return {"doi": doi, "title": re.sub(r"<[^>]+>", "", w.get("title") or ""), "year": w.get("publication_year"),
            "type": w.get("type"), "venue": src.get("display_name"), "venue_type": src.get("type"),
            "authors": authors, "awards": awards,
            "funders_without_award": [f for f in funders.values() if f["ror"] not in with_award],
            "note": "Funding in OpenAlex is often incomplete and award numbers come in several spellings. Check the "
                    "paper's acknowledgments (the fulltext command) and use the form printed there."}


def _award_core(a):
    return re.sub(r"[^A-Z0-9]", "", (a or "").upper())


def group_awards(raw):
    """(funder, ror, award) triples -> one entry per award, with spelling variants grouped.

    "1F32MH119766" and "F32 MH119766", or "R01MH126904" and "MH126904", are the
    same award; one core string ends with the other.
    """
    groups = []
    for funder, ror, award in raw:
        if not award:
            continue
        core = _award_core(award)
        g = next((g for g in groups if g["funder_ror"] == ror and any(
            c.endswith(core) or core.endswith(c) for c in g["_cores"])), None)
        if g is None:
            groups.append({"funder": funder, "funder_ror": ror, "award_numbers": [award], "_cores": [core]})
        elif award not in g["award_numbers"]:
            g["award_numbers"].append(award)
            g["_cores"].append(core)
    for g in groups:
        g.pop("_cores")
    return groups


def openalex_work(doi):
    return _get(f"https://api.openalex.org/works/doi:{norm_doi(doi)}", _polite({"select": OPENALEX_SELECT}))


def crossref_work(doi):
    res = _get(f"https://api.crossref.org/works/{urllib.parse.quote(norm_doi(doi))}", _polite({}))
    return _crossref_compact(res["message"]) if res else None


def _crossref_compact(item, note=True):
    doi = norm_doi(item.get("DOI"))
    if note:
        _note(doi)
    issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return {"doi": doi, "title": (item.get("title") or [""])[0], "journal": (item.get("container-title") or [""])[0],
            "type": item.get("type"), "year": issued, "publisher": item.get("publisher"),
            "authors": [f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in item.get("author") or []][:12],
            "funders": [{"name": f.get("name"), "awards": f.get("award") or []} for f in item.get("funder") or []],
            "published_as": [r.get("id") for r in (item.get("relation") or {}).get("is-preprint-of") or []],
            "preprint": [r.get("id") for r in (item.get("relation") or {}).get("has-preprint") or []]}


# Repositories whose DOIs are code or data archives registered with DataCite, not papers.
DATACITE_PREFIXES = ("10.5281/", "10.6084/", "10.5061/", "10.48324/", "10.17605/", "10.7910/", "10.12751/", "10.5282/")
DATACITE_TYPES = {"Software": ("dcite:Software", "dcite:IsSupplementedBy"), "Dataset": ("dcite:Dataset", None),
                  "ComputationalNotebook": ("dcite:ComputationalNotebook", "dcite:IsSupplementedBy"),
                  "Text": ("dcite:Text", "dcite:IsDescribedBy"), "Preprint": ("dcite:Preprint", "dcite:IsDescribedBy"),
                  "JournalArticle": ("dcite:JournalArticle", "dcite:IsDescribedBy")}


def datacite_work(doi):
    """A code or data DOI (Zenodo, figshare, Dryad, OSF, DANDI) described from its DataCite record."""
    d = _get(f"https://api.datacite.org/dois/{urllib.parse.quote(doi)}")
    attrs = (d or {}).get("data", {}).get("attributes") or {}
    if not attrs:
        return None
    _note(doi)
    general = (attrs.get("types") or {}).get("resourceTypeGeneral") or "Other"
    rtype, rel = DATACITE_TYPES.get(general, ("dcite:Other", None))
    if doi.startswith(DATACITE_PREFIXES) and general in ("JournalArticle", "Text", "Preprint", "Other"):
        # Zenodo uploads of code or data are often labeled with the paper's type
        rtype, rel = None, None
    title = ((attrs.get("titles") or [{}])[0]).get("title")
    publisher = attrs.get("publisher")
    publisher = publisher.get("name") if isinstance(publisher, dict) else publisher
    return {"doi": doi, "registry": "DataCite", "title": title, "type": general, "publisher": publisher,
            "year": attrs.get("publicationYear"),
            "related_resource": {"schemaKey": "Resource", "identifier": f"doi:{doi}", "url": f"https://doi.org/{doi}",
                                 "name": title, "repository": publisher, "resourceType": rtype, "relation": rel},
            "note": f"A {general} record in a data or code repository ({publisher}). Repositories often label code and "
                    "data uploads with the paper's type, so check what it holds and set resourceType (dcite:Software, "
                    "dcite:Dataset) and the relation yourself. Code for this "
                    "dataset is IsSupplementedBy; a dataset this one was derived from is IsDerivedFrom; one derived from "
                    "this is IsSourceOf; a companion dataset is IsPartOf or HasPart."}


def cmd_paper(args):
    doi = extract_doi(args.doi) or norm_doi(args.doi)
    if doi.startswith(DATACITE_PREFIXES):
        dc = datacite_work(doi)
        if dc:
            return dc
    w = openalex_work(doi)
    out = _openalex_compact(w) if w else None
    cr = crossref_work(doi)
    if out is None and cr is None and not datacite_work(doi):
        return {"error": f"No record for {doi} in OpenAlex, Crossref, or DataCite. Check the DOI with the user."}
    if out is None and cr is None:
        dc = datacite_work(doi)
        if dc:
            return dc
    if out is None:
        out = {"doi": doi, "note": "OpenAlex has no record; authors come from Crossref without ORCIDs or institutions."}
    for a in out.get("authors") or []:
        _flag_inactive(a["institutions"], "ror")
    _flag_inactive(out.get("awards") or [], "funder_ror")
    _flag_inactive(out.get("funders_without_award") or [], "ror")
    if cr:
        out["crossref"] = {"title": cr["title"], "journal": cr["journal"], "type": cr["type"], "funders": cr["funders"]}
    is_preprint = (cr or {}).get("type") == "posted-content" or out.get("type") == "preprint"
    out["related_resource"] = {"schemaKey": "Resource", "identifier": f"doi:{doi}", "url": f"https://doi.org/{doi}",
                               "name": out.get("title") or (cr or {}).get("title"),
                               "repository": out.get("venue") or (cr or {}).get("journal"),
                               "resourceType": "dcite:Preprint" if is_preprint else "dcite:JournalArticle",
                               "relation": "dcite:IsDescribedBy"}
    out["relation_note"] = ("IsDescribedBy is for the paper that presents this data. Use IsDocumentedBy (resourceType "
                            "dcite:DataPaper) for a data descriptor, IsCitedBy for a later paper that reuses the data.")
    published = [norm_doi(d) for d in (cr or {}).get("published_as") or []]
    if is_preprint:
        if not published:
            hits = _get("https://api.openalex.org/works", _polite({
                "filter": f"title.search:{re.sub(r'[^A-Za-z0-9 ]', ' ', out['related_resource']['name'] or '')[:200]}",
                "select": "doi,title,type,primary_location", "per_page": 10})) or {}
            title = fold(out["related_resource"]["name"] or "").split()
            for h in hits.get("results") or []:
                d = norm_doi(h.get("doi"))
                if d and d != doi and h.get("type") == "article" and fold(h.get("title") or "").split() == title \
                        and not re.search(r"\.\d+$", d.split("/")[-1].replace("elife.", "")):
                    published.append(d)
        # eLife reviewed-preprint versions (10.7554/elife.85646.3) -> the article DOI
        published = list(dict.fromkeys(re.sub(r"^(10\.7554/elife\.\d+)\.\d+$", r"\1", d) for d in published))
        for d in published:
            _note(d)
        out["published_version"] = published or None
        out["preprint_note"] = ("This is a preprint. If it has been published, list the journal version as IsDescribedBy "
                                "(run paper on its DOI) and keep the preprint as a second entry. eLife reviewed-preprint "
                                "DOIs end in .1, .2, ...; use the unversioned DOI.")
    return out


EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ETHICS = re.compile(r"IACUC|IRB\b|Institutional Animal Care|Institutional Review Board|ethic|animal (?:care|welfare|protocol)|"
                    r"approved by|in accordance with|protocol (?:no|number|#)|licen[cs]e (?:no|number)|Home Office", re.I)


def _xml_text(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _pmc_html(pmcid, attempts=3):
    """Funding, acknowledgments, data availability and ethics sentences from a PMC article page (author manuscripts).

    PMC sometimes answers with a bot-check page instead of the article; that is retried.
    """
    for attempt in range(attempts):
        out = _pmc_html_once(pmcid)
        if "source_page" in out or attempt == attempts - 1:
            return out
        time.sleep(3 * (attempt + 1))


def _pmc_html_once(pmcid):
    import html as htmllib
    r = requests.get(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
                     headers={"User-Agent": "Mozilla/5.0 (compatible; " + USER_AGENT + ")"}, timeout=60)
    if not r.ok:
        return {"note": f"PMC returned HTTP {r.status_code} for {pmcid}. Read the publisher's page or ask the user."}
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
    urls = sorted(set(re.findall(r"https?://(?:github\.com|gitlab\.com|zenodo\.org|doi\.org/10\.5281|osf\.io|figshare\.com|"
                                 r"datadryad\.org|dandiarchive\.org|protocols\.io|www\.protocols\.io)[^\s\"'<>)]*", text)))
    urls = [u for u in urls if not u.rstrip("/").endswith(("github.com/ncbi", "github.com/NLMGov"))]
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    if "Funding" not in text and "Acknowledg" not in text and "Abstract" not in text:
        return {"note": f"PMC served a page without the article text (probably its bot check). Open "
                        f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/ another way, or ask the user."}

    def section(*heads):
        for head in heads:
            m = re.search(rf"\b{head}\b[:.]?\s", text)
            if m:
                return text[m.start():m.start() + 1500]
        return ""

    sentences = []
    for sent in re.split(r"(?<=[.;])\s+(?=[A-Z])", text):
        if ETHICS.search(sent) and 20 < len(sent) < 600 and sent not in sentences:
            sentences.append(sent)
    return {"source_page": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
            "funding_statement": section("Funding", "Acknowledgments", "Acknowledgements"),
            "data_availability": section("Data and materials availability", "Data availability", "Code availability"),
            "code_and_data_links": urls[:20], "ethics_sentences": sentences[:12],
            "note": "Read from the PMC article page (an author manuscript). The funding and availability snippets are the "
                    "1500 characters after the heading; read them, do not copy them wholesale."}


def cmd_fulltext(args):
    """Acknowledgments, funding statement and ethics sentences from Europe PMC's open-access full text."""
    import xml.etree.ElementTree as ET
    doi = extract_doi(args.doi) or norm_doi(args.doi)
    res = _get(f"{EUROPEPMC}/search", {"query": f'DOI:"{doi}"', "format": "json", "resultType": "core"}) or {}
    hits = (res.get("resultList") or {}).get("result") or []
    if not hits:
        return {"doi": doi, "found": False, "note": "Europe PMC has no record. Read the acknowledgments and methods on the "
                                                     "publisher's page, or ask the user."}
    h = hits[0]
    out = {"doi": doi, "found": True, "pmcid": h.get("pmcid"), "source": h.get("source"),
           "grants": [{"agency": g.get("agency"), "award_number": g.get("grantId")}
                      for g in (h.get("grantsList") or {}).get("grant") or []]}
    xml = None
    url = f"{EUROPEPMC}/{h['pmcid']}/fullTextXML" if h.get("pmcid") else \
        f"{EUROPEPMC}/PPR/{h['id']}/fullTextXML" if h.get("source") == "PPR" and h.get("id") else None
    if url:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        xml = r.text if r.ok and r.text.lstrip().startswith("<") else None
    if not xml and h.get("pmcid"):
        return {**out, **_pmc_html(h["pmcid"])}
    if not xml:
        out["note"] = ("No open-access full text. If the paper has a PubMed Central author manuscript, read it there; "
                       "otherwise read the publisher's page or ask the user.")
        return out
    root = ET.fromstring(xml.encode())
    out["acknowledgments"] = " ".join(_xml_text(a) for a in root.iter("ack"))[:4000]
    out["funding_statement"] = [
        {"source": _xml_text(g.find(".//funding-source")), "award_ids": [_xml_text(x) for x in g.iter("award-id")]}
        for g in root.iter("award-group")]
    sentences = []
    for p in root.iter("p"):
        for sent in re.split(r"(?<=[.;])\s+(?=[A-Z])", _xml_text(p)):
            if ETHICS.search(sent) and sent[:500] not in sentences:
                sentences.append(sent[:500])
    out["ethics_sentences"] = sentences[:12]
    avail = [_xml_text(sec) for sec in root.iter("sec") if re.search(r"availab", _xml_text(sec.find("title")), re.I)]
    avail += [_xml_text(n) for n in root.iter("notes") if re.search(r"availab", _xml_text(n), re.I)]
    out["data_availability"] = " ".join(avail)[:2000]
    links = {e.get("{http://www.w3.org/1999/xlink}href") or "" for e in root.iter("ext-link")}
    out["code_and_data_links"] = sorted(l for l in links if re.search(
        r"github\.com|gitlab\.com|zenodo|figshare|osf\.io|datadryad|dandiarchive|protocols\.io|10\.5281", l))[:20]
    out["note"] = ("Code and data links include every repository the paper cites, including general tools; keep only "
                   "the ones for this dataset. Use the award numbers as printed in the acknowledgments or funding statement. The ethics sentences "
                   "usually name the approving committee and sometimes the protocol number.")
    return out


def cmd_search_paper(args):
    cr = _get("https://api.crossref.org/works", _polite({"query.bibliographic": args.query, "rows": 5,
                                                         "select": "DOI,title,container-title,type,author,issued,publisher,funder"}))
    oa = _get("https://api.openalex.org/works", _polite({"search": args.query, "per_page": 5,
                                                         "select": "doi,title,publication_year,primary_location,authorships"}))
    oa_out = []
    for w in (oa or {}).get("results") or []:
        d = norm_doi(w.get("doi"))
        first = next((a for a in w.get("authorships") or [] if a.get("author_position") == "first"), {})
        oa_out.append({"doi": d, "title": w.get("title"), "year": w.get("publication_year"),
                       "first_author": (first.get("author") or {}).get("display_name"),
                       "venue": (((w.get("primary_location") or {}).get("source")) or {}).get("display_name")})
    return {"crossref": [_crossref_compact(i, note=False) for i in ((cr or {}).get("message") or {}).get("items") or []],
            "openalex": oa_out,
            "note": "A search hit is a candidate, not a match. Link a paper only if the dandiset names it or the user confirms it, "
                    "then run paper on its DOI (which records it as verified)."}


# --- ROR and ORCID ---------------------------------------------------------------------

def _ror_name(org):
    names = org.get("names") or []
    return next((n["value"] for n in names if "ror_display" in (n.get("types") or [])), None) or (names[0]["value"] if names else None)


def ror_affiliation(text, note=True):
    res = _get("https://api.ror.org/v2/organizations", {"affiliation": text}) or {}
    out = []
    for item in res.get("items") or []:
        org = item.get("organization") or {}
        country = ((org.get("locations") or [{}])[0].get("geonames_details") or {}).get("country_name")
        if note and (item.get("chosen") or fold(_ror_name(org) or "").split() == fold(text).split()):
            _note(org.get("id"))
        out.append({"ror": org.get("id"), "name": _ror_name(org), "country": country, "types": org.get("types"),
                    "chosen": bool(item.get("chosen")), "score": item.get("score")})
    return out


_ror_status_cache = {}


def ror_status(ror):
    """(status, successor ROR or None) for a ROR identifier."""
    if ror not in _ror_status_cache:
        org = _get(f"https://api.ror.org/v2/organizations/{ror.rsplit('/', 1)[-1]}") or {}
        succ = next((r.get("id") for r in org.get("relationships") or [] if r.get("type") == "successor"), None)
        _ror_status_cache[ror] = (org.get("status") or ("missing" if not org else "active"), succ)
    return _ror_status_cache[ror]


def _require_active_ror(ror):
    status, succ = ror_status(ror)
    if status != "active":
        raise Skip(f"ROR {ror} is {status}" + (f"; its successor is {succ} (run ror on the name to verify it)" if succ else ""))


def _flag_inactive(entries, key):
    for e in entries:
        if e.get(key):
            status, succ = ror_status(e[key])
            if status != "active":
                e["ror_status"] = status + (f"; successor {succ}" if succ else "")


def cmd_ror(args):
    out = ror_affiliation(args.text, note=False)
    if args.country:
        out = [o for o in out if (o.get("country") or "").lower() == args.country.lower()]
    out = out[:args.rows]
    for o in out:
        o["exact_name"] = fold(o.get("name") or "").split() == fold(args.text).split()
        # ROR's chosen flag is tuned for affiliation strings and can pick a different organization
        # ("Kinship Foundation" -> "Kinship Conservation Fellows"), so it counts only when the name agrees
        agrees = o["exact_name"] or (o["chosen"] and _name_appears_in(o.get("name") or "", args.text))
        if agrees:
            _note(o["ror"])
        o["verified"] = bool(agrees)
    return {"matches": out,
            "note": "Use a match marked verified (an exact name, or ROR's chosen match whose name appears in your text). "
                    "Otherwise check name and country with the user (--country narrows the list), then rerun ror with "
                    "the organization's exact name. For funders of a paper, prefer funder_ror from the paper command."}


def cmd_orcid_search(args):
    q = f'given-names:"{args.given}" AND family-name:"{args.family}"' if args.given else f'family-name:"{args.family}"'
    res = _get("https://pub.orcid.org/v3.0/expanded-search/", {"q": q, "rows": args.rows}) or {}
    out = []
    for r in res.get("expanded-result") or []:
        out.append({"orcid": r.get("orcid-id"), "given": r.get("given-names"), "family": r.get("family-names"),
                    "institutions": (r.get("institution-name") or [])[:6]})
    return {"num_found": res.get("num-found"), "candidates": out,
            "note": "Several candidates with the same name is the normal case. Run check-orcid on the one you pick; "
                    "only a corroborated (or user-confirmed) ORCID counts as verified."}


def orcid_profile(orcid):
    rec = _get(f"https://pub.orcid.org/v3.0/{orcid}/record") or {}
    person = rec.get("person") or {}
    n = person.get("name") or {}
    names = [(n.get(k) or {}).get("value") for k in ("given-names", "family-name", "credit-name")]
    names = [x for x in names if x] + [o["content"] for o in (person.get("other-names") or {}).get("other-name") or [] if o.get("content")]
    acts = rec.get("activities-summary") or {}
    dois = set()
    for g in (acts.get("works") or {}).get("group") or []:
        for e in (g.get("external-ids") or {}).get("external-id") or []:
            if (e.get("external-id-type") or "").lower() == "doi" and e.get("external-id-value"):
                dois.add(norm_doi(e["external-id-value"]))
    orgs = []
    for section in ("employments", "educations", "invited-positions", "memberships", "qualifications", "services", "distinctions"):
        for g in (acts.get(section) or {}).get("affiliation-group") or []:
            for summ in g.get("summaries") or []:
                v = next(iter(summ.values()), {}) or {}
                org = v.get("organization") or {}
                dis = org.get("disambiguated-organization") or {}
                ror = dis.get("disambiguated-organization-identifier") if (dis.get("disambiguation-source") or "").upper() == "ROR" else ""
                if org.get("name"):
                    orgs.append({"name": org["name"], "ror": ror or ""})
    return {"exists": bool(rec), "names": names, "dois": dois, "orgs": orgs}


def cmd_check_orcid(args):
    """Whether the ORCID record ties its owner to this dandiset's team.

    OpenAlex links authors to ORCIDs by its own disambiguation and sometimes picks
    a different researcher with the same name. The ORCID record (what the owner
    claims) must carry a matching name and then list one of the dandiset's
    papers, an institution one of its contributors is affiliated with, works
    co-authored with another contributor, or have an OpenAlex profile of at least
    five works at one of those institutions.
    """
    orcid = norm_orcid(args.orcid)
    if not ORCID_PATTERN.match(orcid):
        return {"orcid": orcid, "verdict": "invalid", "reason": "not in the form 0000-0000-0000-000X"}
    prof = orcid_profile(orcid)
    if not prof["exists"]:
        return {"orcid": orcid, "verdict": "invalid", "reason": "the ORCID registry has no such record"}
    base = {"orcid": orcid, "record_names": prof["names"], "record_orgs": sorted({o["name"] for o in prof["orgs"]})[:10]}
    record_tokens = set().union(*[_name_tokens(n) for n in prof["names"]]) if prof["names"] else set()
    if not (_name_tokens(args.name) & record_tokens):
        return {**base, "verdict": "reject", "reason": "the ORCID record is under a different name"}
    m = args.metadata if isinstance(args.metadata, dict) else (_load_metadata(args.metadata) if args.metadata else {})
    papers = {extract_doi(_s(r.get("identifier")) + " " + _s(r.get("url")))
              for r in _arr(m.get("relatedResource")) if isinstance(r, dict)} | {norm_doi(d) for d in args.paper or []}
    papers.discard("")
    shared_papers = prof["dois"] & papers
    if shared_papers:
        _note(orcid)
        return {**base, "verdict": "corroborated", "reason": f"the record lists {', '.join(sorted(shared_papers))}"}
    insts = set()
    for c in _people(m):
        for a in _arr(c.get("affiliation")):
            if isinstance(a, dict):
                insts.add((_s(a.get("identifier")), _s(a.get("name"))))
    for a in args.affiliation or []:
        insts.add(("", a) if not a.startswith("https://ror.org/") else (a, ""))
    rors = {r for r, _ in insts if r}
    names = {_norm_org(n) for _, n in insts if n}

    def shared(ror, name):
        on = _norm_org(name)
        return (ror and ror in rors) or (bool(on) and any(on == n or (len(on) > 8 and (on in n or n in on)) for n in names))

    hit = next((o for o in prof["orgs"] if shared(o["ror"], o["name"])), None)
    if hit:
        _note(orcid)
        return {**base, "verdict": "corroborated", "reason": f"the record lists {hit['name']}, a contributor affiliation"}
    others = set()
    for c in _people(m):
        fam, _, giv = format_person_name(_s(c.get("name"))).partition(",")
        fam, giv = " ".join(fold(fam).split()), fold(giv).split()
        if fam and giv and _name_key(_s(c.get("name"))) != _name_key(args.name):
            others.add((fam, giv[0][0]))
    if prof["dois"] and others:
        dois = sorted(prof["dois"])[:100]
        coauthors = set()
        for i in range(0, len(dois), 50):
            res = _get("https://api.openalex.org/works", _polite({"filter": "doi:" + "|".join(dois[i:i + 50]),
                                                                  "select": "authorships", "per-page": 50})) or {}
            for w in res.get("results") or []:
                for a in w.get("authorships") or []:
                    coauthors.add(" ".join(fold((a.get("author") or {}).get("display_name") or "").split()))
        # same family name and first initial, so a common surname alone does not count
        who = next((f"{fam}, {ini}." for fam, ini in others for n in coauthors
                    if n.split() and n.endswith(" " + fam) and n.split()[0].startswith(ini)), None)
        if who:
            _note(orcid)
            return {**base, "verdict": "corroborated", "reason": f"works on the record are co-authored with contributor {who}"}
    d = _get(f"https://api.openalex.org/authors/orcid:{orcid}", _polite({"select": "works_count,affiliations,last_known_institutions"})) or {}
    oa_insts = [(i.get("ror") or "", i.get("display_name") or "") for i in d.get("last_known_institutions") or []]
    oa_insts += [((a.get("institution") or {}).get("ror") or "", (a.get("institution") or {}).get("display_name") or "")
                 for a in d.get("affiliations") or []]
    if (d.get("works_count") or 0) >= 5 and any(shared(r, n) for r, n in oa_insts):
        _note(orcid)
        return {**base, "verdict": "corroborated", "reason": "an established OpenAlex profile at a contributor institution"}
    if args.confirmed_by_user:
        _note(orcid)
        return {**base, "verdict": "confirmed by user", "reason": "the user confirmed this ORCID"}
    return {**base, "verdict": "unconfirmed",
            "reason": "nothing on the ORCID record ties it to this dandiset (no shared paper, institution, or co-author). "
                      "Ask the user, or leave the ORCID out."}


# --- Ontologies -----------------------------------------------------------------------

OLS = {"anatomy": [("uberon", "Anatomy"), ("cl", "Anatomy")],
       "disorder": [("doid", "Disorder"), ("hp", "Disorder"), ("ncit", "Disorder")]}
MBA_ALIASES = {
    "alm": "MOs", "anterior lateral motor cortex": "MOs", "anterolateral motor cortex": "MOs", "premotor cortex": "MOs",
    "m1": "MOp", "primary motor cortex": "MOp", "m2": "MOs", "secondary motor cortex": "MOs",
    "v1": "VISp", "primary visual cortex": "VISp", "s1": "SSp", "primary somatosensory cortex": "SSp",
    "barrel cortex": "SSp-bfd", "a1": "AUDp", "primary auditory cortex": "AUDp",
    "hippocampus": "HIP", "hippocampal formation": "HPF", "dentate gyrus": "DG", "striatum": "STR",
    "prefrontal cortex": "PL", "medial prefrontal cortex": "PL", "cerebellum": "CB", "thalamus": "TH",
    "amygdala": "sAMY", "hypothalamus": "HY", "olfactory bulb": "MOB", "retrosplenial cortex": "RSP",
    "posterior parietal cortex": "PTLp", "visual cortex": "VIS", "motor cortex": "MO",
    "somatosensory cortex": "SS", "auditory cortex": "AUD", "orbitofrontal cortex": "ORB",
    "anterior cingulate cortex": "ACA", "entorhinal cortex": "ENT", "superior colliculus": "SC",
    "ventral tegmental area": "VTA", "substantia nigra": "SN", "nucleus accumbens": "ACB",
}


def ols_search(term, category, rows=6):
    out = []
    for onto, key in OLS[category]:
        res = _get("https://www.ebi.ac.uk/ols4/api/search", {"q": term, "ontology": onto, "rows": rows,
                                                               "queryFields": "label,synonym"}) or {}
        for d in (res.get("response") or {}).get("docs") or []:
            iri = d.get("iri") or ""
            src = re.search(r"/obo/([A-Za-z]+)_", iri)
            src = src.group(1).upper() if src else ""
            if any(o["identifier"] == iri for o in out) or (category == "anatomy" and src not in ("UBERON", "CL")):
                continue
            _note(iri)
            out.append({"schemaKey": key, "identifier": iri, "name": d.get("label"), "ontology": src or d.get("ontology_name"),
                        "exact": (d.get("label") or "").lower() == term.lower(),
                        "description": ((d.get("description") or [""])[0] or "")[:160]})
    # OLS often ranks a sub-region ("primary motor cortex layer 6") above the term itself
    return sorted(out, key=lambda r: not r["exact"])


def mba_search(term, limit=8):
    mba = json.load(open(os.path.join(DATA, "mba_structures.json")))
    norm = lambda s: re.sub(r"\s+", " ", s.lower().replace("cortex", "area").replace("cortical", "").strip())
    t, tn = term.lower().strip(), norm(term)
    alias = MBA_ALIASES.get(t)
    words = [w for w in tn.split() if len(w) > 2]

    def rank(st):
        name, acr = norm(st["name"]), st["acronym"].lower()
        if acr == t or (alias and st["acronym"] == alias):
            return 0
        if name == tn:
            return 1
        if tn in name:
            return 2
        if words and all(w in name for w in words):
            return 3
        return None

    hits = sorted(((rank(st), st) for st in mba["structures"] if rank(st) is not None), key=lambda h: (h[0], len(h[1]["name"])))
    out = []
    for _, st in hits[:limit]:
        iri = f"{mba['identifier_prefix']}{st['id']}"
        _note(iri)
        out.append({"schemaKey": "Anatomy", "identifier": iri, "name": st["name"], "ontology": "MBA", "acronym": st["acronym"],
                    "exact": _ == 0 or _ == 1})
    return out


COGNITIVE_ALIASES = {"classical conditioning": "pavlovian conditioning", "operant conditioning": "instrumental conditioning",
                     "associative learning": "association learning", "spatial navigation": "navigation",
                     "decision making": "decision making", "short-term memory": "short-term memory"}


def cognitive_atlas_search(term, limit=6):
    concepts = json.load(open(os.path.join(DATA, "cognitive_atlas_concepts.json")))["concepts"]
    t = COGNITIVE_ALIASES.get(term.lower().strip(), term.lower().strip())

    stems = [w[:5] for w in re.findall(r"[a-z]+", t) if len(w) > 2]

    def all_stems(text):
        words = re.findall(r"[a-z]+", text)
        return bool(stems) and all(any(w.startswith(st) for w in words) for st in stems)

    def rank(c):
        name = (c.get("name") or "").lower()
        defn = (c.get("definition") or "").lower()
        for i, ok in enumerate((name == t, name.startswith(t), t in name, all_stems(name), t in defn, all_stems(defn))):
            if ok:
                return i
        return None

    hits = sorted(((rank(c), c) for c in concepts if rank(c) is not None), key=lambda h: (h[0], len(h[1]["name"])))
    out = []
    for _, c in hits[:limit]:
        iri = f"https://www.cognitiveatlas.org/concept/id/{c['id']}"
        _note(iri)
        out.append({"schemaKey": "GenericType", "identifier": iri, "name": c["name"], "ontology": "cognitiveatlas",
                    "exact": c["name"].lower() == t, "description": (c.get("definition") or "")[:160]})
    return out


SUBREGION_WORDS = {"core", "shell", "dorsal", "ventral", "medial", "lateral", "anterior", "posterior", "rostral", "caudal",
                   "superficial", "deep", "layer", "dorsomedial", "dorsolateral", "ventromedial", "ventrolateral", "of", "the"}


def cmd_ontology(args):
    if args.category == "cognitive":
        return {"terms": cognitive_atlas_search(args.term),
                "note": "Cognitive Atlas covers psychological concepts (working memory, attention, reward), not cell types "
                        "or behaviors like head direction. Accept a term only if its definition fits; otherwise use a keyword."}
    if args.category == "anatomy":
        mba = mba_search(args.term) if args.species == "mouse" else []
        if args.species == "mouse" and not mba:
            words = args.term.split()
            parent = " ".join(w for w in words if w.lower() not in SUBREGION_WORDS)
            if parent and parent != args.term:
                mba = [{**t, "parent_of_query": True} for t in mba_search(parent)]
        terms = mba + ols_search(args.term, "anatomy")
        note = ("Mouse dataset: prefer the Allen Mouse Brain Atlas (MBA) term for a brain region. When the atlas lacks a "
                "subregion (nucleus accumbens core), use the Allen parent region and put the subregion in keywords. UBERON "
                "for structures outside the atlas, CL for cell types." if args.species == "mouse" else
                "UBERON for anatomy, CL for cell types. For a mouse-only dataset rerun with --species mouse.")
        return {"terms": terms, "note": note}
    terms = ols_search(args.term, "disorder")
    return {"terms": terms, "note": "Read the labels; prefer an exact match. If nothing fits, leave it out." if terms else
            "No match. Try a synonym or a broader term, or leave it out."}


# --- Contributors from a paper -------------------------------------------------------

def _split_raw(strings):
    seen, out = set(), []
    for raw in strings or []:
        for part in raw.split(";"):
            p = re.sub(r"\s+", " ", part.strip())
            k = fold(p).strip()
            if p and k not in seen:
                seen.add(k)
                out.append(p)
    return out


def _name_appears_in(inst_name, raw):
    raw_words = set(fold(raw).split())
    words = [w for w in fold(re.sub(r"\s*\([^)]*\)\s*$", "", inst_name)).split() if len(w) > 1]
    return bool(words) and all(w in raw_words for w in words)


def _affiliation_for(authorship, raw, ror_cache):
    """A confident ROR match wins; OpenAlex's institution only when its name is in the raw text; else raw text, unresolved."""
    if raw not in ror_cache:
        try:
            chosen = next((r for r in ror_affiliation(raw, note=False) if r["chosen"]), None)
        except RuntimeError:
            chosen = None
        ror_cache[raw] = chosen
    chosen = ror_cache[raw]
    if chosen and chosen.get("ror") and chosen.get("name"):
        _note(chosen["ror"])
        return {"schemaKey": "Affiliation", "name": chosen["name"], "identifier": chosen["ror"]}, "ror"
    insts = [i for i in authorship.get("institutions") or [] if i.get("display_name") and _name_appears_in(i["display_name"], raw)]
    insts.sort(key=lambda i: -len(i["display_name"]))
    if insts:
        aff = {"schemaKey": "Affiliation", "name": re.sub(r"\s*\([^)]*\)\s*$", "", insts[0]["display_name"])}
        if insts[0].get("ror") and ROR_PATTERN.match(insts[0]["ror"]):
            aff["identifier"] = insts[0]["ror"]
            _note(insts[0]["ror"])
        return aff, "openalex"
    return {"schemaKey": "Affiliation", "name": raw}, "unresolved"


def cmd_import_authors(args):
    """Merge a paper's authors into the contributor list, in publication order.

    Adapted from the DANDI metadata editor's import_contributors_from_publication.
    People are matched by ORCID, then by family name plus first given name, then
    by family name with a compatible given name ("Alex" and "Alexander"). A
    matched entry keeps everything it has and only gains a missing ORCID,
    missing affiliations, and the Author role. Rerunning is idempotent.
    """
    doi = extract_doi(args.doi) or norm_doi(args.doi)
    w = _get(f"https://api.openalex.org/works/doi:{doi}", _polite({"select": "id,doi,title,authorships"}))
    if not w:
        return {"error": f"OpenAlex has no record for {doi}"}
    _note(doi)
    m = _load_metadata(args.metadata)
    ror_cache, unresolved, no_orcid, imported = {}, [], [], []
    for a in w.get("authorships") or []:
        disp = (a.get("author") or {}).get("display_name")
        if not disp:
            continue
        person = {"schemaKey": "Person", "name": format_person_name(disp), "roleName": ["dcite:Author"], "includeInCitation": True}
        orcid = norm_orcid((a.get("author") or {}).get("orcid") or "")
        if ORCID_PATTERN.match(orcid):
            person["identifier"] = orcid
        else:
            no_orcid.append(person["name"])
        affs, keys = [], set()
        raws = _split_raw(a.get("raw_affiliation_strings"))
        pairs = [_affiliation_for(a, r, ror_cache) + (r,) for r in raws] if raws else \
            [({"schemaKey": "Affiliation", "name": i["display_name"], **({"identifier": i["ror"]} if i.get("ror") else {})}, "openalex", "")
             for i in a.get("institutions") or [] if i.get("display_name")]
        _note(*[aff.get("identifier") for aff, _, _ in pairs])
        for aff, source, raw in pairs:
            k = aff.get("identifier") or fold(aff["name"]).strip()
            if k in keys:
                continue
            keys.add(k)
            affs.append(aff)
            if source == "unresolved":
                unresolved.append({"author": person["name"], "affiliation": raw})
        if affs:
            person["affiliation"] = affs
        person["_corresponding"] = bool(a.get("is_corresponding"))
        imported.append(person)

    contribs = [copy.deepcopy(c) for c in _arr(m.get("contributor"))]
    used, new_people, paper_order = set(), [], []
    added, matched, orcid_checks, conflicts = [], [], [], []

    def person_idx(pred):
        hits = [i for i, c in enumerate(contribs) if i not in used and isinstance(c, dict)
                and c.get("schemaKey") == "Person" and pred(c)]
        return hits

    # The team is the dandiset's people plus the paper's authors and their affiliations, so an ORCID can be tied
    # to the paper even when the dandiset lists nobody yet.
    team = {**m, "contributor": _arr(m.get("contributor")) + [{k: v for k, v in p.items() if not k.startswith("_")}
                                                             for p in imported]}

    def orcid_ok(orcid, name):
        v = cmd_check_orcid(argparse.Namespace(orcid=orcid, name=name, metadata=team, paper=[doi], affiliation=None,
                                               confirmed_by_user=False))
        return v.get("verdict") == "corroborated", v.get("reason", "")

    orcids_not_attached = []
    for p in imported:
        corresponding = p.pop("_corresponding")
        tag = " (corresponding author)" if corresponding else ""
        hits, how = [], ""
        if p.get("identifier"):
            hits, how = person_idx(lambda c: norm_orcid(_s(c.get("identifier"))) == p["identifier"]), "ORCID"
        if not hits and _name_key(p["name"]):
            hits, how = person_idx(lambda c: _name_key(_s(c.get("name"))) == _name_key(p["name"])), "name"
        if not hits:
            hits, how = person_idx(lambda c: _same_person_loose(_s(c.get("name")), p["name"])), "similar name"
            if len(hits) > 1:
                conflicts.append(f"{p['name']}: several existing contributors could be this author; merge by hand")
                hits = []
        if not hits:
            if p.get("identifier"):
                ok, why = orcid_ok(p["identifier"], p["name"])
                if not ok:
                    orcids_not_attached.append({"name": p["name"], "orcid": p.pop("identifier"), "reason": why})
            new_people.append(p)
            paper_order.append(("new", p))
            added.append(p["name"] + tag)
            continue
        i = hits[0]
        used.add(i)
        cur = contribs[i]
        paper_order.append(("old", i))
        if p.get("identifier"):
            if not cur.get("identifier"):
                ok, why = orcid_ok(p["identifier"], _s(cur.get("name")))
                if ok:
                    cur["identifier"] = p["identifier"]
                    orcid_checks.append({"name": cur.get("name"), "orcid": p["identifier"], "check": why})
                else:
                    orcids_not_attached.append({"name": cur.get("name"), "orcid": p["identifier"], "reason": why})
            elif norm_orcid(_s(cur["identifier"])) != p["identifier"]:
                conflicts.append(f"{cur.get('name')}: has ORCID {cur['identifier']}, the paper gives {p['identifier']}; left as is")
        roles = list(_arr(cur.get("roleName")))
        if "dcite:Author" not in roles:
            roles.append("dcite:Author")
        cur["roleName"] = roles
        existing = list(_arr(cur.get("affiliation")))
        for aff in p.get("affiliation") or []:
            merge_affiliation(existing, aff)
        if existing:
            cur["affiliation"] = existing
        note = f" (matched by {how} to paper author '{p['name']}'; confirm it is the same person)" if how == "similar name" else ""
        matched.append(cur.get("name") + tag + note)

    if args.order == "paper":
        # the paper's authors in publication order, then everyone else as they were
        ordered = [p if kind == "new" else contribs[p] for kind, p in paper_order]
        ordered += [c for i, c in enumerate(contribs) if i not in used]
    else:
        # existing contributors stay where they are; new authors follow the last listed person
        last = max([i for i, c in enumerate(contribs) if isinstance(c, dict) and c.get("schemaKey") == "Person"], default=-1)
        ordered = contribs[:last + 1] + new_people + contribs[last + 1:]
    out = copy.deepcopy(m)
    out["contributor"] = ordered
    json.dump(out, open(args.out, "w"), indent=2, ensure_ascii=False)
    return {"paper": {"doi": doi, "title": w.get("title")}, "written": args.out, "order": args.order,
            "added": added, "matched": matched, "conflicts": conflicts,
            "without_orcid": no_orcid, "unresolved_affiliations": unresolved,
            "orcids_added_to_existing": orcid_checks, "orcids_not_attached": orcids_not_attached,
            "note": "Every ORCID was checked against the ORCID registry; those in orcids_not_attached were left out "
                    "(ask the user). Review added and matched with the user, confirm similar-name matches, resolve "
                    "conflicts by hand, and ask who the contact person is if none is set."}


def merge_affiliation(affs, aff):
    """Add an affiliation unless it is already there. An existing entry without a ROR whose name matches the new
    one (or contains it, as "Allen Institute for Brain Science" contains "Allen Institute") gains the ROR instead."""
    ror = aff.get("identifier")
    for a in affs:
        if not isinstance(a, dict):
            continue
        if ror and a.get("identifier") == ror:
            return "present"
        same = fold(a.get("name", "")).split() == fold(aff["name"]).split()
        if not a.get("identifier") and (same or (ror and (_name_appears_in(aff["name"], a.get("name", ""))
                                                          or _name_appears_in(a.get("name", ""), aff["name"])))):
            if ror:
                a["identifier"] = ror
                return "ror added"
            return "present"
        if same:
            return "present"
    affs.append(aff)
    return "added"


def _same_person_loose(a, b):
    """Same family name and compatible given names: 'Gonzalez, Alex' and 'Gonzalez, Alexander', or 'Li, N.' and 'Li, Nuo'."""
    fa, fb = format_person_name(a), format_person_name(b)
    if "," not in fa or "," not in fb:
        return False
    (fam_a, giv_a), (fam_b, giv_b) = [(" ".join(fold(x.split(",", 1)[0]).split()), fold(x.split(",", 1)[1]).split())
                                      for x in (fa, fb)]
    if fam_a != fam_b or not giv_a or not giv_b:
        return False
    ga, gb = giv_a[0], giv_b[0]
    return ga.startswith(gb) or gb.startswith(ga)


# --- Edits ----------------------------------------------------------------------------------

APPLY_HELP = """Operations for `apply`, as a JSON list (in a file, or - for stdin):

  {"op": "add_person", "name": "Family, Given", "orcid": "...", "email": "...", "roles": ["dcite:Author"],
   "affiliations": [{"name": "...", "ror": "https://ror.org/..."}], "includeInCitation": true}
  {"op": "set_orcid", "contributor": "Family, Given", "orcid": "0000-..."}
  {"op": "add_affiliation", "contributor": "Family, Given", "name": "...", "ror": "https://ror.org/..."}
  {"op": "add_role", "contributor": "Family, Given", "role": "dcite:ContactPerson"}
  {"op": "set_email", "contributor": "Family, Given", "email": "..."}
  {"op": "add_funder", "name": "...", "ror": "https://ror.org/...", "award_number": "..."}   (award optional;
                                                    "no_ror": true for a funder ROR does not list)
  {"op": "set_funder", "contributor": "<existing funder name>", "ror": "...", "award_number": "..."}
        (with several entries of that name, a ROR alone goes to all of them; "match_award" picks one)
  {"op": "split_funder", "contributor": "<existing funder name>", "award_numbers": ["R00MH118422", "R01MH129582"]}
  {"op": "add_resource", "doi": "10....", "name": "...", "repository": "...", "relation": "dcite:IsDescribedBy",
   "resourceType": "dcite:JournalArticle"}                     (or "url" in place of "doi")
  {"op": "add_about", "schemaKey": "Anatomy", "identifier": "<IRI>", "name": "..."}
  {"op": "add_keywords", "keywords": ["..."]}      {"op": "remove_keywords", "keywords": ["..."]}
  {"op": "set_license", "license": "spdx:CC-BY-4.0"}
  {"op": "add_ethics", "identifier": "IACUC #27694 (Stanford University)", "url": "...", "email": "..."}
  {"op": "add_protocol", "url": "https://www.protocols.io/..."}
  {"op": "set_text", "field": "name" | "description" | "acknowledgement", "value": "..."}   (needs --owner)

Operations are applied people first, then emails, then everything else, with add_role last, so a
contact person can be added in the same list as their email.
  {"op": "remove_empty_activities"}
"""


def _enum(name):
    from dandischema import models as dm
    return {e.value for e in getattr(dm, name)}


class Skip(Exception):
    pass


def _contributor(m, name):
    hits = [c for c in _arr(m.get("contributor")) if isinstance(c, dict) and _s(c.get("name")) == name.strip()]
    if len(hits) != 1:
        raise Skip(f"{len(hits)} contributors are named {name!r}")
    return hits[0]


def _check(value, what, pattern=None):
    if not isinstance(value, str) or not value.strip() or is_placeholder(value):
        raise Skip(f"{what}: missing or placeholder value {value!r}")
    if pattern is not None and not pattern.match(value.strip()):
        raise Skip(f"{what}: {value!r} is not in the expected form")
    return value.strip()


def _affiliation(a):
    aff = {"schemaKey": "Affiliation", "name": _check(a.get("name"), "affiliation name")}
    if a.get("ror"):
        aff["identifier"] = _check(a["ror"], "ROR", ROR_PATTERN)
    return aff


def apply_op(m, op):
    kind = op.get("op")
    people = m.setdefault("contributor", [])
    if kind == "add_person":
        name = _check(op.get("name"), "name")
        if "," not in name:
            raise Skip(f"name {name!r} must be 'Family, Given'")
        if any(isinstance(c, dict) and (_name_key(_s(c.get("name"))) == _name_key(name) or
                                        (op.get("orcid") and norm_orcid(_s(c.get("identifier"))) == norm_orcid(op["orcid"])))
               for c in people):
            raise Skip(f"{name} is already a contributor")
        roles = op.get("roles") or ["dcite:Author"]
        bad = set(roles) - _enum("RoleType")
        if bad:
            raise Skip(f"unknown roles {sorted(bad)}")
        p = {"schemaKey": "Person", "name": name, "roleName": roles,
             "includeInCitation": op.get("includeInCitation", "dcite:Author" in roles)}
        if op.get("orcid"):
            p["identifier"] = _check(norm_orcid(op["orcid"]), "ORCID", ORCID_PATTERN)
        if op.get("email"):
            p["email"] = _check(op["email"], "email")
        if op.get("affiliations"):
            p["affiliation"] = [_affiliation(a) for a in op["affiliations"]]
        people.append(p)
        return f"added {name}"
    if kind == "set_orcid":
        c = _contributor(m, op.get("contributor", ""))
        orcid = _check(norm_orcid(op.get("orcid")), "ORCID", ORCID_PATTERN)
        if c.get("identifier"):
            raise Skip(f"{c['name']} already has identifier {c['identifier']}")
        c["identifier"] = orcid
        return f"{c['name']}: ORCID {orcid}"
    if kind == "add_affiliation":
        c = _contributor(m, op.get("contributor", ""))
        aff = _affiliation(op)
        if aff.get("identifier"):
            _require_active_ror(aff["identifier"])
        result = merge_affiliation(c.setdefault("affiliation", []), aff)
        if result == "present":
            raise Skip(f"{c['name']} already has {aff['name']}")
        return f"{c['name']}: {'ROR ' + aff['identifier'] + ' added to existing affiliation' if result == 'ror added' else 'affiliation ' + aff['name']}"
    if kind == "add_role":
        c = _contributor(m, op.get("contributor", ""))
        role = op.get("role")
        if role not in _enum("RoleType"):
            raise Skip(f"unknown role {role!r}")
        if role == "dcite:ContactPerson" and not _s(c.get("email")):
            raise Skip(f"{c['name']} has no email; a contact person needs one (set_email first)")
        roles = c.setdefault("roleName", [])
        if role in roles:
            raise Skip(f"{c['name']} already has {role}")
        roles.append(role)
        return f"{c['name']}: role {role}"
    if kind == "set_email":
        c = _contributor(m, op.get("contributor", ""))
        c["email"] = _check(op.get("email"), "email")
        return f"{c['name']}: email set"
    if kind in ("add_funder", "set_funder"):
        ror = _check(op["ror"], "ROR", ROR_PATTERN) if op.get("ror") else None
        award = op.get("award_number")
        if award is not None:
            award = re.sub(r"(?i)^\s*(?:(?:grant|award|project|number|no\.?|nr\.?)[\s.:#]*)+|^#\s*", "", _check(award, "award number")).strip()
        if kind == "set_funder":
            same = [x for x in people if isinstance(x, dict) and _s(x.get("name")) == op.get("contributor", "").strip()]
            if len(same) > 1 and op.get("match_award"):
                same = [x for x in same if _award_core(x.get("awardNumber")) == _award_core(op["match_award"])]
            if len(same) > 1 and not award:
                results = []
                for x in same:  # the same ROR for every entry of this funder
                    if ror and not x.get("identifier"):
                        _require_active_ror(ror)
                        x["identifier"] = ror
                        results.append(x.get("awardNumber") or "(no award)")
                if not results:
                    raise Skip(f"{op['contributor']}: nothing to change")
                return f"{op['contributor']}: ROR added to {len(results)} entries"
            if len(same) != 1:
                raise Skip(f"{len(same)} contributors are named {op.get('contributor')!r}; pass match_award to pick one")
            c = same[0]
            done = []
            if ror and not c.get("identifier"):
                _require_active_ror(ror)
                c["identifier"] = ror
                done.append("ROR")
            if award and award != _s(c.get("awardNumber")):
                old = _s(c.get("awardNumber"))
                if old and _award_core(old)[-len(_award_core(award)):] != _award_core(award):
                    raise Skip(f"{c['name']}: award {old!r} differs from {award!r}; ask the user")
                c["awardNumber"] = award
                done.append("award number")
            if not set(_roles(c)) & FUNDER_ROLES:
                c.setdefault("roleName", []).append("dcite:Funder")
                done.append("Funder role")
            if not done:
                raise Skip(f"{c['name']}: nothing to change")
            return f"{c['name']}: " + ", ".join(done)
        name = _check(op.get("name"), "funder name")
        if not ror and not op.get("no_ror"):
            raise Skip("add_funder needs a ROR (run ror, or take funder_ror from paper). For a funder that has no ROR "
                       "record (an endowed chair, a small foundation), pass \"no_ror\": true")
        if ror:
            _require_active_ror(ror)
        for c in people:
            if isinstance(c, dict) and set(_roles(c)) & FUNDER_ROLES and \
                    (c.get("identifier") == ror if ror else fold(_s(c.get("name"))).split() == fold(name).split()):
                if award and _award_core(c.get("awardNumber")) == _award_core(award):
                    raise Skip(f"{name} {award} is already listed")
                if not award and not c.get("awardNumber"):
                    raise Skip(f"{name} is already listed")
        f = {"schemaKey": "Organization", "name": name, "roleName": ["dcite:Funder"], "includeInCitation": False}
        if ror:
            f["identifier"] = ror
        if award:
            f["awardNumber"] = award
        people.append(f)
        return f"funder {name}" + (f" {award}" if award else "")
    if kind == "split_funder":
        c = _contributor(m, op.get("contributor", ""))
        awards = [re.sub(r"(?i)^\s*(?:grant|award|project|number|no\.?)[\s.:#]*", "", _check(a, "award number")).strip()
                  for a in op.get("award_numbers") or []]
        if len(awards) < 2:
            raise Skip("split_funder needs award_numbers with at least two awards")
        old = _award_core(c.get("awardNumber"))
        missing = [a for a in awards if _award_core(a) not in old]
        if missing:
            raise Skip(f"{', '.join(missing)} not in the existing award text {c.get('awardNumber')!r}")
        c["awardNumber"] = awards[0]
        i = next(i for i, x in enumerate(people) if x is c)
        for k, a in enumerate(awards[1:], 1):
            people.insert(i + k, {**copy.deepcopy(c), "awardNumber": a})
        return f"{c['name']}: split into {len(awards)} entries ({', '.join(awards)})"
    if kind == "add_resource":
        rel = op.get("relation")
        if rel not in _enum("RelationType"):
            raise Skip(f"unknown relation {rel!r}")
        if op.get("resourceType") and op["resourceType"] not in _enum("ResourceType"):
            raise Skip(f"unknown resourceType {op['resourceType']!r}")
        r = {"schemaKey": "Resource", "relation": rel}
        if op.get("doi"):
            d = extract_doi(op["doi"]) or norm_doi(op["doi"])
            r.update(identifier=f"doi:{d}", url=f"https://doi.org/{d}")
        else:
            r["url"] = _check(op.get("url"), "url")
        for k in ("name", "repository", "resourceType"):
            if op.get(k):
                r[k] = _check(op[k], k)
        res = m.setdefault("relatedResource", [])
        for x in res:
            if not isinstance(x, dict):
                continue
            same_doi = op.get("doi") and extract_doi(_s(x.get("identifier")) + " " + _s(x.get("url"))) == r["identifier"][4:]
            same_url = _s(x.get("url")).rstrip("/") == r["url"].rstrip("/")
            if same_doi or same_url:
                raise Skip(f"{r['url']} is already a related resource")
            if op.get("doi") and r.get("name") and fold(_s(x.get("name"))).split() == fold(r["name"]).split() \
                    and not extract_doi(_s(x.get("identifier")) + " " + _s(x.get("url"))):
                x.setdefault("url", r["url"])
                x["identifier"] = r["identifier"]
                return f"DOI added to existing resource {x.get('name')}"
        res.append(r)
        return f"resource {r.get('name') or r['url']} ({rel})"
    if kind == "add_about":
        key = op.get("schemaKey")
        if key not in ("Anatomy", "Disorder", "GenericType"):
            raise Skip(f"schemaKey must be Anatomy, Disorder or GenericType, not {key!r}")
        iri = _check(op.get("identifier"), "identifier")
        if not iri.startswith("http"):
            raise Skip(f"{iri!r}: use the full IRI from the ontology command, not a CURIE")
        about = m.setdefault("about", [])
        if any(isinstance(a, dict) and a.get("identifier") == iri for a in about):
            raise Skip(f"{iri} is already in about")
        about.append({"schemaKey": key, "identifier": iri, "name": _check(op.get("name"), "name")})
        return f"about {op['name']}"
    if kind in ("add_keywords", "remove_keywords"):
        kws = list(_arr(m.get("keywords")))
        lower = {k.lower() for k in kws if isinstance(k, str)}
        changed = []
        for k in op.get("keywords") or []:
            k = k.strip()
            if kind == "add_keywords":
                if is_placeholder(k) or k.lower() in GENERIC_KEYWORDS or not k or k.lower() in lower:
                    continue
                kws.append(k)
                lower.add(k.lower())
                changed.append(k)
            elif k.lower() in lower:
                kws = [x for x in kws if not (isinstance(x, str) and x.lower() == k.lower())]
                changed.append(k)
        if not changed:
            raise Skip("keywords: nothing to change (duplicates, generic words and placeholders are ignored)")
        m["keywords"] = kws
        return f"{'added' if kind == 'add_keywords' else 'removed'} keywords {', '.join(changed)}"
    if kind == "set_license":
        lic = op.get("license")
        if lic not in ("spdx:CC-BY-4.0", "spdx:CC0-1.0"):
            raise Skip("license must be spdx:CC-BY-4.0 or spdx:CC0-1.0")
        if m.get("license"):
            raise Skip(f"license is already {m['license']}")
        m["license"] = [lic]
        return f"license {lic}"
    if kind == "add_ethics":
        e = {"schemaKey": "EthicsApproval", "identifier": _check(op.get("identifier"), "ethics identifier")}
        cp = {k: _check(op[k], k) for k in ("url", "email") if op.get(k)}
        if cp:
            e["contactPoint"] = {"schemaKey": "ContactPoint", **cp}
        eth = m.setdefault("ethicsApproval", [])
        if any(isinstance(x, dict) and _s(x.get("identifier")) == e["identifier"] for x in eth):
            raise Skip("that ethics approval is already listed")
        eth.append(e)
        return f"ethics approval {e['identifier']}"
    if kind == "add_protocol":
        url = _check(op.get("url"), "url")
        prot = m.setdefault("protocol", [])
        if url in prot:
            raise Skip("protocol already listed")
        prot.append(url)
        return f"protocol {url}"
    if kind == "set_text":
        field = op.get("field")
        if field not in ("name", "description", "acknowledgement"):
            raise Skip("set_text only sets name, description or acknowledgement")
        m[field] = _check(op.get("value"), field)
        return f"{field} set"
    if kind == "remove_empty_activities":
        acts = _arr(m.get("wasGeneratedBy"))
        kept = [a for a in acts if not (isinstance(a, dict) and not any(v for k, v in a.items() if k != "schemaKey"))]
        if len(kept) == len(acts):
            raise Skip("no empty wasGeneratedBy entries")
        m["wasGeneratedBy"] = kept
        return f"removed {len(acts) - len(kept)} empty wasGeneratedBy entries"
    raise Skip(f"unknown op {kind!r}; see apply --help")


def cmd_apply(args):
    ops = json.load(sys.stdin if args.ops == "-" else open(args.ops))
    if isinstance(ops, dict):
        ops = [ops]
    m = _load_metadata(args.metadata)
    applied, skipped = [], []
    # people and emails before the roles and ORCIDs that refer to them, whatever order the list is in
    first = {"add_person": 0, "set_email": 1, "split_funder": 1}
    ops = sorted(ops, key=lambda o: first.get(o.get("op"), 2 if o.get("op") != "add_role" else 3))
    for op in ops:
        if op.get("op") == "set_text" and not args.owner:
            skipped.append("set_text: the title and description are the owner's; pass --owner once the owner approved the text")
            continue
        try:
            applied.append(apply_op(m, op))
        except Skip as e:
            skipped.append(f"{op.get('op')}: {e}")
    out = args.out or args.metadata
    json.dump(m, open(out, "w"), indent=2, ensure_ascii=False)
    return {"written": out, "applied": applied, "skipped": skipped}


# --- Validate, diff, save ------------------------------------------------------------------

def validation_errors(m):
    """Validation messages from dandischema, both stages, as a set."""
    from dandischema import metadata as dm
    out = set()
    for json_validation in (True, False):
        try:
            dm.validate(m, json_validation=json_validation)
        except Exception as e:
            errors = getattr(e, "errors", None)
            items = errors() if callable(errors) else (e.args[0] if e.args and isinstance(e.args[0], list) else [e])
            for it in items:
                if isinstance(it, dict):
                    out.add(f"{'.'.join(str(x) for x in it.get('loc', ()))}: {it.get('msg')}")
                else:
                    out.add(getattr(it, "message", None) or str(it)[:300])
    return {e for e in out if not e.startswith(SERVER_SET)}


def _identifiers(m):
    """(kind, value) for every DOI, ORCID, ROR and ontology IRI in the metadata."""
    out = set()
    for c in _arr(m.get("contributor")):
        if not isinstance(c, dict):
            continue
        ident = _s(c.get("identifier"))
        if ORCID_PATTERN.match(ident):
            out.add(("ORCID", ident))
        elif ROR_PATTERN.match(ident):
            out.add(("ROR", ident))
        for a in _arr(c.get("affiliation")):
            if isinstance(a, dict) and ROR_PATTERN.match(_s(a.get("identifier"))):
                out.add(("ROR", a["identifier"]))
    for r in _arr(m.get("relatedResource")):
        if isinstance(r, dict):
            d = extract_doi(_s(r.get("identifier")) + " " + _s(r.get("url")))
            if d:
                out.add(("DOI", d))
    for a in _arr(m.get("about")):
        if isinstance(a, dict) and _s(a.get("identifier")):
            out.add(("ontology", a["identifier"]))
    return out


def _placeholders(m, path=""):
    if isinstance(m, dict):
        for k, v in m.items():
            yield from _placeholders(v, f"{path}.{k}" if path else k)
    elif isinstance(m, list):
        for i, v in enumerate(m):
            yield from _placeholders(v, f"{path}.{i}")
    elif is_placeholder(m):
        yield f"{path}: {m!r}"


def cmd_validate(args):
    prop = _load_metadata(args.proposed)
    base = _load_metadata(args.base) if args.base else None
    after = validation_errors(prop)
    out = {"errors": sorted(after)}
    if base is not None:
        before = validation_errors(base)
        unindex = lambda errs: {re.sub(r"\.\d+\.", ".N.", e) for e in errs}
        out["new_errors"] = sorted(e for e in after if re.sub(r"\.\d+\.", ".N.", e) not in unindex(before))
        out["fixed_errors"] = sorted(e for e in before if re.sub(r"\.\d+\.", ".N.", e) not in unindex(after))
        new_ids = _identifiers(prop) - _identifiers(base)
        verified = {v.lower() for v in _verified()}
        out["unverified_identifiers"] = sorted(f"{k} {v}" for k, v in new_ids if v.lower() not in verified)
        oc = [c.get("name") for c in _arr(base.get("contributor")) if isinstance(c, dict)]
        pc = [c.get("name") for c in _arr(prop.get("contributor")) if isinstance(c, dict)]
        removed = [n for n in oc if n not in pc]
        if removed:
            out["removed_contributors"] = removed
        it = iter(pc)
        if not all(any(n == x for x in it) for n in oc):
            out["reordered_contributors"] = "existing contributors changed order (fine in owner mode if intended, not in a review)"
        placeholders_before = set(_placeholders(base))
        out["placeholders"] = [p for p in _placeholders(prop) if p not in placeholders_before]
    else:
        out["placeholders"] = list(_placeholders(prop))
    people = _people(prop)
    dupes = []
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            oa, ob = norm_orcid(_s(a.get("identifier"))), norm_orcid(_s(b.get("identifier")))
            if (oa and oa == ob) or _same_person_loose(_s(a.get("name")), _s(b.get("name"))):
                dupes.append(f"{a.get('name')} / {b.get('name')}")
    out["possible_duplicates"] = dupes
    contacts = [p for p in people if "dcite:ContactPerson" in _roles(p)]
    out["contact_person"] = [f"{p.get('name')} <{p.get('email') or 'NO EMAIL'}>" for p in contacts] or "none set"
    out["ok_to_save"] = not out.get("new_errors", []) and not out.get("unverified_identifiers") \
        and not out["placeholders"] and not out.get("removed_contributors") and not dupes
    return out


def _key(x):
    if x.get("schemaKey") == "Resource":
        return x.get("url") or x.get("identifier") or x.get("name")
    if x.get("awardNumber"):
        return f"{x.get('name')} ({x['awardNumber']})"
    return x.get("name") or x.get("url") or x.get("identifier")


def _keyed(items):
    keys = [_key(x) if isinstance(x, dict) else None for x in items]
    return all(keys) and len(set(keys)) == len(keys)


def _diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            p = f"{path}.{k}" if path else k
            if k not in b:
                yield f"- {p}: {json.dumps(a[k], ensure_ascii=False)}"
            elif k not in a:
                yield f"+ {p}: {json.dumps(b[k], ensure_ascii=False)}"
            else:
                yield from _diff(a[k], b[k], p)
    elif isinstance(a, list) and isinstance(b, list) and (a or b) and _keyed(a) and _keyed(b) \
            and any(isinstance(x, dict) for x in a + b):
        # contributors, resources, terms: match entries by name or identifier, not position
        ka, kb = {_key(x): x for x in a}, {_key(x): x for x in b}
        for k, x in ka.items():
            if k not in kb:
                yield f"- {path}[{k!r}]: {json.dumps(x, ensure_ascii=False)}"
            else:
                yield from _diff(x, kb[k], f"{path}[{k!r}]")
        for k, x in kb.items():
            if k not in ka:
                yield f"+ {path}[{k!r}]: {json.dumps(x, ensure_ascii=False)}"
        if [k for k in kb if k in ka] != [k for k in ka if k in kb]:
            yield f"~ {path}: existing entries were reordered"
    elif isinstance(a, list) and isinstance(b, list) and not any(isinstance(x, (dict, list)) for x in a + b):
        # roles, keywords, licenses: what was added and removed, not position by position
        for x in a:
            if x not in b:
                yield f"- {path}: {json.dumps(x, ensure_ascii=False)}"
        for x in b:
            if x not in a:
                yield f"+ {path}: {json.dumps(x, ensure_ascii=False)}"
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            p = f"{path}[{i}]"
            if i >= len(a):
                yield f"+ {p}: {json.dumps(b[i], ensure_ascii=False)}"
            elif i >= len(b):
                yield f"- {p}: {json.dumps(a[i], ensure_ascii=False)}"
            else:
                yield from _diff(a[i], b[i], p)
    elif a != b:
        yield f"~ {path}: {json.dumps(a, ensure_ascii=False)} -> {json.dumps(b, ensure_ascii=False)}"


def cmd_diff(args):
    return {"changes": list(_diff(_load_metadata(args.original), _load_metadata(args.proposed)))}


def cmd_fetch(args):
    info = draft_info(args.id, args.instance)
    m = info["metadata"]
    out = args.out or f"{args.id}.original.json"
    json.dump(m, open(out, "w"), indent=2, ensure_ascii=False)
    return {"written": out, "name": m.get("name"), "modified": info.get("modified"), "status": info.get("status"),
            "server_validation_errors": [f"{e.get('field')}: {e.get('message')}" for e in info.get("version_validation_errors") or []],
            "editor": f"https://medit.dandiarchive.org/?dandiset={args.id}" if args.instance == "dandi" else None,
            "page": f"{WEB[args.instance]}/dandiset/{args.id}/draft"}


def cmd_audit(args):
    m = _load_metadata(args.ref, args.instance)
    items = audit(m)
    return {"checklist": items, "failing": [i["id"] for i in items if i["status"] == "fail"],
            "species": species_of(m) or "not recorded (no NWB assets summarized yet)",
            "paper_hints": paper_hints(m),
            "judgment_items": ["Is the title informative?", "Is the description informative, naming the data streams?",
                               "Does the description summarize the methods?"]}


def cmd_save(args):
    tok = _token(args.instance)
    if not tok:
        raise SystemExit("Set DANDI_API_KEY (the key of an owner of this dandiset) to save.")
    base, prop = _load_metadata(args.base), _load_metadata(args.proposed)
    live = draft_info(args.id, args.instance)["metadata"]
    if live != base:
        return {"saved": False, "reason": "The draft changed since the base file was fetched. Fetch again and redo the changes on the new draft.",
                "changes_since_base": list(_diff(base, live))[:40]}
    report = cmd_validate(argparse.Namespace(proposed=args.proposed, base=args.base))
    if not report["ok_to_save"] and not args.force:
        return {"saved": False, "reason": "validation found problems; fix them or pass --force after the user agrees", **report}
    if not args.yes:
        return {"saved": False, "reason": "Show the diff to the user and rerun with --yes once they approve it.",
                "changes": list(_diff(base, prop))}
    r = requests.put(f"{API[args.instance]}/dandisets/{args.id}/versions/draft/",
                     json={"metadata": prop, "name": prop.get("name", "")},
                     headers={"Authorization": "token " + tok, "User-Agent": USER_AGENT}, timeout=60)
    if r.status_code >= 400:
        return {"saved": False, "status": r.status_code, "response": r.text[:1000]}
    time.sleep(2)
    info = draft_info(args.id, args.instance)
    return {"saved": True, "page": f"{WEB[args.instance]}/dandiset/{args.id}/draft",
            "server_validation_errors": [f"{e.get('field')}: {e.get('message')}" for e in info.get("version_validation_errors") or []]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *spec, **kw):
        p = sub.add_parser(name, **kw)
        for args, opts in spec:
            p.add_argument(*args, **opts)
        p.set_defaults(fn=fn)
        return p

    inst = (("--instance",), {"choices": ["dandi", "sandbox"], "default": "dandi"})
    add("fetch", cmd_fetch, (("id",), {}), (("--out",), {}), inst)
    add("audit", cmd_audit, (("ref",), {"help": "metadata JSON file or dandiset id"}), inst)
    add("paper", cmd_paper, (("doi",), {}))
    add("fulltext", cmd_fulltext, (("doi",), {}))
    add("search-paper", cmd_search_paper, (("query",), {}))
    add("ror", cmd_ror, (("text",), {}), (("--rows",), {"type": int, "default": 5}),
        (("--country",), {"default": "", "help": 'keep matches in this country, e.g. "United States"'}))
    add("orcid-search", cmd_orcid_search, (("--family",), {"required": True}), (("--given",), {"default": ""}),
        (("--rows",), {"type": int, "default": 10}))
    add("check-orcid", cmd_check_orcid, (("orcid",), {}), (("--name",), {"required": True, "help": "Family, Given"}),
        (("--metadata",), {}), (("--paper",), {"action": "append"}),
        (("--affiliation",), {"action": "append", "help": "an institution name or ROR tied to the team"}),
        (("--confirmed-by-user",), {"action": "store_true", "help": "the user confirmed an unconfirmed ORCID"}))
    add("ontology", cmd_ontology, (("term",), {}), (("--category",), {"choices": ["anatomy", "disorder", "cognitive"], "required": True}),
        (("--species",), {"default": ""}))
    add("import-authors", cmd_import_authors, (("doi",), {}), (("--metadata",), {"required": True}), (("--out",), {"required": True}),
        (("--order",), {"choices": ["existing", "paper"], "default": "existing",
                        "help": "existing: keep current order, new authors after the last person (default). "
                                "paper: authors first, in publication order (owner mode)"}))
    add("apply", cmd_apply, (("metadata",), {"help": "metadata JSON file to edit"}), (("ops",), {"help": "JSON list of operations, or -"}),
        (("--out",), {"help": "write here instead of editing in place"}),
        (("--owner",), {"action": "store_true", "help": "working for the owner, who approved set_text changes"}),
        description=APPLY_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    add("validate", cmd_validate, (("proposed",), {}), (("--base",), {}))
    add("diff", cmd_diff, (("original",), {}), (("proposed",), {}))
    add("save", cmd_save, (("id",), {}), (("proposed",), {}), (("--base",), {"required": True}), (("--yes",), {"action": "store_true"}),
        (("--force",), {"action": "store_true"}), inst)
    args = ap.parse_args(argv)
    try:
        result = args.fn(args)
    except RuntimeError as e:
        result = {"error": str(e)}
    json.dump(result, sys.stdout, indent=1, ensure_ascii=False, default=sorted)
    print()


if __name__ == "__main__":
    main()
