const video = document.querySelector("#video");
const subtitle = document.querySelector("#subtitle");
const log = document.querySelector("#subtitle-log");
const statusNodes = {
  input: document.querySelector("#input-status"),
  ws: document.querySelector("#ws-status"),
  hls: document.querySelector("#hls-status"),
  asr: document.querySelector("#asr-status"),
  delay: document.querySelector("#delay-status"),
  terms: document.querySelector("#terms-status"),
  asrLatency: document.querySelector("#asr-latency-status"),
  translationLatency: document.querySelector("#translation-latency-status"),
  termsHit: document.querySelector("#terms-hit-status"),
  error: document.querySelector("#error-status"),
  queueAudio: document.querySelector("#queue-audio-status"),
  queueAsr: document.querySelector("#queue-asr-status"),
  queueTranslation: document.querySelector("#queue-translation-status"),
  queueSubtitle: document.querySelector("#queue-subtitle-status"),
  queueDropped: document.querySelector("#queue-dropped-status"),
  asrAvg: document.querySelector("#asr-avg-status"),
  translationAvg: document.querySelector("#translation-avg-status"),
};
const sourceLog = document.querySelector("#source-log");
const configForm = document.querySelector("#config-form");
const configMessage = document.querySelector("#config-message");
const diagnosticsOutput = document.querySelector("#diagnostics-output");
const historyList = document.querySelector("#history-list");
const qualitySelect = document.querySelector("#quality-select");
let currentConfig = null;
let activeWebSocket = null;
let subtitleClearTimer = null;
let subtitleVersion = 0;
let backendStartedAt = null;
let hlsInstance = null;

async function loadConfig() {
  const response = await fetch("/api/config");
  const config = await response.json();
  currentConfig = config;
  fillConfigForm(config);
  applySubtitleStyle(config);
  statusNodes.delay.textContent = `${config.video.delay_seconds}s`;
  statusNodes.input.textContent = `${config.input.resolver}: ${shorten(config.input.url, 34)}`;
}

async function rememberBackendInstance() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    const data = await response.json();
    backendStartedAt = data.started_at ?? null;
  } catch {
    backendStartedAt = null;
  }
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const status = await response.json();
  statusNodes.input.textContent = status.source_url ? shorten(status.source_url, 42) : "-";
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
  statusNodes.error.textContent = status.last_error || status.hls.last_error || "-";
  statusNodes.queueAudio.textContent = status.queues?.audio ?? 0;
  statusNodes.queueAsr.textContent = status.queues?.asr ?? 0;
  statusNodes.queueTranslation.textContent = status.queues?.translation ?? 0;
  statusNodes.queueSubtitle.textContent = status.queues?.subtitle ?? 0;
  const dropped = status.queues?.dropped || {};
  statusNodes.queueDropped.textContent = Object.values(dropped).reduce((sum, value) => sum + value, 0);
  statusNodes.asrAvg.textContent =
    status.queues?.asr_avg_latency == null ? "-" : `${status.queues.asr_avg_latency.toFixed(2)}s`;
  statusNodes.translationAvg.textContent =
    status.queues?.translation_avg_latency == null
      ? "-"
      : `${status.queues.translation_avg_latency.toFixed(2)}s`;
  if (status.asr.last_source_text) {
    sourceLog.textContent = `Source: ${status.asr.last_source_text}\nNormalized: ${
      status.asr.last_normalized_text || "-"
    }`;
  }
}

async function loadHistory() {
  const response = await fetch("/api/subtitles/history", { cache: "no-store" });
  const items = await response.json();
  renderHistory(items);
}

function renderHistory(items) {
  if (!items.length) {
    historyList.textContent = "No subtitles yet.";
    return;
  }
  historyList.replaceChildren(
    ...items.slice(0, 50).map((item) => {
      const node = document.createElement("article");
      node.className = `history-item${item.error_type ? " has-error" : ""}`;
      const terms = (item.candidate_terms || []).map((term) => term.zh_cn).join(", ") || "-";
      node.innerHTML = `
        <div class="history-text">${escapeHtml(item.text || "")}</div>
        <div class="history-source">${escapeHtml(item.source_text || "")}</div>
        <div class="history-meta">
          <span>terms ${escapeHtml(String(item.terms_hit ?? 0))}: ${escapeHtml(terms)}</span>
          <span>asr ${formatLatency(item.asr_latency)}</span>
          <span>tr ${formatLatency(item.translation_latency)}</span>
          ${item.error_type ? `<span class="error-chip">${escapeHtml(item.error_type)}</span>` : ""}
        </div>
        <div class="toolbar mini-toolbar">
          <button type="button" data-copy-source="${escapeAttr(item.source_text || "")}">Copy source</button>
          <button type="button" data-copy-source="${escapeAttr(item.text || "")}">Copy subtitle</button>
          <button type="button" data-term-source="${escapeAttr(item.source_text || "")}" data-term-zh="${escapeAttr(
            item.text || "",
          )}">Use as term</button>
        </div>
      `;
      node.querySelectorAll("[data-copy-source]").forEach((button) => {
        button.addEventListener("click", () => navigator.clipboard.writeText(button.dataset.copySource || ""));
      });
      node.querySelectorAll("[data-term-source]").forEach((button) => {
        button.addEventListener("click", () => {
          const params = new URLSearchParams({
            source: button.dataset.termSource || "",
            zh_cn: button.dataset.termZh || "",
          });
          window.open(`/admin/live-terms?${params.toString()}`, "_blank");
        });
      });
      return node;
    }),
  );
}

function formatLatency(value) {
  return value == null ? "-" : `${Number(value).toFixed(2)}s`;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (char) => {
    const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
    return map[char];
  });
}

function escapeAttr(text) {
  return escapeHtml(text).replace(/`/g, "&#096;");
}

function fillConfigForm(config) {
  setField("input.url", config.input.url);
  setField("input.resolver", config.input.resolver);
  setQualityOptions([config.input.quality, "best", "worst"], config.input.quality);
  setField("network.proxy_enabled", String(config.network.proxy_enabled));
  setField("network.proxy_url", config.network.proxy_url);
  setField("video.delay_seconds", config.video.delay_seconds);
  setField("subtitle.extra_offset_seconds", config.subtitle.extra_offset_seconds);
  setField("subtitle.bottom_percent", config.subtitle.bottom_percent);
  setField("subtitle.font_size_px", config.subtitle.font_size_px);
  setField("subtitle.min_display_seconds", config.subtitle.min_display_seconds);
  setField("subtitle.max_display_seconds", config.subtitle.max_display_seconds);
  setField("asr.provider", config.asr.provider);
  setField("asr.model", config.asr.model);
  setField("asr.device", config.asr.device);
  setField("asr.compute_type", config.asr.compute_type);
  setField("asr.beam_size", config.asr.beam_size);
  setField("asr.language", config.asr.language || "");
  setField("translation.provider", config.translation.provider);
  setField("translation.deepseek_v4.model", config.translation.deepseek_v4.model);
}

function setField(name, value) {
  const field = configForm.elements[name];
  if (field) field.value = value;
}

function setQualityOptions(qualities, selected) {
  const unique = [...new Set(qualities.filter(Boolean))];
  if (!unique.includes(selected)) unique.unshift(selected);
  qualitySelect.replaceChildren(
    ...unique.map((quality) => {
      const option = document.createElement("option");
      option.value = quality;
      option.textContent = quality;
      option.selected = quality === selected;
      return option;
    }),
  );
}

function readConfigForm() {
  const next = structuredClone(currentConfig);
  next.input.url = getString("input.url");
  next.input.resolver = getString("input.resolver");
  next.input.quality = getString("input.quality");
  next.network.proxy_enabled = getString("network.proxy_enabled") === "true";
  next.network.proxy_url = getString("network.proxy_url");
  next.video.delay_seconds = getNumber("video.delay_seconds");
  next.subtitle.extra_offset_seconds = getNumber("subtitle.extra_offset_seconds");
  next.subtitle.bottom_percent = getNumber("subtitle.bottom_percent");
  next.subtitle.font_size_px = getInteger("subtitle.font_size_px");
  next.subtitle.min_display_seconds = getNumber("subtitle.min_display_seconds");
  next.subtitle.max_display_seconds = getNumber("subtitle.max_display_seconds");
  next.asr.provider = getString("asr.provider");
  next.asr.model = getString("asr.model");
  next.asr.device = getString("asr.device");
  next.asr.compute_type = getString("asr.compute_type");
  next.asr.beam_size = getInteger("asr.beam_size");
  next.asr.language = getString("asr.language") || null;
  next.translation.provider = getString("translation.provider");
  next.translation.deepseek_v4.model = getString("translation.deepseek_v4.model");
  return next;
}

function getString(name) {
  return configForm.elements[name].value.trim();
}

function getNumber(name) {
  return Number(configForm.elements[name].value);
}

function getInteger(name) {
  return Number.parseInt(configForm.elements[name].value, 10);
}

function shorten(text, maxLength) {
  if (!text) return "-";
  return text.length <= maxLength ? text : `${text.slice(0, Math.max(0, maxLength - 1))}...`;
}

function applySubtitleStyle(config) {
  subtitle.style.fontSize = `${config.subtitle.font_size_px}px`;
  positionSubtitle();
}

function videoContentRect() {
  const stageRect = video.getBoundingClientRect();
  const videoWidth = video.videoWidth || 16;
  const videoHeight = video.videoHeight || 9;
  const stageRatio = stageRect.width / stageRect.height;
  const videoRatio = videoWidth / videoHeight;
  if (stageRatio > videoRatio) {
    const renderedWidth = stageRect.height * videoRatio;
    const left = stageRect.left + (stageRect.width - renderedWidth) / 2;
    return { left, top: stageRect.top, width: renderedWidth, height: stageRect.height };
  }
  const renderedHeight = stageRect.width / videoRatio;
  const top = stageRect.top + (stageRect.height - renderedHeight) / 2;
  return { left: stageRect.left, top, width: stageRect.width, height: renderedHeight };
}

function positionSubtitle() {
  if (!currentConfig) return;
  const rect = videoContentRect();
  const bottomPercent = currentConfig.subtitle.bottom_percent ?? 18;
  const top = rect.top + rect.height * (1 - bottomPercent / 100);
  subtitle.style.top = `${top}px`;
  subtitle.style.width = `${Math.min(rect.width * 0.92, window.innerWidth * 0.92)}px`;
}

async function waitForBackend() {
  let sawBackendGoAway = false;
  for (let attempt = 0; attempt < 45; attempt += 1) {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const data = await response.json();
      const changedInstance = backendStartedAt == null || data.started_at == null || data.started_at !== backendStartedAt;
      if (response.ok && sawBackendGoAway && changedInstance) {
        backendStartedAt = data.started_at ?? null;
        return true;
      }
    } catch {
      sawBackendGoAway = true;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return false;
}

function resetVideo() {
  if (hlsInstance) {
    hlsInstance.destroy();
    hlsInstance = null;
  }
  video.pause();
  video.removeAttribute("src");
  video.currentTime = 0;
  video.load();
  clearSubtitle();
  if (subtitleClearTimer) {
    clearTimeout(subtitleClearTimer);
    subtitleClearTimer = null;
  }
}

async function waitForPlaylist() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const response = await fetch(`/hls/index.m3u8?probe=${Date.now()}`, { cache: "no-store" });
      if (response.ok) return true;
    } catch {
      // Playlist may not exist yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

async function startVideo() {
  resetVideo();
  const ready = await waitForPlaylist();
  if (!ready) {
    statusNodes.error.textContent = "HLS playlist is not ready yet.";
    return;
  }
  const src = `/hls/index.m3u8?ts=${Date.now()}`;
  if (window.Hls && Hls.isSupported()) {
    hlsInstance = new Hls({ liveSyncDurationCount: 3 });
    hlsInstance.loadSource(src);
    hlsInstance.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = src;
  }
}

function connectEvents() {
  if (activeWebSocket && activeWebSocket.readyState < WebSocket.CLOSING) {
    activeWebSocket.close();
  }
  const ws = new WebSocket(`ws://${location.host}/ws/events`);
  activeWebSocket = ws;
  ws.addEventListener("open", () => {
    statusNodes.ws.textContent = "connected";
  });
  ws.addEventListener("close", () => {
    if (activeWebSocket === ws) {
      statusNodes.ws.textContent = "closed";
      setTimeout(connectEvents, 1500);
    }
  });
  ws.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "subtitle") {
      subtitleVersion += 1;
      const currentVersion = subtitleVersion;
      if (subtitleClearTimer) {
        clearTimeout(subtitleClearTimer);
        subtitleClearTimer = null;
      }
      renderSubtitle(payload.text || "", payload.source_text || "");
      const logText = `${payload.text || ""}\n${payload.source_text || ""}`;
      log.textContent = `${logText}\n\n${log.textContent}`.slice(0, 1200);
      positionSubtitle();
      const minDisplayMs = Math.max(500, (currentConfig?.subtitle?.min_display_seconds ?? 1.5) * 1000);
      const maxDisplayMs = Math.max(minDisplayMs, (currentConfig?.subtitle?.max_display_seconds ?? 8) * 1000);
      const eventDisplayMs = Math.max(minDisplayMs, (payload.hide_at - payload.show_at) * 1000);
      const hideAfter = Math.min(maxDisplayMs, eventDisplayMs);
      subtitleClearTimer = setTimeout(() => {
        if (subtitleVersion === currentVersion) {
          clearSubtitle();
          subtitleClearTimer = null;
        }
      }, hideAfter);
    }
    if (payload.type === "error") {
      statusNodes.error.textContent = payload.message;
    }
  });
}

function renderSubtitle(zhText, sourceText) {
  subtitle.replaceChildren();
  const main = document.createElement("span");
  main.className = "subtitle-main";
  main.style.display = "block";
  main.textContent = zhText;
  subtitle.append(main);
  if (sourceText && sourceText !== zhText) {
    const source = document.createElement("span");
    source.className = "subtitle-source";
    source.style.display = "block";
    source.style.fontSize = `${Math.max(14, Math.round((currentConfig?.subtitle?.font_size_px ?? 42) * 0.48))}px`;
    source.style.lineHeight = "1.25";
    source.style.marginTop = "4px";
    source.textContent = sourceText;
    subtitle.append(source);
  }
}

function clearSubtitle() {
  subtitle.replaceChildren();
}

document.querySelector("#start-hls").addEventListener("click", async () => {
  await fetch("/api/runtime/start-hls", { method: "POST" });
  await startVideo();
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
  resetVideo();
  await fetch("/api/runtime/stop", { method: "POST" });
  await loadStatus();
});

document.querySelector("#clear-hls").addEventListener("click", async () => {
  resetVideo();
  await fetch("/api/runtime/clear-hls", { method: "POST" });
  await loadStatus();
});

document.querySelector("#clear-history").addEventListener("click", async () => {
  await fetch("/api/subtitles/history/clear", { method: "POST" });
  await loadHistory();
});

document.querySelector("#reload-config").addEventListener("click", async () => {
  configMessage.textContent = "Reloading configuration...";
  await loadConfig();
  configMessage.textContent = "Configuration reloaded.";
});

document.querySelector("#refresh-qualities").addEventListener("click", async () => {
  await refreshQualities();
});

async function refreshQualities() {
  if (!currentConfig) return;
  const currentQuality = getString("input.quality") || currentConfig.input.quality || "best";
  configMessage.textContent = "Loading available qualities...";
  try {
    const response = await fetch("/api/input/qualities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        input: {
          url: getString("input.url"),
          resolver: getString("input.resolver"),
          quality: currentQuality,
        },
        network: {
          proxy_enabled: getString("network.proxy_enabled") === "true",
          proxy_url: getString("network.proxy_url"),
        },
      }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.detail || data.error || "Unable to load qualities.");
    }
    const selected = data.qualities.includes(currentQuality) ? currentQuality : data.selected || "best";
    setQualityOptions(data.qualities, selected);
    configMessage.textContent = data.warnings?.length
      ? `Loaded qualities. ${data.warnings[data.warnings.length - 1]}`
      : "Loaded available qualities.";
  } catch (error) {
    setQualityOptions([currentQuality, "best", "worst"], currentQuality);
    configMessage.textContent = `Quality lookup failed: ${error.message || error}`;
  }
}

document.querySelector("#run-diagnostics").addEventListener("click", async () => {
  diagnosticsOutput.textContent = "Running diagnostics...";
  try {
    const response = await fetch("/api/diagnostics", { cache: "no-store" });
    const data = await response.json();
    diagnosticsOutput.textContent = formatDiagnostics(data);
  } catch (error) {
    diagnosticsOutput.textContent = `Diagnostics failed: ${error}`;
  }
});

document.querySelector("#test-deepseek").addEventListener("click", async () => {
  diagnosticsOutput.textContent = "Testing DeepSeek...";
  try {
    const response = await fetch("/api/diagnostics/deepseek-test", { method: "POST" });
    const data = await response.json();
    diagnosticsOutput.textContent = formatDeepSeekTest(data);
  } catch (error) {
    diagnosticsOutput.textContent = `DeepSeek test failed: ${error}`;
  }
});

function formatDiagnostics(data) {
  const lines = [];
  lines.push(`Python: ${data.python.version}`);
  lines.push(`FFmpeg: ${data.tools.ffmpeg.ok ? data.tools.ffmpeg.version : data.tools.ffmpeg.error}`);
  lines.push(`Streamlink: ${data.tools.streamlink.ok ? data.tools.streamlink.version : data.tools.streamlink.error}`);
  lines.push(`Proxy: ${data.network.proxy_enabled ? data.network.proxy_url : "off"}`);
  lines.push(`Proxy env: ${data.network.proxy_env_active ? "active" : "inactive"}`);
  lines.push(`Input: ${data.input.resolver} ${data.input.url}`);
  lines.push(
    `Resolve: ${data.input.resolution.ok ? "ok" : "failed"}${
      data.input.resolution.error ? ` - ${data.input.resolution.error}` : ""
    }`,
  );
  lines.push(`Translation: ${data.translation.provider}`);
  lines.push(`DeepSeek Key: ${data.translation.deepseek_api_key_present ? "present" : "missing"}`);
  lines.push(`HLS: playlist=${data.hls.playlist_exists}, segments=${data.hls.segment_count}`);
  return lines.join("\\n");
}

function formatDeepSeekTest(data) {
  if (data.ok) {
    return `DeepSeek: ok\nModel: ${data.model}\nLatency: ${data.latency}s\nResult: ${data.text}`;
  }
  return `DeepSeek: failed\nModel: ${data.model || "-"}\nType: ${data.error_type}\nLatency: ${
    data.latency ?? "-"
  }s\nError: ${data.error}`;
}

configForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentConfig) {
    configMessage.textContent = "Configuration is still loading.";
    return;
  }
  configMessage.textContent = "Saving and restarting backend...";
  const payload = readConfigForm();
  const response = await fetch("/api/runtime/save-config-and-restart", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    configMessage.textContent = await response.text();
    return;
  }
  const restored = await waitForBackend();
  if (restored) {
    configMessage.textContent = "Backend restarted.";
    await loadConfig();
    await loadStatus();
    resetVideo();
    connectEvents();
  } else {
    configMessage.textContent = "Backend did not come back within 30 seconds.";
  }
});

loadConfig().then(refreshQualities);
rememberBackendInstance();
loadStatus();
loadHistory();
connectEvents();
setInterval(loadStatus, 3000);
setInterval(loadHistory, 3000);
window.addEventListener("resize", positionSubtitle);
video.addEventListener("loadedmetadata", positionSubtitle);
video.addEventListener("play", positionSubtitle);
