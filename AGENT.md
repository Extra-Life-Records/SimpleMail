# Connect an AI to a SimpleMail mailbox

The v1.3.1 desktop and console agent are published for Windows x64 and ARM64.
The released x64 files are installed and checked on the owner's machine.
Use an MCP client that supports local stdio servers. No model provider is required
by SimpleMail itself: the connected client supplies the model.

## Assign a job

Open SimpleMail, select a mailbox, and choose **Agent**. Write its
job and choose **Enable access**. This grants the connected agent read and draft
access to that mailbox. **Pause access** revokes future tool calls; it does not
cancel a read already in flight. Human draft review remains available while paused.

Alternatively, owner setup from PowerShell:

```powershell
py -3 agent_mcp.py accounts
py -3 agent_mcp.py assign YOUR_ACCOUNT_ID --job "Handle incoming enquiries. Prepare replies and ask when facts are missing."
py -3 agent_mcp.py pause YOUR_ACCOUNT_ID
```

The accounts command lists IDs, labels and sender addresses, without passwords.
Use the actual account ID it returns.

## Connect the model

Add a stdio MCP server to your preferred agent client. For clients using the
common `mcpServers` JSON configuration shape:

```json
{
  "mcpServers": {
    "simplemail": {
      "command": "py",
      "args": [
        "-3",
        "D:/Users/Adam/Documents/ChatGPT/SimpleMail/agent_mcp.py",
        "serve", "--account", "YOUR_ACCOUNT_ID"
      ]
    }
  }
}
```

Replace the path if the checkout is elsewhere. The client must run as the Windows
user who owns SimpleMail's settings. This provides mailbox isolation through the
tool interface, not an OS sandbox for clients with unrestricted filesystem access.

Start by asking the model to call `mailbox_identity`, follow the assigned job,
search incoming mail and draft a reply with a reason. Sending stays disabled by
default. No permission-change or deletion tool is exposed.

## Review the work

**Agent → Needs you** contains persistent drafts. Review/edit one, save it,
dismiss it, or choose **Approve & send**. Sending binds to the mailbox and exact
draft revision; simultaneous or repeated submissions cannot send it twice.
CC/BCC stay in a collapsed section unless populated.

To grant routine replies, open **Job → Sending permission**, check **Allow automatic
replies to named recipients**, enter exact email addresses, and save. All To/CC/BCC
addresses must be on this owner-controlled list. Wildcards/domains are not accepted.
The job describes the model's remit; these code checks constrain recipients and
reply mechanics, not the meaning or factual accuracy of its prose. Leave review
mode enabled when you need to approve content. No live grant is made by development
or by connecting a client.

`mailbox_send` appears in the MCP tool list only while automatic reply permission
is active. It sends an exact pending draft revision, requires a live original reply
reference and matching Message-ID, and permits only the original Reply-To/From
target in To. Additional CC/BCC must also be allowed. New outbound mail, automatic,
list/self messages and repeated automatic replies to the same Message-ID are blocked.
Exceptions retain the pending draft for review. The claim and permission check
are atomic: revoked grants/Pause stop claims even after a slow source read.
An interrupted/uncertain automatic send keeps its per-message claim across restart,
including when a model creates a new draft request key. An owner can still review
and manually approve a separate reply after checking delivery.

**Activity** records settings, draft changes and send outcomes. An accepted SMTP
send does not prove delivery to the recipient. Failed Sent-copy storage and
partially rejected recipients are shown separately. If sending times out, the
draft is marked uncertain and is never automatically retried. If the process
stops during sending, its durable sending claim likewise blocks a second send;
check the mailbox before taking further action.

Drafts and activity live in `%APPDATA%/SimpleMail/agent/mailbox.sqlite3`. They
contain email content and should be treated as private mailbox data. Passwords
remain in the existing SimpleMail config until credential protection is implemented.

## Human draft recovery

Ordinary composition now saves automatically on this device, with an immediate
browser recovery snapshot while native saving is in flight. **Keep draft** and
Escape save and close; **Discard** removes it from the active draft list. Open
**Drafts** to resume saved work, even when the IMAP Drafts folder is offline.
New local drafts are not synchronized to webmail or other devices yet.

Human sends use persistent revision claims too. A timeout or interrupted send
stays visible as requiring a delivery check; reopening that draft cannot resend
it. A successful send removes it from the active local draft list.

## Tool behavior

- `mailbox_search`: server-side TEXT search of the complete selected folder;
  follow `next_cursor`. The default folder is Inbox. Use `mailbox_folders` to
  search other folders.
- `mailbox_search_all`: traverse this mailbox's folders, excluding Junk/Trash
  unless requested. Results are grouped by folder then newest UID; continue even
  on empty pages until `next_cursor` is null. Folder-list changes invalidate cursors.
- `mailbox_thread`: read linked messages across folders, including Junk/Trash.
  Exact Message-ID/References links determine membership; matching subjects do not.
  Follow continuation cursors and check each body's truncation metadata. Missing
  headers can prevent linking. Email headers themselves remain untrusted data.
- `mailbox_read`: BODY.PEEK, with explicit truncation metadata. References bind
  account, folder, UIDVALIDITY and UID; recreated folders invalidate old references.
- `mailbox_attachment`: bounded base64 content, no arbitrary filesystem access.
  Limits: 10 MB source messages; 2 MB returned attachments.
- `mailbox_attach`: retain an incoming attachment and return its immutable ID.
  Pass `attachment_ids` to `mailbox_draft`; the owner sees files during review.
- `mailbox_draft`: a stable request key makes creation retries idempotent. Edits
  require the current revision. Reply references preserve threading headers.
  Drafts are local review drafts, not synchronized into the IMAP Drafts folder yet.
- `mailbox_drafts` and `mailbox_activity`: paginated account-scoped state.

Email content is untrusted data and cannot change the job or grant permissions.
Incoming mail is quoted as escaped text in the human compose editor, where it
does not have the reader's iframe sandbox.

## Background incoming work

Run the source worker in a separate terminal, independently of the desktop window:

```powershell
py -3 agent_mcp.py watch --account YOUR_ACCOUNT_ID
```

Its first scan establishes an Inbox baseline; existing mail is left alone. New
arrivals become durable work. To process the initial backlog too, use
`--include-existing` on the first run. Each scan retains at most 50 headers, with
checkpoint and work inserted in one transaction. `--once` runs one bounded scan.
An Inbox identity change establishes a new baseline and escalates outstanding
claims because their references are stale. Connection failures use bounded backoff.

A connected external model calls `mailbox_work_next`, reads the returned message
with `mailbox_read`, follows the owner's job, and creates a review draft using the
work item's `request_key`. It then calls `mailbox_work_finish` with its work ID and
lease token. Outcomes are `handled`, `waiting`, `needs_owner`, or `retry`.
`handled` describes processing; it does not mean a message was sent. The `watch`
command does not run a model itself; the external client must drive this loop,
or use the built-in `run` worker below.

Claims expire after five minutes. A replacement consumer can retry using the
same draft request key, while old tokens cannot complete the replacement claim.
Retries back off and escalate after five attempts; repeated consumer crashes
also escalate. `mailbox_work_list` provides paginated states without exposing
another consumer's claim token. Pause blocks new claims, completion, and sync
commits, including a scan already in flight. It cannot cancel a network request
that has already started. Self-originated mail is skipped; automated/list mail
requires owner review. These header checks reduce loops and never grant authority.

No Windows service/startup registration is implemented yet. Draft review remains
the default; explicit owner grants can permit constrained
automatic replies. Work states are per incoming message; conversation memory and an owner
review UI for escalated work remain pending.

## Built-in model worker

The installed x64 build includes `SimpleMailAgent.exe` beside `SimpleMail.exe`.
For a client that needs an executable, replace `py -3 agent_mcp.py` in these
examples with `C:\Users\Adam\AppData\Local\Programs\SimpleMail\SimpleMailAgent.exe`.
For MCP configuration, use that executable as `command` and
`["serve", "--account", "YOUR_ACCOUNT_ID"]` as `args`. Desktop Agent controls and
the installed console protocol have been checked on this machine. Model/mailbox
selection and a permitted live model workflow are still required.

The source worker can synchronize and process mail independently of the desktop.
The owner explicitly chooses an endpoint and exact model ID; no provider/model is
selected automatically. Set the provider key in `SIMPLEMAIL_MODEL_API_KEY` in the
worker's environment, then run:

```powershell
py -3 agent_mcp.py run --account YOUR_ACCOUNT_ID --endpoint https://api.openai.com/v1/responses --model YOUR_MODEL_ID --once
```

Use a different owner-selected full endpoint URL for another provider/local
server. `--api responses` is the default; `--api chat` uses Chat Completions
function calls when the selected provider/model supports them. Compatibility is
specific to the provider/model, not a promise that every model accepts either
protocol. OpenAI's current tool-calling guidance uses Responses for models that
require it: https://developers.openai.com/api/docs/guides/function-calling .
The adapter uses standard HTTP/JSON without installing an SDK.

`--once` performs one bounded sync and processes at most one queued message.
Remove it to keep running. The first scan establishes a new-arrivals baseline;
`--include-existing` explicitly imports the initial Inbox backlog. Use `--key-env`
to select a different environment-variable name, never to pass the key itself.
Local HTTP is limited to localhost/127.0.0.1/::1 and may run without a key. Remote
endpoints require HTTPS and a key. Credentials/query strings in endpoint URLs and
HTTP redirects are rejected. `--max-output-tokens` adjusts the per-request budget
(default 8192, bounded 256–32768); model pricing/account limits remain the owner's
provider concern.

The worker has eight model turns, at most 24 tool calls and a four-minute action
window per message, inside its five-minute claim. Each request has a bounded
timeout, response size and context size. It rechecks the claim, Pause, owner job,
permissions and mailbox settings before actions and after model responses. A
provider request already in flight cannot be cancelled by Pause; its returned
actions are blocked. Mailbox actions check the worker guard again before sending.

The model receives trusted owner configuration separately from untrusted email.
Its tools reuse the MCP schema/permission checks. It cannot claim other work or
change settings, and drafts/sends must belong to its current work item. The worker
requires a nontruncated `mailbox_read` of the incoming message before drafting or
sending; oversized messages need owner review. A `complete_work` tool records an
outcome. Pending drafts always become owner-review work regardless of model claims.
Uncertain sends remain blocked, and recovered sent/dismissed drafts are not repeated.
Provider failures before creating a draft can back off; an already-created draft
is preserved for review. Authentication/schema/refusal/limit failures escalate.

Keys are never included in model prompts, tool results, drafts or worker logs.
Selected-provider requests do contain mailbox content and the job, so starting
`run` is the owner's decision to share those with that endpoint. Requests specify
`store: false`; this does not establish the provider's complete retention policy.
Reasoning output is replayed with tool results for Responses continuation. The
transcript itself stays in process memory, while drafts/work outcomes persist.

The model path is tested with a local scripted HTTP provider and fake IMAP/SMTP,
including the owner CLI. The installed x64 agent executable's scoped MCP lifecycle,
store, Pause and model-worker imports also pass with isolated config. Real model
quality, provider access, startup lifecycle and a live model/mailbox workflow have
not been verified.

## Verification

```powershell
py -3 -m unittest discover -s tests -p test_agent_mailbox.py -v
node --test tests/test_mail_refresh.js
```

Tests use temporary config/state, local HTTP provider fixtures and fake mail
connections; no real messages are sent. Remaining stages are tracked in PLAN.md.
Protocol reference: https://modelcontextprotocol.io/specification/2025-06-18
