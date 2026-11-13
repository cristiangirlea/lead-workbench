"use strict";
// All text is set through textContent; nothing from the database is parsed as HTML.

const TOKEN = document.querySelector('meta[name="csrf-token"]').content;
const $ = (id) => document.getElementById(id);
let META = null;
let selectedId = null;
let editOpen = false;

// replaceChildren/append turn null into the text "null"; every child list goes through this first.
const nodes = (...kids) => kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of nodes(kids)) {
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function safeHref(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch { return null; }
}

function link(url, text) {
  const href = safeHref(url);
  return href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text || url) : h("span", { class: "unknown" }, "invalid link");
}

const unknown = (why) => h("span", { class: "unknown" }, why || "unknown");
// null and undefined become "" before any string concatenation.
const txt = (v) => (v === null || v === undefined ? "" : String(v));

// Invitation statuses are a lifecycle, not research to fix: readiness checks apply only before an invitation.
const READINESS_STATUSES = new Set(["research", "qualified"]);
const LIFECYCLE = {
  sent: ["Awaiting reply", "awaiting", "Invitation sent; awaiting reply."],
  pending: ["Awaiting reply", "awaiting", "Invitation sent; awaiting reply."],
  connected: ["Connected", "connected", "Invitation accepted; connected."],
  rejected: ["Closed", "closed", "Closed: the invitation was not accepted."],
  excluded: ["Excluded", "excluded", "Excluded from outreach."],
  uncertain: ["Needs review", "awaiting", "Marked uncertain: decide whether to research further or exclude."],
};

function queueCell(r) {
  const life = LIFECYCLE[r.status];
  if (life && !READINESS_STATUSES.has(r.status)) return h("span", { class: "lifecycle " + life[1] }, life[0]);
  return r.queue_blockers.length
    ? h("span", { class: "blocked", title: r.queue_blockers.join("\n") }, `${r.queue_blockers.length} to resolve`)
    : h("span", { class: "ready" }, "ready");
}
const localTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Bucharest", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const fmtDate = (s) => {
  if (!s) return "";
  if (!String(s).includes("T")) return String(s);
  const date = new Date(s);
  return Number.isNaN(date.valueOf()) ? String(s) : `${localTime.format(date)} Bucharest`;
};

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.headers["X-CSRF-Token"] = TOKEN;
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = isErr ? "toast err" : "toast";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 2500);
}

// ---------------------------------------------------------------- leads list

async function loadCounts() {
  const s = await api("/api/stats");
  $("counts").textContent = s.leads === 0
    ? "No leads yet"
    : `${s.leads} leads, ${s.queue_ready} ready to invite, ${s.by_status.sent + s.by_status.pending} awaiting reply`;
}

async function loadLeads() {
  const p = new URLSearchParams();
  if ($("q").value.trim()) p.set("q", $("q").value.trim());
  if ($("status").value) p.set("status", $("status").value);
  if ($("ready").checked) p.set("ready", "1");
  const rows = await api("/api/leads?" + p);
  const body = $("rows");
  body.replaceChildren(...rows.map((r) => h("tr", {
      tabindex: "0", "aria-current": r.id === selectedId ? "true" : null, "data-id": r.id,
      onclick: () => selectLead(r.id),
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectLead(r.id); }
      },
    },
    h("td", {}, r.name || unknown("name not recorded"), h("span", { class: "sub" }, txt(r.title))),
    h("td", {}, r.company || unknown(), h("span", { class: "sub" }, txt(r.company_market))),
    h("td", {}, h("span", { class: "status " + r.status }, r.status)),
    h("td", {}, queueCell(r)),
    h("td", {}, r.last_checked || unknown("never")),
  )));
  const filtered = $("q").value || $("status").value || $("ready").checked;
  $("empty").hidden = rows.length > 0;
  $("empty").textContent = filtered
    ? "No leads match these filters."
    : "No leads yet. Import researched leads with `python -m app upsert leads.json`, or use Add lead.";
}

// ---------------------------------------------------------------- detail

function fact(label, value, sourceUrl, sourceText) {
  const has = value !== null && value !== undefined && value !== "" && value !== "unknown";
  const dd = h("dd", { class: has && sourceUrl ? "sourced" : null },
    has ? value : unknown(),
    sourceUrl ? h("span", { class: "src" }, "Source: ", link(sourceUrl)) : null,
    !sourceUrl && sourceText ? h("span", { class: "src" }, "Source: " + sourceText) : null,
    has && !sourceUrl && !sourceText && label !== "Relationship" && label !== "Company type"
      ? h("span", { class: "src unknown" }, "no source recorded") : null);
  return [h("dt", {}, label), dd];
}

async function selectLead(id) {
  selectedId = id;
  document.querySelectorAll("#rows tr").forEach((tr) =>
    +tr.dataset.id === id ? tr.setAttribute("aria-current", "true") : tr.removeAttribute("aria-current"));
  const pane = $("detail");
  let lead;
  try { lead = await api("/api/leads/" + id); } catch (e) { return toast(e.message, true); }
  const profile = safeHref(lead.linkedin_url);
  const draft = lead.invitation_draft || "";
  const max = (POLICY && POLICY.invitation && POLICY.invitation.max_note_chars) || 300;
  const preInvite = READINESS_STATUSES.has(lead.status);
  const wasSent = ["sent", "pending"].includes(lead.status) || lead.events.some((ev) => ev.kind === "status" && ["sent", "pending"].includes(ev.to_status));
  const life = LIFECYCLE[lead.status];
  const since = lead.events.filter((ev) => ev.kind === "status" && ev.to_status === lead.status).pop();
  const edit = h("details", { class: "edit", open: editOpen, ontoggle: (e) => { editOpen = e.target.open; } },
    h("summary", {}, "Edit details"), editForm(lead));

  pane.replaceChildren(...nodes(
    h("h2", { class: "lead-name" }, lead.name || "Name not recorded"),
    h("p", { class: "role" }, [lead.title, lead.company].filter(Boolean).join(" at ") || "Role and company not recorded"),
    h("div", { class: "actions" },
      profile ? h("a", { href: profile, target: "_blank", rel: "noopener noreferrer" }, "Open profile") : null,
      h("button", { type: "button", class: "quiet", disabled: !draft, onclick: () => copyDraft(draft) }, "Copy invitation"),
      h("span", { class: "status " + lead.status }, lead.status)),
    preInvite || lead.status === "uncertain"
      ? [
        h("h3", {}, "Invitation readiness"),
        lead.queue_blockers.length
          ? h("ul", { class: "blockers" }, lead.queue_blockers.map((b) => h("li", {}, b)))
          : h("p", { class: "ready" }, "Ready: passes every policy check."),
        h("p", { class: "note" }, "Invitations go out from LinkedIn; record the result here with Change status."),
      ]
      : [
        h("h3", {}, "Invitation"),
        h("p", { class: "lifecycle " + life[1] }, life[2],
          since ? h("span", { class: "sub" }, `Recorded ${fmtDate(since.at)}`) : null),
        lead.hold_reason ? h("p", { class: "note" }, "On hold: " + txt(lead.hold_reason)) : null,
      ],
    lead.exclusion_reason ? h("p", { class: "note" }, "Exclusion reason: " + txt(lead.exclusion_reason)) : null,

    h("h3", {}, wasSent ? "Sent invitation note" : "Invitation draft"),
    draft ? h("p", { class: "draft" }, draft) : unknown("No draft yet."),
    draft ? h("p", { class: "charcount" }, `${draft.length} of ${max} characters`) : null,

    h("h3", {}, "Evidence"),
    h("dl", { class: "facts" },
      fact("Location", [lead.contact_location, lead.contact_country && `(${lead.contact_country})`].filter(Boolean).join(" "), null, lead.contact_location_source),
      fact("Hiring signal", lead.hiring_signal && [lead.hiring_signal, lead.hiring_date && `on ${lead.hiring_date}`].filter(Boolean).join(" "), lead.hiring_source_url),
      fact("Who they hire", lead.eligibility, lead.eligibility_source_url),
      fact("Budget", lead.budget.published ? lead.budget.text + (lead.budget.meets_goal === null ? " (not comparable to goal without conversion)" : lead.budget.meets_goal ? " (meets goal)" : " (below goal)") : null, lead.budget_source_url),
      fact("Company market", lead.company_market),
      fact("Company type", lead.company_type),
      fact("Relationship", lead.relationship),
      fact("Last checked", lead.last_checked),
    ),

    lead.applications.length ? h("h3", {}, "Your applications at this company") : null,
    lead.applications.length ? h("ul", { class: "history" }, lead.applications.map(appItem)) : null,

    lead.notes ? h("h3", {}, "Notes") : null,
    lead.notes ? h("p", { class: "draft" }, lead.notes) : null,

    h("h3", {}, "Change status"),
    statusForm(lead),

    edit,

    h("h3", {}, "History"),
    h("ul", { class: "history" }, lead.events.slice().reverse().map(eventItem)),
  ));
}

function appItem(a) {
  return h("li", {},
    `${a.role_title || "Role not recorded"}: ${a.outcome}`,
    a.decision_basis !== "unknown" ? `, ${a.decision_basis.replaceAll("_", " ")}` : ", decision basis unknown",
    a.evidence_url ? h("span", { class: "src" }, link(a.evidence_url, "evidence")) : null);
}

function eventItem(ev) {
  let text = ev.kind;
  if (ev.kind === "status") text = `${txt(ev.from_status) || "new"} → ${txt(ev.to_status)}`;
  else if (ev.kind === "created") text = `created as ${txt(ev.to_status)}`;
  else if (ev.kind === "updated" && ev.detail) text = "edited " + Object.keys(ev.detail.changed || {}).join(", ");
  return h("li", { class: ev.kind === "status" ? "status-ev" : null },
    h("time", {}, `${fmtDate(ev.at)} by ${txt(ev.actor) || "unknown"}`),
    text,
    ev.evidence ? h("span", { class: "sub" }, ev.evidence) : null,
    ev.evidence_url ? h("span", { class: "src" }, link(ev.evidence_url)) : null);
}

function statusForm(lead) {
  if (!lead.allowed_next.length) return h("p", { class: "note" }, `'${lead.status}' is final.`);
  const sel = h("select", { name: "status", required: true },
    h("option", { value: "" }, "Choose…"), lead.allowed_next.map((s) => h("option", { value: s }, s)));
  const ev = h("textarea", { name: "evidence", rows: "2", required: true, placeholder: "What you saw, and where (e.g. LinkedIn shows Pending on 2026-11-11)" });
  const url = h("input", { name: "evidence_url", placeholder: "Evidence link (optional)" });
  return h("form", { class: "status-form", onsubmit: async (e) => {
      e.preventDefault();
      try {
        await api(`/api/leads/${lead.id}/status`, { method: "POST", body: { status: sel.value, evidence: ev.value, evidence_url: url.value || null } });
        toast(`Status changed to ${sel.value}`);
        await refresh();
      } catch (err) { toast(err.message, true); }
    } },
    h("label", {}, "New status", sel), h("label", {}, "Evidence", ev), h("label", {}, "Evidence link", url),
    h("div", {}, h("button", { type: "submit" }, "Change status")));
}

const EDITABLE = [
  ["name", "Name"], ["title", "Title"], ["company", "Company"], ["company_market", "Company market"],
  ["company_type", "Company type", "company_types"], ["contact_location", "Location (as on profile)"],
  ["contact_country", "Country of location"], ["contact_location_source", "Location source"],
  ["hiring_signal", "Hiring signal"], ["hiring_source_url", "Hiring source link"], ["hiring_date", "Hiring date"],
  ["eligibility", "Who they hire", "eligibility"], ["eligibility_source_url", "Eligibility source link"],
  ["budget_min", "Budget min", "number"], ["budget_max", "Budget max", "number"], ["budget_currency", "Currency"],
  ["budget_period", "Budget period", "budget_periods"], ["budget_source_url", "Budget source link"],
  ["relationship", "Relationship", "relationships"], ["last_checked", "Last checked"],
  ["hold_reason", "Hold reason"], ["exclusion_reason", "Exclusion reason"],
];

function editForm(lead) {
  const inputs = {};
  const fields = EDITABLE.map(([key, label, kind]) => {
    let el;
    if (kind && META[kind]) {
      el = h("select", { name: key }, META[kind].map((v) => h("option", { value: v, selected: lead[key] === v }, v)));
    } else {
      el = h("input", { name: key, type: kind === "number" ? "number" : "text", min: kind === "number" ? "0" : null, step: "any" });
      el.value = txt(lead[key]);
    }
    inputs[key] = el;
    return h("label", {}, label, el);
  });
  const draft = h("textarea", { name: "invitation_draft", rows: "4" });
  draft.value = txt(lead.invitation_draft);
  const notes = h("textarea", { name: "notes", rows: "3" });
  notes.value = txt(lead.notes);
  return h("form", { class: "edit-form", onsubmit: async (e) => {
      e.preventDefault();
      // Send only what was changed here, so a save never reverts a newer import.
      const body = {};
      const norm = (v) => txt(v).trim();
      if (norm(draft.value) !== norm(lead.invitation_draft)) body.invitation_draft = draft.value;
      if (norm(notes.value) !== norm(lead.notes)) body.notes = notes.value;
      for (const [key, , kind] of EDITABLE) {
        const v = inputs[key].value.trim();
        if (kind === "number" ? (v === "" ? null : Number(v)) === lead[key] : v === norm(lead[key])) continue;
        body[key] = kind === "number" ? (v === "" ? null : Number(v)) : v;
      }
      if (!Object.keys(body).length) return toast("Nothing changed");
      try {
        const res = await api(`/api/leads/${lead.id}`, { method: "PATCH", body });
        toast(res.changed.length ? `Saved ${res.changed.length} change(s)` : "Nothing changed");
        await refresh();
      } catch (err) { toast(err.message, true); }
    } },
    h("div", { class: "grid" }, fields),
    h("label", {}, "Invitation draft", draft),
    h("label", {}, "Notes", notes),
    h("p", { class: "note" }, "Empty fields are saved as unknown. Budgets, eligibility and hiring signals need a source link."),
    h("div", {}, h("button", { type: "submit" }, "Save changes")));
}

async function copyDraft(text) {
  try { await navigator.clipboard.writeText(text); toast("Invitation copied"); }
  catch { toast("Copy failed: select the draft text and copy it by hand", true); }
}

// ---------------------------------------------------------------- other views

async function loadApplications() {
  const rows = await api("/api/applications" + ($("revisit-only").checked ? "?revisit=1" : ""));
  $("app-rows").replaceChildren(...rows.map((a) => h("tr", {},
    h("td", {}, a.company),
    h("td", {}, a.role_title || unknown()),
    h("td", {}, a.outcome === "rejected" ? (a.rejection_date || unknown("date unknown")) : a.outcome),
    h("td", {}, a.decision_basis === "unknown" ? unknown("not established") : a.decision_basis.replaceAll("_", " "),
      h("span", { class: "sub" }, "via " + a.rejection_source.replaceAll("_", " "))),
    h("td", {}, a.evidence || "", a.evidence_url ? h("span", { class: "src" }, link(a.evidence_url, "open")) : null),
    h("td", {}, String(a.lead_count)),
    h("td", {}, h("select", { "aria-label": `Revisit status for ${a.company}`, onchange: async (e) => {
        try { await api(`/api/applications/${a.id}`, { method: "PATCH", body: { revisit_status: e.target.value } }); toast("Revisit status saved"); }
        catch (err) { toast(err.message, true); }
      } }, META.revisit_statuses.map((s) => h("option", { value: s, selected: a.revisit_status === s }, s.replaceAll("_", " "))))),
  )));
  $("app-empty").hidden = rows.length > 0;
  $("app-empty").textContent = "No recorded rejections to revisit.";
}

async function loadRuns() {
  const rows = await api("/api/runs");
  $("run-rows").replaceChildren(...rows.map((r) => h("tr", {},
    h("td", {}, fmtDate(r.recorded_at)), h("td", {}, r.kind), h("td", {}, r.summary, r.notes ? h("span", { class: "sub" }, r.notes) : null),
    h("td", {}, r.counts ? Object.entries(r.counts).map(([k, v]) => `${k}: ${v}`).join(", ") : ""))));
  $("run-empty").hidden = rows.length > 0;
  $("run-empty").textContent = "No runs logged yet.";
}

let POLICY = null;
function renderPolicy() {
  const p = POLICY;
  const row = (label, value) => [h("dt", {}, label), h("dd", {}, value)];
  const goal = p.goal || {};
  $("policy").replaceChildren(
    h("p", { class: "policy-text" }, p.statement),
    h("dl", { class: "facts" },
      row("Version", p.policy_version),
      row("Preferred markets", (p.preferred_company_markets || []).join(", ")),
      row("Skip contacts located in", (p.excluded_contact_countries || []).join(", ") + ". " + (p.excluded_contact_countries_basis || "")),
      row("Hiring must cover", (p.required_eligibility || []).join(", ").replaceAll("_", " ")),
      row("Rejected restrictions", (p.rejected_eligibility || []).join(", ").replaceAll("_", " ")),
      row("Skipped company types", (p.skip_company_types || []).join(", ").replaceAll("_", " ")),
      row("Budget goal", goal.amount == null ? "Not configured" : `${goal.amount} ${goal.currency} per ${goal.period}, ${goal.employment || "unspecified"}`),
      row("Invitation note limit", `${(p.invitation || {}).max_note_chars} characters; sent by a person only`)),
    h("p", { class: "note" }, "Copy app/settings.example.json to settings.json in your private workbench data directory, edit your policy, then restart the app."));
}

// ---------------------------------------------------------------- wiring

async function refresh() {
  await Promise.all([loadCounts(), loadLeads()]);
  if (selectedId !== null) await selectLead(selectedId);
}

function showView(name) {
  document.querySelectorAll(".views button").forEach((b) =>
    b.dataset.view === name ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  for (const v of ["leads", "revisit", "runs", "policy"]) $("view-" + v).hidden = v !== name;
  const load = { leads: loadLeads, revisit: loadApplications, runs: loadRuns, policy: async () => renderPolicy() }[name];
  load().catch((e) => toast(e.message, true));
}

async function init() {
  [META, POLICY] = await Promise.all([api("/api/meta"), api("/api/policy")]);
  $("status").append(...META.statuses.map((s) => h("option", { value: s }, s)));
  document.querySelectorAll(".views button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  let timer;
  $("q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(loadLeads, 200); });
  $("status").addEventListener("change", loadLeads);
  $("ready").addEventListener("change", loadLeads);
  $("filters").addEventListener("submit", (e) => { e.preventDefault(); loadLeads(); });
  $("revisit-only").addEventListener("change", loadApplications);
  $("add-toggle").addEventListener("click", () => {
    const form = $("add-form");
    form.hidden = !form.hidden;
    $("add-toggle").setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) form.elements.linkedin_url.focus();
  });
  $("add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    for (const [k, v] of new FormData(e.target)) if (String(v).trim()) body[k] = String(v).trim();
    try {
      const res = await api("/api/leads", { method: "POST", body });
      toast(res.action === "created" ? "Lead saved" : "Lead already existed; details merged");
      e.target.reset();
      selectedId = res.id;
      await refresh();
    } catch (err) { toast(err.message, true); }
  });
  await refresh();
}

init().catch((e) => toast(e.message, true));
