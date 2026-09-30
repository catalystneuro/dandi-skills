# Asset-Level Subject Matter

Most asset metadata (species, sex, age, approach, measurement technique,
variables measured) is extracted from the NWB files when they are uploaded, and
the way to change it is to fix the NWB files and upload again. The one field
worth setting afterwards is `about` on each asset: the brain regions recorded
in that file, when they differ between subjects or sessions. The
dandiset-level `about` then carries the union.

## Build a Subject to Region Map

Ask the user which regions each subject was recorded from, or read it from the
files when `electrodes.location` or `ImagingPlane.location` is filled in:

```python
from pathlib import Path
from pynwb import NWBHDF5IO

subject_regions = {}
for path in sorted(Path("path/to/dandiset").rglob("*.nwb")):
    with NWBHDF5IO(str(path), "r") as io:
        nwb = io.read()
        subject = nwb.subject.subject_id if nwb.subject else None
        regions = set()
        if nwb.electrodes is not None and "location" in nwb.electrodes.colnames:
            regions |= {loc for loc in nwb.electrodes["location"].data[:] if loc and loc != "unknown"}
        for plane in nwb.imaging_planes.values():
            if plane.location and plane.location != "unknown":
                regions.add(plane.location)
        if subject and regions:
            subject_regions.setdefault(subject, set()).update(regions)
```

## Resolve Each Region Once

Run the `ontology` command for each distinct region name (with
`--species mouse` for mouse data), show the user the candidates, and record the
chosen `{"schemaKey": "Anatomy", "name": ..., "identifier": ...}` for each.
Use the full IRI the command returns.

## Write It to Each Asset

Each asset is updated with a PUT of its full metadata and its blob id. Every
PUT creates a new asset version with a new asset id, so list the assets again
before a second pass. Show the user the planned subject-to-term mapping before
running this; it changes every matching asset.

```python
import os
import requests

API = "https://api.dandiarchive.org/api"  # or https://api.sandbox.dandiarchive.org/api
HEADERS = {"Authorization": f"token {os.environ['DANDI_API_KEY']}"}
DANDISET = "000123"
region_terms = {...}  # region name -> {"schemaKey": "Anatomy", "name": ..., "identifier": ...}

url = f"{API}/dandisets/{DANDISET}/versions/draft/assets/"
assets = []
while url:
    page = requests.get(url, headers=HEADERS).json()
    assets += page["results"]
    url = page.get("next")

for asset in assets:
    path = asset["path"]
    subject = path.split("/")[0].removeprefix("sub-") if path.startswith("sub-") else None
    terms = [region_terms[r] for r in sorted(subject_regions.get(subject, ())) if r in region_terms]
    if not terms:
        continue
    meta = requests.get(f"{API}/assets/{asset['asset_id']}/", headers=HEADERS).json()
    meta["about"] = terms
    r = requests.put(f"{API}/dandisets/{DANDISET}/versions/draft/assets/{asset['asset_id']}/",
                     headers=HEADERS, json={"metadata": meta, "blob_id": asset["blob"]})
    print(path, r.status_code, [t["name"] for t in terms])
```

Spot-check a few assets afterwards by fetching `{API}/assets/{asset_id}/` for
the new ids and reading `about`.
