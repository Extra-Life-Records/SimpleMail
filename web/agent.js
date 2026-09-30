/* Owner controls are deliberately separate from the external model tools. */
"use strict";

let agentView = null;
let agentRequest = 0;

async function openAgent() {
  const acct = activeAccount();
  if (!acct) { toast("Add a mailbox first", true); return; }
  const request = ++agentRequest;
  agentView = { accountId: acct.id, label: acct.label, tab: "job", data: null, draft: null, busy: false };
  $("agent-title").textContent = `Agent · ${acct.label}`;
  $("agent-content").textContent = "Loading…";
  $("agent-status").textContent = "";
  $("agent-backdrop").classList.add("show");
  try {
    const data = await api.get_agent_state(acct.id);
    if (request !== agentRequest) return;
    agentView.data = data;
    renderAgent();
  } catch (error) {
    if (request === agentRequest) $("agent-content").textContent = "Could not load agent state: " + error;
  }
}

function closeAgent() {
  if (agentView?.busy) return;
  if (!agentDiscardAllowed()) return;
  ++agentRequest;
  $("agent-backdrop").classList.remove("show");
  $("agent-btn").focus();
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
  }
  return true;
}

function renderAgent() {
  const view = agentView;
  if (!view?.data) return;
  const { profile, drafts } = view.data;
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
  if (view.tab === "job") {
    box.innerHTML = `<p>Give this mailbox a job. Connect your preferred AI using SimpleMail’s mailbox tools; it can read mail and prepare replies here.</p>
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
      <button class="outline" id="agent-toggle">${profile.enabled ? "Pause access" : "Enable access"}</button></div>`;
    $("agent-job").value = profile.job;
    $("agent-auto-reply").checked = profile.mode === "reply_to_allowed";
    $("agent-allowed").value = (profile.allowed_recipients || []).join("\n");
    $("agent-save-job").onclick = () => changeAgentSettings(profile.enabled);
    $("agent-toggle").onclick = () => changeAgentSettings(!profile.enabled, true);
  } else if (view.tab === "drafts") {
    box.innerHTML = drafts.items.length ? drafts.items.map((draft, index) =>
      `<div class="agent-card"><h3>${escapeHtml(draft.payload.subject || "(no subject)")}</h3>
       <p class="agent-muted">To: ${escapeHtml(draft.payload.to || "No recipient yet")}</p>
       <p>${escapeHtml(draft.payload.reason)}</p><button class="outline" data-review="${index}">Review draft</button></div>`
    ).join("") : `<p>No drafts waiting for you.</p><p class="agent-muted">Replies prepared by a connected agent appear here.</p>`;
    box.querySelectorAll("[data-review]").forEach(button => {
      button.onclick = () => { view.draft = drafts.items[Number(button.dataset.review)]; renderAgent(); };
    });
    addAgentMore(box, drafts.next_cursor, "drafts");
  } else {
    const names = { settings: "Job or access changed", draft_created: "Draft prepared", draft_updated: "Draft edited",
                    draft_dismissed: "Draft dismissed", send_started: "Sending reply", send_sent: "Reply accepted by mail server",
                    send_uncertain: "Delivery needs checking" };
    box.innerHTML = view.data.activity.items.length ? view.data.activity.items.map(item => {
      let detail = item.detail.reason || item.detail.subject || "";
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

async function runAgentAction(action) {
  const view = agentView;
  if (!view || view.busy) return;
  view.busy = true;
  $("agent-modal").querySelectorAll("button,input,textarea").forEach(control => { control.disabled = true; });
  try {
    await action(view);
    if (agentView === view) renderAgent();
  } catch (error) {
    toast(String(error), true);
    // Preserve unsaved input on errors, including stale draft revisions.
  } finally {
    view.busy = false;
    $("agent-modal").querySelectorAll("button,input,textarea").forEach(control => { control.disabled = false; });
  }
}

async function changeAgentSettings(enabled, toggle = false) {
  // Pause remains immediate even if the owner has incomplete unsaved permission edits.
  const pausing = toggle && !enabled;
  const profile = agentView.data.profile;
  const job = pausing ? profile.job : $("agent-job").value;
  const mode = pausing ? (profile.mode || "draft_for_review") :
    ($("agent-auto-reply").checked ? "reply_to_allowed" : "draft_for_review");
  const allowed = pausing ? (profile.allowed_recipients || []) : $("agent-allowed").value.split(/[\s,;]+/).filter(Boolean);
  await runAgentAction(async view => {
    view.data.profile = await api.save_agent_settings(view.accountId, enabled, job, mode, allowed);
    view.data.activity = await api.list_agent_activity(view.accountId);
    toast(enabled ? "Agent job saved; access enabled" : "Agent access paused");
  });
}

function renderAgentDraft() {
  const draft = agentView.draft;
  const box = $("agent-content");
  box.innerHTML = `<p class="agent-muted">${escapeHtml(draft.payload.reason)}</p>
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
  view.draft = await api.edit_agent_draft(view.accountId, view.draft.id, view.draft.revision, to, subject, body, cc, bcc);
  view.draft.attachments = attachments;
  const index = view.data.drafts.items.findIndex(draft => draft.id === view.draft.id);
  if (index !== -1) view.data.drafts.items[index] = view.draft;
}

$("agent-close").addEventListener("click", closeAgent);
document.querySelectorAll("[data-agent-tab]").forEach(button => {
  button.addEventListener("click", async () => {
    if (!agentView || agentView.busy || !agentDiscardAllowed()) return;
    agentView.tab = button.dataset.agentTab;
    agentView.draft = null;
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
