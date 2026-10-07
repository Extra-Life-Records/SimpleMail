# Extra Life Records reserved mailbox pilot

**Receiving pilot deployed; not ready for employee use.** This branch implements
the AWS backend, web inbox and SimpleMail provider window. Five reserved inboxes
received real Gmail tests with attachments. Public forwarding and owner web login
work. MFA enrollment confirmation, outbound mail and installed SimpleMail acceptance
remain pending. No employee or
Claude accounts have been created.

## Company domain switch, 7 October 2026

The owner requested employee addresses at **extraliferecords.com**. Fasthosts
receives that domain's mail; its existing apex MX and hello, pippy and playloudr
mailboxes must remain unchanged. Five new exact Fasthosts forwarders connect
one through five at extraliferecords.com to the corresponding existing private
AWS inboxes at inbox.playloudr.com. The latter is an internal transport domain;
employees use the Extra Life Records addresses. No extra mailbox purchase or
Claude domain addition is required for this route.

Keep the existing AWS stack, receiving domain, user pool, owner login and stored
mail. Change only the public Domain parameter to extraliferecords.com and update
the inbox branding. Keep SendingEnabled=false until the new sending identity,
DKIM, return path and regional approval have been verified. The earlier Playloudr
DNS/routing instructions below describe the original pilot, not instructions to
replace Extra Life Records MX records. Its Cloudflare routing script is not used
for this migration. All five new public addresses received real Gmail test messages, verified in
DynamoDB and the owner web inbox. The first test reached one; two through five
returned explicit Fasthosts provisioning failures and a later labelled retest
reached all four. CloudFormation updates completed, with public Domain set to
extraliferecords.com and Extra Life Records branding verified live. Evidence is
saved under ignored `.deployment/extralife-*`. Outbound sending remains disabled;
no employee login or Claude invitation has been created.

## Original pilot deployment evidence, 7 October 2026

- Ireland SES: `ProductionAccessEnabled=false`, `SendingEnabled=true`. It remains
  in the sandbox; London newsletter approval does not apply to Ireland.
- The saved newsletter credential was denied receipt-rule and CloudFormation reads.
  Do not broaden or repurpose that production key.
- AWS Console and Cloudflare owner sessions are available. Use an appropriate temporary
  AWS operator profile and scoped Cloudflare access for deployment.
- A real employee is required for the assignment/Claude pilot.
- Stack `playloudr-reserved-mail` deployed in Ireland, including the web callback.
  Both SES identities verified. Nine additive Cloudflare DNS records published;
  apex MX and existing hello/rewards routes preserved.
- Five subdomain Gmail deliveries reached separate mailbox generations. Each had
  a 61-byte `pilot-check.txt` attachment and passing spam, virus, SPF, DKIM and DMARC
  verdicts. Evidence is saved under ignored `.deployment/`.
- Five Cloudflare destinations verified through the owner's web inbox. All five
  exact public forwarders are active, with hello/rewards and catch-all preserved.
  Gmail public tests reached all five inboxes. Four/five initially returned explicit
  550 address-not-found responses immediately after creation; later labelled retests
  delivered successfully. Existing hello delivery still needs mailbox confirmation.
- Owner password setup and web inbox reading verified. Cognito requires software
  MFA at pool level, but admin_get_user did not yet report an enrolled MFA method;
  confirm enrollment with the owner before treating authentication acceptance complete.
- Installed SimpleMailAgent.exe lists Extra Life Records and Playloudr accounts,
  but both reject MCP startup as paused/unassigned. Owner must enable the scoped
  reading job in Agent setup; do not bypass this permission gate.
- Initial deployment rolled back because reserving Lambda concurrency exceeded the
  account's small regional quota. The corrected template uses shared capacity by
  default. The old empty table/user pool and bucket with AWS's setup notification
  were retained; reconcile these pilot leftovers before final rollout. The sending
  identity was retained and is currently managed outside the replacement stack.
- Renamed `operator.py` to `mailbox_admin.py` to avoid shadowing Python's standard
  library when running from `cloud/`. A subprocess regression verifies CLI startup.

## Implemented architecture

SAM defines SES receiving -> private versioned S3 -> SNS -> Lambda -> DynamoDB,
Cognito with required TOTP MFA, JWT-authorized HTTP API, web UI, delivery/bounce
events, bounded logs, failure queue and $5/$10/$20 account budget notifications.
Budgets notify; they do not cap spending. Outbound mail defaults to disabled. API reserved concurrency defaults to zero (shared account capacity) so small regional quotas work; API throttling remains enabled.

Addresses `one` through `five` at extraliferecords.com have Reserved, Assigned and Disabled
states. Reserved has no employee credentials. Only the explicit owner can inspect
reservations. Every API read, search, download and send checks the current assignment.
New assignments have separate history namespaces, including for delayed retries.

The inbox supports reading, replies, Sent, Drafts, Junk, recoverable Trash,
attachments and paginated full text-body/header search. Binary attachments are
not searched. The API preserves original HTML for SimpleMail’s sandboxed, sanitized reader.
Remote images remain opt-in and scripts are blocked. Plain-text fallback retains
safe link destinations, including action buttons in HTML-only invitations.
The chronological index is eventually consistent, so refresh may be needed after writes.

Limits: 30 MB raw incoming MIME, 500,000 displayed text characters, 3 MB total
outbound attachments or individual attachment download. Virus scan failures and
unknown verdicts are quarantined without body/download access. Spam goes to Junk.
SPF/DKIM/DMARC verdicts are retained. No permanent-delete endpoint exists.

Send claims are durable and uncertain outcomes never automatically retry.
The UI persists the request reference across reloads and saves the draft first.
SES acceptance, delivery, bounce and complaint are separate recorded states.

SimpleMail lists the five employee accounts alongside IMAP accounts in its main
sidebar and uses the shared reader and composer through a Python provider adapter. Sign-in uses the system browser, authorization code and PKCE.
Windows DPAPI protects the refresh session. Clients receive no AWS keys. The web
UI stores refresh sessions in sessionStorage and revokes them on sign-out.
Existing IMAP accounts and agent permissions are unchanged.

## Checks

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r cloud/requirements-dev.txt
.\.venv\Scripts\cfn-lint cloud/template.yaml
.\.venv\Scripts\python -m unittest discover -s cloud/tests -v
.\.venv\Scripts\python -m unittest discover -s tests -p 'test_*.py'
```

Run the existing JavaScript and Chromium checks plus `tests/cloud_mail_browser.cjs`.
Cloud tests simulate AWS. They do not prove live IAM, email delivery, MFA, recovery,
or backup restoration. Keep real evidence under ignored `.deployment/` without secrets.

## Receiving deployment sequence

1. Install AWS SAM CLI from AWS and use a temporary operator profile. Run
   `python cloud/mailbox_admin.py --profile <operator-profile> preflight` and preserve
   existing receipt rules. Only one receipt ruleset can be active per region.
2. Run `sam build --template-file cloud/template.yaml` and
   `sam deploy --guided --region eu-west-1 --profile <operator-profile>`. Use stack
   `playloudr-reserved-mail`, unique AuthDomain, budget email hello@extraliferecords.com and
   `SendingEnabled=false`. Deployment does not activate rules or change DNS.
3. Redeploy with CallbackUrl equal to the WebInbox output. The desktop callback
   `http://localhost:8765/callback` remains registered alongside it.
4. Create the owner's Cognito user using an existing reachable company email.
   Owner completes invitation, password and TOTP. Run
   `python cloud/mailbox_admin.py --profile <operator-profile> reserve --owner-sub <verified-owner-sub>`.
5. Add SES-issued verification/DKIM records for inbox.playloudr.com and MX
   `10 inbound-smtp.eu-west-1.amazonaws.com` on that subdomain only. Keep apex
   Cloudflare MX and the existing hello@playloudr.com route intact.
6. Verify receiving identity/DNS and receipt permissions. If no ruleset exists,
   activate the stack's RuleSet output. Otherwise preserve the existing ruleset
   and add only this receiving rule to it after reviewing rule ordering.
7. Send uniquely labelled tests to all five subdomain destinations. Verify each
   in its own private inbox, record timestamps/message IDs, inspect the failure
   queue, then proceed to Cloudflare destination verification.

## Cloudflare routing

Use a scoped credential-manager token in `CLOUDFLARE_API_TOKEN`, never in chat or
source. It needs zone/DNS read and routing rules/destination-address read/write.
The script accepts only playloudr.com and never edits existing rules or MX.

```powershell
python cloud/cloudflare_routes.py snapshot --zone <zone-id> --snapshot .deployment/routing-before.json
python cloud/cloudflare_routes.py destinations --zone <zone-id> --snapshot .deployment/routing-before.json
# Read and verify all five destination emails through the owner's private inbox.
python cloud/cloudflare_routes.py enable --zone <zone-id> --snapshot .deployment/routing-before.json
```

Activation refuses unverified destinations, conflicting addresses, changed baseline
rules, MX or catch-all. Each public address forwards to its same local part at
inbox.playloudr.com. Save a post-change snapshot and retest hello@playloudr.com.

## Sending

Inspect the existing playloudr.com SES identity in Ireland before creating/modifying
it. Preserve other senders. If absent, set `CreateSendingIdentity=true` to have SAM
create the identity with DKIM and dedicated MAIL FROM. If already present, preserve
or explicitly import it before managing it through SAM. Configure easy DKIM and custom MAIL FROM
return-mail.playloudr.com with reject-on-MX-failure; publish SES-issued CNAMEs and
return-path MX/SPF. Preserve apex SPF/DMARC. Verify regional production approval
and actual Gmail/Outlook Authentication-Results before `SendingEnabled=true`.
Any production-access request must truthfully describe employee correspondence.

## Assignment

```powershell
python cloud/mailbox_admin.py --profile <operator-profile> assign one --login-email <employee-reachable-email>
python cloud/mailbox_admin.py --profile <operator-profile> disable one
# Following offboarding review; old history is retained separately:
python cloud/mailbox_admin.py --profile <operator-profile> reserve-again one
python cloud/mailbox_admin.py --profile <operator-profile> connection
```

Cognito delivers the employee invitation; the operator never chooses or prints
their password. Use an independently reachable recovery address initially.
Assignment creates a fresh namespace, so reserved test mail and former employee
history never transfer. Disable/revoke the former employee's Cognito account and
refresh sessions during offboarding as well. Conditional updates reject concurrent
assignments. The connection command prints only public client configuration.

For the first real hire, use the existing extraliferecords.com Claude domain and invite
them as Member/Premium. They handle verification and terms. Owner plus four employees
fills five seats; the fifth email may remain reserved. Invitations expire after
21 days. Never transfer an old employee's Claude account or history.

## Required live acceptance, all outstanding

- [ ] Five public deliveries and Gmail/Outlook replies, attachments and sender authentication.
- [ ] Existing hello@playloudr.com still receives correctly.
- [ ] Spam/virus handling, duplicate receipts, failed and uncertain sends.
- [ ] Owner/employee login, TOTP, expired sessions and account recovery.
- [ ] Cross-employee read/search/download isolation, including after reassignment.
- [ ] S3 version recovery and DynamoDB PITR restore into isolated recovery resources;
      verify restored MIME, attachments and authorization metadata before cutover.
- [ ] Packaged/installed SimpleMail x64 and ARM64, native login and attachment save.
- [ ] Real employee Claude invitation accepted with correct role/seat and no extra purchase.

Rollback disables only the five new forwarders, sets sending false and restores
the previous active receipt ruleset if changed. Do not purge retained mail or
replace employee history. S3 and DynamoDB are retained on stack deletion.

The earlier $3â€“$10/month estimate remains provisional for 5,000 incoming + 5,000
outgoing and 20 GB stored, excluding development, tax, currency conversion and AI.
Measure after the pilot; credits/free tiers are not permanent guarantees.

References: [SES receiving](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-concepts.html),
[Cloudflare rules](https://developers.cloudflare.com/api/resources/email_routing/subresources/rules/methods/create/),
[Claude members](https://support.claude.com/en/articles/13133750-manage-members-on-team-and-enterprise-plans).
