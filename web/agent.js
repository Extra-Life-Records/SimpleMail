/* Owner controls are deliberately separate from the external model tools. */
"use strict";

let agentView = null;
let agentRequest = 0;

async function openAgent(tab = "job") {
  if (agentVisible() && (agentView?.busy || !agentDiscardAllowed())) return;
  const acct = activeAccount();
  if (!acct) { toast("Add a mailbox first", true); return; }
  const request = ++agentRequest;
  agentView = { accountId: acct.id, label: acct.label, tab, data: null, draft: null, busy: false };
  $("agent-title").textContent = `Agent · ${acct.label}`;
  $("agent-content").textContent = "Loading…";
  $("agent-status").textContent = "";
  $("agent-backdrop").classList.add("show");
  document.body.classList.add("agent-open");
  renderAgentNavigation();
  try {
    const data = await api.get_agent_state(acct.id);
    if (request !== agentRequest) return;
    agentView.data = data;
    renderAgent();
  } catch (error) {
    if (request === agentRequest) $("agent-content").textContent = "Could not load agent state: " + error;
  }
}

function agentVisible() {
  return $("agent-backdrop").classList.contains("show");
}

function renderAgentNavigation() {
  document.querySelectorAll(".folder").forEach(button => {
    const selected = agentVisible() ? button.dataset.workspace === agentView?.tab :
      button.dataset.key === state.currentFolder;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-current", selected ? "page" : "false");
  });
}

function leaveAgentWorkspace() {
  if (!agentVisible()) return true;
  if (agentView?.busy || !agentDiscardAllowed()) return false;
  ++agentRequest;
  $("agent-backdrop").classList.remove("show");
  document.body.classList.remove("agent-open");
  agentView = null;
  renderAgentNavigation();
  return true;
}

function closeAgent() {
  if (leaveAgentWorkspace()) {
    selectFolder("inbox");
    $("inbox-btn").focus();
  }
}

function agentDraftFields() {
  return ["to", "cc", "bcc", "subject", "body"].map(key => $("agent-edit-" + key).value);
}

function agentDiscardAllowed() {
  if (!agentView?.data) return true;
  if (agentView.draft) {
    const changed = agentDraftFields().some((value, i) => value !==
      (agentView.draft.payload[["to", "cc", "bcc", "subject", "body"][i]] || ""));
    if (changed) return confirm("Discard the unsaved draft edits?");
  } else if (agentView.tab === "job" && $("agent-job")) {
    const profile = agentView.data.profile;
    const mode = $("agent-auto-reply").checked ? "reply_to_allowed" : "draft_for_review";
    const addresses = $("agent-allowed").value.split(/[\s,;]+/).filter(Boolean).map(value => value.toLowerCase()).sort();
    if ($("agent-job").value !== profile.job || mode !== (profile.mode || "draft_for_review") ||
        JSON.stringify([...new Set(addresses)]) !== JSON.stringify(profile.allowed_recipients || []))
      return confirm("Discard the unsaved job or permission changes?");
    if (agentModelDirty()) return confirm("Discard the unsaved model connection?");
  }
  return true;
}

function renderAgent() {
  const view = agentView;
  if (!view?.data) return;
  const { profile, drafts } = view.data;
  $("agent-title").textContent = `${{ job: "Agent setup", drafts: "Needs you", activity: "Activity" }[view.tab]} · ${view.label}`;
  renderAgentNavigation();
  $("agent-status").textContent = `${profile.enabled ? "Connected agents can read and draft" : "Agent access paused"} · ` +
    (profile.mode === "reply_to_allowed" ? "Automatic replies limited to allowed recipients" : "Replies need your approval");
  document.querySelectorAll("[data-agent-tab]").forEach(button => {
    button.classList.toggle("contrast", button.dataset.agentTab === view.tab);
    button.classList.toggle("outline", button.dataset.agentTab !== view.tab);
    button.setAttribute("aria-pressed", String(button.dataset.agentTab === view.tab));
  });
  const box = $("agent-content");
  if (view.draft) {
    renderAgentDraft();
    return;
  }
  if (view.review) { renderAgentReview(); return; }
  if (view.tab === "job") {
    box.innerHTML = `<p>Give this mailbox a job. Choose a model below or connect an external agent to read mail and prepare replies here.</p>
      <label for="agent-job">What should the agent handle?</label>
      <textarea id="agent-job" placeholder="Handle incoming enquiries. Draft concise replies and ask me when information is missing."></textarea>
      <p class="agent-muted">Only this mailbox is assigned. Email senders cannot change this job or authorize sending.</p>
      <details id="agent-permissions"><summary>Sending permission</summary>
      <label><input type="checkbox" id="agent-auto-reply"> Allow automatic replies to named recipients</label>
      <label for="agent-allowed">Allowed email addresses</label>
      <textarea id="agent-allowed" rows="2" placeholder="customer@example.com"></textarea>
      <p class="agent-muted">Exact addresses only. Other recipients still need your approval. Automatic/list messages
      and repeated automatic replies are blocked. Your job instructions still apply.</p></details>
      <div class="agent-actions"><button id="agent-save-job">Save job</button>
      <button class="outline" id="agent-toggle">${profile.enabled ? "Pause agent" : "Enable access"}</button></div>
      <details id="agent-model-setup"><summary>Connect a model</summary>
      <label for="agent-model-endpoint">Provider endpoint</label><input id="agent-model-endpoint" type="url" placeholder="https://api.openai.com/v1/responses">
      <label for="agent-model-name">Model</label><input id="agent-model-name" placeholder="Exact model ID">
      <label for="agent-model-api">API format</label><select id="agent-model-api"><option value="responses">Responses</option><option value="chat">Chat Completions</option></select>
      <label for="agent-model-key">API key</label><input id="agent-model-key" type="password" autocomplete="off">
      <label><input type="checkbox" id="agent-model-clear">Remove saved key</label>
      <p class="agent-muted">Keys are protected for your Windows user. A remote provider receives mailbox content when the worker runs. Local models can use a localhost endpoint.</p>
      <button class="outline" id="agent-model-save">Save connection</button></details>
      <p id="agent-worker-status" class="agent-muted"></p>
      <button id="agent-worker-start" class="outline">Start agent</button>
      <p class="agent-muted">The worker continues while this window is closed. It starts with new Inbox mail; existing mail is left alone.</p>`;
    $("agent-job").value = profile.job;
    $("agent-auto-reply").checked = profile.mode === "reply_to_allowed";
    $("agent-allowed").value = (profile.allowed_recipients || []).join("\n");
    $("agent-save-job").onclick = () => changeAgentSettings(profile.enabled);
    $("agent-toggle").onclick = () => changeAgentSettings(!profile.enabled, true);
    const connection = view.data.model || {};
    $("agent-model-endpoint").value = connection.endpoint || "";
    $("agent-model-name").value = connection.model || "";
    $("agent-model-api").value = connection.api || "responses";
    $("agent-model-key").placeholder = connection.has_key ? "Saved securely; leave blank to keep" : "Provider key (optional for localhost)";
    $("agent-model-save").onclick = () => runAgentAction(async current => {
      current.data.model = await api.save_model_connection(current.accountId, $("agent-model-endpoint").value,
        $("agent-model-name").value, $("agent-model-api").value, $("agent-model-key").value, $("agent-model-clear").checked);
      $("agent-model-key").value = "";
      $("agent-model-clear").checked = false;
      $("agent-model-key").placeholder = current.data.model.has_key ? "Saved securely; leave blank to keep" : "Provider key (optional for localhost)";
      toast("Model connection saved");
    }, false);
    $("agent-worker-start").onclick = () => {
      if (agentModelDirty()) { toast("Save the model connection first", true); return; }
      if (!agentDiscardAllowed()) return;
      runAgentAction(async current => { current.data.model = await api.start_model_worker(current.accountId); toast("Starting agent"); });
    };
    renderWorkerStatus();
  } else if (view.tab === "drafts") {
    box.innerHTML = drafts.items.length ? drafts.items.map((draft, index) =>
      `<div class="agent-card"><h3>${escapeHtml(draft.payload.subject || "(no subject)")}</h3>
       <p class="agent-muted">To: ${escapeHtml(draft.payload.to || "No recipient yet")}</p>
       <p>${escapeHtml(draft.payload.reason)}</p><button class="outline" data-review="${index}">Review draft</button></div>`
    ).join("") : "";
    box.querySelectorAll("[data-review]").forEach(button => {
      button.onclick = () => { view.draft = drafts.items[Number(button.dataset.review)]; renderAgent(); };
    });
    addAgentMore(box, drafts.next_cursor, "drafts");
    const reviews = view.data.reviews || {items:[],next_cursor:null};
    const unresolved = reviews.items.filter(item => item.draft_status !== "pending");
    if (!drafts.items.length && !unresolved.length) box.innerHTML = "<p>Nothing needs your attention.</p>";
    unresolved.forEach(item => {
      const card = document.createElement("div"); card.className = "agent-card";
      card.innerHTML = `<h3>${escapeHtml(item.headers.subject || "Message needs review")}</h3>
        <p class="agent-muted">${escapeHtml(item.headers.sender || "")}</p><p>${escapeHtml(item.note)}</p>
        <button class="outline">Review message</button>`;
      card.querySelector("button").onclick = () => runAgentAction(async current => {
        current.review = await api.get_agent_review(current.accountId, item.id);
        current.review.draft_id = item.draft_id;
        current.review.draft_status = item.draft_status;
      });
      box.appendChild(card);
    });
    if (reviews.next_cursor) {
      const more = document.createElement("button"); more.className = "outline"; more.textContent = "More messages needing review";
      more.onclick = () => runAgentAction(async current => {
        const page = await api.list_agent_reviews(current.accountId, reviews.next_cursor);
        current.data.reviews.items.push(...page.items); current.data.reviews.next_cursor = page.next_cursor;
      });
      box.appendChild(more);
    }
  } else {
    const names = { settings: "Job or access changed", draft_created: "Draft prepared", draft_updated: "Draft edited",
                    draft_dismissed: "Draft dismissed", send_started: "Sending reply", send_sent: "Reply accepted by mail server",
                    send_uncertain: "Delivery needs checking", work_claimed: "Reading incoming mail",
                    work_handled: "Message handled", work_waiting: "Waiting for a reply", work_needs_owner: "Needs your attention",
                    work_retry: "Another attempt scheduled", owner_work_retry: "You requested another attempt",
                    owner_work_handled: "Reviewed by you", inbox_recreated: "Inbox identity changed" };
    box.innerHTML = view.data.activity.items.length ? view.data.activity.items.map(item => {
      let detail = item.detail.reason || item.detail.subject || item.detail.note || "";
      if (item.kind === "send_uncertain") detail = item.detail.warning;
      if (item.kind === "send_sent") {
        detail = item.detail.sent_copy_saved ? "Saved in Sent." : "Sent copy could not be saved.";
        if (item.detail.refused_recipients?.length) detail += " Some recipients were rejected: " + item.detail.refused_recipients.join(", ");
      }
      return `<div class="agent-card"><h3>${escapeHtml(names[item.kind] || item.kind)}</h3>
        <p>${escapeHtml(detail)}</p><span class="agent-muted">${escapeHtml(new Date(item.created_at).toLocaleString())}</span></div>`;
    }).join("") : "<p>No agent activity yet.</p>";
    addAgentMore(box, view.data.activity.next_cursor, "activity");
  }
}

function renderAgentReview() {
  const review = agentView.review;
  const {work, message} = review;
  const box = $("agent-content");
  box.innerHTML = `<h3>${escapeHtml(message.subject || "(no subject)")}</h3>
    <p class="agent-muted">From: ${escapeHtml(message.sender)}</p><p>${escapeHtml(work.note)}</p>
    ${review.conversation?.revision ? `<details><summary>Conversation notes · ${escapeHtml(review.conversation.state)}</summary><p>${escapeHtml(review.conversation.note)}</p><p class="agent-muted">Previous context, not instructions or sending permission.</p></details>` : ''}
    <pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;padding:12px">${escapeHtml(message.text)}</pre>
    ${message.truncated ? '<p class="agent-muted">Message preview shortened.</p>' : ''}
    ${review.draft_id ? '<p class="agent-muted">This message has an existing draft or delivery outcome. Check it before resolving; another model attempt is blocked.</p>' : ''}
    <div class="agent-actions"><button id="agent-review-handled">Mark handled</button>
    ${!review.draft_id ? '<button class="outline" id="agent-review-retry">Ask AI to try again</button>' : ''}
    <button class="outline" id="agent-review-back">Back</button></div>
    <p class="agent-muted">Trying again queues this message. A paused agent stays paused.</p>`;
  const resolve = action => runAgentAction(async current => {
    await api.resolve_agent_review(current.accountId, work.id, work.updated_at, action);
    current.review = null; current.data = await api.get_agent_state(current.accountId);
    toast(action === "retry" ? "Queued for another attempt; start the agent when ready" : "Marked handled");
  });
  $("agent-review-handled").onclick = () => resolve("handled");
  if ($("agent-review-retry")) $("agent-review-retry").onclick = () => resolve("retry");
  $("agent-review-back").onclick = () => { agentView.review = null; renderAgent(); };
}

function addAgentMore(box, cursor, tab) {
  if (!cursor) return;
  const button = document.createElement("button");
  button.className = "outline";
  button.textContent = "Load more";
  button.onclick = () => runAgentAction(async view => {
    const result = await api[tab === "drafts" ? "list_agent_drafts" : "list_agent_activity"](view.accountId, cursor);
    view.data[tab].items.push(...result.items);
    view.data[tab].next_cursor = result.next_cursor;
  });
  box.appendChild(button);
}

async function runAgentAction(action, render = true) {
  const view = agentView;
  if (!view || view.busy) return;
  view.busy = true;
  $("agent-modal").querySelectorAll("button,input,textarea,select").forEach(control => { control.disabled = true; });
  try {
    await action(view);
    if (agentView === view && render) renderAgent();
  } catch (error) {
    toast(String(error), true);
    // Preserve unsaved input on errors, including stale draft revisions.
  } finally {
    view.busy = false;
    $("agent-modal").querySelectorAll("button,input,textarea,select").forEach(control => { control.disabled = false; });
    renderWorkerStatus();
  }
}

async function changeAgentSettings(enabled, toggle = false) {
  // Pause remains immediate even if the owner has incomplete unsaved permission edits.
  const pausing = toggle && !enabled;
  if (!pausing && agentModelDirty()) { toast("Save the model connection first", true); return; }
  const profile = agentView.data.profile;
  const job = pausing ? profile.job : $("agent-job").value;
  const mode = pausing ? (profile.mode || "draft_for_review") :
    ($("agent-auto-reply").checked ? "reply_to_allowed" : "draft_for_review");
  const allowed = pausing ? (profile.allowed_recipients || []) : $("agent-allowed").value.split(/[\s,;]+/).filter(Boolean);
  await runAgentAction(async view => {
    view.data.profile = await api.save_agent_settings(view.accountId, enabled, job, mode, allowed);
    view.data.activity = await api.list_agent_activity(view.accountId);
    view.data.model = await api.get_model_state(view.accountId);
    toast(enabled ? "Agent job saved; access enabled" : "Agent access paused");
  });
}

function agentModelDirty() {
  if (!$("agent-model-endpoint")) return false;
  const saved = agentView.data.model || {};
  return $("agent-model-endpoint").value !== (saved.endpoint || "") ||
    $("agent-model-name").value !== (saved.model || "") ||
    $("agent-model-api").value !== (saved.api || "responses") ||
    Boolean($("agent-model-key").value) || $("agent-model-clear").checked;
}

function renderWorkerStatus() {
  if (!agentView || !$("agent-worker-status")) return;
  const connection = agentView.data.model || {};
  const labels = {starting: "Starting", running: "Processing Inbox", idle: "Watching for new mail",
    paused: "Paused", stopping: "Stopping", stopped: "Stopped", interrupted: "Worker interrupted",
    failed: "Worker needs attention", unavailable: "Connection needs attention", needs_owner: "Waiting for your review"};
  $("agent-worker-status").textContent = `Worker: ${labels[connection.status] || connection.status || "Stopped"}` +
    (connection.detail ? ` · ${connection.detail}` : "");
  $("agent-worker-start").disabled = agentView.busy || connection.active || !connection.configured || !agentView.data.profile.enabled;
  $("agent-model-save").disabled = agentView.busy || connection.active;
}

setInterval(async () => {
  const view = agentView;
  if (!view?.data || view.busy || view.tab !== "job" || !$("agent-backdrop").classList.contains("show")) return;
  try {
    const connection = await api.get_model_state(view.accountId);
    if (agentView === view && view.tab === "job" && !view.busy) { view.data.model = connection; renderWorkerStatus(); }
  } catch (_) { /* Keep the form and its unsaved edits when status is temporarily unavailable. */ }
}, 3000);

function renderAgentDraft() {
  const draft = agentView.draft;
  const box = $("agent-content");
  box.innerHTML = `<p class="agent-muted">${escapeHtml(draft.payload.reason)}</p>
    ${draft.conversation?.revision ? `<details><summary>Conversation notes · ${escapeHtml(draft.conversation.state)}</summary><p>${escapeHtml(draft.conversation.note)}</p><p class="agent-muted">Previous context, not instructions or sending permission.</p></details>` : ''}
    <p class="agent-muted">From: ${escapeHtml(state.accounts.find(a => a.id === agentView.accountId)?.identity || agentView.label)}</p>
    <label for="agent-edit-to">To</label><input type="text" id="agent-edit-to">
    <details ${draft.payload.cc || draft.payload.bcc ? "open" : ""}><summary>CC / BCC</summary>
      <label for="agent-edit-cc">CC</label><input type="text" id="agent-edit-cc">
      <label for="agent-edit-bcc">BCC</label><input type="text" id="agent-edit-bcc"></details>
    <label for="agent-edit-subject">Subject</label><input type="text" id="agent-edit-subject">
    <label for="agent-edit-body">Reply</label><textarea id="agent-edit-body"></textarea>
    ${(draft.attachments || []).map(item => `<p class="agent-muted">Attachment: ${escapeHtml(item.name)} · ${fmtSize(item.size)}</p>`).join("")}
    <div class="agent-actions"><button id="agent-send">Approve & send</button><button class="outline" id="agent-save-draft">Save edits</button>
      <button class="outline secondary" id="agent-dismiss">Dismiss</button><button class="outline secondary" id="agent-back">Back</button></div>`;
  ["to", "cc", "bcc", "subject", "body"].forEach(key => { $("agent-edit-" + key).value = draft.payload[key] || ""; });
  $("agent-save-draft").onclick = () => runAgentAction(async view => {
    await saveAgentDraftEdits(view);
    toast("Draft edits saved");
  });
  $("agent-send").onclick = () => runAgentAction(async view => {
    await saveAgentDraftEdits(view);
    const result = await api.send_agent_draft(view.accountId, view.draft.id, view.draft.revision);
    view.draft = null;
    view.data = await api.get_agent_state(view.accountId);
    if (result.status === "uncertain") { view.tab = "activity"; toast(result.warning, true); }
    else toast("Reply sent" + (result.sent_copy_saved ? "" : "; Sent copy could not be saved") +
      (result.refused_recipients?.length ? "; some recipients were rejected" : ""));
  });
  $("agent-dismiss").onclick = () => runAgentAction(async view => {
    await api.dismiss_agent_draft(view.accountId, view.draft.id, view.draft.revision);
    view.draft = null;
    view.data = await api.get_agent_state(view.accountId);
  });
  $("agent-back").onclick = () => { if (agentDiscardAllowed()) { agentView.draft = null; renderAgent(); } };
}

async function saveAgentDraftEdits(view) {
  const [to, cc, bcc, subject, body] = agentDraftFields();
  const attachments = view.draft.attachments;
  const conversation = view.draft.conversation;
  view.draft = await api.edit_agent_draft(view.accountId, view.draft.id, view.draft.revision, to, subject, body, cc, bcc);
  view.draft.attachments = attachments;
  view.draft.conversation = conversation;
  const index = view.data.drafts.items.findIndex(draft => draft.id === view.draft.id);
  if (index !== -1) view.data.drafts.items[index] = view.draft;
}

$("agent-close").addEventListener("click", closeAgent);
document.querySelectorAll("[data-agent-tab]").forEach(button => {
  button.addEventListener("click", async () => {
    if (!agentView || agentView.busy || !agentDiscardAllowed()) return;
    agentView.tab = button.dataset.agentTab;
    agentView.draft = null;
    agentView.review = null;
    if (agentView.tab === "job") renderAgent();
    else await runAgentAction(async view => { view.data = await api.get_agent_state(view.accountId); });
  });
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("agent-backdrop").classList.contains("show")) {
    event.preventDefault();
    closeAgent();
  }
});

$("inbox-btn").addEventListener("click", () => selectFolder("inbox"));
$("needs-you-btn").addEventListener("click", () => openAgent("drafts"));
$("activity-btn").addEventListener("click", () => openAgent("activity"));
