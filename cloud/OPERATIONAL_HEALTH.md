# Owner receiving health

Native SimpleMail: **Settings → Receiving health and owner settings** opens the existing `/ui/ai.html` page at the configured Ireland API. The opener validates the saved service address, never unlocks or exports the desktop session, and accepts no caller-supplied URL. Sign in through the browser inbox if necessary. Cloud and existing accounts with the same identity remain available, with provider badges in the sidebar and settings picker.

`GET /mailboxes/operational-health` requires an unexpired access token from `OWNER_CLIENT_ID` and a fresh, consistent `ADMIN/<subject>` record with `enabled: true`. Employee assignments, mailbox grants and Codex/Claude client tokens do not grant access. It creates no AI tool or permissions.

The view reads only two failure queue counts, two metric alarm states, configured SES identity/DKIM status, the active ordered receipt rules, and the actual sending switches. API general sending is read in-process; manual sending is read from Delivery, and both AI controls from MCP. Provider details, environment contents, queue bodies, subscriber addresses and mail are never returned.

An empty queue is not proof of delivery. Disabled migration routing explicitly says imported history does not confirm new mail delivery. Pending identities/rules remain pending; drift or queue/alarm failures need attention; unavailable/missing observations are unknown. No check sets `delivery_verified` to true. This view does not verify public MX, quarantine contents, successful live delivery, alarm notifications, free-plan continuity or bills.

The browser refreshes every five minutes **only while open and visible**, with manual refresh available. Results older than ten minutes are marked stale; request failures remove old details. This is an on-demand view, not an unattended notification service. Account-plan continuity and the separate proposed gross-cost budget remain operator checks; this change creates neither budgets nor subscribers.

## Deployment scope

The source SAM template defaults `OperationalHealthConfig` to `{}` and makes the additional IAM policy opt-in. Configure bounded `queues` (label/url), `alarms` (label/name), `domains`, `rule_set`, `rules` (name/label/recipients/migration), `topic_arn`, `mcp_function` and `delivery_function` values. Empty or malformed configuration returns unknown without provider reads. No mailbox content belongs in this configuration.

The API role additionally needs only:

| Action | Resource scope |
| --- | --- |
| `sqs:GetQueueAttributes` | The two failure queues |
| `cloudwatch:DescribeAlarms` | The two metric alarms |
| `ses:GetEmailIdentity` | The four receiving identities |
| `lambda:GetFunctionConfiguration` | Delivery and MCP functions |
| `ses:DescribeActiveReceiptRuleSet` | `*`, restricted by `aws:RequestedRegion` |

SES does not provide a resource type for `DescribeActiveReceiptRuleSet`; its wildcard is necessary for this single read operation. See the [SES authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ses.html) and [SES v2 identity authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_sesv2.html).

For the migration deployment, patch the **fresh exact live CloudFormation template**, preserving deployment-specific identities, both failure queues, the disabled migration rule, current API pagination and the receiver setup-control fix. Update API/Web code, the API health environment variable and one additional API-role inline policy only. Preserve all parameters, existing policies, receipt routing, send flags, Cognito clients and grants. Do not deploy this source SAM template over the live migration stack.

Review the prepared change set before execution. Post-deployment acceptance must independently verify changed hashes/IAM/config, owner and non-owner HTTP results, pending migration presentation, unchanged sending controls/rules/grants, then the installed native settings link and provider badges. A source/browser fixture pass does not establish those live gates.

## Local checks

`python -m unittest discover -s cloud/tests -p test_operational_health.py -v`

`python -m unittest discover -s tests -p test_cloud_mail.py -v`

`node tests/receiving_health_browser.cjs` and `node tests/account_identity_browser.cjs` use local fixtures. Set `SIMPLEMAIL_PLAYWRIGHT_MODULE` and optionally `SIMPLEMAIL_CHROMIUM` for the installed runtime. They send no live email or notifications.
