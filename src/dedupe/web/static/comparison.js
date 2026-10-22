import { api } from "./api.js";

// Metadata is fetched only for the visible pair; it never delays a scan.
export function wireComparison(box, anchor, candidate) {
  const metadata = box.querySelector("#comparisonMetadata");
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
        + (candidate.media_type === "video" ? " · Audio content is not compared." : "");
    });
  const videos = [...box.querySelectorAll(".swipe-pair video")];
  const enabled = () => Boolean(box.querySelector("#syncVideos")?.checked);
  const sync = (source, type) => {
    if (!enabled()) return;
    for (const target of videos.filter((v) => v !== source)) {
      if (Number.isFinite(source.currentTime) && Math.abs(target.currentTime - source.currentTime) > 0.15) {
        target.currentTime = Math.min(source.currentTime, Number.isFinite(target.duration) ? target.duration : source.currentTime);
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
}
