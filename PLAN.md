# SimpleMail: an inbox an AI can own

Keep the human interface small: Inbox, Needs you, Activity, and one instruction
box. Ordinary reading and composing remain available. Put complexity in the
mail service, rather than adding Outlook-style settings and toolbars.

## Priority 1 — a reliable model-independent mailbox interface

- An MCP stdio server for external agents, bound to one explicitly assigned
  existing mailbox. Never expose account credentials through tools.
- Complete, paginated listing and server-side search; read without marking mail
  read; stable message references that include IMAP UIDVALIDITY.
- Readable bodies, threading headers and bounded attachment access. Email content
  is untrusted data, never an instruction or a source of permissions.
- Persistent editable local drafts and an action journal shared by the desktop
  app and agent. Retried draft requests do not produce duplicate drafts.
- Account-bound job instructions, enabled/paused state. Human control is outside
  the model tool surface. Start in draft-for-review mode.
- Verification: temporary mailboxes/config fixtures, fake IMAP and subprocess
  MCP lifecycle tests. No real email sent during development tests.

## Priority 2 — finish correspondence and human oversight

- Needs you: review, edit, send or dismiss agent drafts; approvals apply to the
  exact draft revision. Activity: outcome and reason for each action.
- Correct Reply-To / Reply All, CC/BCC, threading and outgoing attachments.
- Visible sending state; never automatically retry an uncertain SMTP outcome.
  Sent-copy failures are visible separately from accepted outgoing mail.
- Draft autosave, resume and recovery for human composition.
- Verification: rendered frontend and isolated send/reply/attachment tests.

## Priority 3 — autonomous operation within an assigned job

- Owner grants a clear remit and allowed recipients; permit routine sends inside
  it, escalate exceptions. A model cannot expand its own authority.
- Background worker independent of the desktop window: incremental inbox sync,
  durable work queue, model adapter, retry/backoff and loop prevention.
- Persistent per-conversation state: waiting, handled, needs owner; job memory
  remains separate from instructions received in email.
- One Pause control stops future agent actions. Recoverable filing and Undo.
- Verification: restart recovery, duplicate events, simultaneous workers,
  pause/permission changes and malicious incoming-email fixtures.

## Priority 4 — polish and delivery

- Bring Inbox, Needs you and Activity into the main navigation; keep folders and
  technical connection details behind a secondary control.
- Protect stored credentials; block remote images until explicitly allowed.
- Package desktop and agent entry points for Windows x64/ARM64; run existing
  release gates and verify an installed build before claiming delivery.
- Connect a user-selected model/mailbox and verify a real permitted workflow.
  Provider credentials or real recipient choices are required for that live gate.

## Deferred

Calendars, elaborate contacts management, themes, extensive rule builders and
productivity options such as snooze/send-later. Add only when a concrete job
requires them.

## Evidence and progress

Priority 1 is implemented in source: scoped MCP tools, complete-folder
pagination/search, UIDVALIDITY references, bounded reads/attachments, durable
drafts, revision checks, idempotent creation, owner-written job and Pause.

Priority 2 has the initial Agent panel with Job, Needs you and Activity; owner
editing/dismissal/approval, SMTP CC/BCC and reply headers, durable sending claims,
uncertain-send handling, and visible Sent-copy/recipient failures. Incoming HTML
is no longer inserted unsanitized into the compose document when quoting.

Human compose autosave/recovery is now implemented in source. Typing writes an
immediate browser recovery snapshot and a serialized durable local save. Drafts
are resumable from the Drafts folder after restarting, remain available when its
IMAP folder is offline, and can be kept or discarded. Sending claims the exact
revision before SMTP; uncertain or interrupted sends cannot be retried from the
same draft. These new human drafts are local to this device, not IMAP-synced.

Outgoing attachments, forwarding files, CC/BCC, Reply-To and Reply All are now
implemented in source. File bytes are retained privately in the shared local
database, so resumed drafts keep their files. Human compose keeps CC/BCC collapsed
until used. Replies preserve Message-ID/References, deduplicate recipients and
exclude mailbox/login identities. The model can retain an incoming attachment
with mailbox_attach and include its ID in a draft for visible owner review.

Conversation reading and cross-folder search now exist in the agent tool
interface. Search traverses all folders with bounded pages, scoped cursors and
explicit ordering; ordinary global search excludes Junk/Trash by default.
Conversation reading links exact Message-ID/References tokens across folders,
including Junk/Trash, and labels shortened bodies. Missing/malformed threading
headers can prevent linking; identical subjects alone are never sufficient.

The human reader has a collapsed Conversation history section with continuation
and retry controls. It works independently of agent enablement. The desktop's
ordinary search input still filters the loaded folder; integrating the complete
search tools into that input remains pending. Priority 3 is partly implemented;
Priority 4 remains pending.

Verification so far: isolated agent/mailbox tests, existing mail-poller and
refresh regressions, updater gates, plus Chromium rendering with fixture mail
at 1240×850 and the app's minimum 980×620 size. A real MCP subprocess handshake
uses temporary config; no live mailbox was assigned or outgoing email sent.
No installed build or live model workflow has been verified yet.

Additional verification: eight human-draft backend tests, four autosave/recovery
frontend tests, and two local-Drafts regression tests. Chromium fixture QA
exercised compose → autosave → reload → Drafts → resume → send → reload, confirming
the saved body returned and the sent draft no longer appeared. No real mail sent.

Correspondence verification: eight MIME/reply/attachment tests and an agent
attachment round-trip through review/send. Chromium fixture QA adds a PDF, reloads
and resumes the draft with its attachment, sends once, and prepares Reply All with
the expected recipients. Attachment bounds: 10 MB/file, 20 MB total, 20 files.

Conversation verification: seven additional backend tests cover full-folder
traversal, bounded empty-page progress, cursor isolation, changed folder lists,
exact thread-token matching, missing thread headers and escaped IMAP criteria.
Three frontend tests cover escaped rendering, stale responses and retry. Chromium
fixture QA loads Inbox history then Sent continuation without executing email HTML.

Priority 3 now includes a standalone source Inbox watcher and durable work queue.
It commits scoped UIDVALIDITY/UID checkpoints with new work, uses bounded header
fetches, starts from new arrivals by default, and supports explicit initial backlog
import. MCP consumers claim work with five-minute leases and stable draft request
keys, then record handled/waiting/needs-owner/retry outcomes. Restart/concurrent
consumers, pause during sync, stale claims, duplicate scans, Inbox recreation,
retry backoff/limits, self-originated and automatic/list mail are covered by 14
isolated tests. The full Python suite passes 70 tests plus 40 existing checks;
21 frontend tests also pass. No live Inbox was scanned for this change.

The standalone `watch` worker does not run a model; an external MCP client must
drive consumption, or use the built-in worker described below. Service/startup registration,
conversation state/memory, filing Undo and owner review of
escalated work are still pending. No installed/live workflow is claimed.

Owner-controlled automatic reply permission is now implemented in source, behind
a collapsed Sending permission section. Review remains the migration/default mode.
An owner can grant exact allowed recipients; no model tool can change the grant.
Automatic sends require the current draft revision, a valid incoming reply reference,
the original reply target/Message-ID and allowed To/CC/BCC. Automatic/list/self mail,
new outbound mail and repeated automatic replies are blocked. Permission checks
and per-message send claims are atomic; Pause/revocation during a network read and
uncertain/interrupted sends fail closed. The written job remains model instructions;
recipient/thread guards do not verify the factual accuracy or meaning of its reply.

Eleven isolated tests cover legacy/concurrent migration, grants/defaults, scope,
recipient checks, malicious mail, revocation, duplicates and uncertain sends.
Chromium fixture QA saves a grant, reflects it, pauses with invalid unsaved edits,
and revokes automatic replies, plus the prior compose/history/review flows at
1240×850 and 980×620 with no console errors. No actual mailbox permission was changed
and SMTP is mocked throughout these new tests.

Built-in source model execution is now implemented: `agent_mcp.py run` accepts an
owner-selected endpoint/model and environment-key name, syncs the Inbox and drives
a bounded tool loop independently of the desktop. It supports Responses and Chat
Completions explicitly; no provider/model is selected automatically. MCP schema
and permission checks are shared. Work claims, owner job/grant/account settings
and Pause are checked around model calls and before actions. The worker uses the
stable work draft key, requires reading the incoming body without truncation before
drafting/sending, and preserves review drafts/uncertain sends across failures.

The HTTP adapter rejects redirects and URL credentials, requires HTTPS for remote
endpoints, bounds requests/responses/time and output budget, and keeps keys out of
prompts/state/logs. Responses continuation retains reasoning items. No mailbox
content was shared with a real provider during development. Local HTTP fixtures
verify request formats and full sync → tool read → draft → outcome, including the
owner CLI and an allowed send with mocked SMTP. Further cases cover limits, unsafe
tool calls, scope, truncation, expired claims, changed settings/job/Pause, redirects,
provider failures and recovery. These tests prove transport/control behavior, not
a real model's adherence to the job or reply quality.

Verification: 22 new model-worker tests pass, bringing the Python suite to 103
tests plus 40 existing checks; the 21 frontend regressions also pass. CLI help and
diff checks pass. The end-to-end owner CLI test uses temporary config/state,
a local HTTP provider and fake IMAP, without changing live account assignments.

Remaining: owner model setup/status in the app, managed worker startup/lifecycle,
conversation state/memory, filing Undo, owner review of escalated work, complete
human search, simplified main navigation, credential/image protection, packaging,
installed verification and the owner-selected live model/mailbox gate.

## Installed delivery — 30 September 2026

Built v1.3.0 desktop and console agent executables on this x64 machine and installed
them in `C:\Users\Adam\AppData\Local\Programs\SimpleMail`. The existing desktop
shortcut targets this installation. The previous desktop v1.2.0 executable is
retained under its `backups` directory. The desktop install's SHA256 matches the
new build, and its native window and backend both report v1.3.0.

Actual installed WebView2 interaction (not a mocked browser API) opens Agent → Job,
Needs you and Activity successfully with the existing accounts. Job and permissions
were unchanged by verification, no messages were sent, and no runtime errors were
observed. The temporary local debugging connection was removed after checking.
The installed console executable passes isolated-config account/assignment, MCP
handshake/tool list/identity/drafts, Pause and model-worker import checks without
live IMAP/SMTP/provider requests. The source suite remains green: 103 Python tests,
40 existing checks and 21 frontend tests.

The current changes are available in the installed x64 app. This is a local
delivery, not a GitHub publication. ARM64 build/installation and a live selected
model workflow remain unverified; the earlier remaining feature list still applies.

Final normal launch is responsive with native title `SimpleMail v1.3.0`, served
Agent frontend, and no local debugging listener. Installed desktop and agent hashes
both match their newly built artifacts. Normal restart needed a second launch
after the verification window exited; it is now running normally.

## Published delivery — v1.3.1

PRs #15 and #16 are merged. The v1.3.1 release is public with desktop and console
agent downloads for x64 and ARM64. Both native runners passed regression, PE
architecture and packaged MCP checks before publication. All four downloaded
files match GitHub's published SHA256 digests. The desktop updater selects the
desktop executable for each architecture, even when agent assets appear first.
The post-publication updater gate passed all 46 checks, including a real file lock.

The released x64 desktop and console agent replaced the installed copies. Native
WebView2 checks verified Job, Needs you and Activity without changing owner job
settings. The installed console passed the isolated protocol smoke check. The
normal desktop is responsive with title `SimpleMail v1.3.1`; the temporary debug
listener is closed. No real mail was sent and no live model was selected.

Installed desktop SHA256: `EC1312E3883C8AF9B16683CC2D0066451B6D95B71DC708563953A84E90F48924`.
Installed agent SHA256: `474DE207A399FD395F88EAE75D2CD707BF16DFBA8A2D8A8A176C5E2715FE2732`.
ARM64 packaging is verified on a native runner; an ARM64 owner-device installation
is not claimed. In-app model setup and worker controls, conversation memory,
recoverable filing, simpler main navigation, credential and image protection,
and a user-selected live model workflow remain open in the original plan.

## In-app model connection and managed worker

Implemented owner-only saved endpoint/model/API setup with a password field for
the provider key. Windows DPAPI protects the key under the current Windows user;
frontend state and worker command lines contain no key or encrypted secret.
Retained keys cannot follow a changed provider endpoint. Connection edits require
a stopped worker. Start launches the packaged console independently of the desktop;
Pause revokes mailbox permissions and requests worker shutdown. SQLite launch
reservations, heartbeats and generation tokens prevent duplicate and stale workers.
The model loop checks owner cancellation before provider and mailbox actions.
Status polling preserves unsaved setup/job text. Existing Inbox mail is excluded
from the initial baseline. No model or live mailbox has been selected for this gate.

Sixteen focused checks cover DPAPI, account/key isolation, concurrent launch,
stale-worker revocation, owner stop and a real isolated source subprocess. Rendered
setup checks cover secret clearing, preserving job edits, Start/Pause and disabling
setup edits while running. All 119 Python tests and 21 frontend tests passed.

v1.4.0 is merged and public. Both native packaging jobs passed the managed-worker
start/pause check, and all four downloads match GitHub's published digests and PE
architectures. Released x64 files are installed; the console passed the extended
smoke check, and actual installed WebView2 verified the saved model fields, blank
key field, paused Start control and unchanged job permissions without runtime errors.

The owner authorized one existing mailbox for a live check. An already-cached
local Ollama qwen3.5:4b model used the Responses adapter to read one existing
newsletter. Its read left the Seen flag unchanged, and no send was started. The
model produced prose rather than complete_work, so the durable work became
needs_owner. This proves real model/mailbox wiring and safe escalation, not reliable
autonomous triage. The job is now paused in draft-for-review mode with no recipient
grants, and the local connection is saved. Improve model completion behaviour and
surface nondraft escalations next; the rest of the original plan remains open.

Installed desktop SHA256: `09735DF956CAEE37D7C6FDAF93F2CD1D4ECE618F56431C3F1C0DBFBB53AC7AE2`.
Installed agent SHA256: `3447446452D55E30F134CA9DAE5B6FEBD1921228D0E95FA1DEDD8CD632B9F068`.

## Triage completion and owner review

After prose-only output, the worker permits one completion-only recovery phase,
within the existing turn/action/time budgets. Mailbox actions are unavailable in
that phase; repeated prose escalates. Handled/waiting outcomes require reading the
incoming body first. A live repeat of the same owner-authorized newsletter now
reads and records handled, with its Seen flag unchanged and no send started.
The mailbox is paused again. This is a bounded newsletter test, not evidence of
general autonomous quality across all correspondence.

Needs you now includes nondraft exceptions and missing facts. Owners can read the
message, mark it handled, or request another attempt without enabling access.
Review decisions require the displayed item's exact updated timestamp. Existing
drafts/delivery outcomes block retry, and pending drafts must be sent or dismissed
before resolution. Reviews stay accessible while paused; missing/moved messages
remain resolvable. Activity displays readable work outcomes and reasons.
All 129 Python tests and 21 frontend tests passed. PR #20 is merged; native x64
and ARM64 package jobs passed and v1.4.1 is published with all four downloads.
Downloaded files match GitHub's published SHA256 digests and native PE machines.
The released x64 desktop and console are installed. The console passed its
protocol and managed start/pause smoke check. Actual installed WebView2 verified
paused owner reads, account isolation, readable Activity and unchanged job settings,
with no runtime errors. The earlier live repair proof remains scoped to one
newsletter. The original plan's navigation, conversation memory, recoverable filing,
and mail-credential/image protection remain unfinished.

### Main navigation (v1.5.0 source)

Inbox, Needs you and Activity now open directly from the sidebar. Needs you and
Activity use the main workspace rather than a modal. Agent setup is secondary,
and other mail folders are collapsed under Folders. Account and view changes
respect unsaved draft/job/connection edits and active actions; late responses
cannot reopen a workspace that was left. Background folder discovery cannot
close an agent workspace opened while the mailbox is connecting.

Verified in Chromium with fixture messages: owner exception review, retry while
paused, uncertain-send retry prevention, main view switching, and preserving
unsaved job edits when discard is declined. Three routing regression tests cover
late responses, competing requests, busy actions and unsaved drafts. Publication
and installed verification of this build remain pending at this checkpoint.

### Navigation delivery and message image privacy

v1.5.0 is merged, published for both architectures and installed on the owner's
x64 machine. Installed WebView2 checks prove direct Inbox/Needs you/Activity
navigation, paused owner review, account isolation, retained model setup and
unchanged permissions. Normal relaunch removed the temporary debugging listener.

The v1.5.1 source blocks remote message resources by default, uses inert template
parsing, a restrictive message Content Security Policy, and strips CSS resource
channels. Load images appears only for messages containing remote pictures;
permission lasts for that selected message. Embedded raster images remain usable.
Chromium tests observe zero default remote requests, one permitted picture,
blocked CSS/media/SVG channels, safe links, permission reset and stale-control
protection. The browser test now runs in CI. Installed delivery remains pending.

### Credential protection (v1.5.2 source)

v1.5.1 image privacy is published and installed, with WebView2 network checks
proving zero default requests and only explicit picture requests. The mailbox
remains paused and no message was sent.

Mailbox and SMTP passwords are now sealed with current-user Windows DPAPI on
save, including automatic migration of existing and legacy account configs.
Writes seal first and replace atomically; protection/write failure preserves the
original file and restores backend settings. Desktop and agent unlock secrets
only in their backend processes. Public settings return blank password fields
and saved/error flags. Blank inputs retain saved credentials; changing account
or server requires replacement credentials, and SMTP fallback is explicit.
Unavailable ciphertext survives ordinary settings saves and can be replaced.

Ten credential tests and rendered settings checks cover these cases. Release
and installed migration verification remain pending at this source checkpoint.

### Conversation state (v1.6.0 source)

v1.5.2 credential protection is released and installed. Three real stored secrets
were verified encrypted, unchanged after migration, and unlocked by the agent
config reader. Native saved-credential IMAP/SMTP login checks passed without
sending mail. Owner settings and the agent's paused state remained intact.

Conversation outcomes/notes now persist in the shared SQLite database and carry
forward to later replies through Message-ID/References/In-Reply-To links. Subject
matches never link conversations. Missing headers keep messages separate;
conflicting known links escalate for owner review. Account isolation, monotonic
work ordering and idempotent completion protect newer notes from older workers.
Existing work is backfilled once. Draft sending/dismissal, uncertain delivery,
owner resolution, expired claims and changed Inbox identities update state.

The model receives previous notes as explicitly untrusted task context, separate
from owner instructions/grants. External MCP consumers can read conversation
state and record new notes through their claimed work's completion. Owner review
shows escaped notes in a collapsed detail. Ten new state tests plus a model-wire
continuation test and rendered owner-review checks cover the feature. Release and
installed checks remain pending at this source checkpoint.
