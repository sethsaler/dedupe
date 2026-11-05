"""Content-aligned video review, with real ffmpeg media and hard negatives."""

import subprocess

import pytest

from dedupe.scanner import inventory
from dedupe.similar_video import (
    _version_candidate_pairs,
    compare_video_audio,
    ffmpeg_available,
    find_video_version_matches,
)


def _ffmpeg(*arguments):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", *map(str, arguments)],
        capture_output=True, check=True, timeout=30,
    )


@pytest.fixture(scope="module")
def version_media(tmp_path_factory):
    if not ffmpeg_available():
        pytest.skip("ffmpeg and ffprobe are required")
    folder = tmp_path_factory.mktemp("video-versions")
    original = folder / "original.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-threads", "1", original)
    trim = folder / "trim.mp4"
    _ffmpeg("-ss", "1.5", "-i", original, "-t", "4", "-threads", "1", trim)
    _ffmpeg("-ss", "1.37", "-i", original, "-t", "4.1", "-threads", "1",
            folder / "off-grid-trim.mp4")
    intro = folder / "intro.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:size=160x120:rate=12:duration=2",
            "-i", original, "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-an", "-threads", "1", intro)
    soundtrack = folder / "soundtrack.mp4"
    _ffmpeg("-i", original, "-f", "lavfi", "-i", "sine=frequency=880:duration=8",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-threads", "1", soundtrack)
    unrelated = folder / "unrelated.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x120:rate=12:duration=8",
            "-pix_fmt", "yuv420p", "-threads", "1", unrelated)
    _ffmpeg("-i", original, "-vf", "reverse", "-an", "-threads", "1", folder / "reversed.mp4")
    return folder


def test_trimmed_excerpt_and_added_intro_have_aligned_review_ranges(version_media, tmp_path):
    original = version_media / "original.mp4"
    trim = version_media / "trim.mp4"
    intro = version_media / "intro.mp4"
    matches = find_video_version_matches(
        inventory([original, trim, intro]), cache_path=tmp_path / "cache.sqlite3",
    )
    by_pair = {frozenset(r.path for r in records): details for records, details in matches}
    trimmed = by_pair[frozenset(map(str, (original, trim)))]
    assert trimmed["manual_review"] is True
    assert trimmed["relation"] == "excerpt"
    assert trimmed["ranges"][str(original)] == pytest.approx([1.5, 5.5], abs=0.15)
    assert trimmed["ranges"][str(trim)] == pytest.approx([0, 4], abs=0.15)
    assert trimmed["audio"] == "same"
    added = by_pair[frozenset(map(str, (original, intro)))]
    assert added["ranges"][str(intro)] == pytest.approx([2, 10], abs=0.15)
    assert added["audio"] == "different"


def test_different_soundtrack_is_manual_review_and_unrelated_video_is_rejected(version_media, tmp_path):
    original, soundtrack, unrelated = [version_media / f"{name}.mp4"
                                       for name in ("original", "soundtrack", "unrelated")]
    matches = find_video_version_matches(
        inventory([original, soundtrack, unrelated]), cache_path=tmp_path / "cache.sqlite3",
    )
    assert len(matches) == 1
    records, details = matches[0]
    assert {record.path for record in records} == {str(original), str(soundtrack)}
    assert details["audio"] == "different"
    assert details["relation"] == "same_footage"
    assert details["manual_review"] is True


def test_audio_failure_is_not_claimed_to_be_a_difference(tmp_path):
    a, b = map(str, (tmp_path / "missing-a.mp4", tmp_path / "missing-b.mp4"))
    assert compare_video_audio(a, b, {a: [0, 10], b: [0, 10]}) == "unavailable"


def test_reversed_footage_is_not_a_trimmed_copy(version_media, tmp_path):
    assert find_video_version_matches(
        inventory([version_media / "original.mp4", version_media / "reversed.mp4"]),
        cache_path=tmp_path / "cache.sqlite3",
    ) == []


def test_trim_alignment_is_not_limited_to_sample_grid(version_media, tmp_path):
    original = version_media / "original.mp4"
    trim = version_media / "off-grid-trim.mp4"
    matches = find_video_version_matches(
        inventory([original, trim]), cache_path=tmp_path / "cache.sqlite3",
    )
    assert len(matches) == 1
    assert matches[0][1]["ranges"][str(original)][0] == pytest.approx(1.37, abs=0.2)
    assert matches[0][1]["approximate_alignment"] is True


def test_warm_version_scan_reuses_samples_and_restores_duration(version_media, tmp_path, monkeypatch):
    import dedupe.similar_video as module

    paths = [version_media / "original.mp4", version_media / "trim.mp4"]
    cache = tmp_path / "cache.sqlite3"
    first = find_video_version_matches(inventory(paths), cache_path=cache)
    original = module._version_frame_batches
    requests = []

    def frames(path, timestamps, cancelled):
        requests.append(len(timestamps))
        return original(path, timestamps, cancelled)

    monkeypatch.setattr(module, "_version_frame_batches", frames)
    second = find_video_version_matches(inventory(paths), cache_path=cache)
    assert first[0][1] == second[0][1]
    assert requests and all(count == 7 for count in requests)


def test_frequent_generic_frames_do_not_form_unbounded_candidate_join():
    assert _version_candidate_pairs([[(t, 123) for t in range(96)] for _ in range(20)]) == {}


def test_version_pass_cancels_before_reading_files(tmp_path):
    records = inventory([])
    from dedupe.models import FileRecord, MediaType
    for index in range(2):
        records.append(FileRecord(
            path=str(tmp_path / f"{index}.mp4"), size=1, mtime=1,
            media_type=MediaType.VIDEO, extension=".mp4",
        ))
    with pytest.raises(InterruptedError):
        find_video_version_matches(records, cache_path=tmp_path / "cache.db", cancelled=lambda: True)


def test_version_subprocess_cancellation_terminates_running_decoder():
    import sys
    import time

    from dedupe.similar_video import _run_video_command

    started = time.monotonic()
    with pytest.raises(InterruptedError):
        _run_video_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cancelled=lambda: time.monotonic() - started > 0.15,
        )
    assert time.monotonic() - started < 3


@pytest.mark.e2e
def test_video_ranges_and_offset_synchronization_in_browser(page, version_media, tmp_path):
    import threading

    from playwright.sync_api import expect
    from werkzeug.serving import make_server

    from dedupe.models import GroupKind, MediaType, ReviewGroup, ScanResult
    from dedupe.web.app import create_app

    original = str(version_media / "original.mp4")
    trimmed = str(version_media / "trim.mp4")
    records = inventory([original, trimmed])
    group = ReviewGroup(
        "version-review", GroupKind.SIMILAR, MediaType.VIDEO, records,
        suggested_keep=original, match_basis="video_version",
        match_details={
            "ranges": {original: [1.5, 5.5], trimmed: [0, 4]},
            "audio": "same", "relation": "excerpt", "manual_review": True,
        },
    )
    app = create_app(
        ScanResult(roots=[str(version_media)], files=records, groups=[group]),
        review_session_path=tmp_path / "review.json", cache_path=tmp_path / "hash.sqlite3",
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page.add_init_script("localStorage.setItem('dedupe.similarView', 'swipe')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator('.tab[data-kind="similar"]').click()
        expect(page.locator(".video-match-ranges")).to_contain_text("1.5")
        expect(page.locator(".video-match-ranges")).to_contain_text("Sampled audio agrees")
        page.get_by_role("button", name="Jump to matching section").click()
        page.wait_for_function(
            "() => Math.abs(document.querySelector('.swipe-keeper video').currentTime - 1.5) < 0.2"
        )
        page.locator("#syncVideos").check()
        page.locator(".swipe-top video").evaluate("video => { video.currentTime = 1; }")
        page.wait_for_function(
            "() => Math.abs(document.querySelector('.swipe-keeper video').currentTime - 2.5) < 0.2"
        )
        assert group.selected_for_removal == []
        assert errors == []
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_disabled_video_cache_never_opens_or_creates_cache(version_media, tmp_path, monkeypatch):
    import dedupe.similar_video as module

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("--no-cache must not open a database")

    monkeypatch.setattr(module.sqlite3, "connect", forbidden_connect)
    cache = tmp_path / "absent" / "versions.sqlite3"
    matches = find_video_version_matches(
        inventory([version_media / "original.mp4", version_media / "trim.mp4"]),
        cache_path=cache, use_cache=False,
    )
    assert len(matches) == 1
    assert not cache.parent.exists()


@pytest.mark.parametrize("failure", ["corrupt", "invalid_parent", "read", "write", "locked"])
def test_optional_video_cache_failure_falls_back_to_uncached_analysis(
    version_media, tmp_path, monkeypatch, failure,
):
    import sqlite3

    import dedupe.similar_video as module

    cache = tmp_path / "versions.sqlite3"
    held = None
    if failure == "corrupt":
        cache.write_bytes(b"corrupt database")
    elif failure == "invalid_parent":
        parent = tmp_path / "not-a-directory"
        parent.write_text("file")
        cache = parent / "cache.sqlite3"
    else:
        held = sqlite3.connect(cache)
        held.execute("CREATE TABLE samples(identity TEXT, " +
                     ("wrong_column TEXT)" if failure == "read" else "samples TEXT)"))
        held.commit()
        if failure == "write":
            held.execute("PRAGMA query_only = ON")
            monkeypatch.setattr(module.sqlite3, "connect", lambda *args, **kwargs: held)
        elif failure == "locked":
            held.execute("BEGIN EXCLUSIVE")
        else:
            held.close()
            held = None
    errors = []
    try:
        matches = find_video_version_matches(
            inventory([version_media / "original.mp4", version_media / "trim.mp4"]),
            cache_path=cache, errors=errors,
        )
        assert len(matches) == 1
        assert len(errors) == 1
        assert "cache unavailable; continued uncached" in errors[0]
    finally:
        if held is not None:
            held.close()
