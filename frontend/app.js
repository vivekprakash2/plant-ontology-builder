// Wired to the real backend (server.py -> ontology_builder.agent.answer_question
// + build_ui_panel, plus GET /api/graph for the whole-plant ontology). This is
// a conversation-style console (chat thread on the left) paired with a
// persistent, always-browsable Plant Ontology Explorer (right) -- not a
// fixed single-result dashboard, so open-ended "show me" questions have
// somewhere to land and the knowledge graph is useful even before/without
// asking a question. Builds all dynamic DOM via createElement/textContent/
// createElementNS (never innerHTML with server/LLM-derived content) to
// avoid XSS, since answer text ultimately comes from live LLM output.

const chatThread = document.getElementById("chatThread");
const customQuestion = document.getElementById("customQuestion");
const runButton = document.getElementById("runButton");
const themeToggle = document.getElementById("themeToggle");

const workspace = document.getElementById("workspace");
const chatPanel = document.getElementById("chatPanel");
const panelSplitter = document.getElementById("panelSplitter");
const collapseToggle = document.getElementById("collapseToggle");

const explorerPanel = document.getElementById("explorerPanel");
const tabOverview = document.getElementById("tabOverview");
const tabWalk = document.getElementById("tabWalk");
const viewOverview = document.getElementById("viewOverview");
const viewWalk = document.getElementById("viewWalk");
const graphViewport = document.getElementById("graphViewport");
const plantGraph = document.getElementById("plantGraph");
const inspector = document.getElementById("inspector");
const explorerHint = document.getElementById("explorerHint");
const zoomInBtn = document.getElementById("zoomInBtn");
const zoomOutBtn = document.getElementById("zoomOutBtn");
const zoomResetBtn = document.getElementById("zoomResetBtn");
const zoomStack = document.getElementById("zoomStack");
const walkEmptyHint = document.getElementById("walkEmptyHint");
const graphLegend = document.getElementById("graphLegend");
const edgeTooltip = document.getElementById("edgeTooltip");

const reasoningTrace = document.getElementById("reasoningTrace");
const traceLogBody = document.getElementById("traceLogBody");
const tracePlayBtn = document.getElementById("tracePlayBtn");
const traceStepBtn = document.getElementById("traceStepBtn");
const traceResetBtn = document.getElementById("traceResetBtn");

// --- Drag-to-resize the chat/graph split + collapse-to-right ---------------
// Same pattern as a collapsible canvas/artifact panel: drag the splitter to
// resize, or click its handle to fully hide the explorer panel (chat fills
// the width); the handle becomes a slim reopen tab pinned to the right edge.
const MIN_CHAT_PCT = 25;
const MAX_CHAT_PCT = 70;
let panelResizing = false;

panelSplitter.addEventListener("mousedown", (e) => {
  if (workspace.classList.contains("collapsed")) return; // acts as the reopen tab instead
  if (e.target.closest("#collapseToggle")) return; // clicking the handle collapses, doesn't resize
  panelResizing = true;
  panelSplitter.classList.add("dragging");
  document.body.style.userSelect = "none";
  e.preventDefault();
});

window.addEventListener("mousemove", (e) => {
  if (!panelResizing) return;
  const rect = workspace.getBoundingClientRect();
  let pct = ((e.clientX - rect.left) / rect.width) * 100;
  pct = Math.min(MAX_CHAT_PCT, Math.max(MIN_CHAT_PCT, pct));
  chatPanel.style.flexBasis = `${pct}%`;
});

window.addEventListener("mouseup", () => {
  if (!panelResizing) return;
  panelResizing = false;
  panelSplitter.classList.remove("dragging");
  document.body.style.userSelect = "";
});

collapseToggle.addEventListener("click", (e) => {
  e.stopPropagation();
  const collapsed = workspace.classList.toggle("collapsed");
  collapseToggle.textContent = collapsed ? "‹" : "›";
  collapseToggle.title = collapsed ? "Expand graph panel" : "Collapse graph panel";
  collapseToggle.setAttribute("aria-label", collapseToggle.title);
});
panelSplitter.addEventListener("click", () => {
  if (workspace.classList.contains("collapsed")) collapseToggle.click();
});

// --- Ontology Overview / Reasoning Walk tabs -------------------------------
// The walk view only ever shows the subgraph scoped to a question's answer.
// Before the first question it's an empty state with a hint; the reasoning-
// trace panel / legend / zoom stack only appear once there's something to see.
function applyWalkUiState() {
  if (graphState.revealedIds.size > 0) {
    explorerHint.textContent = `Showing ${graphState.revealedIds.size} of ${graphState.nodes.length} nodes (scoped to this answer)`;
  } else {
    explorerHint.textContent = "Ask a question to see Ellie's reasoning walk";
  }
}

// The legend is a contextual key, not a fixed table of contents: it only
// lists record types actually present in the current scoped subgraph (plus
// the two color-language rows), and it's hidden completely on the Ontology
// Overview, where every node already shows its type as a caption.
function updateLegend() {
  const present = new Set();
  graphState.revealedIds.forEach((id) => {
    const node = graphState.nodesById.get(id);
    if (node) present.add(node.label);
  });
  let shown = 0;
  graphLegend.querySelectorAll(".legend-item").forEach((item) => {
    if (item.dataset.always) {
      item.style.display = "inline-flex";
      return;
    }
    const labels = (item.dataset.labels || "").split(",");
    const visible = labels.some((l) => present.has(l));
    item.style.display = visible ? "inline-flex" : "none";
    if (visible) shown++;
  });
  return shown;
}

// Shows/hides the floating chrome (empty hint, trace panel, legend, zoom
// stack) to match the active tab + whether any answer subgraph is revealed.
function updateWalkChrome() {
  const walkActive = viewWalk.classList.contains("active");
  const walkEmpty = graphState.revealedIds.size === 0;
  walkEmptyHint.style.display = walkActive && walkEmpty ? "flex" : "none";
  reasoningTrace.style.display = walkActive && !walkEmpty ? "flex" : "none";
  zoomStack.style.display = walkActive && walkEmpty ? "none" : "flex";
  // Legend: walk tab only, and only once there's a subgraph to decode.
  if (!walkActive || walkEmpty) {
    graphLegend.style.display = "none";
    return;
  }
  updateLegend();
  graphLegend.style.display = "flex";
}

function showOverviewTab() {
  const wasWalk = viewWalk.classList.contains("active");
  tabOverview.classList.add("active");
  tabWalk.classList.remove("active");
  viewOverview.classList.add("active");
  viewWalk.classList.remove("active");
  explorerHint.textContent = "Type-level schema -- one node per record type";
  // A node pinned on the walk tab must not leak its panel onto the schema view.
  if (wasWalk) unpinInspector();
  updateWalkChrome();
}
function showWalkTab() {
  const wasOverview = viewOverview.classList.contains("active");
  tabOverview.classList.remove("active");
  tabWalk.classList.add("active");
  viewOverview.classList.remove("active");
  viewWalk.classList.add("active");
  if (wasOverview) unpinInspector(); // clean slate on every tab switch
  applyWalkUiState();
  updateWalkChrome();
}
tabOverview.addEventListener("click", showOverviewTab);
tabWalk.addEventListener("click", showWalkTab);

// --- Light mode (default) with a manual toggle, persisted in localStorage ---
// The actual theme attribute is already set by an inline script in
// index.html's <head> (before first paint, to avoid a flash of the wrong
// theme) -- this just keeps the toggle button's icon/label in sync and
// handles clicks.
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("theme", theme);
  themeToggle.textContent = theme === "dark" ? "☀️" : "🌙";
  themeToggle.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
}

themeToggle.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  applyTheme(current === "dark" ? "light" : "dark");
});

applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");

const CONFIDENCE_SCORE = {
  high: 0.93,
  "medium-high": 0.85,
  medium: 0.75,
  low: 0.55,
  "model-reasoned": 0.85,
  "n/a": 0.6,
};

function confidenceClass(label) {
  if (label === "model-reasoned") return "confidence-ai";
  const score = CONFIDENCE_SCORE[label] ?? 0.7;
  if (score >= 0.9) return "confidence-high";
  if (score >= 0.75) return "confidence-medium";
  return "confidence-low";
}

function formatConfidence(label) {
  if (!label || label === "n/a") return "confidence n/a";
  if (label === "model-reasoned") return "AI-reasoned";
  return `${label} confidence`;
}

// --- Minimal, safe Markdown renderer -------------------------------------
// Handles the subset the agent is instructed to use: #/##/### headings,
// bullet/numbered lists, `code`/**bold**/*italic* inline spans, and
// paragraphs. Builds real DOM nodes via createElement/textContent -- never
// innerHTML with model-provided text -- so this cannot introduce XSS even
// though the text ultimately comes from an LLM response.
function appendInlineMarkdown(parent, text) {
  // Code spans are matched FIRST and their content is never re-parsed for
  // bold/italic -- this matters because every historian tag/identifier the
  // model cites is backtick-wrapped prose (e.g.
  // `FAC1.UNIT100.CENTRIFUGAL_PUMP_101.VIB_01`) and is riddled with
  // underscores. Bare underscore-italic (_text_) is intentionally NOT
  // supported at all: it used to treat any two underscores anywhere in a
  // paragraph as an open/close pair, so one tag name with an odd number of
  // underscores (very common -- they're used as a word separator, not
  // emphasis) would swallow everything up to the NEXT unrelated underscore
  // into a single giant <em>, silently eating the underscores out of the
  // tag names in the process and italicizing half the paragraph. The model
  // is told to use backticks for identifiers and **bold** for emphasis, so
  // dropping _italic_ isn't a real loss -- see docs/CHAT_AGENT.md.
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
  let lastIndex = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    const token = match[0];
    let el;
    if (token.startsWith("`")) {
      el = document.createElement("code");
      el.textContent = token.slice(1, -1);
    } else if (token.startsWith("**")) {
      el = document.createElement("strong");
      el.textContent = token.slice(2, -2);
    } else {
      el = document.createElement("em");
      el.textContent = token.slice(1, -1);
    }
    parent.appendChild(el);
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < text.length) {
    parent.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}

function renderMarkdown(container, text) {
  container.innerHTML = "";
  if (!text) return;
  const lines = text.split(/\r?\n/);
  let listEl = null;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      listEl = null;
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      listEl = null;
      // Shift markdown heading levels down so they never outrank the page's own h2/h3.
      const level = Math.min(headingMatch[1].length + 3, 6);
      const h = document.createElement(`h${level}`);
      appendInlineMarkdown(h, headingMatch[2]);
      container.appendChild(h);
      continue;
    }

    const bulletMatch = line.match(/^[-*]\s+(.*)$/);
    const numberedMatch = line.match(/^\d+[.)]\s+(.*)$/);
    if (bulletMatch || numberedMatch) {
      const tag = numberedMatch ? "ol" : "ul";
      if (!listEl || listEl.tagName.toLowerCase() !== tag) {
        listEl = document.createElement(tag);
        container.appendChild(listEl);
      }
      const li = document.createElement("li");
      appendInlineMarkdown(li, (bulletMatch || numberedMatch)[1]);
      listEl.appendChild(li);
      continue;
    }

    listEl = null;
    const p = document.createElement("p");
    appendInlineMarkdown(p, line);
    container.appendChild(p);
  }
}

// Derive a short, plain-text headline from the (possibly long, multi-
// paragraph) Markdown root-cause text, for the big bold summary title.
// The full text is still shown in full via renderMarkdown() in the
// "Show full analysis" dropdown.
function extractHeadline(markdownText, maxLength = 180) {
  if (!markdownText) return "";
  const plain = markdownText
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/[*_]([^*_]+)[*_]/g, "$1")
    .replace(/\r?\n+/g, " ")
    .trim();
  const sentenceMatch = plain.match(/^.*?[.!?](?=\s|$)/);
  let headline = sentenceMatch ? sentenceMatch[0] : plain;
  if (headline.length > maxLength) {
    headline = `${headline.slice(0, maxLength).trim()}...`;
  }
  return headline;
}

// --- Sensor trend charts ---------------------------------------------------
// One small SVG line chart per historian tag actually cited as evidence for
// a given answer. Built entirely via SVG DOM APIs (createElementNS +
// setAttribute/textContent), never innerHTML, so there's no XSS risk even
// though labels ultimately trace back to config/CSV data.
const SVG_NS = "http://www.w3.org/2000/svg";

function formatTrendTime(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts || "";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function directionClass(direction) {
  if (direction === "rising") return "trend-rising";
  if (direction === "falling") return "trend-falling";
  return "trend-stable";
}

function directionArrow(direction) {
  if (direction === "rising") return "▲";
  if (direction === "falling") return "▼";
  return "→";
}

function buildTrendChartSvg(chart) {
  const width = 600;
  const height = 150;
  const marginLeft = 46;
  const marginRight = 12;
  const marginTop = 12;
  const marginBottom = 24;
  const plotW = width - marginLeft - marginRight;
  const plotH = height - marginTop - marginBottom;

  const points = chart.points || [];
  const values = points.map((p) => p.v);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const span = maxV - minV || 1;
  const padV = span * 0.12;
  const loY = minV - padV;
  const hiY = maxV + padV;

  const xAt = (i) => marginLeft + (points.length <= 1 ? 0 : (i / (points.length - 1)) * plotW);
  const yAt = (v) => marginTop + (1 - (v - loY) / (hiY - loY)) * plotH;

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", `trend-chart-svg ${directionClass(chart.direction)}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${chart.label} trend, ${chart.direction}, ${chart.pct_change}% change`);

  // Baseline grid (min/mid/max horizontal guides)
  [0, 0.5, 1].forEach((frac) => {
    const y = marginTop + frac * plotH;
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", String(marginLeft));
    line.setAttribute("x2", String(width - marginRight));
    line.setAttribute("y1", String(y));
    line.setAttribute("y2", String(y));
    line.setAttribute("class", "trend-grid-line");
    svg.appendChild(line);
  });

  // Filled area under the line
  if (points.length > 1) {
    const areaPoints = points.map((p, i) => `${xAt(i)},${yAt(p.v)}`).join(" ");
    const area = document.createElementNS(SVG_NS, "polygon");
    area.setAttribute(
      "points",
      `${marginLeft},${marginTop + plotH} ${areaPoints} ${width - marginRight},${marginTop + plotH}`
    );
    area.setAttribute("class", "trend-area");
    svg.appendChild(area);
  }

  // The trend line itself
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", points.map((p, i) => `${xAt(i)},${yAt(p.v)}`).join(" "));
  line.setAttribute("class", "trend-line");
  line.setAttribute("fill", "none");
  svg.appendChild(line);

  // Y-axis labels (max at top, min at bottom)
  [
    { v: hiY, y: marginTop },
    { v: loY, y: marginTop + plotH },
  ].forEach(({ v, y }) => {
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", String(marginLeft - 6));
    label.setAttribute("y", String(y + (y === marginTop ? 8 : 0)));
    label.setAttribute("class", "trend-axis-label");
    label.setAttribute("text-anchor", "end");
    label.textContent = v.toFixed(1);
    svg.appendChild(label);
  });

  // X-axis labels (start / end timestamps)
  if (points.length > 0) {
    const startLabel = document.createElementNS(SVG_NS, "text");
    startLabel.setAttribute("x", String(marginLeft));
    startLabel.setAttribute("y", String(height - 4));
    startLabel.setAttribute("class", "trend-axis-label");
    startLabel.setAttribute("text-anchor", "start");
    startLabel.textContent = formatTrendTime(points[0].t);
    svg.appendChild(startLabel);

    const endLabel = document.createElementNS(SVG_NS, "text");
    endLabel.setAttribute("x", String(width - marginRight));
    endLabel.setAttribute("y", String(height - 4));
    endLabel.setAttribute("class", "trend-axis-label");
    endLabel.setAttribute("text-anchor", "end");
    endLabel.textContent = formatTrendTime(points[points.length - 1].t);
    svg.appendChild(endLabel);
  }

  return svg;
}

function buildTrendChartCard(chart) {
  const wrap = document.createElement("div");
  wrap.className = "trend-chart";

  const header = document.createElement("div");
  header.className = "trend-chart-header";

  const titleWrap = document.createElement("div");
  const title = document.createElement("p");
  title.className = "trend-chart-title";
  title.textContent = chart.unit ? `${chart.label} (${chart.unit})` : chart.label;
  const sub = document.createElement("p");
  sub.className = "trend-chart-sub muted";
  sub.textContent = `${chart.n_readings} readings · ${formatTrendTime(chart.start_ts)} - ${formatTrendTime(chart.end_ts)}`;
  titleWrap.appendChild(title);
  titleWrap.appendChild(sub);

  const badge = document.createElement("span");
  badge.className = `trend-badge ${directionClass(chart.direction)}`;
  const pct = typeof chart.pct_change === "number" ? chart.pct_change : 0;
  badge.textContent = `${directionArrow(chart.direction)} ${pct > 0 ? "+" : ""}${pct}%`;

  header.appendChild(titleWrap);
  header.appendChild(badge);

  wrap.appendChild(header);
  wrap.appendChild(buildTrendChartSvg(chart));
  return wrap;
}

// ===========================================================================
// Chat thread -- a real running conversation, not a single overwritten
// result panel. Each turn appends a user bubble + an assistant card; rich
// content (timeline/evidence/trend charts/full analysis) lives inside
// collapsible <details> so the thread stays scannable as it grows.
// ===========================================================================

function scrollThreadToBottom() {
  chatThread.scrollTop = chatThread.scrollHeight;
}

// Turn counter shown in the chat-head strip ("Chat with Ellie · N turns").
let chatTurnCount = 0;
const chatHead = document.getElementById("chatHead");

function updateChatHead() {
  // No turn count before the first question -- matches the initial markup, so
  // resetToStart() lands on exactly the opening state.
  chatHead.textContent = chatTurnCount
    ? `Chat with Ellie · ${chatTurnCount} turn${chatTurnCount === 1 ? "" : "s"}`
    : "Chat with Ellie";
}

function addUserBubble(question) {
  const turn = document.createElement("div");
  turn.className = "chat-turn chat-turn-user";
  const p = document.createElement("p");
  p.className = "chat-bubble user-bubble";
  p.textContent = question;
  turn.appendChild(p);
  const ts = document.createElement("div");
  ts.className = "msg-ts";
  ts.textContent = new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  turn.appendChild(ts);
  chatThread.appendChild(turn);
  chatTurnCount += 1;
  updateChatHead();
  scrollThreadToBottom();
}

// Uppercase mini-heading separating the sections of an assistant card
// (mockup's .section-label) -- replaces the old collapsible <details>
// wrappers: recommended actions and evidence are always visible now.
function sectionLabel(text) {
  const p = document.createElement("p");
  p.className = "section-label";
  p.textContent = text;
  return p;
}

// One color per source system, used for the timeline dots -- matches the
// graph legend's record-type colors so the two read as one language.
const SOURCE_COLOR = {
  AM: "#f94144",
  CMMS: "#577590",
  DCS: "#f8961e",
  APM: "#9b5de5",
  ERP: "#43aa8b",
  HIST: "#adb5bd",
};

// How many evidence/timeline entries are visible before the rest collapse
// behind a "Show all N" toggle -- an alarm-flood answer cites 240+ records,
// which would otherwise swallow the whole chat thread.
const TIMELINE_VISIBLE_LIMIT = 6;

// Merge panel.timeline (dated, chronological) with any panel.evidence
// records that aren't already in it (e.g. alarm CONFIG entries, which have
// no timestamp) into one flat entry list for the combined section.
function buildEvidenceEntries(panel) {
  if (!panel) return [];
  const entries = (panel.timeline || []).map((t) => ({ ...t }));
  const seenRefs = new Set(entries.map((t) => t.ref).filter(Boolean));
  (panel.evidence || []).forEach((ev) => {
    if (ev.ref && seenRefs.has(ev.ref)) return;
    if (ev.ref) seenRefs.add(ev.ref);
    entries.push({
      time: "",
      source: (ev.title || "").split(" ")[0] || "",
      text: ev.record || "",
      ref: ev.ref || "",
    });
  });
  return entries;
}

function buildTimelineList(entries) {
  const wrap = document.createElement("div");
  const ul = document.createElement("ul");
  ul.className = "timeline";

  entries.forEach((entry, i) => {
    const li = document.createElement("li");
    if (i >= TIMELINE_VISIBLE_LIMIT) li.classList.add("timeline-overflow");
    li.style.setProperty("--dot", SOURCE_COLOR[entry.source] || "var(--accent-red)");

    const meta = document.createElement("p");
    meta.className = "timeline-meta";
    if (entry.time) {
      const time = document.createElement("span");
      time.textContent = formatTrendTime(entry.time);
      meta.appendChild(time);
    }
    const chip = document.createElement("span");
    chip.className = "source-chip";
    chip.textContent = entry.source;
    meta.appendChild(chip);
    if (entry.ref) {
      const ref = document.createElement("span");
      ref.className = "timeline-ref";
      ref.textContent = entry.ref;
      meta.appendChild(ref);
    }

    const text = document.createElement("p");
    text.className = "timeline-text";
    text.textContent = entry.text;

    li.appendChild(meta);
    li.appendChild(text);
    ul.appendChild(li);
  });
  wrap.appendChild(ul);

  if (entries.length > TIMELINE_VISIBLE_LIMIT) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "timeline-toggle";
    const collapsedLabel = `Show all ${entries.length} records ▾`;
    toggle.textContent = collapsedLabel;
    toggle.addEventListener("click", () => {
      const expanded = ul.classList.toggle("expanded");
      toggle.textContent = expanded ? "Show fewer ▴" : collapsedLabel;
    });
    wrap.appendChild(toggle);
  }
  return wrap;
}

// Ellie's persona picture (same asset as the topbar logo + favicon) and name,
// shown at the top of every assistant message.
function buildAgentId() {
  const agentId = document.createElement("div");
  agentId.className = "agent-id";
  const avatar = document.createElement("img");
  avatar.className = "agent-avatar";
  avatar.src = "/logo.png";
  avatar.alt = "Ellie";
  const agentName = document.createElement("span");
  agentName.className = "agent-name";
  agentName.textContent = "Ellie";
  agentId.appendChild(avatar);
  agentId.appendChild(agentName);
  return agentId;
}

function addPendingAssistantBubble() {
  const turn = document.createElement("div");
  turn.className = "chat-turn chat-turn-assistant";
  const card = document.createElement("div");
  card.className = "chat-bubble assistant-bubble pending";
  card.appendChild(buildAgentId());

  // Kept deliberately minimal -- the live step-by-step reasoning (plan +
  // tool-call trace) is shown once, in the Reasoning trace panel on the
  // graph side, not duplicated here too.
  const p = document.createElement("p");
  p.className = "muted thinking";
  p.textContent = "Thinking... (see the Reasoning trace panel on the right)";
  card.appendChild(p);

  turn.appendChild(card);
  chatThread.appendChild(turn);
  scrollThreadToBottom();
  return turn;
}

function renderAssistantBubble(turnEl, data) {
  const card = document.createElement("div");
  card.className = "chat-bubble assistant-bubble";
  card.appendChild(buildAgentId());

  const usedLlm = data.presented_by && data.presented_by !== "rule-based";

  const headline = document.createElement("h3");
  headline.className = "assistant-headline";
  headline.textContent = data.headline || extractHeadline(data.answer) || "No answer text returned.";
  card.appendChild(headline);

  // One row of provenance pills under the headline -- what it's about, how
  // sure, and who wrote it -- replacing the old run-on muted sentence
  // ("asset: X · rule-based") plus a separately-wrapped confidence badge.
  // The model pill is tinted when an LLM actually reasoned the answer, so
  // "is this the agentic path?" is readable at a glance on stage.
  const meta = document.createElement("div");
  meta.className = "assistant-meta";

  if (data.asset) {
    const assetChip = document.createElement("span");
    assetChip.className = "meta-chip meta-asset";
    const dot = document.createElement("i");
    dot.className = "meta-dot";
    assetChip.appendChild(dot);
    assetChip.appendChild(document.createTextNode(data.asset));
    meta.appendChild(assetChip);
  }

  const confidence = document.createElement("span");
  confidence.className = `confidence ${confidenceClass(data.confidence)}`;
  confidence.textContent = formatConfidence(data.confidence);
  meta.appendChild(confidence);

  const modelChip = document.createElement("span");
  modelChip.className = `meta-chip meta-model${usedLlm ? " is-ai" : ""}`;
  modelChip.title = usedLlm
    ? `Reasoned by ${data.presented_by} using read-only knowledge-graph tools`
    : "Answered by the deterministic rule-based engine (no language model configured)";
  const icon = document.createElement("span");
  icon.className = "meta-icon";
  icon.textContent = usedLlm ? "🤖" : "⚙";
  modelChip.appendChild(icon);
  modelChip.appendChild(document.createTextNode(usedLlm ? data.presented_by : "rule-based"));
  meta.appendChild(modelChip);

  // The model hit its output limit -- say so rather than presenting a
  // half-written answer as complete.
  if (data.truncated) {
    const warn = document.createElement("span");
    warn.className = "meta-chip meta-warn";
    warn.title = "The model reached its output token limit; the answer may be missing its final points.";
    warn.textContent = "⚠ cut off";
    meta.appendChild(warn);
  }

  card.appendChild(meta);

  // Full analysis shown directly (v3 mockup) -- the headline above is the
  // one-line takeaway, this is the reasoning right beneath it, no click
  // needed to read the actual answer.
  const analysisBody = document.createElement("div");
  analysisBody.className = "markdown-body";
  renderMarkdown(analysisBody, data.answer);
  card.appendChild(analysisBody);

  // Section order follows the mockup's argument flow: the evidence backing
  // the root cause, then what to do about it, then the trend charts. All
  // inline -- nothing hidden behind a dropdown; long evidence lists are
  // capped with a "Show all N" toggle instead.
  const panel = data.panel;
  const entries = buildEvidenceEntries(panel);
  if (entries.length > 0) {
    card.appendChild(sectionLabel(`Evidence & timeline (${entries.length})`));
    card.appendChild(buildTimelineList(entries));
  }

  if (data.recommendation) {
    card.appendChild(sectionLabel("Recommended actions"));
    const recBody = document.createElement("div");
    recBody.className = "markdown-body";
    renderMarkdown(recBody, data.recommendation);
    card.appendChild(recBody);
  }

  if (panel && panel.charts && panel.charts.length > 0) {
    const chartsWrap = document.createElement("div");
    chartsWrap.className = "trend-charts";
    panel.charts.forEach((chart) => chartsWrap.appendChild(buildTrendChartCard(chart)));
    card.appendChild(chartsWrap);
  }

  if (panel && panel.entity_id) {
    const focusBtn = document.createElement("button");
    focusBtn.type = "button";
    focusBtn.className = "focus-in-graph-btn";
    focusBtn.textContent = `Focus "${panel.entity}" in the explorer →`;
    focusBtn.addEventListener("click", () => focusAnswerInGraph(panel));
    card.appendChild(focusBtn);
  }

  turnEl.innerHTML = "";
  turnEl.appendChild(card);
  scrollThreadToBottom();
}

// Conversation history sent with each question so the agentic path can
// resolve follow-ups ("what about its work orders?"). Only the last few
// turns, each side truncated -- the server re-validates and the
// deterministic fallback ignores it entirely.
const chatHistory = [];
const HISTORY_MAX_TURNS = 4;
const HISTORY_MAX_CHARS = 2000;

// AbortController for the current /api/chat stream, so resetToStart() (and a
// new question) can cancel one that's still running.
let activeChatAbort = null;

function recordHistoryTurn(question, data) {
  const answerText = [data.headline, data.answer, data.recommendation]
    .filter(Boolean)
    .join("\n")
    .slice(0, HISTORY_MAX_CHARS);
  if (!answerText) return;
  chatHistory.push({ question: question.slice(0, HISTORY_MAX_CHARS), answer: answerText });
  if (chatHistory.length > HISTORY_MAX_TURNS) chatHistory.splice(0, chatHistory.length - HISTORY_MAX_TURNS);
}

async function submitQuestion(question) {
  question = question.trim();
  if (!question) return;
  runButton.disabled = true;
  const starter = document.getElementById("starterPanel");
  if (starter) starter.remove();
  addUserBubble(question);
  const pendingTurn = addPendingAssistantBubble();
  const card = pendingTurn.querySelector(".assistant-bubble");
  const thinkingEl = card.querySelector(".thinking");

  // Reset any highlighting + scoped subgraph left over from a previous
  // answer before this one's live events (if any) start arriving.
  clearHighlight();
  liveWalkReset();
  stopReplay();
  replayIndex = -1;
  resetTraceLog();
  resetWalkScope();
  let sawLiveEvent = false;

  try {
    if (activeChatAbort) activeChatAbort.abort(); // supersede any still-running answer
    activeChatAbort = new AbortController();
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: chatHistory.slice() }),
      signal: activeChatAbort.signal,
    });
    if (!res.ok || !res.body) {
      let errMsg = "request failed";
      try {
        const errData = await res.json();
        errMsg = errData.error || errMsg;
      } catch (parseErr) {
        /* body wasn't JSON (e.g. a plain-text error page) -- keep the generic message */
      }
      thinkingEl.textContent = `Error: ${errMsg}`;
      card.classList.remove("pending");
      return;
    }

    // /api/chat streams Server-Sent Events: one "data: <json>\n\n" block per
    // live event (plan/tool_call/tool_result), ending with one "final"
    // event carrying the full answer -- see ontology_builder/agent.py's
    // stream_answer() and docs/CHAT_AGENT.md §3.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    readLoop: while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sepIndex;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const dataLine = rawEvent.split("\n").find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        let event;
        try {
          event = JSON.parse(dataLine.slice(5).trim());
        } catch (parseErr) {
          continue; // malformed event -- skip rather than crash the whole stream
        }

        if (event.type === "final") {
          renderAssistantBubble(pendingTurn, event);
          recordHistoryTurn(question, event);
          lastAnswerText = [event.headline, event.answer, event.recommendation]
            .filter(Boolean)
            .join(" ");
          // Keep this answer's panel + precomputed walk steps around so the
          // reasoning-trace panel's play/step controls can replay them later,
          // regardless of which path (live SSE or post-hoc) drove them the
          // first time.
          lastPanel = event.panel;
          lastWalkSteps = (event.panel && event.panel.walk) || [];
          replayIndex = lastWalkSteps.length; // already "played" once automatically below
          // If any live tool-call events arrived, the explorer was already
          // walked live (see liveWalkStep below) -- just settle into the
          // final highlighted state. Otherwise (deterministic fallback,
          // which has no live steps) fall back to the post-hoc replay.
          if (sawLiveEvent) {
            liveWalkFinish();
            focusAnswerInGraph(event.panel);
          } else {
            animateGraphWalk(event.panel);
          }
          break readLoop;
        }

        sawLiveEvent = true;
        thinkingEl.style.display = "none";

        if (event.type === "tool_result" && event.walk_step) {
          liveWalkStep(event.walk_step);
        }
        scrollThreadToBottom();
      }
    }
  } catch (err) {
    // An abort is a deliberate cancel (reset-to-start, or a newer question
    // superseding this one) -- not an error, and the turn it belonged to is
    // already gone from the thread, so say nothing.
    if (err && err.name === "AbortError") return;
    thinkingEl.textContent = "Error: could not reach the server.";
    card.classList.remove("pending");
  } finally {
    runButton.disabled = false;
  }
}

runButton.addEventListener("click", () => {
  const question = customQuestion.value;
  customQuestion.value = "";
  submitQuestion(question);
});

customQuestion.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    runButton.click();
  }
});

// Starter panel -- fills the otherwise-empty thread before the first
// question: a one-line intro plus the seven demo questions (TEAM_HANDBOOK.md
// §7) as a tidy single-column list of tappable cards. Removed as soon as the
// first question is asked (see submitQuestion).
function renderStarter(questions) {
  const wrap = document.createElement("div");
  wrap.className = "starter";
  wrap.id = "starterPanel";

  const title = document.createElement("p");
  title.className = "starter-title";
  title.textContent = "Hi, I'm Ellie \u{1F418}";
  const sub = document.createElement("p");
  sub.className = "starter-sub";
  sub.textContent = "Ask me anything about the plant, or start from one of these:";
  wrap.appendChild(title);
  wrap.appendChild(sub);

  const list = document.createElement("div");
  list.className = "starter-list";
  questions.forEach((question) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "starter-q";
    const arrow = document.createElement("span");
    arrow.className = "q-arrow";
    arrow.textContent = "→";
    btn.appendChild(arrow);
    btn.appendChild(document.createTextNode(question));
    btn.addEventListener("click", () => submitQuestion(question));
    list.appendChild(btn);
  });
  wrap.appendChild(list);
  chatThread.appendChild(wrap);
}

// Cached so resetToStart() can re-render the starter panel without refetching.
let starterQuestions = [];

fetch("/api/suggestions")
  .then((r) => r.json())
  .then((data) => {
    starterQuestions = data.questions || [];
    renderStarter(starterQuestions);
  })
  .catch((err) => {
    console.error("Failed to load suggestions", err);
  });

// --- "Back to start" ------------------------------------------------------
// Clicking the AskEllie wordmark/logo returns the app to its opening state.
// Done client-side rather than as a page reload: instant, and it avoids
// re-fetching + re-laying-out all 162 graph nodes. Any in-flight answer is
// aborted (which also closes the server's SSE stream) so a half-streamed
// response can't land in the fresh thread.
function resetToStart() {
  if (activeChatAbort) {
    activeChatAbort.abort();
    activeChatAbort = null;
  }
  stopReplay();
  replayIndex = -1;
  lastPanel = null;
  lastWalkSteps = [];
  lastAnswerText = "";
  chatHistory.length = 0;
  chatTurnCount = 0;
  updateChatHead();
  resetTraceLog();
  clearHighlight();
  resetWalkScope();
  unpinInspector();
  plantGraph.querySelectorAll(".node.anchor").forEach((n) => n.classList.remove("anchor"));
  chatThread.innerHTML = "";
  renderStarter(starterQuestions);
  customQuestion.value = "";
  runButton.disabled = false;
  showOverviewTab();
  chatThread.scrollTop = 0;
}

const brandHome = document.getElementById("brandHome");
if (brandHome) {
  brandHome.addEventListener("click", resetToStart);
  brandHome.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      resetToStart();
    }
  });
}

// ===========================================================================
// Plant Ontology Explorer -- a persistent, always-browsable force-directed
// view of the WHOLE knowledge graph (GET /api/graph), independent of chat.
// Category filter chips reveal/hide record types on demand (Assets + the
// physical process-flow backbone are always shown, to avoid a hairball by
// default). Clicking any node opens an inspector. When a chat answer
// resolves an entity, the matching node + its cited evidence are
// highlighted and the view pans to it -- this is what ties the graph to
// "what the agent just reasoned about", per the redesign brief.
// ===========================================================================

const graphState = {
  nodes: [], // { id, label, props, x, y, el }
  nodesById: new Map(),
  edgesByNode: new Map(), // id -> [{ other, edge }]
  edges: [], // { source, target, type, el, pathEl, hitEl }
  zoom: 1,
  pan: { x: 0, y: 0 },
  selectedId: null, // pinned graph node (Reasoning Walk)
  pinnedType: null, // pinned schema type {label, count} (Ontology Overview)
  canvasWidth: 1200,
  canvasHeight: 900,
  // The Reasoning Walk view never shows the whole plant graph: only nodes in
  // this set (touched by the current question's walk/answer) are visible.
  // Empty set = nothing asked yet = the walk tab shows its empty-state hint.
  revealedIds: new Set(),
};

function nodeLabelText(node) {
  if (node.label === "Asset") return node.props.canonical_name || node.id;
  return node.id;
}

// Simple, dependency-free force-directed layout (Fruchterman-Reingold-ish):
// repulsion between all node pairs, attraction along edges, mild
// centering pull. Cheap enough at this scale (~150-200 nodes) to run
// synchronously once at load, no animation loop needed.
function computeForceLayout(nodes, edges, width, height) {
  const n = nodes.length;
  if (n === 0) return;
  const area = width * height;
  const k = Math.sqrt(area / n) * 0.9;
  const positions = nodes.map((node, i) => {
    const angle = (i / n) * Math.PI * 2;
    const r = Math.min(width, height) * 0.35 * Math.sqrt(Math.random());
    return {
      x: width / 2 + Math.cos(angle) * r + (Math.random() - 0.5) * 20,
      y: height / 2 + Math.sin(angle) * r + (Math.random() - 0.5) * 20,
    };
  });

  const iterations = 220;
  for (let iter = 0; iter < iterations; iter++) {
    const temp = Math.max(width, height) * 0.06 * (1 - iter / iterations);
    const disp = positions.map(() => ({ x: 0, y: 0 }));

    // Repulsion (all pairs) -- O(n^2), fine at this scale.
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = positions[i].x - positions[j].x;
        let dy = positions[i].y - positions[j].y;
        let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const force = (k * k) / dist;
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        disp[i].x += dx;
        disp[i].y += dy;
        disp[j].x -= dx;
        disp[j].y -= dy;
      }
    }

    // Attraction (edges) -- pulls connected nodes together.
    edges.forEach((edge) => {
      const i = edge.sourceIdx;
      const j = edge.targetIdx;
      if (i == null || j == null) return;
      let dx = positions[i].x - positions[j].x;
      let dy = positions[i].y - positions[j].y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const force = (dist * dist) / k;
      dx = (dx / dist) * force;
      dy = (dy / dist) * force;
      disp[i].x -= dx;
      disp[i].y -= dy;
      disp[j].x += dx;
      disp[j].y += dy;
    });

    // Mild centering pull + apply bounded displacement.
    for (let i = 0; i < n; i++) {
      const cx = width / 2 - positions[i].x;
      const cy = height / 2 - positions[i].y;
      disp[i].x += cx * 0.01;
      disp[i].y += cy * 0.01;

      const dLen = Math.sqrt(disp[i].x * disp[i].x + disp[i].y * disp[i].y) || 0.01;
      const capped = Math.min(dLen, temp);
      positions[i].x += (disp[i].x / dLen) * capped;
      positions[i].y += (disp[i].y / dLen) * capped;
      positions[i].x = Math.max(24, Math.min(width - 24, positions[i].x));
      positions[i].y = Math.max(24, Math.min(height - 24, positions[i].y));
    }
  }

  nodes.forEach((node, i) => {
    node.x = positions[i].x;
    node.y = positions[i].y;
  });
}

// Reasoning Walk visibility: a node is shown only once the current question's
// walk/answer has touched it (progressively revealed live, step by step); an
// edge is shown only when both its endpoints are revealed.
function applyWalkVisibility() {
  const revealed = graphState.revealedIds;
  graphState.nodes.forEach((node) => {
    node.el.classList.toggle("hidden-node", !revealed.has(node.id));
  });
  graphState.edges.forEach((edge) => {
    edge.el.classList.toggle("hidden-node", !(revealed.has(edge.source) && revealed.has(edge.target)));
  });
}

// When true, the scoped layout has already been computed for the walk's FULL
// node set, so per-step reveals must not re-lay-it-out (that would shuffle
// every node on each of a 122-step walk). Set by animateGraphWalk, which
// knows all its steps up front; left false for the live agentic path, which
// learns nodes batch by batch and so re-lays-out as it grows.
let walkLayoutLocked = false;

// Pre-lay-out the union of every node a known-in-advance walk will touch and
// frame the camera on that full extent ONCE, then freeze both (see
// walkLayoutLocked). The result is a completely static stage the walk lights
// up step by step -- no drifting zoom, no nodes sliding around.
function lockWalkLayoutFor(steps, panel) {
  const allIds = new Set();
  (steps || []).forEach((s) => (s.node_ids || []).forEach((id) => allIds.add(id)));
  if (panel && panel.entity_id) allIds.add(panel.entity_id);
  // Anything already on screen must be laid out and framed too. A settled
  // answer reveals nodes that no walk STEP touched -- notably the process-flow
  // neighbours from `panel.relationships` (e.g. Heat Exchanger 101 for a P-101
  // answer). Laying out only the step nodes left those stranded at their old
  // positions while the pump moved out from under them.
  graphState.revealedIds.forEach((id) => allIds.add(id));
  if (allIds.size === 0) return;
  assignScopedLayout(allIds);
  fitScopedView(allIds);
  walkLayoutLocked = true;
}

// Adds nodes to the revealed scope and refreshes visibility + the floating
// chrome/hint in one go -- every walk path (live SSE, post-hoc replay, final
// focus) funnels through this.
//
// It also (re)applies the tidy scoped layout and refits the viewport AS THE
// WALK GROWS, not just when it settles. Without this the walk animated over
// the nodes' original whole-plant force-layout positions -- sprawling,
// overlapping labels, and routinely half off-screen because each step also
// panned to a single node. Now every step shows a fully-framed subgraph.
function revealNodes(ids) {
  ids.forEach((id) => {
    if (graphState.nodesById.has(id)) graphState.revealedIds.add(id);
  });
  applyWalkVisibility();
  // While the layout is locked the camera is already framed on the walk's
  // FULL node set (see lockWalkLayoutFor), so neither the positions nor the
  // zoom/pan change per step -- nodes simply light up in place. Re-fitting
  // here is what used to make the graph lurch on every play/step click.
  if (!walkLayoutLocked) {
    assignScopedLayout(graphState.revealedIds);
    fitScopedView();
  }
  applyWalkUiState();
  updateWalkChrome();
}

// New question: the previous answer's subgraph is cleared and the walk tab
// returns to its empty state until the new question's first step arrives.
function resetWalkScope() {
  graphState.revealedIds.clear();
  walkLayoutLocked = false;
  applyWalkVisibility();
  applyWalkUiState();
  updateWalkChrome();
}

function clearHighlight() {
  graphState.nodes.forEach((node) => node.el.classList.remove("highlighted", "dimmed", "context"));
  graphState.edges.forEach((edge) => edge.el.classList.remove("highlighted", "dimmed"));
}

// --- Inspector: transient on hover, pinned on click ------------------------
// Two distinct modes, because they want different behavior:
//   HOVER  -> a peek. Moving the pointer away must always dismiss it (that
//             includes leaving the whole viewport, and switching tabs), so
//             the panel never sits there stale over the canvas.
//   CLICK  -> pinned. Stays put with a ✕ to dismiss, and grows taller +
//             scrolls internally so a node with many properties/connections
//             is actually readable instead of clipped.
function pinInspector(nodeId) {
  const node = graphState.nodesById.get(nodeId);
  if (!node) return;
  graphState.selectedId = nodeId;
  renderInspector(node, { pinned: true });
  const priorSelected = plantGraph.querySelector(".node.selected");
  if (priorSelected) priorSelected.classList.remove("selected");
  node.el.classList.add("selected");
}

// Kept as the old name because panToNode({select:true}) and the inspector's
// own connection list both call it.
function selectNode(nodeId) {
  pinInspector(nodeId);
}

function hideInspector() {
  inspector.classList.remove("show", "pinned");
  inspector.innerHTML = "";
}

function unpinInspector() {
  graphState.selectedId = null;
  graphState.pinnedType = null;
  const priorSelected = plantGraph.querySelector(".node.selected");
  if (priorSelected) priorSelected.classList.remove("selected");
  viewOverview.querySelectorAll(".overview-node.selected").forEach((n) => n.classList.remove("selected"));
  hideInspector();
}

// Pointer left a node (or the viewport): fall back to whatever is pinned on
// the ACTIVE tab, otherwise dismiss entirely. Tab-aware because the two views
// pin different things -- a graph node id vs a schema type -- and restoring
// the wrong one would strand an unrelated panel on screen.
function hideInspectorPreview() {
  if (viewOverview.classList.contains("active")) {
    if (graphState.pinnedType) {
      renderTypeInspector(graphState.pinnedType.label, graphState.pinnedType.count, { pinned: true });
      return;
    }
  } else if (graphState.selectedId && graphState.nodesById.has(graphState.selectedId)) {
    renderInspector(graphState.nodesById.get(graphState.selectedId), { pinned: true });
    return;
  }
  hideInspector();
}

// Safety net: fast pointer moves and nodes that get re-laid-out mid-hover can
// swallow a node's own mouseleave, which is what left the panel stranded.
// Leaving the viewport always clears an unpinned preview.
graphViewport.addEventListener("mouseleave", () => {
  if (!graphState.selectedId && !graphState.pinnedType) hideInspector();
});

// Clicking empty canvas dismisses a pinned panel -- the same "click away to
// close" reflex the ✕ serves, without having to aim for it. Suppressed after a
// drag-pan, which also ends in a click but isn't a dismiss gesture.
graphViewport.addEventListener("click", (e) => {
  if (canvasDidDrag) return;
  if (!graphState.selectedId && !graphState.pinnedType) return;
  if (
    e.target.closest(".graph-node") ||
    e.target.closest(".overview-node") ||
    e.target.closest("#inspector") ||
    e.target.closest("#reasoningTrace") ||
    e.target.closest(".zoom-stack") ||
    e.target.closest(".graph-legend")
  ) {
    return;
  }
  unpinInspector();
});

function renderInspector(node, { pinned = false } = {}) {
  inspector.classList.add("show");
  inspector.classList.toggle("pinned", pinned);
  inspector.innerHTML = "";
  if (pinned) {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "inspector-close";
    close.setAttribute("aria-label", "Close inspector");
    close.textContent = "✕";
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      unpinInspector();
    });
    inspector.appendChild(close);
  }
  // All content lives in a scrollable body so the ✕ stays put when a pinned
  // panel's content overflows.
  const body = document.createElement("div");
  body.className = "inspector-body";
  inspector.appendChild(body);

  const title = document.createElement("p");
  title.className = "inspector-title";
  title.textContent = nodeLabelText(node);
  const typeP = document.createElement("p");
  typeP.className = "muted";
  typeP.textContent = `${node.label} · ${node.id}`;
  body.appendChild(title);
  body.appendChild(typeP);

  const table = document.createElement("dl");
  table.className = "inspector-fields";
  Object.entries(node.props).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "") return;
    if (typeof value === "object") return; // skip system_ids dict, rendered separately below
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    table.appendChild(dt);
    table.appendChild(dd);
  });
  body.appendChild(table);

  if (node.label === "Asset" && node.props.system_ids) {
    const aliasTitle = document.createElement("p");
    aliasTitle.className = "inspector-subtitle";
    aliasTitle.textContent = "Per-system aliases";
    body.appendChild(aliasTitle);
    const list = document.createElement("ul");
    list.className = "inspector-alias-list";
    Object.entries(node.props.system_ids).forEach(([system, localId]) => {
      const li = document.createElement("li");
      li.textContent = `${system}: ${localId}`;
      list.appendChild(li);
    });
    body.appendChild(list);
  }

  const neighbors = graphState.edgesByNode.get(node.id) || [];
  if (neighbors.length > 0) {
    const neighborsTitle = document.createElement("p");
    neighborsTitle.className = "inspector-subtitle";
    neighborsTitle.textContent = `Connections (${neighbors.length})`;
    body.appendChild(neighborsTitle);
    const list = document.createElement("ul");
    list.className = "inspector-alias-list";
    neighbors.slice(0, 25).forEach(({ other, edge }) => {
      const li = document.createElement("li");
      li.textContent = `${edge.type} → ${nodeLabelText(other)}`;
      li.addEventListener("click", () => panToNode(other.id, { select: true }));
      list.appendChild(li);
    });
    body.appendChild(list);
  }
}

function updateGraphTransform() {
  plantGraph.style.transform = `translate(${graphState.pan.x}px, ${graphState.pan.y}px) scale(${graphState.zoom})`;
}

function panToNode(nodeId, { select = false } = {}) {
  const node = graphState.nodesById.get(nodeId);
  if (!node) return;
  const viewportW = graphViewport.clientWidth || 760;
  const viewportH = graphViewport.clientHeight || 420;
  graphState.pan.x = viewportW / 2 - node.x * graphState.zoom;
  graphState.pan.y = viewportH / 2 - node.y * graphState.zoom;
  updateGraphTransform();
  if (select) selectNode(nodeId);
}

// Re-runs the force layout using ONLY the given node ids (instead of
// relying on their positions from the full 162-node layout, where a small
// handful of relevant nodes can end up clustered wherever the whole-graph
// simulation happened to push them -- crowded/overlapping once everything
// else is hidden). Spreads just this subgraph out to fill the viewport,
// then updates each node's transform + every touched edge's curve so the
// scoped view actually looks readable instead of a tangle.
// Rotates + flattens an already force-laid-out node set so it reads as a
// roughly horizontal, gently-upward-sloping trail (matching the mockup)
// instead of the force layout's natural circular/blob spread: finds the
// principal axis (via 2x2 covariance/PCA) of `pcaNodes` (the "backbone" --
// defaults to every node being moved, but callers can pass just the Asset
// nodes so a few thousand densely-clustered AlarmEvent points don't skew
// the axis away from the backbone that's actually meant to read
// horizontal), rotates that axis to horizontal plus a small upward tilt,
// flattens the remaining vertical spread, then (optionally) re-fits
// everything tightly into the layout box with padding.
function orientSubgraphHorizontally(nodesToMove, layoutW, layoutH, options) {
  const opts = options || {};
  const pcaNodes = opts.pcaNodes && opts.pcaNodes.length >= 2 ? opts.pcaNodes : nodesToMove;
  if (nodesToMove.length < 2 || pcaNodes.length < 2) return;
  const flatten = opts.flatten ?? 0.62;
  const tiltDegrees = opts.tiltDegrees ?? -7;
  const refit = opts.refit ?? true;

  const cx = pcaNodes.reduce((s, n) => s + n.x, 0) / pcaNodes.length;
  const cy = pcaNodes.reduce((s, n) => s + n.y, 0) / pcaNodes.length;
  let sxx = 0;
  let syy = 0;
  let sxy = 0;
  pcaNodes.forEach((n) => {
    const dx = n.x - cx;
    const dy = n.y - cy;
    sxx += dx * dx;
    syy += dy * dy;
    sxy += dx * dy;
  });
  const principalAngle = 0.5 * Math.atan2(2 * sxy, sxx - syy);
  const tiltUp = (tiltDegrees * Math.PI) / 180;
  const rotate = -principalAngle + tiltUp;
  const cos = Math.cos(rotate);
  const sin = Math.sin(rotate);
  nodesToMove.forEach((n) => {
    const dx = n.x - cx;
    const dy = n.y - cy;
    n.x = dx * cos - dy * sin;
    n.y = (dx * sin + dy * cos) * flatten;
  });

  if (!refit) {
    // Just recenter in the layout box without forcing a tight
    // bounding-box fit -- keeps the full graph's existing pan/zoom margin
    // instead of stretching it to fill every pixel.
    nodesToMove.forEach((n) => {
      n.x += layoutW / 2;
      n.y += layoutH / 2;
    });
    return;
  }

  const pad = 34;
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  nodesToMove.forEach((n) => {
    minX = Math.min(minX, n.x);
    maxX = Math.max(maxX, n.x);
    minY = Math.min(minY, n.y);
    maxY = Math.max(maxY, n.y);
  });
  const spanX = Math.max(maxX - minX, 1);
  const spanY = Math.max(maxY - minY, 1);
  const scale = Math.min((layoutW - pad * 2) / spanX, (layoutH - pad * 2) / spanY, 1.4);
  nodesToMove.forEach((n) => {
    n.x = pad + (n.x - minX) * scale;
    n.y = pad + (n.y - minY) * scale;
  });
}

// ---------------------------------------------------------------------------
// Deterministic scoped layout -- replaces the old force layout for the
// settled answer view. A force simulation on a hub-and-spoke subgraph just
// splays the leaves at random angles/distances (different every run), which
// read as noise. Instead:
//   - Asset nodes sit on a left-to-right PROCESS BACKBONE, ordered by their
//     revealed FEEDS/COOLS/SUPPLIES_UTILITY chain depth (E-101 -> H-101 ->
//     Column reads like the actual process line).
//   - Each asset's evidence records fan out around it in sorted arcs
//     (grouped by record type, stable order), on concentric rings when
//     there are too many for one ring -- an alarm flood becomes a tidy
//     radial burst instead of a hairball.
//   - Record types with more than LEAF_LABEL_LIMIT instances hide their
//     labels (hover still shows one) -- 120 alarm dots don't need 120
//     overlapping "AME-000xxx" captions.
// Same input -> same picture, every run.
// ---------------------------------------------------------------------------
const LEAF_BASE_RADIUS = 95;
const LEAF_RING_GAP = 58;
const LEAF_LABEL_LIMIT = 12;
const BACKBONE_COL_SPACING = 330;

function assignScopedLayout(nodeIds) {
  const idSet = nodeIds instanceof Set ? nodeIds : new Set(nodeIds);
  const revealedAssets = graphState.nodes.filter((n) => idSet.has(n.id) && n.label === "Asset");
  if (revealedAssets.length === 0) return;
  const assetIds = new Set(revealedAssets.map((n) => n.id));

  // --- Backbone depth: longest revealed asset->asset chain (BFS from roots).
  const out = new Map(revealedAssets.map((n) => [n.id, []]));
  const indeg = new Map(revealedAssets.map((n) => [n.id, 0]));
  graphState.edges.forEach((e) => {
    if (assetIds.has(e.source) && assetIds.has(e.target)) {
      out.get(e.source).push(e.target);
      indeg.set(e.target, indeg.get(e.target) + 1);
    }
  });
  const depth = new Map();
  const queue = revealedAssets.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
  if (queue.length === 0) queue.push(revealedAssets[0].id);
  queue.forEach((id) => depth.set(id, 0));
  for (let qi = 0; qi < queue.length; qi++) {
    const id = queue[qi];
    out.get(id).forEach((target) => {
      // Shortest-path (first-visit) depth -- immune to process-flow cycles
      // like Column -> V-201 -> P-102 -> Column, which longest-path depth
      // would stretch into an artificially long backbone.
      if (!depth.has(target)) {
        depth.set(target, (depth.get(id) || 0) + 1);
        queue.push(target);
      }
    });
  }
  revealedAssets.forEach((n) => {
    if (!depth.has(n.id)) depth.set(n.id, 0);
  });

  // --- Place assets column by column along the backbone.
  const byDepth = new Map();
  revealedAssets
    .slice()
    .sort((a, b) => a.id.localeCompare(b.id))
    .forEach((n) => {
      const d = depth.get(n.id);
      if (!byDepth.has(d)) byDepth.set(d, []);
      byDepth.get(d).push(n);
    });
  // Deep chains compress their column spacing so the whole backbone still
  // fits at a readable zoom.
  const maxDepthVal = Math.max(...byDepth.keys());
  const colSpacing = maxDepthVal >= 3 ? 250 : BACKBONE_COL_SPACING;
  byDepth.forEach((group, d) => {
    group.forEach((n, i) => {
      n.x = d * colSpacing;
      n.y = -d * 24 + (i - (group.length - 1) / 2) * 240;
    });
  });

  // --- Fan each asset's revealed record leaves around it.
  const placed = new Set();
  revealedAssets.forEach((asset) => {
    const seen = new Set();
    const leaves = (graphState.edgesByNode.get(asset.id) || [])
      .map(({ other }) => other)
      .filter(
        (o) =>
          o && idSet.has(o.id) && o.label !== "Asset" && !placed.has(o.id) && !seen.has(o.id) && seen.add(o.id)
      )
      .sort((a, b) => (a.label + a.id).localeCompare(b.label + b.id));
    if (leaves.length === 0) return;

    // Full circle when the asset stands alone; two arcs (above + below)
    // when backbone neighbors exist, keeping the horizontal chain clear.
    const hasBackboneNeighbor = (graphState.edgesByNode.get(asset.id) || []).some(
      ({ other }) => other && other.id !== asset.id && assetIds.has(other.id)
    );
    const arcs = hasBackboneNeighbor ? [[30, 150], [210, 330]] : [[0, 360]];
    const totalArc = arcs.reduce((sum, [a, b]) => sum + (b - a), 0);

    const typeCounts = {};
    leaves.forEach((o) => {
      typeCounts[o.label] = (typeCounts[o.label] || 0) + 1;
    });
    // Wider spacing when captions are visible; tight packing for bulk
    // (label-hidden) dots like an alarm flood.
    const bulky = Object.values(typeCounts).some((c) => c > LEAF_LABEL_LIMIT);
    const spacingPx = bulky ? 34 : 78;

    // Chunk into concentric rings sized by each ring's circumference.
    let remaining = leaves;
    let ring = 0;
    while (remaining.length > 0) {
      const radius = LEAF_BASE_RADIUS + ring * LEAF_RING_GAP;
      const capacity = Math.max(5, Math.floor(((totalArc / 360) * 2 * Math.PI * radius) / spacingPx));
      const items = remaining.slice(0, capacity);
      remaining = remaining.slice(capacity);
      // Alternate rings are offset by half a step so ring 1's labels don't
      // land directly outside ring 0's and collide.
      const ringOffset = ring % 2 === 0 ? 0.5 : 1.0;
      items.forEach((o, j) => {
        let deg = ((j + ringOffset) / items.length) * totalArc;
        for (const [a, b] of arcs) {
          const span = b - a;
          if (deg <= span) {
            deg = a + deg;
            break;
          }
          deg -= span;
        }
        const rad = (deg * Math.PI) / 180;
        o.x = asset.x + radius * Math.cos(rad);
        o.y = asset.y + radius * Math.sin(rad);
        o.el.classList.toggle("label-hidden", (typeCounts[o.label] || 0) > LEAF_LABEL_LIMIT);
        placed.add(o.id);
      });
      ring++;
    }
  });

  // --- Fallback row for records whose parent asset isn't revealed (rare
  // once parents are auto-revealed, but never leave a node at its stale
  // full-graph force position -- that's what made the old view stretch the
  // fit out to a tiny zoom).
  const orphans = graphState.nodes
    .filter((n) => idSet.has(n.id) && n.label !== "Asset" && !placed.has(n.id))
    .sort((a, b) => (a.label + a.id).localeCompare(b.label + b.id));
  if (orphans.length > 0) {
    let placedMaxY = -Infinity;
    let placedMidX = 0;
    let count = 0;
    graphState.nodes.forEach((n) => {
      if (!idSet.has(n.id) || orphans.includes(n)) return;
      placedMaxY = Math.max(placedMaxY, n.y);
      placedMidX += n.x;
      count++;
    });
    placedMidX = count ? placedMidX / count : 0;
    if (placedMaxY === -Infinity) placedMaxY = 0;
    orphans.forEach((o, i) => {
      o.x = placedMidX + (i - (orphans.length - 1) / 2) * 130;
      o.y = placedMaxY + 170;
      o.el.classList.remove("label-hidden");
      placed.add(o.id);
    });
  }

  // --- Center the whole arrangement on the shared canvas + write DOM.
  const nodes = graphState.nodes.filter((n) => idSet.has(n.id));
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  nodes.forEach((n) => {
    minX = Math.min(minX, n.x);
    maxX = Math.max(maxX, n.x);
    minY = Math.min(minY, n.y);
    maxY = Math.max(maxY, n.y);
  });
  const offsetX = graphState.canvasWidth / 2 - (minX + maxX) / 2;
  const offsetY = graphState.canvasHeight / 2 - (minY + maxY) / 2;
  nodes.forEach((n) => {
    n.x += offsetX;
    n.y += offsetY;
    n.el.setAttribute("transform", `translate(${n.x} ${n.y})`);
  });
  // Re-path EVERY edge, not just those with both endpoints in `idSet`. An edge
  // whose endpoints moved but which was skipped here keeps its old geometry and
  // visibly detaches from its own nodes -- that's what broke the
  // Crude Charge Pump 101 -> Heat Exchanger 101 link on replay (the exchanger
  // was revealed by the answer's `relationships`, so it was on screen, but it
  // isn't in any walk STEP, so it fell outside the replay's layout set).
  // Recomputing all ~160 paths is trivially cheap and makes staleness
  // impossible by construction.
  graphState.edges.forEach((edge) => {
    const from = graphState.nodesById.get(edge.source);
    const to = graphState.nodesById.get(edge.target);
    if (!from || !to) return;
    const d = curvedEdgePath(from, to);
    edge.pathEl.setAttribute("d", d);
    edge.hitEl.setAttribute("d", d);
  });
}

// Zoom/pan the walk viewport so the entire scoped subgraph is visible with
// padding -- called when an answer settles (and by the ⤢ fit button).
function fitScopedView(idSet) {
  const ids = idSet || graphState.revealedIds;
  const nodes = graphState.nodes.filter((n) => ids.has(n.id));
  if (nodes.length === 0) return;
  const pad = 90;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  nodes.forEach((n) => {
    minX = Math.min(minX, n.x);
    maxX = Math.max(maxX, n.x);
    minY = Math.min(minY, n.y);
    maxY = Math.max(maxY, n.y);
  });
  const viewportW = graphViewport.clientWidth || 760;
  const viewportH = graphViewport.clientHeight || 420;
  const w = maxX - minX + pad * 2;
  const h = maxY - minY + pad * 2;
  // Allowed to magnify up to 1.6x: a 5-node answer used to render tiny in the
  // middle of a large empty pane (capped at 1.0), which is a big part of why
  // labels were hard to read. Floor 0.3 so even the widest subgraph still
  // fits rather than clipping.
  graphState.zoom = Math.min(1.6, Math.max(0.3, Math.min(viewportW / w, viewportH / h)));
  graphState.pan.x = viewportW / 2 - ((minX + maxX) / 2) * graphState.zoom;
  graphState.pan.y = viewportH / 2 - ((minY + maxY) / 2) * graphState.zoom;
  updateGraphTransform();
}

// Called from a chat answer's "Focus in explorer" action (also triggered
// automatically once an answer renders): turns on whatever category filters
// are needed to reveal the cited evidence, highlights the resolved entity +
// every evidence/relationship node referenced by this specific answer, dims
// everything else, and pans the viewport to the entity. This is the direct
// link between "the agent reasoned about this" and the always-visible graph.
// The full text of the last answer (headline + analysis + recommendations) --
// used to decide which revealed records the answer ACTUALLY CITES by name
// (e.g. "WO-4471", "DCS-A-0450"), so only those get the loud red treatment.
let lastAnswerText = "";

// Which revealed nodes deserve the red "settled" highlight: the resolved
// entity, every revealed asset (the causal backbone), any record id the
// answer text names, and the trended tags shown as charts. Everything else
// the reasoning touched stays visible but quiet (.context) -- this is what
// keeps a 16-record context pull from reading as an explosion of equal-
// weight red dots.
function computeCitedIds(panel) {
  const cited = new Set([panel.entity_id]);
  (panel.charts || []).forEach((c) => {
    if (c.tag) cited.add(c.tag);
  });
  let namedRecords = 0;
  graphState.revealedIds.forEach((id) => {
    const node = graphState.nodesById.get(id);
    if (!node) return;
    if (node.label === "Asset") {
      cited.add(id);
    } else if (lastAnswerText && lastAnswerText.includes(id)) {
      cited.add(id);
      namedRecords++;
    }
  });
  // If the answer named no records at all (e.g. "show me everything about
  // X", whose prose reports counts rather than ids), then every revealed
  // record IS the answer -- dimming the whole subgraph would wrongly say
  // "none of this matters".
  if (namedRecords === 0) graphState.revealedIds.forEach((id) => cited.add(id));
  return cited;
}

function focusAnswerInGraph(panel) {
  if (!panel || !panel.entity_id || !graphState.nodesById.has(panel.entity_id)) return;
  showWalkTab(); // an answer's highlighted evidence lives on the Reasoning Walk tab, not the schema overview

  const refs = new Set([panel.entity_id]);
  (panel.relationships || []).forEach((r) => r.ref && refs.add(r.ref));
  (panel.timeline || []).forEach((t) => t.ref && refs.add(t.ref));
  (panel.evidence || []).forEach((e) => e.ref && refs.add(e.ref));

  // Pull in the parent Asset of every cited record, so cross-asset evidence
  // shows its full hop -- e.g. the Column answer citing E-101's cleaning
  // work order reveals E-101 itself, completing the visible causal chain
  // (E-101 -> H-101 -> Column) instead of leaving the record floating.
  [...refs].forEach((id) => {
    const node = graphState.nodesById.get(id);
    if (!node || node.label === "Asset") return;
    (graphState.edgesByNode.get(id) || []).forEach(({ other }) => {
      if (other && other.label === "Asset") refs.add(other.id);
    });
  });

  // Settling adds nodes the walk itself never stepped on (cited records'
  // parent assets), so release the frozen walk layout and lay out the final
  // set.
  walkLayoutLocked = false;
  revealNodes(refs);

  // Visual hierarchy: red for the backbone + evidence the answer names;
  // quiet ".context" for everything else the reasoning merely touched.
  clearHighlight();
  const cited = computeCitedIds(panel);
  graphState.nodes.forEach((node) => {
    if (!graphState.revealedIds.has(node.id)) return;
    node.el.classList.toggle("highlighted", cited.has(node.id));
    node.el.classList.toggle("context", !cited.has(node.id));
  });
  graphState.edges.forEach((edge) => {
    if (cited.has(edge.source) && cited.has(edge.target)) {
      edge.el.classList.add("highlighted");
    }
  });

  // Ring the entity as the answer's anchor (.anchor, distinct from .selected
  // which means "pinned by a click") WITHOUT pinning the inspector over the
  // graph -- hover/click still opens it on demand.
  unpinInspector();
  plantGraph.querySelectorAll(".node.anchor").forEach((n) => n.classList.remove("anchor"));
  const entityNode = graphState.nodesById.get(panel.entity_id);
  if (entityNode) entityNode.el.classList.add("anchor");

  fitScopedView(); // show the whole scoped subgraph, not a 100% crop of it
}

// Time each walk step stays "current" before advancing, in ms. Long walks
// (e.g. TK-201's alarm flood answer touches 100+ nodes) fast-forward so the
// animation stays seconds, not minutes.
const WALK_STEP_DELAY_MS = 550;
const WALK_FAST_DELAY_MS = 110;
const WALK_FAST_THRESHOLD = 20;

function walkStepDelay(stepCount) {
  return stepCount > WALK_FAST_THRESHOLD ? WALK_FAST_DELAY_MS : WALK_STEP_DELAY_MS;
}

// Plain-language narration log shown in the floating "Reasoning trace"
// panel on the graph viewport -- fed both by the live SSE walk (as it
// happens) and by the post-hoc/manual replay below, so it always reflects
// whatever's actually driving the highlight right now. Each line is
// numbered (step order matters -- it's a walk, not just a list of facts)
// and the log scrolls internally once it outgrows the panel.
let traceLineCount = 0;

function logTraceLine(text, cls) {
  if (!text) return;
  document.querySelectorAll("#traceLogBody .log-entry").forEach((el) => el.classList.remove("latest"));
  traceLineCount += 1;
  const entry = document.createElement("div");
  entry.className = `log-entry latest${cls ? ` ${cls}` : ""}`;
  const n = document.createElement("span");
  n.className = "log-entry-n";
  n.textContent = String(traceLineCount);
  entry.appendChild(n);
  entry.appendChild(document.createTextNode(text));
  traceLogBody.appendChild(entry);
  traceLogBody.scrollTop = traceLogBody.scrollHeight;
}

function resetTraceLog() {
  traceLogBody.innerHTML = "";
  traceLineCount = 0;
}

// Replays `panel.walk` (see agent.py's build_graph_walk()) as an animated,
// step-by-step highlight across the explorer -- "here's where the
// reasoning is looking right now" -- before settling into
// focusAnswerInGraph()'s final all-at-once highlighted state. This is a
// post-hoc replay of the already-completed answer's tool-call/evidence
// trace (used automatically for the deterministic fallback path, which has
// no live SSE steps to show) -- but the exact same steps are also kept
// around afterwards (see lastWalkSteps below) so the user can manually
// replay them later via the reasoning-trace panel's play/step controls.
function animateGraphWalk(panel) {
  const steps = (panel && panel.walk) || [];
  if (!panel || !panel.entity_id || !graphState.nodesById.has(panel.entity_id) || steps.length === 0) {
    focusAnswerInGraph(panel);
    return;
  }
  showWalkTab();
  clearHighlight();

  lockWalkLayoutFor(steps, panel);

  function playStep(i) {
    if (i > 0) {
      // Demote the previous step's nodes/edges from "current" to a
      // fading "visited" trail.
      const prevIds = steps[i - 1].node_ids || [];
      graphState.nodes.forEach((node) => {
        if (prevIds.includes(node.id)) {
          node.el.classList.remove("walk-current");
          node.el.classList.add("walk-visited");
        }
      });
      graphState.edges.forEach((edge) => {
        if (prevIds.includes(edge.source) && prevIds.includes(edge.target)) {
          edge.el.classList.remove("walk-current");
          edge.el.classList.add("walk-visited");
        }
      });
    }

    if (i >= steps.length) {
      // Walk finished -- clear the walk-only classes and settle into the
      // normal "final answer" highlighted state.
      graphState.nodes.forEach((node) => node.el.classList.remove("walk-current", "walk-visited"));
      graphState.edges.forEach((edge) => edge.el.classList.remove("walk-current", "walk-visited"));
      focusAnswerInGraph(panel);
      logTraceLine("Answer settled -- full path highlighted.", "final");
      return;
    }

    const step = steps[i];
    const ids = step.node_ids || [];
    revealNodes(ids); // progressive reveal -- nodes appear as the walk reaches them
    logTraceLine(step.label);
    graphState.nodes.forEach((node) => {
      if (ids.includes(node.id)) node.el.classList.add("walk-current");
    });
    graphState.edges.forEach((edge) => {
      if (ids.includes(edge.source) && ids.includes(edge.target)) edge.el.classList.add("walk-current");
    });
    // No per-step panToNode: revealNodes() already refits the viewport to the
    // whole scoped subgraph, and re-centring on one node was what pushed the
    // rest of the walk off-screen mid-animation.

    setTimeout(() => playStep(i + 1), walkStepDelay(steps.length));
  }

  playStep(0);
}

// --- Live graph walk -- driven by real "tool_result" SSE events as they
// arrive from the server (submitQuestion), instead of animateGraphWalk's
// post-hoc replay of an already-finished trace. Tracks only the previous
// step's node ids so each new step can demote them to a fading trail.
const liveWalk = { prevIds: [] };

function liveWalkReset() {
  liveWalk.prevIds = [];
}

function liveWalkStep(step) {
  const ids = (step && step.node_ids) || [];
  if (ids.length === 0) return;
  showWalkTab();
  revealNodes(ids); // progressive reveal, straight from the live SSE step

  graphState.nodes.forEach((node) => {
    if (liveWalk.prevIds.includes(node.id)) {
      node.el.classList.remove("walk-current");
      node.el.classList.add("walk-visited");
    }
  });
  graphState.edges.forEach((edge) => {
    if (liveWalk.prevIds.includes(edge.source) && liveWalk.prevIds.includes(edge.target)) {
      edge.el.classList.remove("walk-current");
      edge.el.classList.add("walk-visited");
    }
  });

  graphState.nodes.forEach((node) => {
    if (ids.includes(node.id)) node.el.classList.add("walk-current");
  });
  graphState.edges.forEach((edge) => {
    if (ids.includes(edge.source) && ids.includes(edge.target)) edge.el.classList.add("walk-current");
  });
  // No per-step panToNode: revealNodes() already refits the viewport to the
  // whole scoped subgraph, and re-centring on one node was what pushed the
  // rest of the walk off-screen mid-animation.
  if (step.label) {
    logTraceLine(step.label);
  }

  liveWalk.prevIds = ids;
}

function liveWalkFinish() {
  graphState.nodes.forEach((node) => node.el.classList.remove("walk-current", "walk-visited"));
  graphState.edges.forEach((edge) => edge.el.classList.remove("walk-current", "walk-visited"));
  liveWalk.prevIds = [];
  logTraceLine("Answer settled -- full path highlighted.", "final");
}

// --- Manual replay controls (play/step/reset) on the reasoning-trace panel
// itself -- lets the user re-watch the walk that just answered (or any
// earlier answer, since lastPanel/lastWalkSteps are overwritten per turn),
// independent of whichever path (live SSE or post-hoc) originally drove it.
let lastPanel = null;
let lastWalkSteps = [];
let replayIndex = -1;
let replayTimer = null;

function setPlayIcon(playing) {
  tracePlayBtn.textContent = playing ? "⏸" : "▶";
  tracePlayBtn.classList.toggle("playing", playing);
  tracePlayBtn.title = playing ? "Pause" : "Play walk";
  tracePlayBtn.setAttribute("aria-label", tracePlayBtn.title);
  tracePlayBtn.dataset.tip = tracePlayBtn.title;
}

function stopReplay() {
  clearInterval(replayTimer);
  replayTimer = null;
  setPlayIcon(false);
}

// Applies one step's node/edge classes (current + demoting the previous
// step to a fading "visited" trail) -- the same visual language as
// animateGraphWalk()/liveWalkStep() above, just driven by manual
// play/step clicks instead of a timer or live SSE events.
function applyReplayStep(i) {
  if (i > 0) {
    const prevIds = lastWalkSteps[i - 1].node_ids || [];
    graphState.nodes.forEach((node) => {
      if (prevIds.includes(node.id)) {
        node.el.classList.remove("walk-current");
        node.el.classList.add("walk-visited");
      }
    });
    graphState.edges.forEach((edge) => {
      if (prevIds.includes(edge.source) && prevIds.includes(edge.target)) {
        edge.el.classList.remove("walk-current");
        edge.el.classList.add("walk-visited");
      }
    });
  }

  if (i >= lastWalkSteps.length) {
    graphState.nodes.forEach((node) => node.el.classList.remove("walk-current", "walk-visited"));
    graphState.edges.forEach((edge) => edge.el.classList.remove("walk-current", "walk-visited"));
    if (lastPanel) focusAnswerInGraph(lastPanel);
    logTraceLine("Answer settled -- full path highlighted.", "final");
    stopReplay();
    return;
  }

  const step = lastWalkSteps[i];
  const ids = step.node_ids || [];
  revealNodes(ids);
  graphState.nodes.forEach((node) => {
    if (ids.includes(node.id)) node.el.classList.add("walk-current");
  });
  graphState.edges.forEach((edge) => {
    if (ids.includes(edge.source) && ids.includes(edge.target)) edge.el.classList.add("walk-current");
  });
  // See the note in animateGraphWalk: revealNodes() keeps the whole scoped
  // subgraph framed, so no per-step re-centring.
  logTraceLine(step.label);
}

tracePlayBtn.addEventListener("click", () => {
  if (replayTimer) {
    stopReplay();
    return;
  }
  if (!lastWalkSteps.length) return;
  if (replayIndex >= lastWalkSteps.length) {
    replayIndex = -1;
    resetTraceLog();
  }
  clearHighlight();
  showWalkTab();
  lockWalkLayoutFor(lastWalkSteps, lastPanel); // stable positions across the replay
  setPlayIcon(true);
  replayTimer = setInterval(() => {
    replayIndex++;
    applyReplayStep(replayIndex);
    if (replayIndex >= lastWalkSteps.length) stopReplay();
  }, walkStepDelay(lastWalkSteps.length));
});

traceStepBtn.addEventListener("click", () => {
  stopReplay(); // manual stepping takes over from any running auto-play
  if (!lastWalkSteps.length) return;
  if (replayIndex >= lastWalkSteps.length) {
    replayIndex = -1;
    resetTraceLog();
    clearHighlight();
  }
  showWalkTab();
  lockWalkLayoutFor(lastWalkSteps, lastPanel);
  replayIndex++;
  applyReplayStep(replayIndex);
});

traceResetBtn.addEventListener("click", () => {
  stopReplay();
  replayIndex = -1;
  resetTraceLog();
  clearHighlight();
});

// ===========================================================================
// Ontology Overview -- a small, ALWAYS-BOUNDED schema/type-level diagram (one
// node per record type + a live count, computed from the same /api/graph
// payload the Reasoning Walk tab uses), instead of the per-instance graph.
// This answers "why isn't the whole plant graph shown at once": with 100+
// AlarmEvent instances alone, rendering every record would be a hairball --
// this view is bounded at one node per type no matter how much data exists.
// ===========================================================================
// Pan/zoom for the overview -- one translate+scale transform on the group
// that wraps the whole schema diagram, in viewBox (300x260) units.
const OVERVIEW_W = 300;
const OVERVIEW_H = 260;
const overviewView = { zoom: 1, panX: 0, panY: 0, group: null };

function applyOverviewTransform() {
  if (!overviewView.group) return;
  overviewView.group.setAttribute(
    "transform",
    `translate(${overviewView.panX} ${overviewView.panY}) scale(${overviewView.zoom})`
  );
}

function setOverviewZoom(newZoom, anchorX, anchorY) {
  newZoom = Math.min(4, Math.max(0.5, newZoom));
  // Keep the point under (anchorX, anchorY) stationary while zooming.
  overviewView.panX = anchorX - (anchorX - overviewView.panX) * (newZoom / overviewView.zoom);
  overviewView.panY = anchorY - (anchorY - overviewView.panY) * (newZoom / overviewView.zoom);
  overviewView.zoom = newZoom;
  applyOverviewTransform();
}

function overviewPointFromEvent(e) {
  const svg = viewOverview.querySelector("svg");
  if (!svg) return { x: OVERVIEW_W / 2, y: OVERVIEW_H / 2 };
  const rect = svg.getBoundingClientRect();
  return {
    x: ((e.clientX - rect.left) / rect.width) * OVERVIEW_W,
    y: ((e.clientY - rect.top) / rect.height) * OVERVIEW_H,
  };
}

const OVERVIEW_TYPE_COLOR = {
  Asset: "#f9c74f",
  AlarmEvent: "#f94144",
  AlarmConfig: "#f94144",
  WorkOrder: "#577590",
  OperatorAction: "#f8961e",
  HealthEvent: "#9b5de5",
  CostPosting: "#43aa8b",
  HistorianTag: "#adb5bd",
};

const OVERVIEW_TYPE_INFO = {
  Asset: {
    source: "Stage 1 entity resolution",
    fields: "canonical_name, confidence, system_ids",
    relations: "FEEDS / COOLS / SUPPLIES_UTILITY (asset → asset)",
  },
  AlarmEvent: { source: "AM", fields: "priority, value, state, timestamp" },
  AlarmConfig: { source: "AM", fields: "HH/H/L/LL limits, deadband" },
  WorkOrder: { source: "CMMS", fields: "status, type, technician" },
  OperatorAction: { source: "DCS", fields: "setpoint change, operator, time" },
  HealthEvent: { source: "APM", fields: "condition, severity, notes" },
  CostPosting: { source: "ERP", fields: "amount, linked_wo" },
  HistorianTag: { source: "Historian", fields: "description, unit, min, max" },
};

// Fixed layout (viewBox 0-300 x 0-260) -- the schema shape doesn't change at
// runtime, only the counts do, so static positions are fine.
const OVERVIEW_LAYOUT = [
  { label: "AlarmEvent", edgeType: "HAS_ALARM", x: 90, y: 55 },
  { label: "AlarmConfig", edgeType: "HAS_ALARM_CONFIG", x: 168, y: 32 },
  { label: "WorkOrder", edgeType: "HAS_WORK_ORDER", x: 238, y: 58 },
  { label: "OperatorAction", edgeType: "HAS_SETPOINT_CHANGE", x: 268, y: 128 },
  { label: "HealthEvent", edgeType: "HAS_HEALTH_EVENT", x: 240, y: 198 },
  { label: "CostPosting", edgeType: "HAS_COST_POSTING", x: 168, y: 224 },
  { label: "HistorianTag", edgeType: "HAS_HISTORIAN_TAG", x: 92, y: 205 },
];

// Per-type instance ids + the relation each type hangs off, indexed once when
// the schema view renders. This is what gives the type inspector enough
// content to be worth pinning and scrolling: "AlarmEvent, 126 instances, via
// HAS_ALARM, e.g. AME-000001 …".
const overviewTypeIndex = new Map(); // label -> { ids: [...], relation, linkCount }

// How many instance ids a pinned type panel lists before saying "+N more".
const TYPE_INSTANCE_LIMIT = 40;

function renderTypeInspector(label, count, { pinned = false } = {}) {
  inspector.classList.add("show");
  inspector.classList.toggle("pinned", pinned);
  inspector.innerHTML = "";
  if (pinned) {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "inspector-close";
    close.setAttribute("aria-label", "Close inspector");
    close.textContent = "✕";
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      unpinInspector();
    });
    inspector.appendChild(close);
  }
  const body = document.createElement("div");
  body.className = "inspector-body";
  inspector.appendChild(body);

  const title = document.createElement("p");
  title.className = "inspector-title";
  title.textContent = label;
  body.appendChild(title);
  const sub = document.createElement("p");
  sub.className = "muted";
  sub.textContent = `Type node · ${count} instance${count === 1 ? "" : "s"}`;
  body.appendChild(sub);

  const info = OVERVIEW_TYPE_INFO[label];
  const indexed = overviewTypeIndex.get(label);
  const table = document.createElement("dl");
  table.className = "inspector-fields";
  const addRow = (key, value) => {
    if (!value) return;
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    table.append(dt, dd);
  };
  if (info) {
    addRow("source system", info.source);
    addRow("fields", info.fields);
    addRow("relations", info.relations);
  }
  if (indexed && indexed.relation) {
    addRow(
      "connected via",
      `${indexed.relation} (${indexed.linkCount} link${indexed.linkCount === 1 ? "" : "s"})`
    );
  }
  if (table.childElementCount) body.appendChild(table);

  // Instance ids -- only on a pinned panel. A hover peek stays short (it's a
  // glance, and it must not grow tall enough to cover the diagram); pinning
  // is the deliberate "show me more" action, so that's where the list lives.
  if (pinned && indexed && indexed.ids.length) {
    const listTitle = document.createElement("p");
    listTitle.className = "inspector-subtitle";
    listTitle.textContent = `Instances (${indexed.ids.length})`;
    body.appendChild(listTitle);
    const list = document.createElement("ul");
    list.className = "inspector-alias-list inspector-instance-list";
    indexed.ids.slice(0, TYPE_INSTANCE_LIMIT).forEach((id) => {
      const li = document.createElement("li");
      li.textContent = id;
      list.appendChild(li);
    });
    body.appendChild(list);
    if (indexed.ids.length > TYPE_INSTANCE_LIMIT) {
      const more = document.createElement("p");
      more.className = "insp-more muted";
      more.textContent = `+${indexed.ids.length - TYPE_INSTANCE_LIMIT} more`;
      body.appendChild(more);
    }
  } else if (!pinned) {
    const hint = document.createElement("p");
    hint.className = "insp-more muted";
    hint.textContent = "Click to pin and list instances";
    body.appendChild(hint);
  }
}

// Pinned schema-type node (the overview counterpart of pinInspector()).
function pinTypeInspector(label, count) {
  graphState.selectedId = null; // a type node isn't a graph node id
  graphState.pinnedType = { label, count };
  viewOverview.querySelectorAll(".overview-node.selected").forEach((n) => n.classList.remove("selected"));
  const node = viewOverview.querySelector(`.overview-node[data-type="${label}"]`);
  if (node) node.classList.add("selected");
  renderTypeInspector(label, count, { pinned: true });
}

function overviewNode(cx, cy, r, color, label, count, labelInside = false) {
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "overview-node");
  g.setAttribute("data-type", label);
  g.setAttribute("transform", `translate(${cx} ${cy})`);

  const circle = document.createElementNS(SVG_NS, "circle");
  circle.setAttribute("r", String(r));
  circle.setAttribute("stroke", color);
  circle.setAttribute("fill", color);
  circle.setAttribute("fill-opacity", "0.16");
  g.appendChild(circle);

  const countText = document.createElementNS(SVG_NS, "text");
  countText.setAttribute("class", "overview-count");
  countText.setAttribute("y", labelInside ? "-2" : "3");
  countText.textContent = String(count);
  g.appendChild(countText);

  // The big hub circle keeps its name inside itself (there's room, and every
  // edge converges on it so there's nowhere clean outside); satellites put
  // theirs just below their circle.
  const labelText = document.createElementNS(SVG_NS, "text");
  labelText.setAttribute("class", "overview-label");
  labelText.setAttribute("y", labelInside ? "10" : String(r + 11));
  labelText.textContent = label;
  g.appendChild(labelText);

  // Same two-mode inspector as the walk view: hover = transient peek that
  // always dismisses, click = pinned panel with a ✕ that scrolls its own
  // content.
  g.addEventListener("mouseenter", () => {
    if (graphState.pinnedType && graphState.pinnedType.label === label) return; // already pinned open
    renderTypeInspector(label, count);
  });
  g.addEventListener("mouseleave", () => hideInspectorPreview());
  g.addEventListener("click", (e) => {
    e.stopPropagation();
    pinTypeInspector(label, count);
  });
  return g;
}

// Schema edge with an always-visible relation name at its midpoint, a hover
// highlight, and a cursor tooltip carrying the full name + live record count.
function overviewEdge(parent, x1, y1, x2, y2, labelText, tooltipText, r1 = 0, r2 = 0) {
  // Trim both ends back to each circle's boundary (plus a 2px breathing gap)
  // instead of drawing center-to-center -- a line crossing under the node
  // fill reads as "through", not "connected to".
  const fullDx = x2 - x1;
  const fullDy = y2 - y1;
  const fullLen = Math.sqrt(fullDx * fullDx + fullDy * fullDy) || 1;
  const ux = fullDx / fullLen;
  const uy = fullDy / fullLen;
  const gap = 2;
  x1 += ux * (r1 + gap);
  y1 += uy * (r1 + gap);
  x2 -= ux * (r2 + gap);
  y2 -= uy * (r2 + gap);

  const line = document.createElementNS(SVG_NS, "line");
  line.setAttribute("x1", String(x1));
  line.setAttribute("y1", String(y1));
  line.setAttribute("x2", String(x2));
  line.setAttribute("y2", String(y2));
  line.setAttribute("class", "overview-edge");
  parent.appendChild(line);

  const hit = document.createElementNS(SVG_NS, "line");
  hit.setAttribute("x1", String(x1));
  hit.setAttribute("y1", String(y1));
  hit.setAttribute("x2", String(x2));
  hit.setAttribute("y2", String(y2));
  hit.setAttribute("class", "overview-edge-hit");
  hit.addEventListener("mousemove", (e) => {
    line.classList.add("hover");
    showEdgeTooltip(e, tooltipText);
  });
  hit.addEventListener("mouseleave", () => {
    line.classList.remove("hover");
    hideEdgeTooltip();
  });
  parent.appendChild(hit);

  // Relation name rotated to run along the edge itself (kept upright), and
  // nudged off the line along its perpendicular -- with seven edges
  // converging on one hub, horizontal labels would pile into each other.
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  let angle = (Math.atan2(dy, dx) * 180) / Math.PI;
  if (angle > 90) angle -= 180;
  if (angle < -90) angle += 180;
  const text = document.createElementNS(SVG_NS, "text");
  text.setAttribute("class", "overview-edge-label");
  text.setAttribute(
    "transform",
    `translate(${mx + (-dy / len) * 5} ${my + (dx / len) * 5}) rotate(${angle})`
  );
  text.textContent = labelText;
  parent.appendChild(text);
}

// Satellite node radius grows gently with how many records sit behind it,
// so "126 alarm events" visibly outweighs "2 cost postings".
function overviewRadius(count) {
  return Math.min(23, 11 + Math.sqrt(count) * 1.1);
}

function renderOntologyOverview(rawNodes, rawEdges) {
  const counts = {};
  rawNodes.forEach((n) => {
    counts[n.label] = (counts[n.label] || 0) + 1;
  });
  const edgeCounts = {};
  rawEdges.forEach((e) => {
    edgeCounts[e.type] = (edgeCounts[e.type] || 0) + 1;
  });

  // Index each type's instance ids (sorted, so a pinned panel is stable) plus
  // the relation it hangs off -- content for the type inspector.
  overviewTypeIndex.clear();
  rawNodes.forEach((n) => {
    if (!overviewTypeIndex.has(n.label)) {
      overviewTypeIndex.set(n.label, { ids: [], relation: null, linkCount: 0 });
    }
    overviewTypeIndex.get(n.label).ids.push(n.id);
  });
  overviewTypeIndex.forEach((entry) => entry.ids.sort());
  OVERVIEW_LAYOUT.forEach((item) => {
    const entry = overviewTypeIndex.get(item.label);
    if (entry) {
      entry.relation = item.edgeType;
      entry.linkCount = edgeCounts[item.edgeType] || 0;
    }
  });
  const assetEntry = overviewTypeIndex.get("Asset");
  if (assetEntry) {
    const topo = ["FEEDS", "COOLS", "SUPPLIES_UTILITY"].reduce((s, t) => s + (edgeCounts[t] || 0), 0);
    assetEntry.relation = "FEEDS / COOLS / SUPPLIES_UTILITY";
    assetEntry.linkCount = topo;
  }

  viewOverview.innerHTML = "";
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 300 260");
  svg.setAttribute("class", "overview-svg");

  // Everything renders inside one group so pan/zoom is a single transform
  // (same translate+scale approach as the walk view).
  const zoomG = document.createElementNS(SVG_NS, "g");
  overviewView.group = zoomG;
  overviewView.zoom = 1;
  overviewView.panX = 0;
  overviewView.panY = 0;
  svg.appendChild(zoomG);

  const centerX = 150;
  const centerY = 128;
  const centerR = 24;

  const visibleTypes = OVERVIEW_LAYOUT.filter((item) => counts[item.label]);

  // Edges first, so nodes render on top of them.
  visibleTypes.forEach((item) => {
    const n = edgeCounts[item.edgeType] || 0;
    overviewEdge(
      zoomG,
      centerX,
      centerY,
      item.x,
      item.y,
      item.edgeType,
      `Asset —${item.edgeType}→ ${item.label} · ${n} link${n === 1 ? "" : "s"}`,
      centerR,
      overviewRadius(counts[item.label])
    );
  });

  // Asset-to-asset process topology (FEEDS/COOLS/SUPPLIES_UTILITY) is
  // deliberately NOT drawn as a self-loop here -- it cluttered the diagram
  // badly. It lives in the Asset hub's hover inspector instead (see
  // OVERVIEW_TYPE_INFO.Asset / renderTypeInspector).

  visibleTypes.forEach((item) => {
    zoomG.appendChild(
      overviewNode(
        item.x,
        item.y,
        overviewRadius(counts[item.label]),
        OVERVIEW_TYPE_COLOR[item.label],
        item.label,
        counts[item.label]
      )
    );
  });

  zoomG.appendChild(
    overviewNode(centerX, centerY, centerR, OVERVIEW_TYPE_COLOR.Asset, "Asset", counts.Asset || 0, true)
  );

  viewOverview.appendChild(svg);
}

// Gentle quadratic-bezier curve between two nodes (perpendicular offset
// proportional to the segment length, capped) instead of a straight line --
// purely cosmetic (matches design_mockups/frontend_redesign_v3.html), and
// it also helps separate edges that would otherwise overlap when several
// converge on the same hub node.
function curvedEdgePath(fromNode, toNode) {
  const dx = toNode.x - fromNode.x;
  const dy = toNode.y - fromNode.y;
  const dist = Math.sqrt(dx * dx + dy * dy) || 1;
  const curvature = Math.min(dist * 0.18, 45);
  const mx = (fromNode.x + toNode.x) / 2;
  const my = (fromNode.y + toNode.y) / 2;
  const nx = -dy / dist;
  const ny = dx / dist;
  const cx = mx + nx * curvature;
  const cy = my + ny * curvature;

  // Pull each endpoint back to its node's boundary (+2px gap) so the curve
  // meets the circle instead of running under it. A quadratic bezier's
  // tangent at each end points at the control point, so that's the
  // direction to trim along.
  const [sx, sy] = trimToward(fromNode.x, fromNode.y, cx, cy, nodeRadius(fromNode) + 2);
  const [ex, ey] = trimToward(toNode.x, toNode.y, cx, cy, nodeRadius(toNode) + 2);
  return `M ${sx} ${sy} Q ${cx} ${cy} ${ex} ${ey}`;
}

// Move (px,py) `amount` px toward (tx,ty).
function trimToward(px, py, tx, ty, amount) {
  const dx = tx - px;
  const dy = ty - py;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  const t = Math.min(amount / len, 0.45); // never collapse a short edge to nothing
  return [px + dx * t, py + dy * t];
}

function nodeRadius(node) {
  return (node && NODE_RADIUS[node.label]) || 7;
}

// Cursor-following tooltip shared by both graph views -- instant and styled,
// instead of the browser's slow native <title> tooltip.
function showEdgeTooltip(e, text) {
  const rect = graphViewport.getBoundingClientRect();
  edgeTooltip.style.display = "block";
  edgeTooltip.style.left = `${e.clientX - rect.left}px`;
  edgeTooltip.style.top = `${e.clientY - rect.top}px`;
  edgeTooltip.textContent = text;
}

function hideEdgeTooltip() {
  edgeTooltip.style.display = "none";
}

// One walk edge = a <g class="graph-edge"> holding the visible curve plus an
// invisible 12px-wide "hit" copy underneath the cursor, so hovering the edge
// (to see its relation type) doesn't demand pixel-perfect aim. The state
// classes (hidden-node/dimmed/highlighted/walk-*) go on the group; the
// visible path inherits its stroke from them.
function drawGraphEdgeEl(fromNode, toNode, edgeType) {
  const el = document.createElementNS(SVG_NS, "g");
  el.setAttribute("class", "graph-edge");
  const d = curvedEdgePath(fromNode, toNode);

  const hitEl = document.createElementNS(SVG_NS, "path");
  hitEl.setAttribute("class", "graph-edge-hit");
  hitEl.setAttribute("d", d);
  el.appendChild(hitEl);

  const pathEl = document.createElementNS(SVG_NS, "path");
  pathEl.setAttribute("fill", "none");
  pathEl.setAttribute("d", d);
  el.appendChild(pathEl);

  if (edgeType) {
    el.addEventListener("mousemove", (e) => {
      el.classList.add("hover");
      showEdgeTooltip(e, `${fromNode ? nodeLabelText(fromNode) : "?"} —${edgeType}→ ${toNode ? nodeLabelText(toNode) : "?"}`);
    });
    el.addEventListener("mouseleave", () => {
      el.classList.remove("hover");
      hideEdgeTooltip();
    });
  }
  return { el, pathEl, hitEl };
}

// Traditional node-and-edge circle sizing per record type -- Asset nodes
// (the ontology's backbone) are largest, everything else smaller and
// scaled down further still for the busiest category (AlarmEvent, which
// can run into the hundreds of instances) so labels don't collide as much.
const NODE_RADIUS = {
  Asset: 14,
  AlarmEvent: 7,
  AlarmConfig: 8,
  WorkOrder: 8,
  OperatorAction: 8,
  HealthEvent: 8,
  CostPosting: 8,
  HistorianTag: 7,
};

function truncateLabel(text, max = 22) {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max - 1)}\u2026` : text;
}

// Caption shown under a graph node -- deliberately shorter than
// nodeLabelText() (which stays full-length for the inspector title and edge
// tooltips). Historian tag ids share a long useless prefix
// ("FAC1.UNIT100.CENTRIFUGAL_PUMP_101.") and differ only in their LAST
// segment, so truncating from the end rendered four of a pump's tags as four
// identical "FAC1.UNIT100.CENTRIFU\u2026" captions. Use the distinguishing
// segment instead.
function nodeShortLabel(node) {
  if (node.label === "HistorianTag" && node.id.includes(".")) {
    return node.id.split(".").pop();
  }
  return nodeLabelText(node);
}

async function loadPlantGraph() {
  let data;
  try {
    const res = await fetch("/api/graph");
    data = await res.json();
  } catch (err) {
    explorerHint.textContent = "Could not load the plant ontology graph.";
    return;
  }

  const rawNodes = data.nodes || [];
  const rawEdges = data.edges || [];

  renderOntologyOverview(rawNodes, rawEdges);

  const width = Math.max(graphViewport.clientWidth * 1.6, 1200);
  const height = Math.max(graphViewport.clientHeight * 1.6, 900);

  const nodes = rawNodes.map((raw) => {
    const { id, label, ...props } = raw;
    return { id, label, props, x: 0, y: 0 };
  });
  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  const idxById = new Map(nodes.map((n, i) => [n.id, i]));

  const edges = rawEdges
    .map((raw) => ({
      source: raw.source,
      target: raw.target,
      type: raw.type,
      sourceIdx: idxById.get(raw.source),
      targetIdx: idxById.get(raw.target),
    }))
    .filter((e) => e.sourceIdx != null && e.targetIdx != null);

  computeForceLayout(nodes, edges, width, height);
  // Full graph is denser/has more categories than a scoped answer -- orient
  // using just the Asset "backbone" (so ~150 densely-clustered AlarmEvent
  // points don't skew the axis), gentler flatten so per-asset alarm
  // clusters don't overlap, and no tight re-fit (keeps the existing
  // pan/zoom margin instead of stretching to fill every pixel).
  orientSubgraphHorizontally(nodes, width, height, {
    pcaNodes: nodes.filter((n) => n.label === "Asset"),
    flatten: 0.8,
    tiltDegrees: -4,
    refit: false,
  });
  graphState.canvasWidth = width;
  graphState.canvasHeight = height;

  graphState.nodesById = nodesById;
  graphState.edgesByNode = new Map();
  nodes.forEach((n) => graphState.edgesByNode.set(n.id, []));
  edges.forEach((e) => {
    graphState.edgesByNode.get(e.source)?.push({ other: nodesById.get(e.target), edge: e });
    graphState.edgesByNode.get(e.target)?.push({ other: nodesById.get(e.source), edge: e });
  });

  plantGraph.innerHTML = "";
  // Attributes (not CSS width/height) so 1 SVG user-unit = 1px, matching the
  // force layout's pixel-space x/y with no viewBox scaling to account for.
  plantGraph.setAttribute("width", String(width));
  plantGraph.setAttribute("height", String(height));

  const edgeEls = edges.map((edge) => {
    const built = drawGraphEdgeEl(nodesById.get(edge.source), nodesById.get(edge.target), edge.type);
    plantGraph.appendChild(built.el);
    return { ...edge, el: built.el, pathEl: built.pathEl, hitEl: built.hitEl };
  });

  const nodeEls = nodes.map((node) => {
    const r = NODE_RADIUS[node.label] || 7;
    const el = document.createElementNS(SVG_NS, "g");
    el.setAttribute("class", "node graph-node");
    el.setAttribute("transform", `translate(${node.x} ${node.y})`);

    const ring = document.createElementNS(SVG_NS, "circle");
    ring.setAttribute("class", "node-ring");
    ring.setAttribute("r", String(r));
    el.appendChild(ring);

    const color = OVERVIEW_TYPE_COLOR[node.label] || "#858f9c";
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("class", "node-circle");
    circle.setAttribute("r", String(r));
    circle.setAttribute("stroke", color);
    // Soft category-colored tint inside each circle -- reads as "filled" in
    // both themes without shouting over the stroke/highlight colors.
    circle.setAttribute("fill", color);
    circle.setAttribute("fill-opacity", "0.16");
    el.appendChild(circle);

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("class", "node-label");
    label.setAttribute("y", String(r + 12));
    label.textContent = truncateLabel(nodeShortLabel(node));
    el.appendChild(label);

    el.addEventListener("click", () => pinInspector(node.id));
    // Hover preview: shows the node's info immediately on hover, reverting
    // to whatever's pinned (or the default hint) once the mouse leaves --
    // clicking still pins it so it stays visible after moving away.
    el.addEventListener("mouseenter", () => renderInspector(node));
    el.addEventListener("mouseleave", () => hideInspectorPreview());
    plantGraph.appendChild(el);
    node.el = el;
    return node;
  });

  graphState.nodes = nodeEls;
  graphState.edges = edgeEls;

  // Nothing revealed yet -- the walk view stays empty until a question is
  // asked (see revealNodes/resetWalkScope).
  applyWalkVisibility();
  updateWalkChrome();

  // Center the initial view on the Asset nodes' median position at a fixed,
  // legible zoom -- deliberately NOT a min/max bounding-box fit, because a
  // single spatial outlier (e.g. one asset the force layout pushed far from
  // the rest) would otherwise drag the "center" into empty space between
  // clusters and make the whole view look off-balance. The explorer is
  // meant to be panned/zoomed anyway (drag + zoom controls), so a well-
  // centered default beats a perfectly-fitted-but-tiny one.
  const assetNodes = nodeEls.filter((n) => n.label === "Asset");
  if (assetNodes.length > 0) {
    const xs = assetNodes.map((n) => n.x).sort((a, b) => a - b);
    const ys = assetNodes.map((n) => n.y).sort((a, b) => a - b);
    const median = (arr) => arr[Math.floor(arr.length / 2)];
    const cx = median(xs);
    const cy = median(ys);
    const viewportW = graphViewport.clientWidth || 760;
    const viewportH = graphViewport.clientHeight || 420;
    graphState.zoom = 0.85;
    graphState.pan.x = viewportW / 2 - cx * graphState.zoom;
    graphState.pan.y = viewportH / 2 - cy * graphState.zoom;
    updateGraphTransform();
  }
}

// --- Pan (click-drag) and zoom on the explorer viewport ---------------------
// One set of controls (zoom stack, scroll wheel, click-drag) drives whichever
// tab is active: the walk view's pixel-space CSS transform, or the overview's
// viewBox-space group transform.
let dragState = null;
// Set while a canvas drag-pan is in progress so the trailing click isn't
// mistaken for a "click empty canvas to dismiss the pinned panel" gesture.
let canvasDidDrag = false;

function overviewActive() {
  return viewOverview.classList.contains("active");
}

// Fit-to-view (the ⤢ button): overview resets to its natural full-diagram
// framing; the walk view re-centers on the current answer's entity (or the
// first revealed node) at 100%.
function fitActiveView() {
  if (overviewActive()) {
    overviewView.zoom = 1;
    overviewView.panX = 0;
    overviewView.panY = 0;
    applyOverviewTransform();
    return;
  }
  if (graphState.revealedIds.size > 0) {
    fitScopedView(); // frame the whole scoped subgraph
    return;
  }
  graphState.zoom = 1;
  updateGraphTransform();
}

graphViewport.addEventListener("mousedown", (e) => {
  if (e.target.closest(".graph-node") || e.target.closest(".overview-node")) return; // node click, not a pan
  // ...or from interacting with a floating overlay panel (reasoning trace,
  // inspector, zoom stack, legend) -- those sit on top of the canvas but
  // shouldn't drag it.
  if (
    e.target.closest("#reasoningTrace") ||
    e.target.closest("#inspector") ||
    e.target.closest(".zoom-stack") ||
    e.target.closest(".graph-legend")
  ) {
    return;
  }
  if (overviewActive()) {
    dragState = { mode: "overview", startX: e.clientX, startY: e.clientY, panX: overviewView.panX, panY: overviewView.panY };
  } else {
    dragState = { mode: "walk", startX: e.clientX, startY: e.clientY, panX: graphState.pan.x, panY: graphState.pan.y };
  }
  canvasDidDrag = false;
  graphViewport.classList.add("grabbing");
});

window.addEventListener("mousemove", (e) => {
  if (!dragState) return;
  if (Math.abs(e.clientX - dragState.startX) + Math.abs(e.clientY - dragState.startY) > 4) {
    canvasDidDrag = true;
  }
  if (dragState.mode === "overview") {
    const svg = viewOverview.querySelector("svg");
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    overviewView.panX = dragState.panX + (e.clientX - dragState.startX) * (OVERVIEW_W / rect.width);
    overviewView.panY = dragState.panY + (e.clientY - dragState.startY) * (OVERVIEW_H / rect.height);
    applyOverviewTransform();
    return;
  }
  graphState.pan.x = dragState.panX + (e.clientX - dragState.startX);
  graphState.pan.y = dragState.panY + (e.clientY - dragState.startY);
  updateGraphTransform();
});

window.addEventListener("mouseup", () => {
  dragState = null;
  graphViewport.classList.remove("grabbing");
});

zoomInBtn.addEventListener("click", () => {
  if (overviewActive()) {
    setOverviewZoom(overviewView.zoom * 1.2, OVERVIEW_W / 2, OVERVIEW_H / 2);
    return;
  }
  graphState.zoom = Math.min(2.2, graphState.zoom + 0.15);
  updateGraphTransform();
});

zoomOutBtn.addEventListener("click", () => {
  if (overviewActive()) {
    setOverviewZoom(overviewView.zoom / 1.2, OVERVIEW_W / 2, OVERVIEW_H / 2);
    return;
  }
  graphState.zoom = Math.max(0.3, graphState.zoom - 0.15);
  updateGraphTransform();
});

zoomResetBtn.addEventListener("click", fitActiveView);

// Gentle, proportional-to-scroll-amount zoom (not a fixed jump per wheel
// tick) so a single fast trackpad/mouse-wheel tick only moves the zoom
// level by a few percent -- anchored to the cursor position so the point
// under it stays put while zooming. Skipped entirely (falls through to
// native scrolling) when the pointer is over the reasoning-trace log or
// the inspector, so those can still be scrolled normally.
graphViewport.addEventListener(
  "wheel",
  (e) => {
    if (e.target.closest("#reasoningTrace") || e.target.closest("#inspector")) return;
    e.preventDefault();
    const delta = Math.max(-40, Math.min(40, e.deltaY));
    const factor = 1 - delta * 0.0035;
    if (overviewActive()) {
      const pt = overviewPointFromEvent(e);
      setOverviewZoom(overviewView.zoom * factor, pt.x, pt.y);
      return;
    }
    const rect = graphViewport.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    const newZoom = Math.min(2.2, Math.max(0.3, graphState.zoom * factor));
    graphState.pan.x = mouseX - (mouseX - graphState.pan.x) * (newZoom / graphState.zoom);
    graphState.pan.y = mouseY - (mouseY - graphState.pan.y) * (newZoom / graphState.zoom);
    graphState.zoom = newZoom;
    updateGraphTransform();
  },
  { passive: false }
);

showOverviewTab(); // schema view is the pre-question default
loadPlantGraph();

