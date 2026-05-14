const video = document.querySelector("#video");
const subtitle = document.querySelector("#subtitle");
const log = document.querySelector("#subtitle-log");
const statusNodes = {
  ws: document.querySelector("#ws-status"),
  hls: document.querySelector("#hls-status"),
  asr: document.querySelector("#asr-status"),
  delay: document.querySelector("#delay-status"),
  terms: document.querySelector("#terms-status"),
  asrLatency: document.querySelector("#asr-latency-status"),
  translationLatency: document.querySelector("#translation-latency-status"),
  termsHit: document.querySelector("#terms-hit-status"),
  error: document.querySelector("#error-status"),
};
const sourceLog = document.querySelector("#source-log");

async function loadConfig() {
  const response = await fetch("/api/config/public");
  const config = await response.json();
  statusNodes.delay.textContent = `${config.video.delay_seconds}s`;
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const status = await response.json();
  statusNodes.hls.textContent = status.hls.playlist_exists ? "playlist ready" : "waiting";
  statusNodes.asr.textContent = status.asr.running
    ? `${status.asr.provider} running`
    : `${status.asr.provider} stopped`;
  statusNodes.terms.textContent = status.terms;
  statusNodes.asrLatency.textContent =
    status.asr.last_asr_latency == null ? "-" : `${status.asr.last_asr_latency.toFixed(2)}s`;
  statusNodes.translationLatency.textContent =
    status.asr.last_translation_latency == null ? "-" : `${status.asr.last_translation_latency.toFixed(2)}s`;
  statusNodes.termsHit.textContent = status.asr.last_terms_hit ?? "-";
  statusNodes.error.textContent = status.last_error || "-";
  if (status.asr.last_source_text) {
    sourceLog.textContent = `Source: ${status.asr.last_source_text}\nNormalized: ${
      status.asr.last_normalized_text || "-"
    }`;
  }
}

function startVideo() {
  const src = "/hls/index.m3u8";
  if (window.Hls && Hls.isSupported()) {
    const hls = new Hls({ liveSyncDurationCount: 3 });
    hls.loadSource(src);
    hls.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = src;
  }
}

function connectEvents() {
  const ws = new WebSocket(`ws://${location.host}/ws/events`);
  ws.addEventListener("open", () => {
    statusNodes.ws.textContent = "connected";
  });
  ws.addEventListener("close", () => {
    statusNodes.ws.textContent = "closed";
    setTimeout(connectEvents, 1500);
  });
  ws.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "subtitle") {
      subtitle.textContent = payload.text;
      log.textContent = `${payload.text}\n\n${log.textContent}`.slice(0, 1200);
      const hideAfter = Math.max(1500, (payload.hide_at - payload.show_at) * 1000);
      setTimeout(() => {
        if (subtitle.textContent === payload.text) subtitle.textContent = "";
      }, hideAfter);
    }
    if (payload.type === "error") {
      statusNodes.error.textContent = payload.message;
    }
  });
}

document.querySelector("#start-hls").addEventListener("click", async () => {
  await fetch("/api/runtime/start-hls", { method: "POST" });
  startVideo();
  await loadStatus();
});

document.querySelector("#mock-subtitle").addEventListener("click", async () => {
  await fetch("/api/runtime/mock-subtitle", { method: "POST" });
  await loadStatus();
});

document.querySelector("#mock-loop").addEventListener("click", async () => {
  await fetch("/api/runtime/start-mock-loop", { method: "POST" });
  await loadStatus();
});

document.querySelector("#start-asr").addEventListener("click", async () => {
  await fetch("/api/runtime/start-asr", { method: "POST" });
  await loadStatus();
});

document.querySelector("#stop-runtime").addEventListener("click", async () => {
  await fetch("/api/runtime/stop", { method: "POST" });
  await loadStatus();
});

loadConfig();
loadStatus();
startVideo();
connectEvents();
setInterval(loadStatus, 3000);
