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
            real_connect = sqlite3.connect
            monkeypatch.setattr(
                module.sqlite3,
                "connect",
                lambda *args, **kwargs: real_connect(*args, **{**kwargs, "timeout": 0.05}),
            )
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


def test_parallel_video_versions_matches_serial_and_skips_exact_copy_redecode(
    version_media, tmp_path, monkeypatch
) -> None:
    import shutil
    import threading

    import dedupe.similar_video as module

    work_dir = tmp_path / "copies"
    work_dir.mkdir()
    orig = work_dir / "original.mp4"
    trim = work_dir / "trim.mp4"
    intro = work_dir / "intro.mp4"
    exact_copy = work_dir / "original_exact_copy.mp4"
    shutil.copy2(version_media / "original.mp4", orig)
    shutil.copy2(version_media / "trim.mp4", trim)
    shutil.copy2(version_media / "intro.mp4", intro)
    shutil.copy2(orig, exact_copy)

    serial_matches = find_video_version_matches(
        inventory([orig, trim, intro]),
        workers=1,
        use_cache=False,
    )

    real_batches = module._version_frame_batches
    sample_calls: list[str] = []
    lock = threading.Lock()

    def counted_batches(path: str, timestamps: list[float], cancelled):
        if len(timestamps) != 7:
            with lock:
                sample_calls.append(str(path))
        return real_batches(path, timestamps, cancelled)

    monkeypatch.setattr(module, "_version_frame_batches", counted_batches)

    parallel_matches = find_video_version_matches(
        inventory([orig, exact_copy, trim, intro]),
        workers=4,
        use_cache=False,
    )

    def normalize(matches):
        return sorted(
            (tuple(sorted(r.path for r in pair)), details["relation"], details["audio"])
            for pair, details in matches
            if exact_copy.name not in pair[0].path and exact_copy.name not in pair[1].path
        )

    assert normalize(parallel_matches) == normalize(serial_matches)
    # Dense sampling must run once across {original, original_exact_copy}.
    orig_family_calls = [p for p in sample_calls if orig.name in p or exact_copy.name in p]
    assert len(orig_family_calls) == 1


def test_version_candidate_pairs_counts_distinct_videos_not_raw_frames():
    # 10 versions of a slow/tripod video each repeating 3 hashes 32 times (320 > 256 raw occurrences)
    # must NOT drop matches, while 300 distinct videos sharing a generic hash ARE capped.
    h1, h2, h3 = 0x1111111111111111, 0x2222222222222222, 0x3333333333333333
    slow_timeline = (
        [(t * 0.5, h1) for t in range(32)]
        + [(16.0 + t * 0.5, h2) for t in range(32)]
        + [(32.0 + t * 0.5, h3) for t in range(32)]
    )
    pairs = _version_candidate_pairs([slow_timeline for _ in range(10)])
    assert len(pairs) == 45  # all 10*9/2 pairs kept

    # 270 distinct videos sharing a single generic hash h1 (> 256 distinct videos) are capped.
    generic = [[(0.0, h1), (1.0, 0x4444444444444444 + i), (2.0, 0x5555555555555555 + i)]
               for i in range(270)]
    # Only h1 is shared across 270 distinct videos; with h1 dropped, <3 votes remain per pair.
    assert _version_candidate_pairs(generic) == {}


def test_verify_video_offset_survives_hdr_sdr_luma_shift_and_rejects_two_frame_diversity(
    tmp_path, monkeypatch
):
    import numpy as np

    import dedupe.similar_video as module
    from dedupe.models import FileRecord, MediaType

    # Create 7 genuinely distinct 32x32 patterns with real spatial variance.
    base_frames = []
    sdr_frames = []
    other_frames = []
    yy, xx = np.mgrid[0:32, 0:32]
    for idx in range(7):
        pat = ((xx * (idx * 7 + 5) + yy * (idx * 11 + 9)) % 256).astype(float)
        u8_a = pat.astype(np.uint8)
        # Simulated tone-mapping + TV-range compression: raw MAD ~23 (> 18) yet high NCC.
        u8_b = (28.0 + 190.0 * ((pat / 255.0) ** 0.72)).clip(16, 235).astype(np.uint8)
        u8_c = ((xx * (idx * 13 + 3) - yy * (idx * 5 + 7)) % 256).astype(np.uint8)
        base_frames.append(u8_a.tobytes())
        sdr_frames.append(u8_b.tobytes())
        other_frames.append(u8_c.tobytes())

    a = FileRecord(path=str(tmp_path / "hdr.mp4"), size=10, mtime=1,
                   media_type=MediaType.VIDEO, extension=".mp4", duration=10.0)
    b = FileRecord(path=str(tmp_path / "sdr.mp4"), size=11, mtime=1,
                   media_type=MediaType.VIDEO, extension=".mp4", duration=10.0)

    monkeypatch.setattr(
        module,
        "_version_frame_batches",
        lambda path, _ts, _c, **_kw: base_frames if path == a.path else sdr_frames,
    )
    evidence = module._verify_video_offset(a, b, 0.0, None)
    assert evidence is not None
    assert evidence["relation"] == "same_footage"

    # Distinct scenes stay rejected.
    monkeypatch.setattr(
        module,
        "_version_frame_batches",
        lambda path, _ts, _c, **_kw: base_frames if path == a.path else other_frames,
    )
    assert module._verify_video_offset(a, b, 0.0, None) is None

    # Two-distinct-hash sequence [A, B, B, B, B, B, B] must fail the pairwise diverse check.
    two_only = [base_frames[0]] + [base_frames[1]] * 6
    monkeypatch.setattr(module, "_version_frame_batches", lambda _p, _ts, _c, **_kw: two_only)
    assert module._verify_video_offset(a, b, 0.0, None) is None


def test_version_frame_batches_clears_hwaccel_after_chunk_zero_failure(monkeypatch):
    import dedupe.similar_video as module

    hw_calls = []

    def fake_extract(path, chunk, hwaccel, **_kw):
        hw_calls.append(hwaccel)
        if hwaccel:
            return []
        return [bytes(module.HASH_FRAME_SIZE * module.HASH_FRAME_SIZE) for _ in chunk]

    monkeypatch.setattr(module, "_hwaccel_args", lambda: ("-hwaccel", "videotoolbox"))
    monkeypatch.setattr(module, "_extract_hash_frames_once", fake_extract)

    frames = module._version_frame_batches("clip.mp4", [float(i) for i in range(25)], None)

    assert len(frames) == 25
    # Chunk 0 tried hwaccel and fell back; chunks 1 and 2 skipped hwaccel immediately.
    assert hw_calls == [("-hwaccel", "videotoolbox"), (), (), ()]


def test_refine_and_verify_offset_recovers_subsecond_drift_around_bin_median(
    version_media, tmp_path, monkeypatch
):
    import dedupe.similar_video as module

    orig = version_media / "original.mp4"
    trim = version_media / "off-grid-trim.mp4"
    rec_a, rec_b = inventory([orig, trim])
    rec_a.duration, rec_b.duration = 8.0, 4.1
    # True offset is -1.37s (t_b = t_a - 1.37). Supply an off-grid bin median at -1.0s
    # (0.37s off, enough for testsrc2 frame hashes to miss at -1.0s) and verify local refinement.
    assert module._verify_video_offset(rec_a, rec_b, -1.0, None) is None
    refined = module._refine_and_verify_offset(rec_a, rec_b, -1.0, 1.0, None)
    assert refined is not None
    assert refined["ranges"][str(orig)][0] == pytest.approx(1.37, abs=0.25)


def test_version_local_offset_refinement_and_same_footage_preservation(tmp_path):
    if not ffmpeg_available():
        pytest.skip("ffmpeg and ffprobe are required")
    orig = tmp_path / "orig.mp4"
    tone_mapped = tmp_path / "sdr_same.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-threads", "1", orig)
    # Same-footage copy with strong brightness/gamma shift and matching audio.
    _ffmpeg("-i", orig, "-vf", "eq=gamma=1.7:brightness=0.10", "-c:a", "aac",
            "-threads", "1", tone_mapped)

    matches = find_video_version_matches(
        inventory([orig, tone_mapped]), cache_path=tmp_path / "cache.sqlite3"
    )
    assert len(matches) == 1
    _pair, details = matches[0]
    assert details["relation"] == "same_footage"
    assert details["audio"] == "same"


def test_spectral_audio_handles_reencode_polarity_eq_drift_lag_and_silence(tmp_path):
    if not ffmpeg_available():
        pytest.skip("ffmpeg and ffprobe are required")
    expr1 = (
        "0.4*sin(2*PI*(300+250*sin(2*PI*0.7*t))*t)*(0.5+0.5*sin(2*PI*3.1*t))"
        "+0.15*sin(2*PI*1100*t)*(0.5+0.5*sin(2*PI*2.3*t))"
    )
    expr2 = (
        "0.4*sin(2*PI*(500+400*cos(2*PI*1.3*t))*t)*(0.5+0.5*cos(2*PI*4.7*t))"
        "+0.15*sin(2*PI*2100*t)*(0.5+0.5*cos(2*PI*1.9*t))"
    )
    base = tmp_path / "base.mp4"
    inv_eq_aac = tmp_path / "inv_eq_aac.mp4"
    drift_lag = tmp_path / "drift_lag.mp4"
    different = tmp_path / "different.mp4"
    tail_changed = tmp_path / "tail_changed.mp4"
    sil_wav = tmp_path / "sil.mp4"
    sil_aac = tmp_path / "sil_aac.mp4"

    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=128x96:rate=10:duration=12",
            "-f", "lavfi", "-i", f"aevalsrc={expr1}:s=44100:d=12",
            "-c:a", "pcm_s16le", "-f", "mov", base)
    _ffmpeg("-i", base, "-c:v", "copy",
            "-af", "aeval=-val(0):c=same,volume=-8dB,equalizer=f=300:width_type=h:width=200:g=6",
            "-c:a", "aac", "-b:a", "64k", inv_eq_aac)
    _ffmpeg("-i", base, "-c:v", "copy",
            "-af", "atempo=1.004,adelay=1400|1400",
            "-c:a", "aac", "-b:a", "96k", drift_lag)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=128x96:rate=10:duration=12",
            "-f", "lavfi", "-i", f"aevalsrc={expr2}:s=44100:d=12",
            "-c:a", "aac", different)
    # First half matches base, second half switches to expr2 (caught by tail window sampling).
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=128x96:rate=10:duration=12",
            "-f", "lavfi", "-i", f"aevalsrc={expr1}:s=44100:d=6",
            "-f", "lavfi", "-i", f"aevalsrc={expr2}:s=44100:d=6",
            "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
            "-map", "0:v", "-map", "[a]", "-c:a", "aac", tail_changed)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=128x96:rate=10:duration=6",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "6",
            "-c:a", "pcm_s16le", "-f", "mov", sil_wav)
    _ffmpeg("-i", sil_wav, "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", sil_aac)

    r12 = {str(base): [0.0, 12.0], str(inv_eq_aac): [0.0, 12.0]}
    assert compare_video_audio(str(base), str(inv_eq_aac), r12) == "same"
    assert compare_video_audio(
        str(base), str(drift_lag), {str(base): [0.0, 12.0], str(drift_lag): [0.0, 12.0]}
    ) == "same"
    assert compare_video_audio(
        str(base), str(different), {str(base): [0.0, 12.0], str(different): [0.0, 12.0]}
    ) == "different"
    assert compare_video_audio(
        str(base), str(tail_changed), {str(base): [0.0, 12.0], str(tail_changed): [0.0, 12.0]}
    ) != "same"
    assert compare_video_audio(
        str(sil_wav), str(sil_aac), {str(sil_wav): [0.0, 6.0], str(sil_aac): [0.0, 6.0]}
    ) == "same"
    assert compare_video_audio(
        str(sil_wav), str(base), {str(sil_wav): [0.0, 6.0], str(base): [0.0, 6.0]}
    ) == "different"


def test_version_cache_uses_wal_device_inode_rename_fallback_and_persists_short_probes(
    version_media, tmp_path, monkeypatch
):
    import shutil
    import sqlite3

    import dedupe.similar_video as module

    cache = tmp_path / "versions.sqlite3"
    orig = tmp_path / "orig.mp4"
    trim = tmp_path / "trim.mp4"
    short_clip = tmp_path / "short.mp4"
    shutil.copy2(version_media / "original.mp4", orig)
    shutil.copy2(version_media / "trim.mp4", trim)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=128x96:rate=12:duration=1.2", short_clip)

    first = find_video_version_matches(inventory([orig, trim, short_clip]), cache_path=cache)
    assert len(first) == 1

    with sqlite3.connect(cache) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    # Rename orig -> renamed.mp4 (changes path & ctime_ns, preserves device+inode+size+mtime_ns).
    renamed = tmp_path / "renamed.mp4"
    orig.rename(renamed)

    probe_calls = []
    batch_calls = []
    real_probe = module.probe_video
    real_batches = module._version_frame_batches

    def tracked_probe(path, **kw):
        probe_calls.append(str(path))
        return real_probe(path, **kw)

    def tracked_batches(path, timestamps, cancelled):
        if len(timestamps) != 7:
            batch_calls.append(str(path))
        return real_batches(path, timestamps, cancelled)

    monkeypatch.setattr(module, "probe_video", tracked_probe)
    monkeypatch.setattr(module, "_version_frame_batches", tracked_batches)

    warm_records = inventory([renamed, trim, short_clip])
    second = find_video_version_matches(warm_records, cache_path=cache)

    assert len(second) == 1
    assert batch_calls == []  # device+inode fallback avoided re-decoding renamed.mp4
    assert probe_calls == []  # <2s short.mp4 probe was cached too
    short_rec = next(r for r in warm_records if r.path == str(short_clip))
    assert short_rec.duration == pytest.approx(1.2, abs=0.1)
    assert (short_rec.width, short_rec.height) == (128, 96)
