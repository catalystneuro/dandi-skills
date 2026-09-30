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
    import-authors DOI --metadata FILE --out FILE
                                                merge a paper's authors into the contributors
    validate PROPOSED [--base ORIGINAL]         schema errors, new errors, unverified identifiers
    diff ORIGINAL PROPOSED                      what changed, path by path
    save ID PROPOSED --base ORIGINAL [--yes]    write the draft metadata (needs DANDI_API_KEY)

Identifiers returned by the lookup commands (paper, search-paper, ror,
orcid-search, check-orcid, ontology, import-authors) are appended to a
session log, .dandiset-metadata/verified.txt in the working directory (or
$DANDISET_METADATA_DIR). `validate` flags any new DOI, ORCID, ROR or ontology
identifier in the proposal that no lookup returned, which is how an
identifier recalled from memory gets caught.

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
        ("no award number", not _s(f.get("awardNumber"))), ("no ROR", not ROR_PATTERN.match(_s(f.get("identifier")))),
        ("award number has a prefix", bool(re.match(r"(?i)\s*(grant|award|project|no\.?|number|#)", _s(f.get("awardNumber")))))) if bad)
        for f in funders]
    problems = [p for p in problems if not p.endswith(": ")]
    out.append(_item("funders", bool(funders) and not problems,
                     "; ".join(problems) if problems else (f"{len(funders)} funder entries." if funders else "No funder is listed.")))
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
    pubs = [r for r in res if extract_doi(_s(r.get("identifier")) + " " + _s(r.get("url")))]
    out.append(_item("publication", any(_s(r.get("relation")) for r in pubs),
                     f"{len(pubs)} related resources with a DOI" if pubs else
                     ("Related resources have no DOI." if res else "No related resources.")))
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
        _note(orcid, *[i["ror"] for i in insts])
        authors.append({"name": format_person_name(person.get("display_name") or ""), "orcid": orcid,
                        "position": a.get("author_position"), "corresponding": bool(a.get("is_corresponding")),
                        "raw_affiliations": a.get("raw_affiliation_strings") or [], "institutions": insts})
    funders = {f.get("id"): {"name": f.get("display_name"), "ror": f.get("ror")} for f in w.get("funders") or []}
    _note(*[f["ror"] for f in funders.values()])
    awards = []
    for a in w.get("awards") or []:
        f = funders.get(a.get("funder_id")) or {}
        awards.append({"funder": a.get("funder_display_name") or f.get("name"), "funder_ror": f.get("ror"),
                       "award_number": a.get("funder_award_id")})
    _note(doi)
    return {"doi": doi, "title": re.sub(r"<[^>]+>", "", w.get("title") or ""), "year": w.get("publication_year"),
            "type": w.get("type"), "venue": src.get("display_name"), "venue_type": src.get("type"),
            "authors": authors, "funders": list(funders.values()), "awards": awards,
            "note": "Funding in OpenAlex is often incomplete; check the paper's acknowledgments and ask the user."}


def openalex_work(doi):
    return _get(f"https://api.openalex.org/works/doi:{norm_doi(doi)}", _polite({"select": OPENALEX_SELECT}))


def crossref_work(doi):
    res = _get(f"https://api.crossref.org/works/{urllib.parse.quote(norm_doi(doi))}", _polite({}))
    return _crossref_compact(res["message"]) if res else None


def _crossref_compact(item):
    doi = norm_doi(item.get("DOI"))
    _note(doi)
    issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return {"doi": doi, "title": (item.get("title") or [""])[0], "journal": (item.get("container-title") or [""])[0],
            "type": item.get("type"), "year": issued, "publisher": item.get("publisher"),
            "authors": [f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in item.get("author") or []][:12],
            "funders": [{"name": f.get("name"), "awards": f.get("award") or []} for f in item.get("funder") or []]}


def cmd_paper(args):
    doi = extract_doi(args.doi) or norm_doi(args.doi)
    w = openalex_work(doi)
    out = _openalex_compact(w) if w else None
    cr = crossref_work(doi)
    if out is None and cr is None:
        return {"error": f"Neither OpenAlex nor Crossref has a record for {doi}. Check the DOI with the user."}
    if out is None:
        out = {"doi": doi, "note": "OpenAlex has no record; authors come from Crossref without ORCIDs or institutions."}
    if cr:
        out["crossref"] = {"title": cr["title"], "journal": cr["journal"], "type": cr["type"], "funders": cr["funders"]}
    out["related_resource"] = {"schemaKey": "Resource", "identifier": f"doi:{doi}", "url": f"https://doi.org/{doi}",
                               "name": out.get("title") or (cr or {}).get("title"),
                               "repository": out.get("venue") or (cr or {}).get("journal"),
                               "resourceType": "dcite:Preprint" if ((cr or {}).get("type") == "posted-content"
                                                                   or out.get("type") == "preprint") else "dcite:JournalArticle",
                               "relation": "dcite:IsDescribedBy (confirm: IsDocumentedBy for a data descriptor, IsCitedBy for a reuse)"}
    return out


def cmd_search_paper(args):
    cr = _get("https://api.crossref.org/works", _polite({"query.bibliographic": args.query, "rows": 5,
                                                         "select": "DOI,title,container-title,type,author,issued,publisher,funder"}))
    oa = _get("https://api.openalex.org/works", _polite({"search": args.query, "per_page": 5,
                                                         "select": "doi,title,publication_year,primary_location,authorships"}))
    oa_out = []
    for w in (oa or {}).get("results") or []:
        d = norm_doi(w.get("doi"))
        _note(d)
        first = next((a for a in w.get("authorships") or [] if a.get("author_position") == "first"), {})
        oa_out.append({"doi": d, "title": w.get("title"), "year": w.get("publication_year"),
                       "first_author": (first.get("author") or {}).get("display_name"),
                       "venue": (((w.get("primary_location") or {}).get("source")) or {}).get("display_name")})
    return {"crossref": [_crossref_compact(i) for i in ((cr or {}).get("message") or {}).get("items") or []],
            "openalex": oa_out,
            "note": "A search hit is a candidate, not a match. Link a paper only if the dandiset names it or the user confirms it."}


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
        if note:
            _note(org.get("id"))
        out.append({"ror": org.get("id"), "name": _ror_name(org), "country": country, "types": org.get("types"),
                    "chosen": bool(item.get("chosen")), "score": item.get("score")})
    return out


def cmd_ror(args):
    out = ror_affiliation(args.text)
    return {"matches": out[:args.rows],
            "note": "Use the entry marked chosen. Without one, check name and country with the user; many countries have an 'NIH'."}


def cmd_orcid_search(args):
    q = f'given-names:"{args.given}" AND family-name:"{args.family}"' if args.given else f'family-name:"{args.family}"'
    res = _get("https://pub.orcid.org/v3.0/expanded-search/", {"q": q, "rows": args.rows}) or {}
    out = []
    for r in res.get("expanded-result") or []:
        _note(r.get("orcid-id"))
        out.append({"orcid": r.get("orcid-id"), "given": r.get("given-names"), "family": r.get("family-names"),
                    "institutions": (r.get("institution-name") or [])[:6]})
    return {"num_found": res.get("num-found"), "candidates": out,
            "note": "Several candidates with the same name is the normal case. Run check-orcid on the one you pick."}


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
    m = _load_metadata(args.metadata) if args.metadata else {}
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
    others = {fold(c.get("name", "").split(",")[0]).strip() for c in _people(m) if fold(c.get("name", "")) != fold(args.name)}
    others = {f for f in others if len(f) >= 3}
    if prof["dois"] and others:
        dois = sorted(prof["dois"])[:100]
        coauthors = set()
        for i in range(0, len(dois), 50):
            res = _get("https://api.openalex.org/works", _polite({"filter": "doi:" + "|".join(dois[i:i + 50]),
                                                                  "select": "authorships", "per-page": 50})) or {}
            for w in res.get("results") or []:
                for a in w.get("authorships") or []:
                    coauthors.add(fold((a.get("author") or {}).get("display_name") or ""))
        who = next((f for f in others for n in coauthors if re.search(rf"\b{re.escape(f)}\b", n)), None)
        if who:
            _note(orcid)
            return {**base, "verdict": "corroborated", "reason": f"works on the record are co-authored with contributor '{who}'"}
    d = _get(f"https://api.openalex.org/authors/orcid:{orcid}", _polite({"select": "works_count,affiliations,last_known_institutions"})) or {}
    oa_insts = [(i.get("ror") or "", i.get("display_name") or "") for i in d.get("last_known_institutions") or []]
    oa_insts += [((a.get("institution") or {}).get("ror") or "", (a.get("institution") or {}).get("display_name") or "")
                 for a in d.get("affiliations") or []]
    if (d.get("works_count") or 0) >= 5 and any(shared(r, n) for r, n in oa_insts):
        _note(orcid)
        return {**base, "verdict": "corroborated", "reason": "an established OpenAlex profile at a contributor institution"}
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
            _note(d.get("iri"))
            out.append({"schemaKey": key, "identifier": d.get("iri"), "name": d.get("label"), "ontology": d.get("ontology_name"),
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
        out.append({"schemaKey": "Anatomy", "identifier": iri, "name": st["name"], "ontology": "MBA", "acronym": st["acronym"]})
    return out


def cognitive_atlas_search(term, limit=6):
    concepts = json.load(open(os.path.join(DATA, "cognitive_atlas_concepts.json")))["concepts"]
    t = term.lower().strip()

    def rank(c):
        name = (c.get("name") or "").lower()
        for i, ok in enumerate((name == t, name.startswith(t), t in name, t in (c.get("definition") or "").lower())):
            if ok:
                return i
        return None

    hits = sorted(((rank(c), c) for c in concepts if rank(c) is not None), key=lambda h: (h[0], len(h[1]["name"])))
    out = []
    for _, c in hits[:limit]:
        iri = f"https://www.cognitiveatlas.org/concept/id/{c['id']}"
        _note(iri)
        out.append({"schemaKey": "GenericType", "identifier": iri, "name": c["name"], "ontology": "cognitiveatlas",
                    "description": (c.get("definition") or "")[:160]})
    return out


def cmd_ontology(args):
    if args.category == "cognitive":
        return {"terms": cognitive_atlas_search(args.term)}
    if args.category == "anatomy":
        terms = (mba_search(args.term) if args.species == "mouse" else []) + ols_search(args.term, "anatomy")
        note = ("Mouse dataset: prefer the Allen Mouse Brain Atlas (MBA) term for a brain region; UBERON only when the atlas "
                "lacks it, CL for cell types." if args.species == "mouse" else
                "UBERON for anatomy, CL for cell types. For a mouse-only dataset rerun with --species mouse.")
        return {"terms": terms, "note": note}
    return {"terms": ols_search(args.term, "disorder")}


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

    Ported from the DANDI metadata editor's import_contributors_from_publication.
    People are matched by ORCID, then by family name plus first given name. A
    matched entry keeps everything it has and only gains a missing ORCID,
    missing affiliations, and the Author role. Unmatched existing contributors
    (funders, curators) follow the authors, so rerunning is idempotent.
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
            _note(orcid)
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

    remaining = [copy.deepcopy(c) for c in _arr(m.get("contributor"))]
    merged, added, matched, orcid_checks = [], [], [], []
    for p in imported:
        corresponding = p.pop("_corresponding")
        idx = -1
        if p.get("identifier"):
            idx = next((i for i, c in enumerate(remaining) if isinstance(c, dict) and c.get("schemaKey") == "Person"
                        and norm_orcid(_s(c.get("identifier"))) == p["identifier"]), -1)
        if idx == -1 and _name_key(p["name"]):
            idx = next((i for i, c in enumerate(remaining) if isinstance(c, dict) and c.get("schemaKey") == "Person"
                        and _name_key(_s(c.get("name"))) == _name_key(p["name"])), -1)
        if idx == -1:
            merged.append(p)
            added.append(p["name"] + (" (corresponding author)" if corresponding else ""))
            continue
        cur = remaining.pop(idx)
        if not cur.get("identifier") and p.get("identifier"):
            cur["identifier"] = p["identifier"]
            orcid_checks.append({"name": cur.get("name"), "orcid": p["identifier"]})
        roles = list(_arr(cur.get("roleName")))
        if "dcite:Author" not in roles:
            roles.append("dcite:Author")
        cur["roleName"] = roles
        existing = list(_arr(cur.get("affiliation")))
        keys = {(a.get("identifier") or fold(a.get("name", "")).strip()) for a in existing if isinstance(a, dict)}
        for aff in p.get("affiliation") or []:
            k = aff.get("identifier") or fold(aff["name"]).strip()
            if k not in keys:
                existing.append(aff)
                keys.add(k)
        if existing:
            cur["affiliation"] = existing
        merged.append(cur)
        matched.append(cur.get("name") + (" (corresponding author)" if corresponding else ""))
    out = copy.deepcopy(m)
    out["contributor"] = merged + remaining
    json.dump(out, open(args.out, "w"), indent=2, ensure_ascii=False)
    return {"paper": {"doi": doi, "title": w.get("title")}, "written": args.out, "added": added, "matched": matched,
            "carried_over": [c.get("name") for c in remaining if isinstance(c, dict)],
            "without_orcid": no_orcid, "unresolved_affiliations": unresolved,
            "check_orcid_for": orcid_checks,
            "note": "Authors are now first, in publication order. Review the added list with the user, run check-orcid on "
                    "ORCIDs that were added to existing people, and ask who the contact person is if none is set."}


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
        placeholders_before = set(_placeholders(base))
        out["placeholders"] = [p for p in _placeholders(prop) if p not in placeholders_before]
    else:
        out["placeholders"] = list(_placeholders(prop))
    people = _people(prop)
    contacts = [p for p in people if "dcite:ContactPerson" in _roles(p)]
    out["contact_person"] = [f"{p.get('name')} <{p.get('email') or 'NO EMAIL'}>" for p in contacts] or "none set"
    out["ok_to_save"] = not out.get("new_errors", []) and not out.get("unverified_identifiers") \
        and not out["placeholders"] and not out.get("removed_contributors")
    return out


def _key(x):
    return x.get("name") or x.get("url") or x.get("identifier")


def _keyed(items):
    keys = [_key(x) if isinstance(x, dict) else None for x in items]
    return all(keys) and len(set(keys)) == len(keys)


def _diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            p = f"{path}.{k}" if path else k
            if k not in b:
                yield f"- {p}: {json.dumps(a[k], ensure_ascii=False)[:300]}"
            elif k not in a:
                yield f"+ {p}: {json.dumps(b[k], ensure_ascii=False)[:300]}"
            else:
                yield from _diff(a[k], b[k], p)
    elif isinstance(a, list) and isinstance(b, list) and (a or b) and _keyed(a) and _keyed(b) \
            and any(isinstance(x, dict) for x in a + b):
        # contributors, resources, terms: match entries by name or identifier, not position
        ka, kb = {_key(x): x for x in a}, {_key(x): x for x in b}
        for k, x in ka.items():
            if k not in kb:
                yield f"- {path}[{k!r}]: {json.dumps(x, ensure_ascii=False)[:300]}"
            else:
                yield from _diff(x, kb[k], f"{path}[{k!r}]")
        for k, x in kb.items():
            if k not in ka:
                yield f"+ {path}[{k!r}]: {json.dumps(x, ensure_ascii=False)[:300]}"
        if [k for k in kb if k in ka] != [k for k in ka if k in kb]:
            yield f"~ {path}: existing entries were reordered"
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            p = f"{path}[{i}]"
            if i >= len(a):
                yield f"+ {p}: {json.dumps(b[i], ensure_ascii=False)[:300]}"
            elif i >= len(b):
                yield f"- {p}: {json.dumps(a[i], ensure_ascii=False)[:300]}"
            else:
                yield from _diff(a[i], b[i], p)
    elif a != b:
        yield f"~ {path}: {json.dumps(a, ensure_ascii=False)[:200]} -> {json.dumps(b, ensure_ascii=False)[:200]}"


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
    add("search-paper", cmd_search_paper, (("query",), {}))
    add("ror", cmd_ror, (("text",), {}), (("--rows",), {"type": int, "default": 5}))
    add("orcid-search", cmd_orcid_search, (("--family",), {"required": True}), (("--given",), {"default": ""}),
        (("--rows",), {"type": int, "default": 10}))
    add("check-orcid", cmd_check_orcid, (("orcid",), {}), (("--name",), {"required": True, "help": "Family, Given"}),
        (("--metadata",), {}), (("--paper",), {"action": "append"}),
        (("--affiliation",), {"action": "append", "help": "an institution name or ROR tied to the team"}))
    add("ontology", cmd_ontology, (("term",), {}), (("--category",), {"choices": ["anatomy", "disorder", "cognitive"], "required": True}),
        (("--species",), {"default": ""}))
    add("import-authors", cmd_import_authors, (("doi",), {}), (("--metadata",), {"required": True}), (("--out",), {"required": True}))
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
