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
const suggestionRow = document.getElementById("suggestionRow");
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
const explorerControls = document.getElementById("explorerControls");
const graphViewport = document.getElementById("graphViewport");
const plantGraph = document.getElementById("plantGraph");
const graphFilterGroup = document.getElementById("graphFilterGroup");
const inspector = document.getElementById("inspector");
const explorerHint = document.getElementById("explorerHint");
const zoomInBtn = document.getElementById("zoomInBtn");
const zoomOutBtn = document.getElementById("zoomOutBtn");
const zoomResetBtn = document.getElementById("zoomResetBtn");

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
// While scoped to an answer, the category filter chips stay hidden and the
// hint reports the scoped node count -- see graphState.scopedToAnswer.
function applyWalkUiState() {
  if (graphState.scopedToAnswer) {
    explorerControls.style.display = "none";
    explorerHint.textContent = `Showing ${graphState.scopedCount} of ${graphState.nodes.length} nodes (scoped to this answer).`;
  } else {
    explorerControls.style.display = "flex";
    explorerHint.textContent = "Browse the unified knowledge graph directly, or ask a question to see it come alive.";
  }
}

function showOverviewTab() {
  tabOverview.classList.add("active");
  tabWalk.classList.remove("active");
  viewOverview.classList.add("active");
  viewWalk.classList.remove("active");
  explorerControls.style.display = "none";
  document.getElementById("zoomStack").style.display = "none";
  reasoningTrace.style.display = "none"; // only relevant to a graph walk, not the static schema view
  explorerHint.textContent = "Always fully visible -- bounded at one node per record type, no matter how much data is behind each one.";
}
function showWalkTab() {
  tabOverview.classList.remove("active");
  tabWalk.classList.add("active");
  viewOverview.classList.remove("active");
  viewWalk.classList.add("active");
  document.getElementById("zoomStack").style.display = "flex";
  reasoningTrace.style.display = "flex";
  applyWalkUiState();
}
tabOverview.addEventListener("click", showOverviewTab);
tabWalk.addEventListener("click", showWalkTab);

// --- Dark mode (default) with a manual toggle, persisted in localStorage ---
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

applyTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");

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
  if (!label || label === "n/a") return "Confidence: n/a";
  if (label === "model-reasoned") return "Confidence: AI-reasoned";
  return `Confidence: ${label}`;
}

// --- Minimal, safe Markdown renderer -------------------------------------
// Handles the subset the agent is instructed to use: #/##/### headings,
// bullet/numbered lists, **bold**/*italic* inline spans, and paragraphs.
// Builds real DOM nodes via createElement/textContent -- never innerHTML
// with model-provided text -- so this cannot introduce XSS even though the
// text ultimately comes from an LLM response.
function appendInlineMarkdown(parent, text) {
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)/g;
  let lastIndex = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    const token = match[0];
    const el = document.createElement(token.startsWith("**") ? "strong" : "em");
    el.textContent = token.startsWith("**") ? token.slice(2, -2) : token.slice(1, -1);
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

function addUserBubble(question) {
  const turn = document.createElement("div");
  turn.className = "chat-turn chat-turn-user";
  const p = document.createElement("p");
  p.className = "chat-bubble user-bubble";
  p.textContent = question;
  turn.appendChild(p);
  chatThread.appendChild(turn);
  scrollThreadToBottom();
}

function buildDetails(summaryText, open = false) {
  const details = document.createElement("details");
  details.className = "chat-details";
  details.open = open;
  const summary = document.createElement("summary");
  summary.textContent = summaryText;
  details.appendChild(summary);
  const body = document.createElement("div");
  details.appendChild(body);
  return { details, body };
}

function addPendingAssistantBubble() {
  const turn = document.createElement("div");
  turn.className = "chat-turn chat-turn-assistant";
  const card = document.createElement("div");
  card.className = "chat-bubble assistant-bubble pending";

  const agentId = document.createElement("div");
  agentId.className = "agent-id";
  const avatar = document.createElement("span");
  avatar.className = "agent-avatar";
  avatar.textContent = "E";
  const agentName = document.createElement("span");
  agentName.className = "agent-name";
  agentName.textContent = "Ellie";
  agentId.appendChild(avatar);
  agentId.appendChild(agentName);
  card.appendChild(agentId);

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

  const agentId = document.createElement("div");
  agentId.className = "agent-id";
  const avatar = document.createElement("span");
  avatar.className = "agent-avatar";
  avatar.textContent = "E";
  const agentName = document.createElement("span");
  agentName.className = "agent-name";
  agentName.textContent = "Ellie";
  agentId.appendChild(avatar);
  agentId.appendChild(agentName);
  card.appendChild(agentId);

  const usedLlm = data.presented_by && data.presented_by !== "rule-based";
  const badgeText = usedLlm
    ? data.scenario === "agentic"
      ? `🤖 reasoned by ${data.presented_by}`
      : `✨ polished by ${data.presented_by}`
    : "rule-based";

  const headlineRow = document.createElement("div");
  headlineRow.className = "assistant-headline-row";
  const headline = document.createElement("h3");
  headline.className = "assistant-headline";
  headline.textContent = data.headline || extractHeadline(data.answer) || "No answer text returned.";
  const confidence = document.createElement("span");
  confidence.className = `confidence ${confidenceClass(data.confidence)}`;
  confidence.textContent = formatConfidence(data.confidence);
  headlineRow.appendChild(headline);
  headlineRow.appendChild(confidence);
  card.appendChild(headlineRow);

  const meta = document.createElement("p");
  meta.className = "muted assistant-meta";
  meta.textContent = `asset: ${data.asset ?? "n/a"} · ${badgeText}`;
  card.appendChild(meta);

  const { details: analysisDetails, body: analysisBody } = buildDetails("Show full analysis");
  analysisBody.className = "markdown-body";
  renderMarkdown(analysisBody, data.answer);
  card.appendChild(analysisDetails);

  if (data.recommendation) {
    const { details: recDetails, body: recBody } = buildDetails("Recommended actions", true);
    recBody.className = "markdown-body";
    renderMarkdown(recBody, data.recommendation);
    card.appendChild(recDetails);
  }

  const panel = data.panel;
  if (panel && panel.charts && panel.charts.length > 0) {
    const chartsWrap = document.createElement("div");
    chartsWrap.className = "trend-charts";
    panel.charts.forEach((chart) => chartsWrap.appendChild(buildTrendChartCard(chart)));
    card.appendChild(chartsWrap);
  }

  if (panel && ((panel.timeline && panel.timeline.length) || (panel.evidence && panel.evidence.length))) {
    const { details: evDetails, body: evBody } = buildDetails(
      `Evidence & timeline (${(panel.timeline || []).length + (panel.evidence || []).length})`
    );

    if (panel.timeline && panel.timeline.length > 0) {
      const ul = document.createElement("ul");
      ul.className = "timeline";
      evBody.appendChild(ul);
      panel.timeline.forEach((step) => {
        const li = document.createElement("li");
        const metaP = document.createElement("p");
        metaP.className = "timeline-meta";
        metaP.textContent = step.time;
        const chip = document.createElement("span");
        chip.className = "source-chip";
        chip.textContent = step.source;
        metaP.appendChild(chip);
        const txt = document.createElement("p");
        txt.textContent = step.text;
        li.appendChild(metaP);
        li.appendChild(txt);
        ul.appendChild(li);
      });
    }

    if (panel.evidence && panel.evidence.length > 0) {
      const grid = document.createElement("div");
      grid.className = "evidence-list";
      panel.evidence.forEach((ev) => {
        const evCard = document.createElement("div");
        evCard.className = "evidence-card";
        const title = document.createElement("p");
        const strong = document.createElement("strong");
        strong.textContent = ev.title;
        title.appendChild(strong);
        const source = document.createElement("p");
        source.className = "evidence-label";
        source.textContent = ev.source;
        const record = document.createElement("p");
        record.textContent = ev.record;
        evCard.appendChild(title);
        evCard.appendChild(source);
        evCard.appendChild(record);
        grid.appendChild(evCard);
      });
      evBody.appendChild(grid);
    }

    card.appendChild(evDetails);
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

async function submitQuestion(question) {
  question = question.trim();
  if (!question) return;
  runButton.disabled = true;
  addUserBubble(question);
  const pendingTurn = addPendingAssistantBubble();
  const card = pendingTurn.querySelector(".assistant-bubble");
  const thinkingEl = card.querySelector(".thinking");

  // Reset any highlighting left over from a previous answer before this
  // one's live events (if any) start arriving.
  clearHighlight();
  liveWalkReset();
  stopReplay();
  replayIndex = -1;
  resetTraceLog();
  let sawLiveEvent = false;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
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

function renderSuggestions(questions) {
  suggestionRow.innerHTML = "";
  questions.forEach((question) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "suggestion-chip";
    chip.textContent = question;
    chip.addEventListener("click", () => submitQuestion(question));
    suggestionRow.appendChild(chip);
  });
}

fetch("/api/suggestions")
  .then((r) => r.json())
  .then((data) => renderSuggestions(data.questions || []))
  .catch((err) => {
    console.error("Failed to load suggestions", err);
  });

// Welcome message so the thread isn't empty on load.
(function seedWelcomeMessage() {
  const turn = document.createElement("div");
  turn.className = "chat-turn chat-turn-assistant";
  const card = document.createElement("div");
  card.className = "chat-bubble assistant-bubble";

  const agentId = document.createElement("div");
  agentId.className = "agent-id";
  const avatar = document.createElement("span");
  avatar.className = "agent-avatar";
  avatar.textContent = "E";
  const agentName = document.createElement("span");
  agentName.className = "agent-name";
  agentName.textContent = "Ellie";
  agentId.appendChild(avatar);
  agentId.appendChild(agentName);
  card.appendChild(agentId);

  const p = document.createElement("p");
  p.textContent =
    "Hi, I'm Ellie. Ask me anything about the plant -- a specific problem (\"why is P-101 vibrating?\"), " +
    "an open-ended look-around (\"show me everything on Unit 100\", \"what's alarming right now?\"), " +
    "or browse the Ontology Overview / Reasoning Walk on the right directly.";
  card.appendChild(p);
  turn.appendChild(card);
  chatThread.appendChild(turn);
})();

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

const NODE_STYLE = {
  Asset: { categoryLabel: "Assets" },
  AlarmEvent: { categoryLabel: "Alarms" },
  AlarmConfig: { categoryLabel: "Alarms" },
  WorkOrder: { categoryLabel: "Work Orders" },
  OperatorAction: { categoryLabel: "Operator Actions" },
  HealthEvent: { categoryLabel: "Health Events" },
  CostPosting: { categoryLabel: "Cost Postings" },
  HistorianTag: { categoryLabel: "Historian Tags" },
};

const ALWAYS_ON_CATEGORY = "Assets";
const FILTER_CATEGORIES = [
  "Assets",
  "Alarms",
  "Work Orders",
  "Operator Actions",
  "Health Events",
  "Cost Postings",
  "Historian Tags",
];

const graphState = {
  nodes: [], // { id, label, props, x, y, el }
  nodesById: new Map(),
  edgesByNode: new Map(), // id -> [{ other, edge }]
  edges: [], // { source, target, type, el }
  activeCategories: new Set([ALWAYS_ON_CATEGORY]),
  zoom: 1,
  pan: { x: 0, y: 0 },
  selectedId: null,
  canvasWidth: 1200,
  canvasHeight: 900,
  // Whether the Reasoning Walk view is currently scoped to one answer's
  // evidence (see focusAnswerInGraph) -- while scoped, the category filter
  // chips are hidden (the graph is already showing exactly what matters
  // for this answer, so "why would you filter it further" -- see
  // docs/CHAT_AGENT.md's design notes on this) and the hint line reports
  // how much of the whole graph is currently in view instead.
  scopedToAnswer: false,
  scopedCount: 0,
};

function categoryFor(label) {
  return (NODE_STYLE[label] || {}).categoryLabel || "Other";
}

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

function edgeAllowed(edge) {
  const sourceCat = categoryFor(graphState.nodesById.get(edge.source)?.label);
  const targetCat = categoryFor(graphState.nodesById.get(edge.target)?.label);
  return graphState.activeCategories.has(sourceCat) && graphState.activeCategories.has(targetCat);
}

function applyVisibility() {
  graphState.nodes.forEach((node) => {
    const visible = graphState.activeCategories.has(categoryFor(node.label));
    node.el.classList.toggle("hidden-node", !visible);
  });
  graphState.edges.forEach((edge) => {
    edge.el.classList.toggle("hidden-node", !edgeAllowed(edge));
  });
}

function clearHighlight() {
  graphState.nodes.forEach((node) => node.el.classList.remove("highlighted", "dimmed"));
  graphState.edges.forEach((edge) => edge.el.classList.remove("highlighted", "dimmed"));
}

function selectNode(nodeId) {
  const node = graphState.nodesById.get(nodeId);
  if (!node) return;
  graphState.selectedId = nodeId;
  renderInspector(node);
  const priorSelected = plantGraph.querySelector(".node.selected");
  if (priorSelected) priorSelected.classList.remove("selected");
  node.el.classList.add("selected");
}

// Restores the inspector to whatever the "pinned" (clicked) node is, or
// hides it entirely if nothing is pinned -- used when the mouse leaves a
// node that was only being previewed on hover, not clicked. The inspector
// is a hover popup: it should only be on screen while there's something to
// show, never sitting empty with a placeholder hint.
function resetInspectorToDefault() {
  if (graphState.selectedId && graphState.nodesById.has(graphState.selectedId)) {
    renderInspector(graphState.nodesById.get(graphState.selectedId));
    return;
  }
  inspector.classList.remove("show");
  inspector.innerHTML = "";
}

function renderInspector(node) {
  inspector.classList.add("show");
  inspector.innerHTML = "";
  const title = document.createElement("p");
  title.className = "inspector-title";
  title.textContent = nodeLabelText(node);
  const typeP = document.createElement("p");
  typeP.className = "muted";
  typeP.textContent = `${node.label} · ${node.id}`;
  inspector.appendChild(title);
  inspector.appendChild(typeP);

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
  inspector.appendChild(table);

  if (node.label === "Asset" && node.props.system_ids) {
    const aliasTitle = document.createElement("p");
    aliasTitle.className = "inspector-subtitle";
    aliasTitle.textContent = "Per-system aliases";
    inspector.appendChild(aliasTitle);
    const list = document.createElement("ul");
    list.className = "inspector-alias-list";
    Object.entries(node.props.system_ids).forEach(([system, localId]) => {
      const li = document.createElement("li");
      li.textContent = `${system}: ${localId}`;
      list.appendChild(li);
    });
    inspector.appendChild(list);
  }

  const neighbors = graphState.edgesByNode.get(node.id) || [];
  if (neighbors.length > 0) {
    const neighborsTitle = document.createElement("p");
    neighborsTitle.className = "inspector-subtitle";
    neighborsTitle.textContent = `Connections (${neighbors.length})`;
    inspector.appendChild(neighborsTitle);
    const list = document.createElement("ul");
    list.className = "inspector-alias-list";
    neighbors.slice(0, 25).forEach(({ other, edge }) => {
      const li = document.createElement("li");
      li.textContent = `${edge.type} → ${nodeLabelText(other)}`;
      li.addEventListener("click", () => panToNode(other.id, { select: true }));
      list.appendChild(li);
    });
    inspector.appendChild(list);
  }
}

function updateGraphTransform() {
  plantGraph.style.transform = `translate(${graphState.pan.x}px, ${graphState.pan.y}px) scale(${graphState.zoom})`;
  zoomResetBtn.textContent = `${Math.round(graphState.zoom * 100)}%`;
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

function layoutScopedSubgraph(nodeIds) {
  const idSet = new Set(nodeIds);
  const subNodes = graphState.nodes.filter((n) => idSet.has(n.id));
  if (subNodes.length < 2) return;
  const idxById = new Map(subNodes.map((n, i) => [n.id, i]));
  const subEdges = graphState.edges
    .filter((e) => idSet.has(e.source) && idSet.has(e.target))
    .map((e) => ({ ...e, sourceIdx: idxById.get(e.source), targetIdx: idxById.get(e.target) }));

  const viewportW = graphViewport.clientWidth || 760;
  const viewportH = graphViewport.clientHeight || 420;
  const layoutW = Math.max(viewportW * 0.85, 360);
  const layoutH = Math.max(viewportH * 0.85, 280);
  computeForceLayout(subNodes, subEdges, layoutW, layoutH);
  orientSubgraphHorizontally(subNodes, layoutW, layoutH);

  // Center the freshly-laid-out subgraph within the (much larger) shared
  // canvas so panToNode's math still works unchanged.
  const offsetX = (graphState.canvasWidth - layoutW) / 2;
  const offsetY = (graphState.canvasHeight - layoutH) / 2;
  subNodes.forEach((n) => {
    n.x += offsetX;
    n.y += offsetY;
    n.el.setAttribute("transform", `translate(${n.x} ${n.y})`);
  });

  graphState.edges.forEach((edge) => {
    if (idSet.has(edge.source) && idSet.has(edge.target)) {
      const fromNode = graphState.nodesById.get(edge.source);
      const toNode = graphState.nodesById.get(edge.target);
      edge.el.setAttribute("d", curvedEdgePath(fromNode, toNode));
    }
  });
}

// Called from a chat answer's "Focus in explorer" action (also triggered
// automatically once an answer renders): turns on whatever category filters
// are needed to reveal the cited evidence, highlights the resolved entity +
// every evidence/relationship node referenced by this specific answer, dims
// everything else, and pans the viewport to the entity. This is the direct
// link between "the agent reasoned about this" and the always-visible graph.
function focusAnswerInGraph(panel) {
  if (!panel || !panel.entity_id || !graphState.nodesById.has(panel.entity_id)) return;
  showWalkTab(); // an answer's highlighted evidence lives on the Reasoning Walk tab, not the schema overview

  const refs = new Set([panel.entity_id]);
  (panel.relationships || []).forEach((r) => r.ref && refs.add(r.ref));
  (panel.timeline || []).forEach((t) => t.ref && refs.add(t.ref));
  (panel.evidence || []).forEach((e) => e.ref && refs.add(e.ref));

  // Reveal whatever categories these refs belong to, so highlighted nodes
  // are actually visible rather than hidden behind an off filter chip.
  refs.forEach((id) => {
    const node = graphState.nodesById.get(id);
    if (node) graphState.activeCategories.add(categoryFor(node.label));
  });
  renderFilterChips();
  applyVisibility();
  layoutScopedSubgraph(refs);

  clearHighlight();
  graphState.nodes.forEach((node) => {
    if (refs.has(node.id)) {
      node.el.classList.add("highlighted");
    } else {
      node.el.classList.add("dimmed");
    }
  });
  graphState.edges.forEach((edge) => {
    if (refs.has(edge.source) && refs.has(edge.target)) {
      edge.el.classList.add("highlighted");
    } else {
      edge.el.classList.add("dimmed");
    }
  });

  // Scoped mode: hide the category filter chips (the graph is already
  // showing exactly this answer's evidence, nothing to filter further) and
  // report the scoped count instead of the generic browse-mode hint.
  graphState.scopedToAnswer = true;
  graphState.scopedCount = refs.size;
  applyWalkUiState();
  graphState.zoom = 1; // reset in case a previous answer/browsing session left it zoomed out
  panToNode(panel.entity_id, { select: true });
}

// Time each walk step stays "current" before advancing, in ms.
const WALK_STEP_DELAY_MS = 550;

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

  // Reveal whatever categories the whole walk will touch up front, so
  // nothing pops in/out mid-animation.
  const allRefs = new Set();
  steps.forEach((step) => (step.node_ids || []).forEach((id) => allRefs.add(id)));
  allRefs.forEach((id) => {
    const node = graphState.nodesById.get(id);
    if (node) graphState.activeCategories.add(categoryFor(node.label));
  });
  renderFilterChips();
  applyVisibility();
  clearHighlight();

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
    explorerHint.textContent = step.label;
    logTraceLine(step.label);
    graphState.nodes.forEach((node) => {
      if (ids.includes(node.id)) node.el.classList.add("walk-current");
    });
    graphState.edges.forEach((edge) => {
      if (ids.includes(edge.source) && ids.includes(edge.target)) edge.el.classList.add("walk-current");
    });
    if (ids.length > 0) panToNode(ids[0]);

    setTimeout(() => playStep(i + 1), WALK_STEP_DELAY_MS);
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

  ids.forEach((id) => {
    const node = graphState.nodesById.get(id);
    if (node) graphState.activeCategories.add(categoryFor(node.label));
  });
  renderFilterChips();
  applyVisibility();

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
  panToNode(ids[0]);
  if (step.label) {
    explorerHint.textContent = step.label;
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
  ids.forEach((id) => {
    const node = graphState.nodesById.get(id);
    if (node) graphState.activeCategories.add(categoryFor(node.label));
  });
  renderFilterChips();
  applyVisibility();
  graphState.nodes.forEach((node) => {
    if (ids.includes(node.id)) node.el.classList.add("walk-current");
  });
  graphState.edges.forEach((edge) => {
    if (ids.includes(edge.source) && ids.includes(edge.target)) edge.el.classList.add("walk-current");
  });
  if (ids.length > 0) panToNode(ids[0]);
  explorerHint.textContent = step.label;
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
  setPlayIcon(true);
  replayTimer = setInterval(() => {
    replayIndex++;
    applyReplayStep(replayIndex);
    if (replayIndex >= lastWalkSteps.length) stopReplay();
  }, WALK_STEP_DELAY_MS);
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
  replayIndex++;
  applyReplayStep(replayIndex);
});

traceResetBtn.addEventListener("click", () => {
  stopReplay();
  replayIndex = -1;
  resetTraceLog();
  clearHighlight();
});

function renderFilterChips() {
  graphFilterGroup.innerHTML = "";
  FILTER_CATEGORIES.forEach((category) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `focus-chip ${graphState.activeCategories.has(category) ? "active" : ""}`;
    chip.textContent = category;
    if (category === ALWAYS_ON_CATEGORY) {
      chip.disabled = true;
      chip.title = "Assets are always shown -- they're the backbone of the ontology.";
    } else {
      chip.addEventListener("click", () => {
        if (graphState.activeCategories.has(category)) {
          graphState.activeCategories.delete(category);
        } else {
          graphState.activeCategories.add(category);
        }
        renderFilterChips();
        applyVisibility();
        clearHighlight();
        explorerHint.textContent =
          "Browse the unified knowledge graph directly, or ask a question to see it come alive.";
      });
    }
    graphFilterGroup.appendChild(chip);
  });
}

// ===========================================================================
// Ontology Overview -- a small, ALWAYS-BOUNDED schema/type-level diagram (one
// node per record type + a live count, computed from the same /api/graph
// payload the Reasoning Walk tab uses), instead of the per-instance graph.
// This answers "why isn't the whole plant graph shown at once": with 100+
// AlarmEvent instances alone, rendering every record would be a hairball --
// this view is bounded at one node per type no matter how much data exists.
// ===========================================================================
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
  Asset: { source: "Stage 1 entity resolution", fields: "canonical_name, confidence, system_ids" },
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

function renderTypeInspector(label, count) {
  inspector.classList.add("show");
  inspector.innerHTML = "";
  const title = document.createElement("p");
  title.className = "inspector-title";
  title.textContent = label;
  inspector.appendChild(title);
  const sub = document.createElement("p");
  sub.className = "muted";
  sub.textContent = `Type node · ${count} instance${count === 1 ? "" : "s"}`;
  inspector.appendChild(sub);
  const info = OVERVIEW_TYPE_INFO[label];
  if (info) {
    const table = document.createElement("dl");
    table.className = "inspector-fields";
    const dt1 = document.createElement("dt");
    dt1.textContent = "source system";
    const dd1 = document.createElement("dd");
    dd1.textContent = info.source;
    const dt2 = document.createElement("dt");
    dt2.textContent = "fields";
    const dd2 = document.createElement("dd");
    dd2.textContent = info.fields;
    table.append(dt1, dd1, dt2, dd2);
    inspector.appendChild(table);
  }
}

function overviewNode(cx, cy, r, color, label, count) {
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "overview-node");
  g.setAttribute("transform", `translate(${cx} ${cy})`);

  const circle = document.createElementNS(SVG_NS, "circle");
  circle.setAttribute("r", String(r));
  circle.setAttribute("stroke", color);
  g.appendChild(circle);

  const countText = document.createElementNS(SVG_NS, "text");
  countText.setAttribute("class", "overview-count");
  countText.setAttribute("y", "4");
  countText.textContent = String(count);
  g.appendChild(countText);

  const labelText = document.createElementNS(SVG_NS, "text");
  labelText.setAttribute("class", "overview-label");
  labelText.setAttribute("y", String(r + 12));
  labelText.textContent = label;
  g.appendChild(labelText);

  g.addEventListener("mouseenter", () => renderTypeInspector(label, count));
  g.addEventListener("mouseleave", () => resetInspectorToDefault());
  return g;
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

  viewOverview.innerHTML = "";
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 300 260");
  svg.setAttribute("class", "overview-svg");

  const centerX = 150;
  const centerY = 128;
  const centerR = 26;

  const visibleTypes = OVERVIEW_LAYOUT.filter((item) => counts[item.label]);

  // Edges first, so nodes render on top of them.
  visibleTypes.forEach((item) => {
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", String(centerX));
    line.setAttribute("y1", String(centerY));
    line.setAttribute("x2", String(item.x));
    line.setAttribute("y2", String(item.y));
    line.setAttribute("class", "overview-edge");
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${item.edgeType} (${edgeCounts[item.edgeType] || 0})`;
    line.appendChild(title);
    svg.appendChild(line);
  });

  // Asset-to-asset process topology (FEEDS/COOLS/SUPPLIES_UTILITY) shown as
  // a small self-loop on the Asset hub, combined count in its hover title.
  const topologyCount =
    (edgeCounts.FEEDS || 0) + (edgeCounts.COOLS || 0) + (edgeCounts.SUPPLIES_UTILITY || 0);
  if (topologyCount > 0) {
    const loop = document.createElementNS(SVG_NS, "path");
    loop.setAttribute(
      "d",
      `M ${centerX - 14},${centerY + 20} C ${centerX - 48},${centerY + 4} ${centerX - 48},${centerY - 30} ${centerX - 10},${centerY - 22}`
    );
    loop.setAttribute("class", "overview-edge");
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `FEEDS (${edgeCounts.FEEDS || 0}) / COOLS (${edgeCounts.COOLS || 0}) / SUPPLIES_UTILITY (${edgeCounts.SUPPLIES_UTILITY || 0})`;
    loop.appendChild(title);
    svg.appendChild(loop);
  }

  visibleTypes.forEach((item) => {
    svg.appendChild(overviewNode(item.x, item.y, 14, OVERVIEW_TYPE_COLOR[item.label], item.label, counts[item.label]));
  });

  svg.appendChild(
    overviewNode(centerX, centerY, centerR, OVERVIEW_TYPE_COLOR.Asset, "Asset", counts.Asset || 0)
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
  return `M ${fromNode.x} ${fromNode.y} Q ${cx} ${cy} ${toNode.x} ${toNode.y}`;
}

function drawGraphEdgeEl(fromNode, toNode, edgeType) {
  const el = document.createElementNS(SVG_NS, "path");
  el.setAttribute("class", "graph-edge");
  el.setAttribute("fill", "none");
  el.setAttribute("d", curvedEdgePath(fromNode, toNode));
  if (edgeType) {
    // Hover-only relation label (never shown by default) -- an SVG <title>
    // child renders as the browser's native tooltip, same as the HTML
    // title attribute would, just the SVG-correct way to do it.
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = edgeType;
    el.appendChild(title);
  }
  return el;
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
    const el = drawGraphEdgeEl(nodesById.get(edge.source), nodesById.get(edge.target), edge.type);
    plantGraph.appendChild(el);
    return { ...edge, el };
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

    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("class", "node-circle");
    circle.setAttribute("r", String(r));
    circle.setAttribute("stroke", OVERVIEW_TYPE_COLOR[node.label] || "#858f9c");
    el.appendChild(circle);

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("class", "node-label");
    label.setAttribute("y", String(r + 12));
    label.textContent = truncateLabel(nodeLabelText(node));
    el.appendChild(label);

    el.addEventListener("click", () => selectNode(node.id));
    // Hover preview: shows the node's info immediately on hover, reverting
    // to whatever's pinned (or the default hint) once the mouse leaves --
    // clicking still pins it so it stays visible after moving away.
    el.addEventListener("mouseenter", () => renderInspector(node));
    el.addEventListener("mouseleave", () => resetInspectorToDefault());
    plantGraph.appendChild(el);
    node.el = el;
    return node;
  });

  graphState.nodes = nodeEls;
  graphState.edges = edgeEls;

  renderFilterChips();
  applyVisibility();

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

// --- Pan (click-drag) and zoom on the explorer viewport ---
let dragState = null;

graphViewport.addEventListener("mousedown", (e) => {
  if (e.target.closest(".graph-node")) return; // don't start a pan from a node click
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
  dragState = { startX: e.clientX, startY: e.clientY, panX: graphState.pan.x, panY: graphState.pan.y };
  graphViewport.classList.add("grabbing");
});

window.addEventListener("mousemove", (e) => {
  if (!dragState) return;
  graphState.pan.x = dragState.panX + (e.clientX - dragState.startX);
  graphState.pan.y = dragState.panY + (e.clientY - dragState.startY);
  updateGraphTransform();
});

window.addEventListener("mouseup", () => {
  dragState = null;
  graphViewport.classList.remove("grabbing");
});

zoomInBtn.addEventListener("click", () => {
  graphState.zoom = Math.min(2.2, graphState.zoom + 0.15);
  updateGraphTransform();
});

zoomOutBtn.addEventListener("click", () => {
  graphState.zoom = Math.max(0.3, graphState.zoom - 0.15);
  updateGraphTransform();
});

zoomResetBtn.addEventListener("click", () => {
  graphState.zoom = 1;
  updateGraphTransform();
});

// Gentle, proportional-to-scroll-amount zoom (not a fixed jump per wheel
// tick) so a single fast trackpad/mouse-wheel tick only moves the zoom
// level by a few percent -- anchored to the cursor position so the point
// under it stays put while zooming. Only active on the Reasoning Walk tab,
// and skipped entirely (falls through to native scrolling) when the
// pointer is over the reasoning-trace log or the inspector, so those can
// still be scrolled normally with the mouse wheel.
graphViewport.addEventListener(
  "wheel",
  (e) => {
    if (!viewWalk.classList.contains("active")) return;
    if (e.target.closest("#reasoningTrace") || e.target.closest("#inspector")) return;
    e.preventDefault();
    const rect = graphViewport.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    const delta = Math.max(-40, Math.min(40, e.deltaY));
    const factor = 1 - delta * 0.0035;
    const newZoom = Math.min(2.2, Math.max(0.3, graphState.zoom * factor));
    graphState.pan.x = mouseX - (mouseX - graphState.pan.x) * (newZoom / graphState.zoom);
    graphState.pan.y = mouseY - (mouseY - graphState.pan.y) * (newZoom / graphState.zoom);
    graphState.zoom = newZoom;
    updateGraphTransform();
  },
  { passive: false }
);

loadPlantGraph();

