import { api } from "./api.js";
import { $, isOverlayOpen } from "./util.js";

function timestamp(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(value / 60)}:${(value % 60).toFixed(1).padStart(4, "0")}`;
}

function formatBitrate(bps) {
  const value = Number(bps);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")} Mbps`
    : `${Math.round(value / 1000)} kbps`;
}

function formatFps(fps) {
  const value = Number(fps);
  if (!Number.isFinite(value) || value <= 0) return null;
  return `${Number(value.toFixed(2))} fps`;
}

function describeMediaDetail(detail) {
  if (!detail) return "metadata unavailable";
  if (detail.audio !== undefined || detail.video_codec !== undefined) {
    const parts = [];
    if (detail.video_codec) parts.push(String(detail.video_codec).toUpperCase());
    const fps = formatFps(detail.fps);
    if (fps) parts.push(fps);
    const bitrate = formatBitrate(detail.bit_rate);
    if (bitrate) parts.push(bitrate);
    if (detail.hdr) parts.push("HDR");
    const audioText = Array.isArray(detail.audio) && detail.audio.length
      ? detail.audio.map((a) => `${a.codec_name || "audio"}, ${a.channels || "?"} channels`).join(" / ")
      : "no audio track";
    parts.push(audioText);
    return parts.join(" · ");
  }
  return [detail.format, detail.camera, detail.capture_date].filter(Boolean).join(" · ") || "No capture metadata";
}

function computeVideoDiffBadges(ref, copy) {
  if (!ref || !copy) return [];
  const diffs = [];
  if (ref.video_codec && copy.video_codec && ref.video_codec !== copy.video_codec) {
    diffs.push(`Codec: ${String(ref.video_codec).toUpperCase()} vs ${String(copy.video_codec).toUpperCase()}`);
  }
  if (Number(ref.bit_rate) > 0 && Number(copy.bit_rate) > 0) {
    const ratio = ref.bit_rate / copy.bit_rate;
    if (ratio >= 1.15) diffs.push(`Higher bitrate: Ref (${formatBitrate(ref.bit_rate)} vs ${formatBitrate(copy.bit_rate)})`);
    else if (ratio <= 1 / 1.15) diffs.push(`Higher bitrate: Copy (${formatBitrate(copy.bit_rate)} vs ${formatBitrate(ref.bit_rate)})`);
  }
  if (Number(ref.fps) > 0 && Number(copy.fps) > 0 && Math.abs(ref.fps - copy.fps) >= 0.5) {
    diffs.push(`Frame rate: ${formatFps(ref.fps)} vs ${formatFps(copy.fps)}`);
  }
  if (Boolean(ref.hdr) !== Boolean(copy.hdr)) {
    diffs.push(ref.hdr ? "Dynamic range: Ref is HDR (Copy SDR)" : "Dynamic range: Copy is HDR (Ref SDR)");
  }
  const refAudio = Array.isArray(ref.audio) ? ref.audio.length : 0;
  const copyAudio = Array.isArray(copy.audio) ? copy.audio.length : 0;
  if (refAudio !== copyAudio) {
    diffs.push(`Audio tracks: Ref ${refAudio} vs Copy ${copyAudio}`);
  }
  return diffs;
}

function makeButton(label, { title = "", className = "btn ghost", pressed = null } = {}) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = className;
  btn.textContent = label;
  if (title) btn.title = title;
  if (pressed !== null) btn.setAttribute("aria-pressed", pressed ? "true" : "false");
  return btn;
}

// Metadata is fetched only for the visible pair; it never delays a scan.
export function wireComparison(box, anchor, candidate, group = null) {
  const metadata = box.querySelector("#comparisonMetadata");
  const details = group?.match_details;
  const ranges = details?.ranges || details?.video_ranges;
  const aligned = ranges?.[anchor.path]?.length === 2 && ranges?.[candidate.path]?.length === 2;
  const audioLabels = {
    same: "Sampled audio agrees within the matching section.",
    different: "Same footage, different audio. Review both versions before removing either.",
    none: "Neither video has an audio track.",
    unavailable: "Audio could not be confirmed. Review both versions before removing either.",
  };
  if (aligned) {
    const summary = document.createElement("span");
    summary.className = "video-match-ranges";
    summary.textContent = `${details.approximate_alignment ? "Approximate matching section" : "Matching section"} — Reference ${ranges[anchor.path].map(timestamp).join("–")} · Copy ${ranges[candidate.path].map(timestamp).join("–")}. ${audioLabels[details.audio] || ""}`;
    metadata?.before(summary);
  }

  let detectedFps = 30;
  Promise.allSettled([anchor, candidate].map((m) => api(`/api/media-details?path=${encodeURIComponent(m.path)}`)))
    .then((results) => {
      if (!metadata?.isConnected) return;
      const refDetail = results[0].status === "fulfilled" ? results[0].value : null;
      const copyDetail = results[1].status === "fulfilled" ? results[1].value : null;
      if (Number(refDetail?.fps) > 0) detectedFps = Number(refDetail.fps);
      else if (Number(copyDetail?.fps) > 0) detectedFps = Number(copyDetail.fps);
      metadata.textContent = results
        .map((r, i) => `${i ? "Copy" : "Reference"}: ${r.status === "fulfilled" ? describeMediaDetail(r.value) : "metadata unavailable"}`)
        .join(" | ")
        + (candidate.media_type === "video" && !aligned ? " · Audio content has not been compared." : "");
      const diffs = computeVideoDiffBadges(refDetail, copyDetail);
      if (diffs.length) {
        let diffEl = box.querySelector(".video-meta-diffs");
        if (!diffEl) {
          diffEl = document.createElement("div");
          diffEl.className = "video-meta-diffs muted small";
          metadata.after(diffEl);
        }
        diffEl.textContent = diffs.join(" · ");
      }
    });

  const videos = [...box.querySelectorAll(".swipe-pair video")];
  const starts = aligned ? [Number(ranges[anchor.path][0]) || 0, Number(ranges[candidate.path][0]) || 0] : [0, 0];
  let offsetNudge = 0;

  const syncCheckbox = box.querySelector("#syncVideos");
  if (syncCheckbox && group?.match_basis === "video_version" && aligned) {
    syncCheckbox.checked = true;
  }
  const enabled = () => Boolean(box.querySelector("#syncVideos")?.checked);

  // Re-entry lock plus asynchronous media-event suppression prevents the
  // ping-pong yank loop when clamping out-of-range excerpt positions.
  let syncing = false;
  const suppressedSeek = new WeakMap();
  const suppressedState = new WeakMap();
  const suppressedRate = new WeakMap();

  const suppressSeek = (video, time) => {
    suppressedSeek.set(video, { time, until: performance.now() + 300 });
  };
  const isSuppressedSeek = (video) => {
    const rec = suppressedSeek.get(video);
    if (!rec) return false;
    if (performance.now() > rec.until) {
      suppressedSeek.delete(video);
      return false;
    }
    if (Math.abs(video.currentTime - rec.time) < 0.25) {
      suppressedSeek.delete(video);
      return true;
    }
    return false;
  };
  const suppressPlayState = (video, paused) => {
    suppressedState.set(video, { paused, until: performance.now() + 300 });
  };
  const isSuppressedPlayState = (video, paused) => {
    const rec = suppressedState.get(video);
    if (!rec) return false;
    if (performance.now() > rec.until) {
      suppressedState.delete(video);
      return false;
    }
    if (rec.paused === paused) {
      suppressedState.delete(video);
      return true;
    }
    return false;
  };

  const mappedTimeFor = (sourceIndex, targetIndex, sourceTime) => {
    const baseDelta = starts[1] - starts[0] + offsetNudge;
    return targetIndex === 1 ? sourceTime + baseDelta : sourceTime - baseDelta;
  };

  const sync = (source, type, { forceSeek = false, fromEvent = false } = {}) => {
    if (!enabled() || syncing || videos.length < 2) return;
    if (fromEvent && type === "seeking" && isSuppressedSeek(source)) return;
    if (fromEvent && (type === "play" || type === "pause") && isSuppressedPlayState(source, source.paused)) return;
    if (fromEvent && type === "ratechange") {
      const rateRec = suppressedRate.get(source);
      if (rateRec && performance.now() < rateRec.until && Math.abs(source.playbackRate - rateRec.rate) < 0.01) {
        suppressedRate.delete(source);
        return;
      }
    }
    const sourceIndex = videos.indexOf(source);
    if (sourceIndex < 0) return;

    syncing = true;
    try {
      for (const target of videos) {
        if (target === source) continue;
        const targetIndex = videos.indexOf(target);
        const mapped = mappedTimeFor(sourceIndex, targetIndex, source.currentTime);
        const dur = Number.isFinite(target.duration) ? target.duration : Infinity;
        const outOfRange = mapped < 0 || mapped > dur;
        const clamped = Math.max(0, Math.min(dur, mapped));

        if (Number.isFinite(clamped) && (forceSeek || Math.abs(target.currentTime - clamped) > 0.15)) {
          suppressSeek(target, clamped);
          target.currentTime = clamped;
        }
        if (Math.abs(target.playbackRate - source.playbackRate) > 0.01) {
          suppressedRate.set(target, { rate: source.playbackRate, until: performance.now() + 250 });
          target.playbackRate = source.playbackRate;
        }
        if (outOfRange) {
          // Freeze out-of-range excerpt instead of seeking or pausing the source.
          if (!target.paused) {
            suppressPlayState(target, true);
            target.pause();
          }
          continue;
        }
        if ((type === "play" || (type === "seeking" && !source.paused)) && target.paused) {
          suppressPlayState(target, false);
          target.play().catch(() => {});
        } else if (type === "pause" && !source.ended && !target.paused) {
          suppressPlayState(target, true);
          target.pause();
        }
      }
    } finally {
      syncing = false;
    }
  };

  videos.forEach((video) => {
    ["play", "pause", "seeking", "ratechange"].forEach((type) => {
      video.addEventListener(type, () => sync(video, type, { fromEvent: true }));
    });
    // Freeze cleanly when an excerpt finishes while the longer video continues.
    video.addEventListener("timeupdate", () => {
      if (!enabled() || syncing || videos.length < 2) return;
      const other = videos.find((v) => v !== video);
      if (!other || other.paused) return;
      const mapped = mappedTimeFor(videos.indexOf(other), videos.indexOf(video), other.currentTime);
      const dur = Number.isFinite(video.duration) ? video.duration : Infinity;
      if ((mapped < 0 || mapped > dur) && !video.paused) {
        suppressPlayState(video, true);
        video.pause();
      } else if (mapped >= 0 && mapped <= dur && video.paused && !other.paused && !video.ended) {
        suppressSeek(video, mapped);
        video.currentTime = mapped;
        suppressPlayState(video, false);
        video.play().catch(() => {});
      }
    });
  });

  box.querySelector("#syncVideos")?.addEventListener("change", () => {
    if (enabled() && videos[0]) {
      sync(videos[0], videos[0].paused ? "pause" : "play", { forceSeek: true });
    }
  });

  if (videos.length === 2) {
    const bar = document.createElement("div");
    bar.className = "video-compare-bar";
    bar.style.display = "flex";
    bar.style.flexWrap = "wrap";
    bar.style.alignItems = "center";
    bar.style.gap = "8px";

    if (aligned) {
      const jump = makeButton("Jump to matching section");
      jump.addEventListener("click", () => {
        videos.forEach((video, i) => {
          suppressPlayState(video, true);
          video.pause();
          const start = i === 1 ? Math.max(0, starts[1] + offsetNudge) : starts[0];
          suppressSeek(video, start);
          video.currentTime = start;
        });
      });
      bar.append(jump);
    }

    const playBothBtn = makeButton("Play both");
    const syncPlayLabel = () => {
      const anyPlaying = videos.some((v) => !v.paused);
      playBothBtn.textContent = anyPlaying ? "Pause both" : "Play both";
    };
    videos.forEach((v) => {
      v.addEventListener("play", syncPlayLabel);
      v.addEventListener("pause", syncPlayLabel);
    });
    playBothBtn.addEventListener("click", () => {
      const anyPlaying = videos.some((v) => !v.paused);
      if (anyPlaying) {
        videos.forEach((v) => {
          if (!v.paused) {
            suppressPlayState(v, true);
            v.pause();
          }
        });
      } else if (enabled()) {
        suppressPlayState(videos[0], false);
        videos[0].play().catch(() => {});
        sync(videos[0], "play", { forceSeek: true });
      } else {
        videos.forEach((v) => {
          suppressPlayState(v, false);
          v.play().catch(() => {});
        });
      }
    });
    bar.append(playBothBtn);

    // Audio Solo: Mute | Ref | Copy
    const audioGroup = document.createElement("div");
    audioGroup.className = "video-audio-solo";
    audioGroup.setAttribute("role", "group");
    audioGroup.setAttribute("aria-label", "Audio solo");
    audioGroup.style.display = "inline-flex";
    audioGroup.style.gap = "4px";
    const audioBtns = [
      ["mute", "Mute", true, true],
      ["ref", "Ref", false, true],
      ["copy", "Copy", true, false],
    ].map(([mode, label, muteRef, muteCopy]) => {
      const btn = makeButton(label, { pressed: mode === "mute" });
      btn.dataset.audioMode = mode;
      btn.addEventListener("click", () => {
        videos[0].muted = muteRef;
        videos[1].muted = muteCopy;
        audioGroup.querySelectorAll("button").forEach((b) => {
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
      });
      return btn;
    });
    audioGroup.append(...audioBtns);
    bar.append(audioGroup);

    // Frame step: , / . (±1/fps)
    const stepFrames = (dir) => {
      const step = dir / Math.max(1, detectedFps);
      const primary = videos[0];
      videos.forEach((v) => {
        if (!v.paused) {
          suppressPlayState(v, true);
          v.pause();
        }
      });
      if (enabled()) {
        const dur = Number.isFinite(primary.duration) ? primary.duration : Infinity;
        const nextTime = Math.max(0, Math.min(dur, primary.currentTime + step));
        suppressSeek(primary, nextTime);
        primary.currentTime = nextTime;
        sync(primary, "seeking", { forceSeek: true });
      } else {
        videos.forEach((v) => {
          const dur = Number.isFinite(v.duration) ? v.duration : Infinity;
          const nextTime = Math.max(0, Math.min(dur, v.currentTime + step));
          suppressSeek(v, nextTime);
          v.currentTime = nextTime;
        });
      }
    };
    const prevFrameBtn = makeButton("‹ -1f", { title: "Previous frame (,)" });
    const nextFrameBtn = makeButton("+1f ›", { title: "Next frame (.)" });
    prevFrameBtn.addEventListener("click", () => stepFrames(-1));
    nextFrameBtn.addEventListener("click", () => stepFrames(1));
    bar.append(prevFrameBtn, nextFrameBtn);

    // Speed selector (0.5x..2x)
    const speedWrap = document.createElement("label");
    speedWrap.textContent = "Speed ";
    const speedSelect = document.createElement("select");
    speedSelect.setAttribute("aria-label", "Comparison playback speed");
    for (const rate of [0.5, 1, 1.5, 2]) {
      const opt = new Option(`${rate}×`, String(rate), rate === 1, rate === 1);
      speedSelect.append(opt);
    }
    speedSelect.addEventListener("change", () => {
      const rate = Number(speedSelect.value);
      videos.forEach((v) => {
        suppressedRate.set(v, { rate, until: performance.now() + 250 });
        v.playbackRate = rate;
      });
    });
    speedWrap.append(speedSelect);
    bar.append(speedWrap);

    // Offset nudge (-0.1s / +0.1s + Reset) for approximate alignment
    if (aligned && details?.approximate_alignment) {
      const nudgeWrap = document.createElement("div");
      nudgeWrap.className = "video-offset-nudge";
      nudgeWrap.style.display = "inline-flex";
      nudgeWrap.style.alignItems = "center";
      nudgeWrap.style.gap = "4px";
      const readout = document.createElement("span");
      readout.className = "muted small";
      const updateReadout = () => {
        const sign = offsetNudge > 0 ? "+" : "";
        readout.textContent = `Offset ${sign}${offsetNudge.toFixed(1)}s`;
      };
      updateReadout();
      const nudgeBy = (delta) => {
        offsetNudge = delta === null ? 0 : Math.round((offsetNudge + delta) * 10) / 10;
        updateReadout();
        if (enabled() && videos[0]) sync(videos[0], "seeking", { forceSeek: true });
      };
      const minusBtn = makeButton("-0.1s", { title: "Shift copy alignment earlier by 0.1s" });
      const plusBtn = makeButton("+0.1s", { title: "Shift copy alignment later by 0.1s" });
      const resetBtn = makeButton("Reset", { title: "Reset offset nudge to 0.0s" });
      minusBtn.addEventListener("click", () => nudgeBy(-0.1));
      plusBtn.addEventListener("click", () => nudgeBy(0.1));
      resetBtn.addEventListener("click", () => nudgeBy(null));
      nudgeWrap.append(minusBtn, plusBtn, resetBtn, readout);
      bar.append(nudgeWrap);
    }

    metadata?.after(bar);

    const keyHandler = (event) => {
      if (!bar.isConnected) {
        document.removeEventListener("keydown", keyHandler);
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const tag = event.target?.tagName || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || event.target?.isContentEditable) return;
      if (
        isOverlayOpen($("lightbox"))
        || isOverlayOpen($("cropEditor"))
        || isOverlayOpen($("modalBackdrop"))
        || isOverlayOpen($("helpBackdrop"))
        || isOverlayOpen($("historyPanel"))
      ) return;
      if (event.key === ",") {
        stepFrames(-1);
        event.preventDefault();
      } else if (event.key === ".") {
        stepFrames(1);
        event.preventDefault();
      }
    };
    document.addEventListener("keydown", keyHandler);
  }
}
