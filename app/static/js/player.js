const video = document.querySelector("#video");
const stage = video.closest(".stage");
const subtitle = document.querySelector("#subtitle");
const fullscreenButton = document.querySelector("#stage-fullscreen");
fullscreenButton.hidden = !document.fullscreenEnabled;
fullscreenButton.addEventListener("click", () => {
  stage.requestFullscreen().catch((error) => console.warn("无法进入播放器全屏", error));
});
document.addEventListener("fullscreenchange", positionSubtitle);
const log = document.querySelector("#subtitle-log");
const statusNodes = {
  input: document.querySelector("#input-status"),
  ws: document.querySelector("#ws-status"),
  hls: document.querySelector("#hls-status"),
  recording: document.querySelector("#recording-status"),
  recordingSubtitles: document.querySelector("#recording-subtitles-status"),
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
const captureMode = new URLSearchParams(location.search).get("capture") === "1";
document.body.classList.toggle("capture-mode", captureMode);
if (captureMode) {
  document.title = "LiveHime Capture";
  video.defaultMuted = false;
  video.muted = false;
  const enableAudio = document.querySelector("#enable-capture-audio");
  enableAudio.addEventListener("click", () => {
    video.muted = false;
    video.play().catch(() => {});
  });
  video.addEventListener("volumechange", () => { enableAudio.hidden = !video.muted; });
}
let currentConfig = null;
let activeWebSocket = null;
let subtitleClearTimer = null;
let subtitleVersion = 0;
let backendStartedAt = null;
let hlsInstance = null;
let activeStreamId = null;
let activeCueId = null;
const mediaSubtitles = new Map();

async function loadConfig() {
  const response = await fetch("/api/config");
  const config = await response.json();
  currentConfig = config;
  fillConfigForm(config);
  applySubtitleStyle(config);
  statusNodes.delay.textContent = `${config.video.delay_seconds} 秒`;
  statusNodes.input.textContent = shorten(config.input.url, 42);
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
  if (activeStreamId && status.hls.stream_id !== activeStreamId) resetVideo();
  if (captureMode && currentConfig && status.hls.stream_id && !activeStreamId) {
    void startVideo(status.hls.stream_id);
  }
  const sourceUrl = status.source_url || currentConfig?.input.url;
  statusNodes.input.textContent = sourceUrl ? shorten(sourceUrl, 42) : "-";
  statusNodes.hls.textContent = status.hls.playlist_exists ? "播放列表已就绪" : "待机";
  const recording = status.hls.recording;
  if (recording) {
    const labels = { disabled: "关闭", idle: "待机", pending: "等待源流", recording: "录制中", saved: "已保存", failed: "录制异常" };
    const size = recording.bytes ? ` · ${(recording.bytes / 1024 ** 2).toFixed(1)} MB` : "";
    statusNodes.recording.textContent = (labels[recording.state] || "待机") + size;
    statusNodes.recording.title = [recording.path, recording.error].filter(Boolean).join("\n");
  }
  const archive = status.recording_subtitles;
  if (archive) {
    const ja = archive.tracks.ja;
    const zh = archive.tracks["zh-CN"];
    const statusText = archive.finishing ? "正在保存末尾字幕 · " : "";
    const files = `日语 ${ja.cues} 条 · 中文 ${zh.cues} 条`;
    const warning = archive.error ? " · 写入异常" : archive.missing_fragments ? " · 部分分片未识别" : "";
    statusNodes.recordingSubtitles.textContent = statusText + files + warning;
    statusNodes.recordingSubtitles.title = [ja.path, zh.path, archive.error].filter(Boolean).join("\n");
  } else {
    statusNodes.recordingSubtitles.textContent = "待机";
    statusNodes.recordingSubtitles.title = "";
  }
  statusNodes.asr.textContent = status.asr.running ? "运行中" : "待机";
  statusNodes.terms.textContent = status.terms;
  statusNodes.asrLatency.textContent = formatLatency(status.asr.last_asr_latency);
  statusNodes.translationLatency.textContent = formatLatency(status.asr.last_translation_latency);
  statusNodes.termsHit.textContent = status.asr.last_terms_hit ?? "-";
  statusNodes.error.textContent = status.last_error || status.hls.last_error || "-";
  statusNodes.queueAudio.textContent = status.queues?.audio ?? 0;
  statusNodes.queueAsr.textContent = status.queues?.asr ?? 0;
  statusNodes.queueTranslation.textContent = status.queues?.translation ?? 0;
  statusNodes.queueSubtitle.textContent = status.queues?.subtitle ?? 0;
  const dropped = status.queues?.dropped || {};
  statusNodes.queueDropped.textContent = Object.values(dropped).reduce((sum, value) => sum + value, 0);
  statusNodes.asrAvg.textContent = formatLatency(status.queues?.asr_avg_latency);
  statusNodes.translationAvg.textContent = formatLatency(status.queues?.translation_avg_latency);
  if (status.asr.last_source_text) {
    sourceLog.textContent = `识别原文　${status.asr.last_source_text}\n术语归一化　${
      status.asr.last_normalized_text || "-"
    }`;
  }
}

async function loadHistory() {
  const response = await fetch("/api/subtitles/history", { cache: "no-store" });
  const items = await response.json();
  renderHistory(items);
  for (const item of items) bufferMediaSubtitle(item);
  syncMediaSubtitle();
}

function renderHistory(items) {
  if (!items.length) {
    historyList.textContent = "暂无字幕。";
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
          <span>术语命中 ${escapeHtml(String(item.terms_hit ?? 0))} 条　${escapeHtml(terms)}</span>
          <span>识别 ${formatLatency(item.asr_latency)}</span>
          <span>翻译 ${formatLatency(item.translation_latency)}</span>
          ${item.error_type ? `<span class="error-chip">${escapeHtml(item.error_type)}</span>` : ""}
        </div>
        <div class="toolbar mini-toolbar">
          <button type="button" data-copy-source="${escapeAttr(item.source_text || "")}">复制原文</button>
          <button type="button" data-copy-source="${escapeAttr(item.text || "")}">复制字幕</button>
          <button type="button" data-term-source="${escapeAttr(item.source_text || "")}" data-term-zh="${escapeAttr(
            item.text || "",
          )}">用作术语</button>
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
  return value == null ? "-" : `${Number(value).toFixed(2)} 秒`;
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
  setField("video.record_source", String(config.video.record_source));
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
  setField("translation.deepseek_v4.thinking_enabled", String(config.translation.deepseek_v4.thinking_enabled ?? ""));
  setField("translation.program_context", config.translation.program_context ?? "");
}

function setField(name, value) {
  const field = configForm.elements[name];
  if (field) field.value = value;
}

function setQualityOptions(qualities, selected) {
  const labels = { best: "最高可用画质", worst: "最低可用画质", direct: "原始画质（直接读取）" };
  const unique = [...new Set(qualities.filter(Boolean))];
  if (!unique.includes(selected)) unique.unshift(selected);
  qualitySelect.replaceChildren(
    ...unique.map((quality) => {
      const option = document.createElement("option");
      option.value = quality;
      option.textContent = Object.hasOwn(labels, quality) ? labels[quality] : quality;
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
  next.video.record_source = getString("video.record_source") === "true";
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
  const thinking = getString("translation.deepseek_v4.thinking_enabled");
  next.translation.deepseek_v4.thinking_enabled = thinking === "" ? null : thinking === "true";
  next.translation.program_context = getString("translation.program_context");
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
  if (captureMode) {
    const bottomPercent = config.subtitle.bottom_percent ?? 18;
    subtitle.style.setProperty("--subtitle-top", `${100 - bottomPercent}%`);
  }
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
  if (!currentConfig || captureMode) return;
  const rect = videoContentRect();
  const bottomPercent = currentConfig.subtitle.bottom_percent ?? 18;
  const top = rect.top - stage.getBoundingClientRect().top + rect.height * (1 - bottomPercent / 100);
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
  activeStreamId = null;
  activeCueId = null;
  mediaSubtitles.clear();
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

async function waitForPlaylist(streamId, delay) {
  const finishBy = performance.now() + (delay + 35) * 1000;
  while (activeStreamId === streamId && performance.now() < finishBy) {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      const { hls } = await response.json();
      if (hls.stream_id !== streamId) return false;
      if (!hls.running && !hls.ended) return false;
      const oldEnough = hls.first_media_epoch != null && Date.now() / 1000 >= hls.first_media_epoch + delay;
      if (oldEnough && (hls.buffered_seconds > delay || hls.ended)) return true;
      statusNodes.hls.textContent = `缓冲中 ${hls.buffered_seconds.toFixed(1)} / ${delay} 秒`;
    } catch {
      // Playlist may not exist yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

async function startVideo(streamId) {
  if (!currentConfig) await loadConfig();
  if (!streamId || activeStreamId === streamId) return;
  resetVideo();
  activeStreamId = streamId;
  const delay = Math.max(0.5, currentConfig.video.delay_seconds);
  const ready = await waitForPlaylist(streamId, delay);
  if (activeStreamId !== streamId) return;
  if (!ready) {
    resetVideo();
    statusNodes.error.textContent = "HLS 播放列表尚未就绪。";
    return;
  }
  const src = `/hls/index.m3u8?stream=${streamId}`;
  if (captureMode) {
    video.addEventListener("loadeddata", async () => {
      if (activeStreamId !== streamId) return;
      try {
        await video.play();
      } catch (error) {
        if (error.name === "NotAllowedError" && activeStreamId === streamId && !video.muted) {
          video.muted = true;
          await video.play().catch(() => {});
        }
      }
    }, { once: true });
  }
  if (window.Hls && Hls.isSupported()) {
    hlsInstance = new Hls({ liveSyncDuration: delay, lowLatencyMode: false });
    hlsInstance.loadSource(src);
    hlsInstance.attachMedia(video);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = src;
    video.addEventListener("loadedmetadata", () => {
      if (activeStreamId === streamId && !Number.isFinite(video.duration) && video.seekable.length) {
        const end = video.seekable.end(video.seekable.length - 1);
        video.currentTime = Math.max(video.seekable.start(0), end - delay);
      }
    }, { once: true });
  }
  void loadHistory();
}

function bufferMediaSubtitle(payload) {
  if (payload.stream_id !== activeStreamId || !Number.isFinite(payload.media_show_at)) return;
  mediaSubtitles.set(payload.id, payload);
}

function playingEpoch() {
  const hlsDate = hlsInstance?.playingDate;
  if (hlsDate) return hlsDate.getTime() / 1000;
  const nativeStart = video.getStartDate?.();
  return nativeStart ? nativeStart.getTime() / 1000 + video.currentTime : NaN;
}

function syncMediaSubtitle() {
  if (!activeStreamId || video.readyState < 2) return;
  const position = playingEpoch();
  if (!Number.isFinite(position)) return;
  let current = null;
  for (const [id, cue] of mediaSubtitles) {
    if (cue.media_hide_at < position - 150) mediaSubtitles.delete(id);
    if (cue.media_show_at <= position && position < cue.media_hide_at &&
        (!current || cue.media_show_at > current.media_show_at)) current = cue;
  }
  if ((current?.id ?? null) === activeCueId) return;
  activeCueId = current?.id ?? null;
  if (current) {
    clearTimeout(subtitleClearTimer);
    subtitleClearTimer = null;
    renderSubtitle(current.text, current.source_text);
    positionSubtitle();
  } else {
    clearSubtitle();
  }
}

function connectEvents() {
  if (activeWebSocket && activeWebSocket.readyState < WebSocket.CLOSING) {
    activeWebSocket.close();
  }
  const ws = new WebSocket(`ws://${location.host}/ws/events`);
  activeWebSocket = ws;
  ws.addEventListener("open", () => {
    statusNodes.ws.textContent = "已连接";
    void loadHistory();
  });
  ws.addEventListener("close", () => {
    if (activeWebSocket === ws) {
      statusNodes.ws.textContent = "已断开";
      setTimeout(connectEvents, 1500);
    }
  });
  ws.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "subtitle") {
      const logText = `${payload.text || ""}\n${payload.source_text || ""}`;
      log.textContent = `${logText}\n\n${log.textContent}`.slice(0, 1200);
      if (payload.stream_id) {
        bufferMediaSubtitle(payload);
        syncMediaSubtitle();
        return;
      }
      subtitleVersion += 1;
      activeCueId = null;
      const currentVersion = subtitleVersion;
      if (subtitleClearTimer) {
        clearTimeout(subtitleClearTimer);
        subtitleClearTimer = null;
      }
      renderSubtitle(payload.text || "", payload.source_text || "");
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
  const response = await fetch("/api/runtime/start-hls", { method: "POST" });
  if (response.ok) void startVideo((await response.json()).stream_id);
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
  const response = await fetch("/api/runtime/start-asr", { method: "POST" });
  if (response.ok) void startVideo((await response.json()).hls.stream_id);
  await loadStatus();
});

document.querySelector("#capture-view").addEventListener("click", (event) => {
  event.preventDefault();
  window.open(event.currentTarget.href, "_blank", "popup,noopener,width=1280,height=720");
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
  configMessage.textContent = "正在重新载入配置…";
  await loadConfig();
  configMessage.textContent = "配置已重新载入。";
});

document.querySelector("#refresh-qualities").addEventListener("click", async () => {
  await refreshQualities();
});

async function refreshQualities() {
  if (!currentConfig) return;
  const currentQuality = getString("input.quality") || currentConfig.input.quality || "best";
  configMessage.textContent = "正在查询可用画质…";
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
      throw new Error(data.detail || data.error || "未获取到可用画质。");
    }
    const selected = data.qualities.includes(currentQuality) ? currentQuality : data.selected || "best";
    setQualityOptions(data.qualities, selected);
    configMessage.textContent = data.warnings?.length
      ? `已更新画质选项。${data.warnings[data.warnings.length - 1]}`
      : "已更新画质选项。";
  } catch (error) {
    setQualityOptions([currentQuality, "best", "worst"], currentQuality);
    configMessage.textContent = `查询画质失败。${error.message || error}`;
  }
}

document.querySelector("#run-diagnostics").addEventListener("click", async () => {
  diagnosticsOutput.textContent = "正在运行诊断…";
  try {
    const response = await fetch("/api/diagnostics", { cache: "no-store" });
    const data = await response.json();
    diagnosticsOutput.textContent = formatDiagnostics(data);
  } catch (error) {
    diagnosticsOutput.textContent = `诊断失败。${error}`;
  }
});

document.querySelector("#test-deepseek").addEventListener("click", async () => {
  diagnosticsOutput.textContent = "正在测试 DeepSeek…";
  try {
    const response = await fetch("/api/diagnostics/deepseek-test", { method: "POST" });
    const data = await response.json();
    diagnosticsOutput.textContent = formatDeepSeekTest(data);
  } catch (error) {
    diagnosticsOutput.textContent = `DeepSeek 测试失败。${error}`;
  }
});

function formatDiagnostics(data) {
  const lines = [];
  lines.push(`Python　${data.python.version}`);
  lines.push(`FFmpeg　${data.tools.ffmpeg.ok ? data.tools.ffmpeg.version : data.tools.ffmpeg.error}`);
  lines.push(`Streamlink　${data.tools.streamlink.ok ? data.tools.streamlink.version : data.tools.streamlink.error}`);
  lines.push(`代理　${data.network.proxy_enabled ? data.network.proxy_url : "关闭"}`);
  lines.push(`代理环境变量　${data.network.proxy_env_active ? "已启用" : "未启用"}`);
  lines.push(`节目源　${data.input.resolver === "direct" ? "直接读取媒体" : "Streamlink"}　${data.input.url}`);
  lines.push(
    `节目源解析　${data.input.resolution.ok ? "成功" : "失败"}${
      data.input.resolution.error ? ` - ${data.input.resolution.error}` : ""
    }`,
  );
  lines.push(`翻译服务　${data.translation.provider === "mock" ? "模拟翻译" : "DeepSeek V4"}`);
  lines.push(`DeepSeek 密钥　${data.translation.deepseek_api_key_present ? "已配置" : "未配置"}`);
  lines.push(`HLS　${data.hls.playlist_exists ? "已生成播放列表" : "暂无播放列表"}，${data.hls.segment_count} 个分片`);
  return lines.join("\n");
}

function formatDeepSeekTest(data) {
  if (data.ok) {
    return `DeepSeek 连通\n模型　${data.model}\n耗时　${formatLatency(data.latency)}\n结果　${data.text}`;
  }
  return `DeepSeek 测试失败\n模型　${data.model || "-"}\n错误类型　${data.error_type}\n耗时　${formatLatency(
    data.latency,
  )}\n错误详情　${data.error}`;
}

configForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentConfig) {
    configMessage.textContent = "配置仍在加载。";
    return;
  }
  configMessage.textContent = "正在保存并重启播放器服务…";
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
    configMessage.textContent = "配置已保存，播放器服务已重启。";
    await loadConfig();
    await loadStatus();
    resetVideo();
    connectEvents();
  } else {
    configMessage.textContent = "播放器服务在 30 秒内未恢复连接。";
  }
});

loadConfig().then(() => captureMode ? loadStatus() : refreshQualities());
rememberBackendInstance();
loadStatus();
loadHistory();
connectEvents();
setInterval(loadStatus, 3000);
setInterval(loadHistory, 3000);
window.addEventListener("resize", positionSubtitle);
video.addEventListener("loadedmetadata", positionSubtitle);
video.addEventListener("play", positionSubtitle);
for (const event of ["timeupdate", "seeked", "playing", "loadeddata"]) {
  video.addEventListener(event, syncMediaSubtitle);
}
if (video.requestVideoFrameCallback) {
  const renderFrameSubtitle = () => {
    syncMediaSubtitle();
    video.requestVideoFrameCallback(renderFrameSubtitle);
  };
  video.requestVideoFrameCallback(renderFrameSubtitle);
}
