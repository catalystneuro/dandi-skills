"""Tests for the parts of dandiset_metadata.py that need no network.

Run with `pytest dandiset-metadata/tests` (needs dandischema).
"""

import argparse
import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import dandiset_metadata as dm  # noqa: E402


def base_metadata():
    return {
        "schemaKey": "Dandiset",
        "name": "Test",
        "description": "Test dandiset",
        "contributor": [
            {"schemaKey": "Person", "name": "Gonzalez, Alex", "roleName": ["dcite:ContactPerson"], "email": "a@example.org",
             "affiliation": [{"schemaKey": "Affiliation", "name": "Allen Institute for Brain Science"}]},
            {"schemaKey": "Organization", "name": "National Institute of Mental Health", "roleName": ["dcite:Funder"],
             "awardNumber": "R00MH118422, R01MH129582"},
        ],
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "SESSION_DIR", str(tmp_path / ".dandiset-metadata"))
    monkeypatch.setattr(dm, "ror_status", lambda ror: ("inactive", "https://ror.org/03cpe7c52")
                        if ror == "https://ror.org/00dcv1019" else ("active", None))

    def fail(*a, **k):
        raise AssertionError("network access in an offline test")

    monkeypatch.setattr(dm, "_get", fail)


def test_format_person_name():
    assert dm.format_person_name("Lisa M. Giocomo") == "Giocomo, Lisa M."
    assert dm.format_person_name("Matthijs van der Meer") == "van der Meer, Matthijs"
    assert dm.format_person_name("Li, Nuo") == "Li, Nuo"


def test_same_person_loose():
    assert dm._same_person_loose("Gonzalez, Alex", "Gonzalez, Alexander")
    assert dm._same_person_loose("Li, N.", "Li, Nuo")
    assert not dm._same_person_loose("Li, Nuo", "Li, Na")
    assert not dm._same_person_loose("Gonzalez, Alex", "Garcia, Alex")


def test_group_awards():
    nimh = "https://ror.org/04xeg9z08"
    groups = dm.group_awards([("NIMH", nimh, "1F32MH119766"), ("NIMH", nimh, "F32 MH119766"),
                              ("NIMH", nimh, "MH126904"), ("NIMH", nimh, "R01MH126904")])
    assert [g["award_numbers"] for g in groups] == [["1F32MH119766", "F32 MH119766"], ["MH126904", "R01MH126904"]]


def test_extract_doi():
    assert dm.extract_doi("see https://doi.org/10.1101/2022.12.15.520660v2.") == "10.1101/2022.12.15.520660"
    assert dm.extract_doi("doi:10.7554/eLife.85646") == "10.7554/elife.85646"


def test_merge_affiliation_attaches_ror_to_named_entry():
    affs = [{"schemaKey": "Affiliation", "name": "Allen Institute for Brain Science"}]
    assert dm.merge_affiliation(affs, {"schemaKey": "Affiliation", "name": "Allen Institute",
                                       "identifier": "https://ror.org/03cpe7c52"}) == "ror added"
    assert len(affs) == 1 and affs[0]["identifier"] == "https://ror.org/03cpe7c52"
    assert dm.merge_affiliation(affs, {"schemaKey": "Affiliation", "name": "Stanford University",
                                       "identifier": "https://ror.org/00f54p054"}) == "added"
    assert len(affs) == 2


def apply(m, ops):
    applied, skipped = [], []
    for op in ops:
        try:
            applied.append(dm.apply_op(m, op))
        except dm.Skip as e:
            skipped.append(str(e))
    return applied, skipped


def test_apply_split_and_set_funder():
    m = base_metadata()
    applied, skipped = apply(m, [
        {"op": "split_funder", "contributor": "National Institute of Mental Health", "award_numbers": ["R00MH118422", "R01MH129582"]},
        {"op": "set_funder", "contributor": "National Institute of Mental Health", "ror": "https://ror.org/04xeg9z08"},
    ])
    assert not skipped, skipped
    funders = [c for c in m["contributor"] if c["schemaKey"] == "Organization"]
    assert [f["awardNumber"] for f in funders] == ["R00MH118422", "R01MH129582"]
    assert all(f["identifier"] == "https://ror.org/04xeg9z08" for f in funders)


def test_apply_rejects_bad_values():
    m = base_metadata()
    applied, skipped = apply(m, [
        {"op": "add_about", "schemaKey": "Anatomy", "identifier": "UBERON:0002421", "name": "hippocampal formation"},
        {"op": "add_keywords", "keywords": ["neuroscience", "N/A"]},
        {"op": "add_funder", "name": "Allen Institute", "ror": "https://ror.org/00dcv1019"},
        {"op": "add_person", "name": "Giocomo Lisa", "roles": ["dcite:Author"]},
        {"op": "add_resource", "doi": "10.7554/elife.85646", "relation": "dcite:IsRelatedTo"},
        {"op": "add_role", "contributor": "Gonzalez, Alex", "role": "dcite:Author"},
        {"op": "add_funder", "name": "Scott Alan Myers Endowed Professorship", "no_ror": True},
        {"op": "add_funder", "name": "Scott Alan Myers Endowed Professorship", "no_ror": True},
    ])
    assert applied == ["Gonzalez, Alex: role dcite:Author", "funder Scott Alan Myers Endowed Professorship"]
    assert len(skipped) == 6
    assert any("inactive" in s for s in skipped)


def test_validate_flags_unverified_duplicates_and_placeholders(tmp_path):
    base = base_metadata()
    prop = copy.deepcopy(base)
    prop["contributor"].insert(1, {"schemaKey": "Person", "name": "Gonzalez, Alexander", "identifier": "0000-0002-1825-0097",
                                   "roleName": ["dcite:Author"]})
    prop["keywords"] = ["TBD"]
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "p.json").write_text(json.dumps(prop))
    out = dm.cmd_validate(argparse.Namespace(proposed=str(tmp_path / "p.json"), base=str(tmp_path / "b.json")))
    assert out["unverified_identifiers"] == ["ORCID 0000-0002-1825-0097"]
    assert out["possible_duplicates"] == ["Gonzalez, Alex / Gonzalez, Alexander"]
    assert out["placeholders"] == ["keywords.0: 'TBD'"]
    assert "reordered_contributors" not in out
    assert out["ok_to_save"] is False


def test_validate_detects_reordering(tmp_path):
    base = base_metadata()
    prop = copy.deepcopy(base)
    prop["contributor"].reverse()
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "p.json").write_text(json.dumps(prop))
    out = dm.cmd_validate(argparse.Namespace(proposed=str(tmp_path / "p.json"), base=str(tmp_path / "b.json")))
    assert "reordered_contributors" in out


def test_diff_reports_roles_as_added_and_removed():
    a = base_metadata()
    b = copy.deepcopy(a)
    b["contributor"][0]["roleName"] = ["dcite:Author", "dcite:ContactPerson"]
    changes = list(dm._diff(a, b))
    assert changes == ["+ contributor['Gonzalez, Alex'].roleName: \"dcite:Author\""]


def test_audit_counts_only_publications():
    m = base_metadata()
    m["relatedResource"] = [{"schemaKey": "Resource", "identifier": "doi:10.5281/zenodo.7302777",
                             "url": "https://doi.org/10.5281/zenodo.7302777", "relation": "dcite:IsSupplementedBy",
                             "resourceType": "dcite:Software"}]
    pub = next(i for i in dm.audit(m) if i["id"] == "publication")
    assert pub["status"] == "fail"
