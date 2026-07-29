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
const statusPill = document.getElementById("statusPill");
const themeToggle = document.getElementById("themeToggle");

const graphViewport = document.getElementById("graphViewport");
const plantGraph = document.getElementById("plantGraph");
const graphFilterGroup = document.getElementById("graphFilterGroup");
const inspector = document.getElementById("inspector");
const explorerHint = document.getElementById("explorerHint");
const zoomInBtn = document.getElementById("zoomInBtn");
const zoomOutBtn = document.getElementById("zoomOutBtn");
const zoomResetBtn = document.getElementById("zoomResetBtn");

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
  const p = document.createElement("p");
  p.className = "muted thinking";
  p.textContent = "Thinking...";
  card.appendChild(p);

  // Populated live as SSE events arrive (see submitQuestion) -- a checklist
  // of the agent's write_plan steps, and a running breadcrumb trace of
  // tool calls as they happen, in the style of a coding agent's activity
  // feed. Both stay empty (and invisible via :empty) for the deterministic
  // fallback path, which has no intermediate steps to show.
  const plan = document.createElement("ul");
  plan.className = "plan-checklist";
  card.appendChild(plan);

  const trace = document.createElement("div");
  trace.className = "trace";
  card.appendChild(trace);

  turn.appendChild(card);
  chatThread.appendChild(turn);
  scrollThreadToBottom();
  return turn;
}

const PLAN_STATUS_ICON = { pending: "○", in_progress: "◐", done: "●" };

// Renders the agent's live write_plan() steps as a checklist (replaced
// wholesale on every "plan" event -- the model may reorder/add/remove
// steps, not just flip a status, so a full re-render is simpler and safer
// than diffing).
function renderPlanChecklist(container, steps) {
  container.innerHTML = "";
  (steps || []).forEach((step) => {
    const li = document.createElement("li");
    li.className = `plan-step plan-step-${step.status}`;
    const icon = document.createElement("span");
    icon.className = "plan-step-icon";
    icon.textContent = PLAN_STATUS_ICON[step.status] || PLAN_STATUS_ICON.pending;
    const text = document.createElement("span");
    text.textContent = step.text;
    li.appendChild(icon);
    li.appendChild(text);
    container.appendChild(li);
  });
}

// One breadcrumb chip per tool call, appended live as "tool_call" events
// arrive and marked done once the matching "tool_result" arrives -- the
// coding-agent-style "thinking / using a tool" activity feed.
function addTraceStep(container, label) {
  const chip = document.createElement("span");
  chip.className = "trace-step active";
  chip.textContent = label;
  container.appendChild(chip);
  return chip;
}

function markTraceStepDone(chip) {
  if (!chip) return;
  chip.classList.remove("active");
  chip.classList.add("done");
}

function renderAssistantBubble(turnEl, data) {
  const card = document.createElement("div");
  card.className = "chat-bubble assistant-bubble";

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
  const planEl = card.querySelector(".plan-checklist");
  const traceEl = card.querySelector(".trace");

  // Reset any highlighting left over from a previous answer before this
  // one's live events (if any) start arriving.
  clearHighlight();
  liveWalkReset();
  let sawLiveEvent = false;
  let activeChip = null;

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
    // stream_answer() and docs/CHAT_RAG.md §3.
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

        if (event.type === "plan") {
          renderPlanChecklist(planEl, event.steps);
        } else if (event.type === "tool_call") {
          activeChip = addTraceStep(traceEl, event.label || event.tool);
        } else if (event.type === "tool_result") {
          markTraceStepDone(activeChip);
          if (event.walk_step) liveWalkStep(event.walk_step);
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
  .catch(() => {
    statusPill.textContent = "Offline";
  });

// Welcome message so the thread isn't empty on load.
(function seedWelcomeMessage() {
  const turn = document.createElement("div");
  turn.className = "chat-turn chat-turn-assistant";
  const card = document.createElement("div");
  card.className = "chat-bubble assistant-bubble";
  const p = document.createElement("p");
  p.textContent =
    "Ask me anything about the plant -- a specific problem (\"why is P-101 vibrating?\"), " +
    "an open-ended look-around (\"show me everything on Unit 100\", \"what's alarming right now?\"), " +
    "or browse the Plant Ontology Explorer on the right directly.";
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
  Asset: { className: "node-asset", categoryLabel: "Assets" },
  AlarmEvent: { className: "node-alarm", categoryLabel: "Alarms" },
  AlarmConfig: { className: "node-alarm", categoryLabel: "Alarms" },
  WorkOrder: { className: "node-workorder", categoryLabel: "Work Orders" },
  OperatorAction: { className: "node-operatoraction", categoryLabel: "Operator Actions" },
  HealthEvent: { className: "node-healthevent", categoryLabel: "Health Events" },
  CostPosting: { className: "node-costposting", categoryLabel: "Cost Postings" },
  HistorianTag: { className: "node-historiantag", categoryLabel: "Historian Tags" },
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

function renderInspector(node) {
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

// Called from a chat answer's "Focus in explorer" action (also triggered
// automatically once an answer renders): turns on whatever category filters
// are needed to reveal the cited evidence, highlights the resolved entity +
// every evidence/relationship node referenced by this specific answer, dims
// everything else, and pans the viewport to the entity. This is the direct
// link between "the agent reasoned about this" and the always-visible graph.
function focusAnswerInGraph(panel) {
  if (!panel || !panel.entity_id || !graphState.nodesById.has(panel.entity_id)) return;

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

  explorerHint.textContent = `Showing evidence for "${panel.entity}".`;
  panToNode(panel.entity_id, { select: true });
}

// Time each walk step stays "current" before advancing, in ms.
const WALK_STEP_DELAY_MS = 550;

// Replays `panel.walk` (see agent.py's build_graph_walk()) as an animated,
// step-by-step highlight across the explorer -- "here's where the
// reasoning is looking right now" -- before settling into
// focusAnswerInGraph()'s final all-at-once highlighted state. This is a
// post-hoc replay of the already-completed answer's tool-call/evidence
// trace, not a live view of the agent loop actually running (that would
// need streaming/SSE from /api/chat, which doesn't exist here -- see
// docs/CHAT_RAG.md §4b's "Known limitation" note) -- but it's what turns
// the previous instant snapshot into a visible walk across the graph.
function animateGraphWalk(panel) {
  const steps = (panel && panel.walk) || [];
  if (!panel || !panel.entity_id || !graphState.nodesById.has(panel.entity_id) || steps.length === 0) {
    focusAnswerInGraph(panel);
    return;
  }

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
      return;
    }

    const step = steps[i];
    const ids = step.node_ids || [];
    explorerHint.textContent = step.label;
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
  if (step.label) explorerHint.textContent = step.label;

  liveWalk.prevIds = ids;
}

function liveWalkFinish() {
  graphState.nodes.forEach((node) => node.el.classList.remove("walk-current", "walk-visited"));
  graphState.edges.forEach((edge) => edge.el.classList.remove("walk-current", "walk-visited"));
  liveWalk.prevIds = [];
}

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

function drawGraphEdgeEl(fromNode, toNode) {
  const el = document.createElement("div");
  el.className = "graph-edge";

  const fromX = fromNode.x;
  const fromY = fromNode.y;
  const toX = toNode.x;
  const toY = toNode.y;
  const dx = toX - fromX;
  const dy = toY - fromY;
  const len = Math.sqrt(dx * dx + dy * dy);
  const angle = Math.atan2(dy, dx) * (180 / Math.PI);

  el.style.left = `${fromX}px`;
  el.style.top = `${fromY}px`;
  el.style.width = `${len}px`;
  el.style.transform = `rotate(${angle}deg)`;
  return el;
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

  graphState.nodesById = nodesById;
  graphState.edgesByNode = new Map();
  nodes.forEach((n) => graphState.edgesByNode.set(n.id, []));
  edges.forEach((e) => {
    graphState.edgesByNode.get(e.source)?.push({ other: nodesById.get(e.target), edge: e });
    graphState.edgesByNode.get(e.target)?.push({ other: nodesById.get(e.source), edge: e });
  });

  plantGraph.innerHTML = "";
  plantGraph.style.width = `${width}px`;
  plantGraph.style.height = `${height}px`;

  const edgeEls = edges.map((edge) => {
    const el = drawGraphEdgeEl(nodesById.get(edge.source), nodesById.get(edge.target));
    plantGraph.appendChild(el);
    return { ...edge, el };
  });

  const nodeEls = nodes.map((node) => {
    const style = NODE_STYLE[node.label] || {};
    const el = document.createElement("div");
    el.className = `node graph-node ${style.className || "node-other"}`;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;

    const title = document.createElement("div");
    title.className = "node-title";
    title.textContent = nodeLabelText(node);
    el.appendChild(title);

    if (node.label !== "Asset") {
      const meta = document.createElement("div");
      meta.className = "node-meta";
      meta.textContent = node.label;
      el.appendChild(meta);
    }

    el.addEventListener("click", () => selectNode(node.id));
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

loadPlantGraph();

