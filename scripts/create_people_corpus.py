#!/usr/bin/env python3
"""Build the seeded COCO val2017 people / no-people corpus used to tune people scores.

Writes ``dev.json`` (tune on this) and ``test.json`` (held out; report this) in the
``benchmark-humans`` / ``benchmark-people`` manifest format, plus ``img/``. Each split
is half people-free images (annotated, but with no person) and half images with
people, stratified by the largest person's share of the frame: tiny < 0.5%,
small < 3%, medium < 15%, large. Downloads the COCO annotations (~240 MB zip) and
the sampled images (~160 MB for the default 500 per split). COCO images carry
their own Flickr licenses; keep the corpus local.

    python scripts/create_people_corpus.py /tmp/people-corpus
    dedupe benchmark-people /tmp/people-corpus/dev.json
"""

from __future__ import annotations

import argparse
import json
import random
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ANNOTATIONS_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
ANNOTATIONS_MEMBER = "annotations/instances_val2017.json"
SEED = 20261106
PERSON_CATEGORY = 1


def bucket(area_fraction: float) -> str:
    if area_fraction < 0.005:
        return "tiny"
    if area_fraction < 0.03:
        return "small"
    if area_fraction < 0.15:
        return "medium"
    return "large"


def load_annotations(output: Path) -> dict:
    target = output / "instances_val2017.json"
    if not target.exists():
        archive = output / "annotations_trainval2017.zip"
        if not archive.exists():
            print(f"Downloading {ANNOTATIONS_URL}…")
            urllib.request.urlretrieve(ANNOTATIONS_URL, archive)
        with zipfile.ZipFile(archive) as bundle:
            target.write_bytes(bundle.read(ANNOTATIONS_MEMBER))
        archive.unlink()
    return json.loads(target.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--per-split", type=int, default=500)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    (output / "img").mkdir(parents=True, exist_ok=True)

    data = load_annotations(output)
    images = {image["id"]: image for image in data["images"]}
    person_area: dict[int, float] = defaultdict(float)
    annotated: set[int] = set()
    for annotation in data["annotations"]:
        annotated.add(annotation["image_id"])
        if annotation["category_id"] == PERSON_CATEGORY:
            image = images[annotation["image_id"]]
            fraction = annotation["area"] / (image["width"] * image["height"])
            person_area[image["id"]] = max(person_area[image["id"]], fraction)

    strata: dict[str, list[int]] = defaultdict(list)
    for image_id in images:
        if image_id in person_area:
            strata[bucket(person_area[image_id])].append(image_id)
        elif image_id in annotated:
            strata["none"].append(image_id)
    rng = random.Random(SEED)
    for members in strata.values():
        rng.shuffle(members)

    n = args.per_split
    quota = {"none": n // 2, "tiny": n // 8, "small": n // 8, "medium": n // 8,
             "large": n // 2 - 3 * (n // 8)}
    downloads: list[tuple[str, Path]] = []
    for index, split in enumerate(("dev", "test")):
        items = []
        for stratum, count in quota.items():
            for image_id in strata[stratum][index * count:(index + 1) * count]:
                image = images[image_id]
                relative = f"img/{image['file_name']}"
                items.append({
                    "path": relative,
                    "has_person": stratum != "none",
                    "stratum": stratum,
                    "person_area": round(person_area.get(image_id, 0.0), 5),
                })
                downloads.append((image["coco_url"], output / relative))
        (output / f"{split}.json").write_text(json.dumps({"items": items}, indent=1))

    pending = [(url, path) for url, path in downloads if not path.exists()]
    print(f"Downloading {len(pending)} of {len(downloads)} images…")

    def fetch(job: tuple[str, Path]) -> None:
        url, path = job
        partial = path.with_suffix(".part")
        urllib.request.urlretrieve(url, partial)
        partial.rename(path)

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(fetch, pending))
    print(f"Wrote {output / 'dev.json'} and {output / 'test.json'}")


if __name__ == "__main__":
    main()
