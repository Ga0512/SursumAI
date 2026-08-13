const API = "/api";

let selectedTarget = "local";
let selectedRuntime = "vllm";
let creating = false;

/* ---- auth ---- */
const TOKEN_KEY = "sg_token";
let authMode = "login";
let dashTimer = null;

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function authHeaders() {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function setSession(data) {
  localStorage.setItem(TOKEN_KEY, data.token);
  const u = document.getElementById("userName");
  u.textContent = data.user.name || data.user.email;
  u.classList.remove("hidden");
}

function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  const u = document.getElementById("userName");
  u.classList.add("hidden");
}

function toggleAuth() {
  authMode = authMode === "login" ? "register" : "login";
  document.getElementById("authTitle").textContent =
    authMode === "register" ? "Create your account" : "Welcome back";
  document.getElementById("authSub").textContent =
    authMode === "register"
      ? "Start self-hosting models under your account"
      : "Sign in to manage your deployments";
  document.getElementById("authBtn").textContent =
    authMode === "register" ? "Create account" : "Sign in";
  document.getElementById("authToggle").textContent =
    authMode === "register" ? "Back to sign in" : "Create account";
}

async function submitAuth() {
  const email = document.getElementById("auth_email").value.trim();
  const password = document.getElementById("auth_password").value;
  if (!email || !password) { toast("Email and password required"); return; }
  const url = authMode === "register" ? `${API}/auth/register` : `${API}/auth/login`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) { toast(data.detail || "Auth failed"); return; }
    setSession(data);
    showView("dashboard");
    loadDeploys();
    if (!dashTimer) dashTimer = setInterval(loadDeploys, 5000);
  } catch {
    toast("Could not reach server");
  }
}

function logout() {
  fetch(`${API}/auth/logout`, { method: "POST", headers: authHeaders() }).catch(() => {});
  clearSession();
  showView("landing");
  document.getElementById("auth_email").value = "";
  document.getElementById("auth_password").value = "";
  authMode = "login";
}

function restoreSession() {
  if (!getToken()) return;
  fetch(`${API}/auth/me`, { headers: authHeaders() })
    .then((r) => (r.ok ? r.json() : null))
    .then((user) => {
      if (!user) { clearSession(); showView("landing"); return; }
      const u = document.getElementById("userName");
      u.textContent = user.name || user.email;
      u.classList.remove("hidden");
      showView("dashboard");
      loadDeploys();
      if (!dashTimer) dashTimer = setInterval(loadDeploys, 5000);
    })
    .catch(() => {});
}

const VIEWS = ["landing", "login", "dashboard"];
function showView(name) {
  VIEWS.forEach((v) => document.getElementById(v).classList.toggle("hidden", v !== name));
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2500);
}

/* ---- hero rotator ---- */
const PILLARS = [
  ["open weights", "tc-blue"],
  ["fine-tuned models", "tc-purple"],
  ["your own cloud (BYOC)", "tc-green"],
  ["unlimited tokens", "tc-pink"],
];

const rotator = document.getElementById("rotator");
let pillarIdx = 0;
let charIdx = 0;
let deleting = false;
let paused = false;

function typeTick() {
  if (paused) return;
  const [text, cls] = PILLARS[pillarIdx];
  if (!deleting) {
    charIdx++;
    if (charIdx >= text.length) {
      paused = true;
      setTimeout(() => { paused = false; deleting = true; typeTick(); }, 1800);
    }
  } else {
    charIdx--;
    if (charIdx <= 0) {
      deleting = false;
      pillarIdx = (pillarIdx + 1) % PILLARS.length;
    }
  }
  const [t, c] = PILLARS[pillarIdx];
  rotator.innerHTML = `<span class="token-chip ${c}">${t.slice(0, Math.max(charIdx, 0))}</span>`;
  if (!paused) setTimeout(typeTick, deleting ? 45 : 95);
}
typeTick();

/* ---- model provider picker (Bedrock-style) ---- */
const PROVIDERS = {
  qwen: {
    name: "Qwen",
    models: [
      { id: "Qwen/Qwen3.6-35B-A3B", tag: "Flagship · 2026", runtime: "vllm" },
      { id: "Qwen/Qwen3.6-27B", tag: "Dense · 2026", runtime: "vllm" },
      { id: "Qwen/Qwen3.5-397B-A17B", tag: "Huge MoE · 2026", runtime: "vllm" },
      { id: "Qwen/Qwen3-VL-32B-Instruct", tag: "Vision · 2025", runtime: "vllm" },
      { id: "Qwen/Qwen3-VL-8B-Instruct", tag: "Vision · 2025", runtime: "vllm" },
      { id: "Qwen/Qwen3-4B", tag: "Light · 2025", runtime: "vllm" },
      { id: "Qwen/Qwen3-30B-A3B-GGUF", tag: "MoE · GGUF", runtime: "llama" },
      { id: "Qwen/Qwen3-8B-GGUF", tag: "8B · GGUF", runtime: "llama" },
      { id: "Qwen/Qwen3-VL-8B-Instruct-GGUF", tag: "Vision · GGUF", runtime: "llama" },
      { id: "Qwen/Qwen3-4B-GGUF", tag: "4B · GGUF", runtime: "llama" },
      { id: "Qwen/Qwen3-1.7B-GGUF", tag: "1.7B · GGUF", runtime: "llama" },
      { id: "Qwen/Qwen3-0.6B-GGUF", tag: "0.6B · GGUF", runtime: "llama" },
    ],
  },
  kimi: {
    name: "Kimi",
    models: [
      { id: "moonshotai/Kimi-K3", tag: "Latest · 2026", runtime: "vllm" },
      { id: "moonshotai/Kimi-K2.6", tag: "Agentic · 2026", runtime: "vllm" },
      { id: "moonshotai/Kimi-K2.5", tag: "Multimodal · 2026", runtime: "vllm" },
      { id: "moonshotai/Kimi-VL-A3B-Thinking", tag: "Vision · 2026", runtime: "vllm" },
      { id: "unsloth/Kimi-K2-Instruct-GGUF", tag: "K2 · GGUF", runtime: "llama" },
    ],
  },
  deepseek: {
    name: "DeepSeek",
    models: [
      { id: "deepseek-ai/DeepSeek-V4-Flash", tag: "Flagship · 2026", runtime: "vllm" },
      { id: "deepseek-ai/DeepSeek-V4-Pro", tag: "Pro · 2026", runtime: "vllm" },
      { id: "deepseek-ai/DeepSeek-V4-Flash-0731", tag: "Newest · 2026", runtime: "vllm" },
      { id: "deepseek-ai/DeepSeek-V3.2", tag: "Prev gen", runtime: "vllm" },
      { id: "unsloth/DeepSeek-R1-Distill-Llama-8B-GGUF", tag: "R1 · 8B · GGUF", runtime: "llama" },
      { id: "unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF", tag: "R1 · 1.5B · GGUF", runtime: "llama" },
    ],
  },
  llama: {
    name: "Muse-Glimmer",
    models: [
      { id: "meta-models/Muse-Glimmer-30B", tag: "Muse · 2026", runtime: "vllm" },
      { id: "meta-models/Muse-Glimmer-30B-GGUF", tag: "Muse · GGUF", runtime: "llama" },
    ],
  },
  mistral: {
    name: "Mistral",
    models: [
      { id: "mistralai/Mistral-Small-4-119B-2603", tag: "Flagship · 2026", runtime: "vllm" },
      { id: "mistralai/Mistral-Medium-3.5-128B", tag: "Medium · 2026", runtime: "vllm" },
      { id: "mistralai/Mistral-Large-3-675B-Instruct-2512", tag: "Large · 2025", runtime: "vllm" },
      { id: "unsloth/Mistral-Small-24B-Instruct-2501-GGUF", tag: "Small · 24B · GGUF", runtime: "llama" },
    ],
  },
  bonsai: {
    name: "Bonsai",
    models: [
      { id: "prism-ml/Bonsai-8B-gguf", tag: "1-bit · 1.15GB · 2026", runtime: "llama" },
      { id: "prism-ml/Bonsai-4B-gguf", tag: "1-bit · 0.57GB · 2026", runtime: "llama" },
      { id: "prism-ml/Bonsai-1.7B-gguf", tag: "1-bit · 0.24GB · 2026", runtime: "llama" },
      { id: "prism-ml/Bonsai-27B-gguf", tag: "1-bit · 3.9GB · 2026", runtime: "llama" },
    ],
  },
};

function pickProvider(key) {
  const box = document.getElementById("provider_models");
  const list = document.getElementById("provider_models_list");
  const input = document.getElementById("f_model");
  const prevModel = input.value;
  document.querySelectorAll(".provider").forEach((p) => p.classList.toggle("active", p.dataset.provider === key));

  if (key === "custom") {
    box.classList.add("hidden");
    input.classList.remove("hidden");
    input.value = "";
    input.focus();
    document.getElementById("f_model_hint").textContent = "Paste your finetuned / huggingface model id.";
    return;
  }

  const p = PROVIDERS[key];
  const models = p.models.filter((m) => !m.runtime || m.runtime === selectedRuntime);
  const keep = models.some((m) => m.id === prevModel) ? prevModel : (models.length ? models[0].id : "");
  box.classList.remove("hidden");
  input.classList.add("hidden");
  input.value = "";
  list.innerHTML = models.map((m) => `
    <div class="model-static ${m.id === keep ? "selected" : ""}" data-model="${m.id}" data-runtime="${m.runtime || ""}">
      <span class="pmd">${m.id}</span>
      <span class="pmd-side">
        <span class="pmd-tag">${m.tag}</span>
        <span class="pmd-check">✓</span>
      </span>
    </div>`).join("");
  list.querySelectorAll(".model-static").forEach((el) => {
    el.addEventListener("click", () => {
      list.querySelectorAll(".model-static").forEach((x) => x.classList.remove("selected"));
      el.classList.add("selected");
      const prev = input.value;
      input.value = el.dataset.model;
      if (el.dataset.runtime && el.dataset.runtime !== selectedRuntime) setRuntimeForModel(el.dataset.runtime);
      applyFit(prev);
    });
  });
  input.value = keep;
  if (keep) {
    document.getElementById("f_model_hint").textContent = `${p.name} — select a model to deploy.`;
  } else {
    input.value = "";
    document.getElementById("f_model_hint").textContent = `${p.name} has no models for ${selectedRuntime.toUpperCase()} runtime.`;
  }
}

function pickerProvidersForRuntime() {
  const buttons = document.querySelectorAll(".provider");
  for (const b of buttons) {
    const key = b.dataset.provider;
    if (key === "custom") continue;
    const p = PROVIDERS[key];
    const has = p && p.models.some((m) => !m.runtime || m.runtime === selectedRuntime);
    b.classList.toggle("hidden", !has);
  }
}

/* ---- target segmented control ---- */
function setTarget(t) {
  if (t === "aws") { toast("AWS cloud is coming soon"); return; }
  selectedTarget = t;
  document.querySelectorAll(".target-card").forEach((b) => {
    b.classList.toggle("active", b.dataset.target === t);
  });
}

function setRuntime(r) {
  selectedRuntime = r;
  document.querySelectorAll(".runtime-card").forEach((b) => {
    b.classList.toggle("active", b.dataset.runtime === r);
  });
  pickerProvidersForRuntime();
  const active = document.querySelector(".provider.active");
  let key = active ? active.dataset.provider : "qwen";
  const p = PROVIDERS[key];
  const compatible = p && p.models.some((m) => !m.runtime || m.runtime === r);
  if (key === "custom" || !compatible) {
    const first = document.querySelector(".provider:not(.hidden):not([data-provider=custom])");
    key = first ? first.dataset.provider : "custom";
  }
  if (key !== "custom") pickProvider(key);
}

function setRuntimeForModel(r) {
  setRuntime(r);
  const note = document.getElementById("runtimeNote");
  if (r === "llama") {
    note.textContent = "GGUF model — using llama-server (runs on any machine).";
  } else {
    note.textContent = "Safetensors model — using vLLM for best performance.";
  }
}

/* ---- modal ---- */
let editingId = null;

async function openModal() {
  editingId = null;
  document.getElementById("modalTitle").textContent = "New deployment";
  document.getElementById("deployBtn").textContent = "Deploy";
  document.getElementById("f_model").value = "";
  document.getElementById("f_hf_token").value = "";
  document.getElementById("f_gpus").value = 1;
  document.getElementById("f_mem").value = 0.5;
  document.getElementById("f_len").value = 300;
  document.getElementById("f_tokens").value = 2048;
  document.getElementById("f_temp").value = 0;
  setTarget("local");
  document.getElementById("provider_models").classList.add("hidden");
  document.getElementById("f_model").classList.add("hidden");
  document.getElementById("modal").classList.remove("hidden");
  if (!editingId) pickProvider("qwen");
  await recommendRuntime();
}

async function recommendRuntime() {
  const note = document.getElementById("runtimeNote");
  try {
    const res = await fetch(`${API}/meta/capabilities`);
    const caps = await res.json();
    const rec = caps.recommended_runtime || "llama";
    setRuntime(rec);
    if (rec === "vllm") {
      note.textContent = "NVIDIA GPU detected — using vLLM for best performance.";
    } else if (caps.gpu && !caps.docker) {
      note.textContent = "NVIDIA GPU found but Docker isn't running — using llama-server.";
    } else {
      note.textContent = "No NVIDIA GPU detected — using llama-server (works on any machine).";
    }
  } catch {
    setRuntime("llama");
    note.textContent = "Could not detect your machine — using llama-server.";
  }
}

function closeModal() {
  editingId = null;
  document.getElementById("modal").classList.add("hidden");
  document.getElementById("progress").classList.add("hidden");
  document.getElementById("deployBtn").disabled = false;
}

async function openRedeploy(id) {
  const res = await fetch(`${API}/deploys/${id}`, { headers: authHeaders() });
  const d = await res.json();
  if (!res.ok) { toast(d.detail || "Failed to load deploy"); return; }
  editingId = id;
  document.getElementById("modalTitle").textContent = "Redeploy deployment";
  document.getElementById("deployBtn").textContent = "Redeploy";
  const s = d.spec;
  setModelField(s.model);
  setTarget(s.target || "local");
  setRuntime(s.runtime || "vllm");
  document.getElementById("f_gpus").value = s.gpus || 1;
  document.getElementById("f_mem").value = s.gpu_memory_utilization || 0.5;
  document.getElementById("f_len").value = s.max_model_len || 300;
  document.getElementById("f_tokens").value = s.max_tokens || 2048;
  document.getElementById("f_temp").value = s.temperature ?? 0;
  document.getElementById("f_hf_token").value = "";
  document.getElementById("modal").classList.remove("hidden");
}

async function applyFit(prevModel) {
  const input = document.getElementById("f_model");
  const model = input.value.trim();
  if (!model || model === prevModel) return;
  const hint = document.getElementById("f_model_hint");
  try {
    const res = await fetch(`${API}/meta/model_fit?model=${encodeURIComponent(model)}&runtime=${selectedRuntime}`);
    const fit = await res.json();
    if (!fit.ok || fit.fits === false) {
      hint.textContent = fit.message || "Selected model — customize configuration below if you like.";
      return;
    }
    if (!fit.suggest) { hint.textContent = "Selected model — customize configuration below if you like."; return; }
    document.getElementById("f_mem").value = fit.suggest.gpu_memory_utilization;
    document.getElementById("f_len").value = fit.suggest.max_model_len;
    const gb = (fit.weights_mb / 1024).toFixed(1);
    hint.textContent = `${model} needs ~${gb}GB VRAM for weights; suggested config below is editable.`;
  } catch {
    hint.textContent = "Selected model — customize configuration below if you like.";
  }
}

function setModelField(model) {
  const input = document.getElementById("f_model");
  const prev = input.value;
  for (const [key, p] of Object.entries(PROVIDERS)) {
    const m = p.models.find((m) => m.id === model);
    if (m) {
      if (m.runtime && m.runtime !== selectedRuntime) {
        setRuntime(m.runtime);
      }
      pickProvider(key);
      input.value = model;
      document.querySelectorAll(".model-static").forEach((el) => {
        el.classList.toggle("selected", el.dataset.model === model);
      });
      applyFit(prev);
      return;
    }
  }
  pickProvider("custom");
  input.value = model;
  applyFit(prev);
}

/* ---- dashboard ---- */
const STATUS_LABEL = {
  pending: "Pending", checking: "Checking", provisioning: "Provisioning",
  healthy: "Healthy", failed: "Failed",
  destroying: "Destroying", redeploying: "Redeploying",
};

async function loadDeploys() {
  const res = await fetch(`${API}/deploys`, { headers: authHeaders() });
  const deploys = await res.json();
  render(deploys);
}

function render(deploys) {
  const grid = document.getElementById("sursumaiGrid");
  if (!deploys.length) {
    grid.innerHTML = `
      <div class="empty glass">
        <h3>No deployments yet</h3>
        <p>Deploy your first self-hosted model in one click.</p>
        <br />
        <button class="btn btn-primary" onclick="openModal()">+ New deployment</button>
      </div>`;
    return;
  }
  grid.innerHTML = deploys.map(cardHTML).join("");
}

function cardHTML(d) {
  const status = d.status;
  const meta = `${d.spec.runtime || "vllm"} · ${d.spec.target} · ${d.spec.gpus} GPU${d.spec.gpus > 1 ? "s" : ""} · TP-${d.spec.gpus}`;
  const url = d.endpoint || "…";
  const err = d.error ? `<div class="meta" style="color:#f43f5e">${d.error}</div>` : "";
  const stageLabel = deployStageLabel(d);
  const stage = stageLabel ? `<div class="meta">${stageLabel}</div>` : "";
  const checks = d.preflight && d.preflight.length ? `
    <div class="preflight">
      ${d.preflight.map((c) => `
        <span class="preflight-item ${c.ok ? "ok" : "bad"}">
          ${c.ok ? "✓" : "✗"} ${c.name}: ${c.detail}
        </span>`).join("")}
    </div>` : "";
  const m = d.metrics;
  const metrics = m && status === "healthy" ? `
    <div class="metrics-grid">
      <div class="metric"><span>Output</span><b>${fmtTokens(m.generation_tokens)}</b></div>
      <div class="metric"><span>Input</span><b>${fmtTokens(m.prompt_tokens)}</b></div>
      <div class="metric"><span>Output/s</span><b>${m.generation_tokens_per_s ?? "—"}</b></div>
      <div class="metric"><span>Requests</span><b>${m.requests}</b></div>
      <div class="metric"><span>TTFT</span><b>${m.ttft_avg_ms ? m.ttft_avg_ms + "ms" : "—"}</b></div>
      <div class="metric"><span>Token/s</span><b>${m.output_token_avg_ms ? Math.round(1000 / m.output_token_avg_ms) + "/s" : "—"}</b></div>
      <div class="metric"><span>KV cache</span><b>${m.kv_cache_usage_perc ?? 0}%</b></div>
      <div class="metric"><span>Queue</span><b>${m.num_running} / ${m.num_waiting}</b></div>
    </div>
    <div class="spark-row">
      <span class="spark-label">Output tokens / s</span>
      ${sparkSVG(d.spark)}
    </div>` : "";
  return `
    <div class="deploy-card glass" onclick="openDetail('${d.id}')">
      <div class="row">
        <span class="model">◆ ${d.spec.model}</span>
        <span class="status ${status}"><span class="dot"></span>${STATUS_LABEL[status] || status}</span>
      </div>
      <div class="meta">${meta}</div>
      <div class="row">
        <code class="url">${url}</code>
        <button class="btn btn-ghost" onclick="event.stopPropagation();copyUrl('${d.endpoint || ""}')">Copy</button>
      </div>
      ${err}
      ${stage}
      ${checks}
      ${metrics}
      <div class="card-actions" onclick="event.stopPropagation()">
        <button class="btn" onclick="openDetail('${d.id}')">Details</button>
        <button class="btn" onclick="openLogs('${d.id}')">Logs</button>
        <button class="btn" onclick="openRedeploy('${d.id}')">Redeploy</button>
        <button class="btn" onclick="destroy('${d.id}')">Destroy</button>
      </div>
    </div>`;
}

function sparkSVG(series) {
  if (!series || series.length < 2) return `<div class="spark-empty">—</div>`;
  const W = 260, H = 44, pad = 2;
  const max = Math.max(...series, 1);
  const pts = series.map((v, i) => {
    const x = pad + (i / (series.length - 1)) * (W - pad * 2);
    const y = H - pad - (v / max) * (H - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const area = `${pad},${H - pad} ${pts} ${W - pad},${H - pad}`;
  const last = series[series.length - 1];
  return `
    <svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="sparkfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#4f7cff" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="#4f7cff" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <polygon points="${area}" fill="url(#sparkfill)"/>
      <polyline points="${pts}" fill="none" stroke="#4f7cff" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>
    <span class="spark-last">${last}/s</span>`;
}

function fmtTokens(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

async function copyUrl(url) {
  if (!url) { toast("No endpoint yet"); return; }
  try { await navigator.clipboard.writeText(url); toast("Copied!"); }
  catch { toast(url); }
}

async function destroy(id) {
  if (!confirm(`Destroy ${id.slice(0, 8)}?`)) return;
  await fetch(`${API}/deploys/${id}`, { method: "DELETE", headers: authHeaders() });
  toast("Destroyed");
  loadDeploys();
}

/* ---- logs ---- */
let logsTimer = null;
let logsDeployId = null;

async function openLogs(id) {
  logsDeployId = id;
  document.getElementById("logsTitle").textContent = `Logs — ${id.slice(0, 8)}`;
  document.getElementById("logsModal").classList.remove("hidden");
  await refreshLogs();
  logsTimer = setInterval(refreshLogs, 5000);
}

function closeLogs() {
  if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
  logsDeployId = null;
  document.getElementById("logsModal").classList.add("hidden");
}

async function refreshLogs() {
  if (!logsDeployId) return;
  const el = document.getElementById("logsContent");
  try {
    const res = await fetch(`${API}/deploys/${logsDeployId}/logs?tail=500`, { headers: authHeaders() });
    const data = await res.json();
    if (!res.ok) { el.textContent = data.detail || "Failed to load logs"; return; }
    el.textContent = data.logs || "(no output yet)";
    el.scrollTop = el.scrollHeight;
  } catch {
    el.textContent = "Could not reach server";
  }
}

/* ---- create / redeploy ---- */
async function deploy() {
  if (creating) return;
  const payload = {
    model: document.getElementById("f_model").value.trim(),
    runtime: selectedRuntime,
    target: selectedTarget,
    gpus: parseInt(document.getElementById("f_gpus").value, 10) || 1,
    gpu_memory_utilization: parseFloat(document.getElementById("f_mem").value) || 0.5,
    max_model_len: parseInt(document.getElementById("f_len").value, 10) || 300,
    max_tokens: parseInt(document.getElementById("f_tokens").value, 10) || 2048,
    temperature: parseFloat(document.getElementById("f_temp").value) || 0,
    hf_token: document.getElementById("f_hf_token").value.trim() || undefined,
  };
  if (!payload.model) { toast("Enter a model"); return; }

  creating = true;
  document.getElementById("progress").classList.remove("hidden");
  document.getElementById("progressText").textContent = "Starting deployment…";
  document.getElementById("deployBtn").disabled = true;
  try {
    const url = editingId ? `${API}/deploys/${editingId}/redeploy` : `${API}/deploys`;
    const res = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) { toast(data.detail || "Deploy failed"); document.getElementById("deployBtn").disabled = false; return; }
    await pollUntilHealthy(data.id);
  } finally {
    creating = false;
  }
}

function deployStageLabel(d) {
  const s = d.status;
  if (s === "checking") return "Checking your machine…";
  if (s === "provisioning") return `Provisioning${d.stage ? " — " + d.stage : "…"}`;
  if (s === "redeploying") return "Redeploying…";
  return null;
}

async function pollUntilHealthy(id) {
  for (let i = 0; i < 180; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    let d = null;
    try {
      const res = await fetch(`${API}/deploys/${id}`, { headers: authHeaders() });
      if (res.ok) d = await res.json();
    } catch { /* retry */ }
    const label = d && deployStageLabel(d);
    if (label) document.getElementById("progressText").textContent = label;
    if (d && (d.status === "healthy" || d.status === "failed")) {
      document.getElementById("progress").classList.add("hidden");
      document.getElementById("deployBtn").disabled = false;
      closeModal();
      showView("dashboard");
      loadDeploys();
      if (d.status === "failed") {
        const err = d.error || "Deploy failed";
        toast(err.length > 90 ? err.slice(0, 90) + "…" : err);
      } else {
        toast("Deploy is ready!");
      }
      return;
    }
  }
  document.getElementById("progress").classList.add("hidden");
  document.getElementById("deployBtn").disabled = false;
}

/* ---- deploy detail modal ---- */
let detailId = null;
let detailTimer = null;

function openDetail(id) {
  detailId = id;
  document.getElementById("detailModal").classList.remove("hidden");
  switchTab("metrics");
  refreshDetail();
  if (detailTimer) clearInterval(detailTimer);
  detailTimer = setInterval(refreshDetail, 5000);
}

function closeDetail() {
  if (detailTimer) { clearInterval(detailTimer); detailTimer = null; }
  detailId = null;
  document.getElementById("detailModal").classList.add("hidden");
}

function switchTab(name) {
  document.querySelectorAll("#detailModal .tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll("#detailModal .tab-pane").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "code") renderSnippets();
}

async function refreshDetail() {
  if (!detailId) return;
  const res = await fetch(`${API}/deploys/${detailId}`, { headers: authHeaders() });
  const d = await res.json();
  if (!res.ok) { toast(d.detail || "Failed"); return; }
  renderDetailMetrics(d);
  if (d.status === "healthy") enablePlayground(d);
  else disablePlayground(d.status);
}

function renderDetailMetrics(d) {
  document.getElementById("detailTitle").textContent = d.spec.model;
  document.getElementById("detailSub").textContent =
    `${d.spec.runtime} · ${d.spec.target} · ${d.spec.gpus} GPU · TP-${d.spec.gpus}`;
  const st = document.getElementById("detailStatus");
  st.className = `status ${d.status}`;
  st.innerHTML = `<span class="dot"></span>${STATUS_LABEL[d.status] || d.status}`;
  document.getElementById("detailUrl").textContent = d.endpoint || "…";

  const m = d.metrics;
  if (!m || d.status !== "healthy") {
    document.getElementById("detailMetricsGrid").innerHTML =
      `<div class="metric" style="grid-column:1/-1"><span>Status</span><b>${STATUS_LABEL[d.status] || d.status}</b></div>`;
    document.getElementById("detailSpark").innerHTML = `<div class="spark-empty">—</div>`;
    return;
  }

  const isLlama = m.runtime === "llama";
  // llama.cpp does not expose request/completion counters or KV/TTFT stats.
  const reqVal = isLlama ? "—" : fmtInt(m.requests);
  const failVal = isLlama ? "—" : (m.requests_failed > 0 ? m.requests_failed : "—");
  const kvVal = isLlama ? "—" : (m.kv_cache_usage_perc ? m.kv_cache_usage_perc + "%" : "—");
  const ttftVal = isLlama ? "—" : (m.ttft_avg_ms ? m.ttft_avg_ms + "ms" : "—");
  const latVal = isLlama ? "—" : (m.e2e_latency_avg_ms ? m.e2e_latency_avg_ms + "ms" : "—");

  const cards = [
    ["Output tokens", fmtTokens(m.generation_tokens)],
    ["Output /s", m.generation_tokens_per_s ?? "—"],
    ["Input /s", m.prompt_tokens_per_s ?? "—"],
    ["Requests", reqVal],
    ["Failed", failVal],
    ["Queue", `${m.num_running} / ${m.num_waiting}`],
    ["KV cache", kvVal],
    ["TTFT", ttftVal],
    ["Latency", latVal],
    ["Cached", fmtTokens(m.prompt_tokens_cached)],
  ];
  document.getElementById("detailMetricsGrid").innerHTML =
    cards.map(([k, v]) => `<div class="metric"><span>${k}</span><b>${v}</b></div>`).join("");

  document.getElementById("detailSpark").innerHTML =
    sparkSVG(d.spark) || `<div class="spark-empty">—</div>`;
}

function fmtInt(n) {
  return n == null ? "—" : String(n);
}

/* ---- playground ---- */
const playHistory = [];
let playImageData = null;
let playIsVision = false;
let playController = null;
let playStreaming = false;

function enablePlayground(d) {
  document.getElementById("playInput").disabled = false;
  document.getElementById("playSend").disabled = false;
  playIsVision = !!(d.preflight || []).find((c) => c.name === "vision" && c.ok);
  document.getElementById("playAttach").disabled = !playIsVision;
  if (!playIsVision) removePlayImage();
}

function disablePlayground(status) {
  document.getElementById("playInput").disabled = true;
  document.getElementById("playSend").disabled = true;
  document.getElementById("playAttach").disabled = true;
  removePlayImage();
  const box = document.getElementById("playMessages");
  if (!box.querySelector(".msg")) {
    box.innerHTML = `<div class="play-empty">Deploy is ${(STATUS_LABEL[status] || status).toLowerCase()}. Test available when healthy.</div>`;
  }
}

function pickPlayImage() {
  if (document.getElementById("playAttach").disabled) return;
  document.getElementById("playImageInput").click();
}

function attachPlayImage(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) { toast("Image too large (max 10 MB)"); return; }
  const reader = new FileReader();
  reader.onload = () => {
    playImageData = reader.result;
    document.getElementById("playImageThumb").src = playImageData;
    document.getElementById("playImagePreview").classList.remove("hidden");
  };
  reader.readAsDataURL(file);
  ev.target.value = "";
}

function removePlayImage() {
  playImageData = null;
  document.getElementById("playImagePreview").classList.add("hidden");
  document.getElementById("playImageThumb").removeAttribute("src");
}

function addPlayMsg(role, html, meta) {
  const box = document.getElementById("playMessages");
  const empty = box.querySelector(".play-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = `msg ${role === "user" ? "user" : role === "error" ? "error" : "assistant"}`;
  div.innerHTML = `<div class="role">${role === "user" ? "You" : "Assistant"}</div>${html}` +
    (meta ? `<div class="meta">${meta}</div>` : "");
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

async function sendPlay() {
  const input = document.getElementById("playInput");
  const text = input.value.trim();
  if ((!text && !playImageData) || !detailId) return;
  input.value = "";
  const userHtml = escapeHtml(text) +
    (playImageData ? `<div class="play-image"><img src="${playImageData}" alt="attached image"></div>` : "");
  addPlayMsg("user", `<div class="content">${userHtml}</div>`);
  const content = playImageData
    ? [
        { type: "text", text },
        { type: "image_url", image_url: { url: playImageData } },
      ]
    : text;
  removePlayImage();
  const sendBtn = document.getElementById("playSend");
  const stopBtn = document.getElementById("playStop");
  sendBtn.disabled = true;
  stopBtn.classList.remove("hidden");
  playStreaming = true;
  playController = new AbortController();
  try {
    const res = await fetch(`${API}/deploys/${detailId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ messages: [{ role: "user", content }], max_tokens: 2048, stream: true }),
      signal: playController.signal,
    });
    if (!res.ok) {
      let detail = "Request failed";
      try { detail = (await res.json()).detail || detail; } catch {}
      addPlayMsg("error", `<div class="content">${escapeHtml(detail)}</div>`);
      return;
    }
    // create the assistant bubble up-front so we can fill it token by token
    const box = document.getElementById("playMessages");
    const div = document.createElement("div");
    div.className = "msg assistant";
    div.innerHTML = '<div class="role">Assistant</div><div class="think hidden"></div><div class="content"></div>';
    box.appendChild(div);
    const thinkEl = div.querySelector(".think");
    const contentEl = div.querySelector(".content");

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let usage = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (!data || data === "[DONE]") continue;
          try {
            const obj = JSON.parse(data);
            if (obj.error) { addPlayMsg("error", `<div class="content">${escapeHtml(obj.error)}</div>`); return; }
            if (obj.usage) usage = obj.usage;
            const delta = obj.choices && obj.choices[0] && obj.choices[0].delta;
            if (!delta) continue;
            if (delta.reasoning_content) {
              thinkEl.classList.remove("hidden");
              thinkEl.textContent += delta.reasoning_content;
            }
            if (delta.content) contentEl.textContent += delta.content;
            box.scrollTop = box.scrollHeight;
          } catch {}
        }
      }
    }
    if (!contentEl.textContent && thinkEl.classList.contains("hidden")) {
      contentEl.textContent = "(no content)";
    }
    const meta = usage ? `${usage.prompt_tokens} in · ${usage.completion_tokens} out` : "";
    if (meta) {
      const m = document.createElement("div");
      m.className = "meta";
      m.textContent = meta;
      div.appendChild(m);
    }
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    if (e.name === "AbortError") {
      addPlayMsg("assistant", '<div class="content"><em>stopped</em></div>');
    } else {
      addPlayMsg("error", `<div class="content">Could not reach server</div>`);
    }
  } finally {
    playStreaming = false;
    playController = null;
    stopBtn.classList.add("hidden");
    sendBtn.disabled = false;
  }
}

function stopPlay() {
  if (playController) playController.abort();
}

/* ---- code snippets ---- */
let currentLang = "python";
let currentSnippet = "";

function switchLang(lang) {
  currentLang = lang;
  document.querySelectorAll(".code-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.lang === lang));
  renderSnippets();
}

function buildSnippets(d) {
  const url = d.endpoint ? d.endpoint.replace(/\/v1$/, "") : "http://localhost:YOUR_PORT";
  const model = d.spec.model;
  const python = `import requests

url = "${url}/v1/chat/completions"
payload = {
    "model": "${model}",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 512,
}

resp = requests.post(url, json=payload, timeout=120)
resp.raise_for_status()
print(resp.json()["choices"][0]["message"]["content"])`;

  const js = `const resp = await fetch("${url}/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "${model}",
    messages: [{ role: "user", content: "Hello!" }],
    max_tokens: 512,
  }),
});

const data = await resp.json();
console.log(data.choices[0].message.content);`;

  const curl = `curl ${url}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 512
  }'`;

  return { python, js, curl };
}

async function renderSnippets() {
  if (!detailId) return;
  const res = await fetch(`${API}/deploys/${detailId}`, { headers: authHeaders() });
  const d = await res.json();
  const snippets = buildSnippets(d);
  currentSnippet = snippets[currentLang] || "";
  document.getElementById("codeBlock").textContent = currentSnippet;
}

async function copySnippet() {
  if (!currentSnippet) return;
  try { await navigator.clipboard.writeText(currentSnippet); toast("Copied!"); }
  catch { toast(currentSnippet.slice(0, 60) + "…"); }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---- init ---- */
restoreSession();
