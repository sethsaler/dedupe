import { api } from "./api.js";

function timestamp(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(value / 60)}:${(value % 60).toFixed(1).padStart(4, "0")}`;
}

// Metadata is fetched only for the visible pair; it never delays a scan.
export function wireComparison(box, anchor, candidate, group = null) {
  const metadata = box.querySelector("#comparisonMetadata");
  const details = group?.match_details;
  const ranges = details?.ranges;
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
  const describe = (detail) => {
    if (detail.audio) return detail.audio.length
      ? detail.audio.map((a) => `${a.codec_name || "audio"}, ${a.channels || "?"} channels`).join(" / ")
      : "no audio track";
    return [detail.format, detail.camera, detail.capture_date].filter(Boolean).join(" · ") || "No capture metadata";
  };
  Promise.allSettled([anchor, candidate].map((m) => api(`/api/media-details?path=${encodeURIComponent(m.path)}`)))
    .then((results) => {
      if (!metadata?.isConnected) return;
      metadata.textContent = results.map((r, i) => `${i ? "Copy" : "Reference"}: ${r.status === "fulfilled" ? describe(r.value) : "metadata unavailable"}`).join(" | ")
        + (candidate.media_type === "video" && !aligned ? " · Audio content has not been compared." : "");
    });
  const videos = [...box.querySelectorAll(".swipe-pair video")];
  const starts = aligned ? [ranges[anchor.path][0], ranges[candidate.path][0]] : [0, 0];
  const enabled = () => Boolean(box.querySelector("#syncVideos")?.checked);
  const sync = (source, type) => {
    if (!enabled()) return;
    const sourceIndex = videos.indexOf(source);
    for (const target of videos.filter((v) => v !== source)) {
      const mapped = Math.max(0, source.currentTime - starts[sourceIndex] + starts[videos.indexOf(target)]);
      if (Number.isFinite(mapped) && Math.abs(target.currentTime - mapped) > 0.15) {
        target.currentTime = Math.min(mapped, Number.isFinite(target.duration) ? target.duration : mapped);
      }
      if (type === "play" && target.paused) target.play().catch(() => {});
      if (type === "pause" && !target.paused) target.pause();
      if (target.playbackRate !== source.playbackRate) target.playbackRate = source.playbackRate;
    }
  };
  videos.forEach((video) => ["play", "pause", "seeking", "ratechange"].forEach((type) => video.addEventListener(type, () => sync(video, type))));
  box.querySelector("#syncVideos")?.addEventListener("change", () => {
    if (enabled() && videos[0]) sync(videos[0], videos[0].paused ? "pause" : "play");
  });
  if (aligned && videos.length === 2) {
    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "btn ghost";
    jump.textContent = "Jump to matching section";
    jump.addEventListener("click", () => {
      videos.forEach((video, i) => { video.pause(); video.currentTime = starts[i]; });
    });
    metadata?.after(jump);
  }
}
