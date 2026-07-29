const presetList = document.getElementById("presetList");
const customQuestion = document.getElementById("customQuestion");
const runButton = document.getElementById("runButton");
const rootCauseTitle = document.getElementById("rootCauseTitle");
const recommendedAction = document.getElementById("recommendedAction");
const confidenceBadge = document.getElementById("confidenceBadge");
const timelineEl = document.getElementById("timeline");
const evidenceEl = document.getElementById("evidence");
const relationshipGraph = document.getElementById("relationshipGraph");
const graphViewport = document.getElementById("graphViewport");
const entityLabel = document.getElementById("entityLabel");
const focusGroup = document.getElementById("focusGroup");
const zoomInBtn = document.getElementById("zoomInBtn");
const zoomOutBtn = document.getElementById("zoomOutBtn");
const zoomResetBtn = document.getElementById("zoomResetBtn");

let selectedCaseId = null;
let graphZoom = 1;
let activeFocus = "All";
let activeQuestion = "";

function confidenceClass(score) {
  if (score >= 0.9) return "confidence-high";
  if (score >= 0.75) return "confidence-medium";
  return "confidence-low";
}

function formatConfidence(score) {
  if (typeof score !== "number") return "--";
  return `Confidence ${(score * 100).toFixed(0)}%`;
}

function renderPresets() {
  presetList.innerHTML = "";
  MOCK_CASES.forEach((item) => {
    const btn = document.createElement("button");
    btn.className = "preset-btn";
    btn.textContent = item.label;
    btn.addEventListener("click", () => {
      selectedCaseId = item.id;
      activeQuestion = item.label;
      customQuestion.value = item.label;
      updateActivePreset();
      renderCase(item);
    });
    btn.dataset.caseId = item.id;
    presetList.appendChild(btn);
  });
}

function updateActivePreset() {
  const buttons = presetList.querySelectorAll("button");
  buttons.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.caseId === selectedCaseId);
  });
}

function renderTimeline(items) {
  timelineEl.innerHTML = "";
  items.forEach((step, idx) => {
    const li = document.createElement("li");
    li.style.animationDelay = `${idx * 70}ms`;

    const meta = document.createElement("p");
    meta.className = "timeline-meta";
    meta.innerHTML = `${step.time}<span class="source-chip">${step.source}</span>`;

    const txt = document.createElement("p");
    txt.textContent = step.text;

    li.appendChild(meta);
    li.appendChild(txt);
    timelineEl.appendChild(li);
  });
}

function renderEvidence(items) {
  evidenceEl.innerHTML = "";
  items.forEach((ev) => {
    const card = document.createElement("div");
    card.className = "evidence-card";
    const natural = humanizeEvidence(ev);
    card.innerHTML = `
      <p><strong>${ev.title}</strong></p>
      <p class="evidence-label">${ev.source}</p>
      <p>${natural}</p>
    `;
    evidenceEl.appendChild(card);
  });
}

function humanizeEvidence(ev) {
  if (ev.narrative) return ev.narrative;

  const row = ev.record || "";

  // DCS action: "..., U100_P101, SP_CHANGE, 320.0 -> 358.4"
  if (row.includes("SP_CHANGE")) {
    const loopMatch = row.match(/,\s*(U\d+_[A-Z]\d+)\s*,\s*SP_CHANGE/);
    const valuesMatch = row.match(/SP_CHANGE,\s*([0-9.]+)\s*->\s*([0-9.]+)/);
    if (loopMatch && valuesMatch) {
      const oldVal = Number(valuesMatch[1]);
      const newVal = Number(valuesMatch[2]);
      const pct = oldVal > 0 ? (((newVal - oldVal) / oldVal) * 100).toFixed(1) : "0.0";
      return `Operator changed setpoint on ${loopMatch[1]} from ${oldVal.toFixed(1)} to ${newVal.toFixed(1)} m3/h (${pct > 0 ? "+" : ""}${pct}%).`;
    }
  }

  // CMMS work order
  if (row.includes("WO-") && row.includes("EQ-")) {
    const wo = row.match(/(WO-\d+)/)?.[1];
    const eq = row.match(/(EQ-\d+)/)?.[1];
    const status = row.match(/,\s*(Closed|Open|Deferred)\s*,/i)?.[1];
    const note = row.split(",").slice(3).join(",").trim();
    if (wo && eq) {
      const statusText = status ? ` and marked as ${status.toLowerCase()}` : "";
      return `Maintenance work order ${wo} for asset ${eq} was logged${statusText}. ${note}`.trim();
    }
  }

  // Alarm event
  if (row.includes("AME-") && row.includes("HH") || row.includes("AME-") && row.includes("ACTIVE")) {
    const tag = row.match(/,\s*([A-Z0-9.-]+),\s*(H|HH|L|LL),\s*(ACTIVE|ACK|RTN)/i);
    const value = row.match(/,\s*([0-9]+(?:\.[0-9]+)?)\s*$/)?.[1];
    if (tag) {
      const alarmLevel = tag[2].toUpperCase();
      const state = tag[3].toUpperCase();
      const measured = value ? ` at ${value}` : "";
      return `Alarm ${tag[1]} reached ${alarmLevel} and is ${state}${measured}.`;
    }
  }

  // ERP posting link
  if (row.includes("POST-") && row.includes("linked_wo=")) {
    const post = row.match(/(POST-\d+)/)?.[1];
    const asset = row.match(/,\s*(ERP-[A-Z0-9-]+)/)?.[1];
    const wo = row.match(/linked_wo=([A-Z0-9-]+)/)?.[1];
    if (post && asset && wo) {
      return `ERP posting ${post} charges maintenance cost to ${asset} and links directly to work order ${wo}.`;
    }
  }

  return row;
}

function updateGraphZoom() {
  relationshipGraph.style.transform = `scale(${graphZoom})`;
  zoomResetBtn.textContent = `${Math.round(graphZoom * 100)}%`;
}

function inferDefaultFocus(item, question) {
  const q = (question || "").toLowerCase();
  if (q.includes("alarm") || item.id === "S5") return "Alarm";
  if (q.includes("same problem") || q.includes("k-101") || item.id === "S4") return "Maintenance";
  return "Operations";
}

function buildGraphModel(item) {
  const nodes = [];
  const edges = [];

  const center = {
    id: "cause",
    label: "Likely Cause",
    meta: item.entity,
    kind: "main",
    source: "Reasoning",
    x: 390,
    y: 150,
    detail: item.rootCause
  };
  nodes.push(center);

  const entityNode = {
    id: "entity",
    label: item.entity,
    meta: "Canonical asset",
    kind: "source",
    source: "Ontology",
    x: 185,
    y: 68,
    detail: "Resolved canonical equipment identity across applications."
  };
  nodes.push(entityNode);
  edges.push({ from: "entity", to: "cause" });

  item.relationships.slice(0, 4).forEach((rel, idx) => {
    const nodeId = `alias-${idx}`;
    nodes.push({
      id: nodeId,
      label: rel.label,
      meta: rel.type === "linked" ? "Dependency" : "Alias",
      kind: "source",
      source: "Identity",
      x: 40 + idx * 155,
      y: 20,
      detail: `Mapped as ${rel.type} for ${item.entity}.`
    });
    edges.push({ from: nodeId, to: "entity" });
  });

  item.timeline.forEach((step, idx) => {
    const nodeId = `event-${idx}`;
    nodes.push({
      id: nodeId,
      label: step.text,
      meta: `${step.time} | ${step.source}`,
      kind: "event",
      source: step.source,
      x: 155 + idx * 150,
      y: 252,
      detail: step.text
    });
    edges.push({ from: nodeId, to: "cause" });
  });

  item.evidence.slice(0, 3).forEach((ev, idx) => {
    const nodeId = `evidence-${idx}`;
    nodes.push({
      id: nodeId,
      label: ev.title,
      meta: ev.source,
      kind: "evidence",
      source: "Evidence",
      x: 610,
      y: 40 + idx * 92,
      detail: ev.record
    });
    edges.push({ from: nodeId, to: "cause" });
  });

  return { nodes, edges };
}

function sourceToFocus(source) {
  const s = source.toLowerCase();
  if (s.includes("cmms")) return "Maintenance";
  if (s.includes("am")) return "Alarm";
  if (s.includes("dcs")) return "Operations";
  if (s.includes("apm")) return "Performance";
  if (s.includes("hist")) return "Process";
  if (s.includes("erp")) return "Finance";
  if (s.includes("reasoning") || s.includes("ontology") || s.includes("identity")) return "Core";
  return "Core";
}

function renderFocusControls(defaultFocus) {
  const focusItems = ["All", "Operations", "Maintenance", "Alarm", "Performance", "Process", "Finance", "Core"];
  if (activeFocus === "All") activeFocus = defaultFocus;
  focusGroup.innerHTML = "";

  focusItems.forEach((label) => {
    const btn = document.createElement("button");
    btn.className = `focus-chip ${activeFocus === label ? "active" : ""}`;
    btn.type = "button";
    btn.textContent = label;
    btn.addEventListener("click", () => {
      activeFocus = label;
      renderFocusControls(defaultFocus);
      applyFocusState();
    });
    focusGroup.appendChild(btn);
  });
}

function applyFocusState() {
  const nodes = relationshipGraph.querySelectorAll(".node");
  const edges = relationshipGraph.querySelectorAll(".graph-edge");

  nodes.forEach((node) => {
    const focusType = node.dataset.focus;
    const match = activeFocus === "All" || focusType === activeFocus;
    node.classList.toggle("dimmed", !match);
  });

  edges.forEach((edge) => {
    const from = relationshipGraph.querySelector(`[data-node-id="${edge.dataset.from}"]`);
    const to = relationshipGraph.querySelector(`[data-node-id="${edge.dataset.to}"]`);
    const fromDim = from ? from.classList.contains("dimmed") : false;
    const toDim = to ? to.classList.contains("dimmed") : false;
    edge.classList.toggle("dimmed", fromDim || toDim);
  });
}

function drawEdge(fromNode, toNode, idPair) {
  const edge = document.createElement("div");
  edge.className = "graph-edge";
  edge.dataset.from = idPair.from;
  edge.dataset.to = idPair.to;

  const fromX = fromNode.x + 75;
  const fromY = fromNode.y + 22;
  const toX = toNode.x + 75;
  const toY = toNode.y + 22;
  const dx = toX - fromX;
  const dy = toY - fromY;
  const len = Math.sqrt(dx * dx + dy * dy);
  const angle = Math.atan2(dy, dx) * (180 / Math.PI);

  edge.style.left = `${fromX}px`;
  edge.style.top = `${fromY}px`;
  edge.style.width = `${len}px`;
  edge.style.transform = `rotate(${angle}deg)`;

  relationshipGraph.appendChild(edge);
}

function renderRelationships(item, question) {
  entityLabel.textContent = item.entity;
  relationshipGraph.innerHTML = "";

  const graphModel = buildGraphModel(item);
  const viewportWidth = graphViewport.clientWidth || 760;
  const scaleX = Math.min(1, Math.max(0.72, (viewportWidth - 30) / 860));
  const scaleY = Math.min(1, Math.max(0.8, (graphViewport.clientHeight - 30) / 360));

  graphModel.nodes.forEach((node) => {
    node.x = Math.round(node.x * scaleX);
    node.y = Math.round(node.y * scaleY);
  });

  graphModel.edges.forEach((edge) => {
    const fromNode = graphModel.nodes.find((n) => n.id === edge.from);
    const toNode = graphModel.nodes.find((n) => n.id === edge.to);
    if (fromNode && toNode) {
      drawEdge(fromNode, toNode, edge);
    }
  });

  graphModel.nodes.forEach((node) => {
    const el = document.createElement("div");
    const kindClass =
      node.kind === "main"
        ? "node-main"
        : node.kind === "event"
          ? "node-event"
          : node.kind === "evidence"
            ? "node-evidence"
            : "node-source";

    el.className = `node ${kindClass}`;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;
    el.dataset.nodeId = node.id;
    el.dataset.focus = sourceToFocus(node.source);

    el.innerHTML = `<div class="node-title">${node.label}</div><div class="node-meta">${node.meta}</div>`;
    el.addEventListener("click", () => {
      const prior = relationshipGraph.querySelector(".node.focused");
      if (prior) prior.classList.remove("focused");
      el.classList.add("focused");
      entityLabel.textContent = `${item.entity} - ${node.detail}`;
    });

    relationshipGraph.appendChild(el);
  });

  const defaultFocus = inferDefaultFocus(item, question);
  activeFocus = defaultFocus;
  renderFocusControls(defaultFocus);
  applyFocusState();
}

function renderCase(item) {
  rootCauseTitle.textContent = item.rootCause;
  recommendedAction.textContent = item.recommendation;

  confidenceBadge.className = `confidence ${confidenceClass(item.confidence)}`;
  confidenceBadge.textContent = formatConfidence(item.confidence);

  renderTimeline(item.timeline);
  renderEvidence(item.evidence);
  renderRelationships(item, activeQuestion);
}

function findCaseForQuestion(question) {
  const q = question.toLowerCase();
  const exact = MOCK_CASES.find((item) => item.label.toLowerCase() === q);
  if (exact) return exact;

  if (q.includes("p-101") || q.includes("vibrat")) return MOCK_CASES[0];
  if (q.includes("k-101") || q.includes("same problem")) return MOCK_CASES[1];
  if (q.includes("tk-201") || q.includes("alarm")) return MOCK_CASES[2];

  return MOCK_CASES[0];
}

runButton.addEventListener("click", () => {
  activeQuestion = customQuestion.value.trim();
  const item = findCaseForQuestion(activeQuestion);
  selectedCaseId = item.id;
  updateActivePreset();
  renderCase(item);
});

zoomInBtn.addEventListener("click", () => {
  graphZoom = Math.min(1.8, graphZoom + 0.15);
  updateGraphZoom();
});

zoomOutBtn.addEventListener("click", () => {
  graphZoom = Math.max(0.65, graphZoom - 0.15);
  updateGraphZoom();
});

zoomResetBtn.addEventListener("click", () => {
  graphZoom = 1;
  updateGraphZoom();
});

renderPresets();
selectedCaseId = MOCK_CASES[0].id;
customQuestion.value = MOCK_CASES[0].label;
activeQuestion = MOCK_CASES[0].label;
updateActivePreset();
renderCase(MOCK_CASES[0]);
updateGraphZoom();
