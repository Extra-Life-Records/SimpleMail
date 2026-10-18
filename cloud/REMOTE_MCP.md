# SimpleMail remote AI access

The existing cloud mailbox service now has a provider-neutral, stateless Streamable HTTP MCP endpoint at `/mcp`. It supports mailbox listing, search, message and attachment reading, shared plain-text drafts, and sending only an exact revision approved in the owner's SimpleMail web screen. It runs without the desktop PC. This does not require Claude-specific client software.

## Rollout

Defaults remain off. Update the existing SAM stack after review, preserving its existing domain, mailbox assignments, secrets, Cognito users and receipt rules. Build with SAM's Python 3.12 ARM64 container so PyJWT's cryptography dependency matches Lambda. Do not package a Windows virtual environment.

- Set `McpEnabled=true` and `McpCallbackUrl` / `McpSecondCallbackUrl` to the exact callbacks shown by the intended AI clients. Two separate public authorization-code clients are provisioned, without client secrets. More clients require explicit registration and their own exact callback and grants.
- Set `McpResourceUrl` to the existing stack's `ApiUrl` output followed by `/mcp`. Passing the existing API address as a parameter prevents a CloudFormation dependency cycle between the HTTP API's integration and the MCP function's environment.
- Use the stack's MCP URL/client ID outputs in a compatible client. OAuth discovery advertises the existing Cognito issuer; the client must support authorization code with PKCE and send `resource=<MCP URL>` so Cognito binds the access token audience. A client that omits resource binding cannot read mail. There is no dynamic client registration or shared bearer key.
- MCP enables Cognito managed login v2 and provisions default branding for the owner and both AI clients. The pool must use Essentials or Plus. Classic hosted UI v1 silently omits the resource audience even when the client supplies `resource`, so a successful callback alone does not prove MCP authentication. After upgrading, reconnect each client to obtain a newly bound token; existing refresh tokens retain their original audience.
- For a local Codex connection, register the complete callback printed by `codex mcp add`, including its server-specific suffix. Cognito supports an exact `http://localhost:<port>/...` callback for local testing; configure the same fixed listener port in Codex. HTTPS remains required for remote callbacks. Each client retains its own grants.
- Sign into the normal owner web inbox and open **AI connections and draft approvals**. Choose the client and explicitly allow each mailbox and action. OAuth consent alone grants no mailbox access. Revoking an assignment or a grant takes effect on subsequent operations.
- Keep `AiSendingEnabled=false` for the first read/draft test. Enable it only after the owner accepts sending. Native SES sending also requires the existing `SendingEnabled` gate and valid regional/domain sending configuration. Existing Fasthosts mailboxes require `smtp_host` and optional `smtp_port` (TLS port 465 by default) alongside their existing IMAP credentials in the configured secret. No credentials are copied into prompts or the settings page.

The remote client cannot call the owner approval API: that API additionally requires the owner's separate OAuth client ID. A draft approval includes sender, recipient lists, text, attachment bytes, source revision and mailbox generation. Edits require another review. The grant must still permit sending when the approved reference is used. Approval expires after 24 hours.

## Acceptance checks

1. Connect two different MCP clients through their ordinary OAuth flow. Verify the same explicit grants work from a cloud client with the PC shut down. Verify the other client and another account cannot see ungranted mailboxes.
2. Search an existing mailbox, paginate, read an attachment, and confirm reads do not mark messages as read. Save a draft; find it in the ordinary SimpleMail Drafts folder.
3. Review an exact message in the owner screen. Changing recipients, body or attachments must prevent its earlier approval from sending. Revocation must also block it.
4. Only with an explicitly approved recipient/message, send the approved reference once and read the outcome. `accepted` means provider acceptance, not recipient delivery. Native SES delivery events reconcile the receipt. External SMTP has no delivery receipt here. A timeout remains `uncertain` and cannot cause an automatic second send; inspect provider/Sent evidence before taking another action.
5. Disconnect the client and verify subsequent tools fail. Keep MFA, login confirmation, CAPTCHA and social signup verification in the owner's normal sign-in flow.

The server does not administer Fasthosts or create social accounts. A new `DizzyTeapot@ExtraLifeRecords.com` mailbox still needs provisioning and a controlled inbound receipt, then adding its credentials to the existing cloud external-mailbox secret. An MX result is not mailbox acceptance. These are rollout tasks, not outcomes implied by passing tests.

Validation: install `cloud/requirements-dev.txt`, run `cfn-lint cloud/template.yaml` and `python -m unittest discover -s cloud/tests -v`. Tests cover real signed JWT audience/client/expiry checks, cross-client and mailbox isolation, changed drafts, owner-only approval, partial SMTP acceptance and uncertain-send non-repetition. Deployment and live AI-client interoperability require the acceptance checks above.
