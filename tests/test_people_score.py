"""People-likelihood scoring: model, ordering, cache, CLI, benchmark, and web API."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from dedupe import cli
from dedupe.cache import HashCache
from dedupe.human_benchmark import run_people_rank_benchmark, summarize_ranking
from dedupe.models import FileRecord, MediaType, ScanResult
from dedupe.people_score import (
    PEOPLE_MODEL_PATH,
    PEOPLE_MODEL_SHA256,
    has_current_people_score,
    least_likely_first_key,
    people_score_signature,
    score_people,
)
from dedupe.scanner import inventory
from dedupe.web.app import create_app

ASTRONAUT = Path(__file__).parent / "fixtures" / "astronaut.png"
cv2 = pytest.importorskip("cv2")


def _no_people_image(path: Path) -> Path:
    """A plain still life: sky gradient, a box, and a ball — no people."""
    image = Image.new("RGB", (480, 360))
    draw = ImageDraw.Draw(image)
    for y in range(360):
        draw.line([(0, y), (480, y)], fill=(120 + y // 6, 160 + y // 8, 220))
    draw.rectangle((60, 220, 200, 330), fill=(150, 90, 40))
    draw.ellipse((300, 240, 380, 320), fill=(220, 40, 40))
    image.save(path)
    return path


@pytest.fixture
def media(tmp_path: Path) -> Path:
    folder = tmp_path / "media"
    folder.mkdir()
    shutil.copy(ASTRONAUT, folder / "astronaut.png")
    _no_people_image(folder / "still-life.png")
    return folder


def _scored(folder: Path) -> dict[str, float | None]:
    records = inventory([folder])
    score_people(records, workers=2)
    return {Path(record.path).name: record.people_score for record in records}


def test_bundled_model_is_present_and_pinned() -> None:
    import hashlib

    assert PEOPLE_MODEL_PATH.is_file()
    with PEOPLE_MODEL_PATH.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == PEOPLE_MODEL_SHA256
    assert PEOPLE_MODEL_SHA256[:12] in people_score_signature()


def test_a_person_scores_far_above_a_people_free_photo(media: Path) -> None:
    scores = _scored(media)

    assert scores["astronaut.png"] > 0.5
    assert scores["still-life.png"] < 0.1


def test_least_likely_first_ordering_puts_unscored_files_last() -> None:
    def record(name: str, score: float | None) -> FileRecord:
        return FileRecord(
            path=f"/m/{name}", size=1, mtime=0.0, media_type=MediaType.IMAGE,
            extension=".jpg", people_score=score,
        )

    ordered = sorted(
        [record("b", 0.9), record("c", None), record("a", 0.01), record("d", 0.01)],
        key=least_likely_first_key,
    )

    assert [Path(item.path).name for item in ordered] == ["a", "d", "b", "c"]


def test_current_scores_are_reused_and_stale_scores_recomputed(media: Path) -> None:
    records = inventory([media])
    current, stale = records
    current.people_score = 0.42
    current.people_score_signature = people_score_signature()
    stale.people_score = 0.42
    stale.people_score_signature = "people-score-v0|old-model"

    scored_now = score_people(records, workers=1)

    assert scored_now == [stale]
    assert current.people_score == 0.42
    assert stale.people_score != 0.42
    assert has_current_people_score(stale)


def test_unreadable_media_is_left_unscored(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    records = inventory([tmp_path])

    score_people(records, workers=1)

    assert records[0].people_score is None
    assert records[0].people_score_signature is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_videos_score_the_most_person_like_sampled_frame(tmp_path: Path) -> None:
    video = tmp_path / "astronaut.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-loop", "1", "-i", str(ASTRONAUT), "-t", "2",
         "-vf", "scale=256:256", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    records = inventory([tmp_path])

    score_people(records, workers=1)

    assert records[0].media_type == MediaType.VIDEO
    assert records[0].people_score is not None and records[0].people_score > 0.5


def test_scores_round_trip_through_the_hash_cache_until_the_file_changes(
    media: Path, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    records = inventory([media])
    score_people(records, workers=1)
    cache = HashCache(cache_path)
    cache.store_all(records)
    cache.close()

    fresh = inventory([media])
    cache = HashCache(cache_path)
    cache.hydrate(fresh)
    cache.close()
    assert {r.path: r.people_score for r in fresh} == {r.path: r.people_score for r in records}
    assert all(has_current_people_score(record) for record in fresh)

    still_life = media / "still-life.png"
    time.sleep(0.01)
    _no_people_image(still_life)
    with still_life.open("ab") as handle:
        handle.write(b"\0")
    changed = inventory([media])
    cache = HashCache(cache_path)
    cache.hydrate(changed)
    cache.close()
    by_name = {Path(record.path).name: record for record in changed}
    assert by_name["still-life.png"].people_score is None
    assert by_name["astronaut.png"].people_score is not None


def test_summarize_ranking_reports_order_quality() -> None:
    perfect = summarize_ranking(
        [
            {"path": "a", "has_person": False, "score": 0.01},
            {"path": "b", "has_person": False, "score": 0.02},
            {"path": "c", "has_person": True, "score": 0.9},
            {"path": "d", "has_person": True, "score": None},
        ]
    )
    assert perfect["nonhuman_average_precision"] == 1.0
    assert perfect["auc"] == 1.0
    assert perfect["clean_run"] == 2
    assert perfect["errors"] == 1

    # A person scored lower than every people-free file ruins the clean run;
    # ties break pessimistically (the person sorts first).
    tied = summarize_ranking(
        [
            {"path": "a", "has_person": False, "score": 0.1},
            {"path": "c", "has_person": True, "score": 0.1},
        ]
    )
    assert tied["clean_run"] == 0
    assert tied["auc"] == 0.5
    assert tied["lowest_scored_people"] == [{"path": "c", "score": 0.1}]


def test_people_rank_benchmark_runs_a_labeled_manifest(media: Path) -> None:
    manifest = media / "labels.json"
    manifest.write_text(
        json.dumps(
            [
                {"path": "astronaut.png", "has_person": True},
                {"path": "still-life.png", "has_person": False},
            ]
        )
    )

    report = run_people_rank_benchmark(manifest, workers=1)

    assert report["evaluated"] == 2
    assert report["nonhuman_average_precision"] == 1.0
    assert report["scorer"] == people_score_signature()


def test_rank_people_cli_lists_least_likely_first(media: Path, capsys) -> None:
    assert cli.main(["rank-people", str(media), "--no-cache"]) == 0
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert [line.split()[-1].rsplit("/", 1)[-1] for line in lines] == [
        "still-life.png",
        "astronaut.png",
    ]
    assert "2 of 2 media files listed" in out.err

    assert cli.main(["rank-people", str(media), "--no-cache", "--max-score", "0.1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [Path(item["path"]).name for item in payload["files"]] == ["still-life.png"]
    assert payload["scanned"] == 2


def test_rank_people_cli_uses_and_fills_the_cache(media: Path, tmp_path: Path, capsys) -> None:
    assert cli.main(["rank-people", str(media)]) == 0
    capsys.readouterr()
    cache = HashCache(tmp_path / "hashes.sqlite3")  # conftest's isolated default
    records = inventory([media])
    assert cache.hydrate(records) == 2
    cache.close()
    assert all(has_current_people_score(record) for record in records)


def test_rank_people_cli_reports_missing_paths(tmp_path: Path, capsys) -> None:
    assert cli.main(["rank-people", str(tmp_path / "nope"), "--no-cache"]) == 2
    assert "no such file or folder" in capsys.readouterr().err


def _files_app(media: Path, tmp_path: Path):
    records = inventory([media])
    result = ScanResult(roots=[str(media)], files=records, groups=[])
    app = create_app(result, review_session_path=tmp_path / "review.json")
    app.config["DEDUPE_CACHE_PATH"] = str(tmp_path / "web-cache.sqlite3")
    return app


def _wait_for_scoring(client) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = client.get("/api/people-scores").get_json()
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("people scoring did not finish")


def test_web_scores_a_group_on_demand_and_caches_it(media: Path, tmp_path: Path) -> None:
    app = _files_app(media, tmp_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    status = client.get("/api/status").get_json()
    assert status["capabilities"]["people_score"] is True
    groups = client.get("/api/groups").get_json()["groups"]
    files_group = next(group for group in groups if group["kind"] == "all_files")
    assert all(member["people_score"] is None for member in files_group["members"])

    started = client.post(
        "/api/people-scores",
        json={"scan_id": status["scan_id"], "group_id": files_group["id"]},
        headers=headers,
    )
    assert started.status_code == 202
    done = _wait_for_scoring(client)
    assert done["error"] is None
    assert done["done"] == done["total"] == 2

    group = client.get(f"/api/groups/{files_group['id']}").get_json()
    scores = {Path(m["path"]).name: m["people_score"] for m in group["members"]}
    assert scores["still-life.png"] < 0.1 < scores["astronaut.png"]

    cache = HashCache(tmp_path / "web-cache.sqlite3")
    fresh = inventory([media])
    assert cache.hydrate(fresh) == 2
    cache.close()

    # Everything is scored now: a second request finishes immediately.
    again = client.post(
        "/api/people-scores",
        json={"scan_id": status["scan_id"], "group_id": files_group["id"]},
        headers=headers,
    )
    assert again.status_code == 200
    assert again.get_json()["running"] is False


def test_web_hides_scores_from_an_older_model(media: Path, tmp_path: Path) -> None:
    app = _files_app(media, tmp_path)
    result = app.extensions["dedupe_state"]["result"]
    for record in result.files:
        record.people_score = 0.5
        record.people_score_signature = "people-score-v0|old-model"
    client = app.test_client()

    groups = client.get("/api/groups").get_json()["groups"]
    files_group = next(group for group in groups if group["kind"] == "all_files")

    assert all(member["people_score"] is None for member in files_group["members"])


def test_web_refuses_stale_or_unknown_scoring_requests(media: Path, tmp_path: Path) -> None:
    app = _files_app(media, tmp_path)
    client = app.test_client()
    headers = {"X-Dedupe-Token": app.config["DEDUPE_CSRF_TOKEN"]}
    scan_id = client.get("/api/status").get_json()["scan_id"]

    stale = client.post(
        "/api/people-scores", json={"scan_id": "old", "group_id": "x"}, headers=headers
    )
    assert stale.status_code == 409
    missing = client.post(
        "/api/people-scores", json={"scan_id": scan_id, "group_id": "x"}, headers=headers
    )
    assert missing.status_code == 404
    no_token = client.post("/api/people-scores", json={"scan_id": scan_id, "group_id": "x"})
    assert no_token.status_code == 403

    app.extensions["dedupe_state"]["scanning"] = True
    busy = client.post(
        "/api/people-scores", json={"scan_id": scan_id, "group_id": "x"}, headers=headers
    )
    assert busy.status_code == 409
