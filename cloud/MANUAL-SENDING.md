# Owner-controlled outgoing mail

The owner requested normal sending for every existing SimpleMail account on
27 October 2026. This supersedes the earlier receive-only migration scope.
AI sending and automatic website notifications remain disabled independently.

The desktop sends the exact saved compose revision through
`POST /human/mailboxes/{name}/send`. API Gateway accepts the owner app client;
the Lambda also requires an unexpired access token, the owner client ID, an
enabled ADMIN record, current mailbox access and an exact enabled sender.
From is derived from the server registry. MCP tokens cannot use this route.

Before the POST the desktop durably claims its draft. The draft UUID is the
server idempotency reference. A lost response triggers only a status GET.
Uncertain outcomes retain the local draft and never send again automatically.
An accepted result means SES accepted the message; it does not prove delivery.
The server stores the Sent copy, HTML, reply headers and attachments. Embedded
signature images become related MIME parts. Bcc recipients are absent from MIME.

Manual delivery uses London SES, separately from the Ireland receiving API.
`HumanSenderAddresses` must enumerate only existing approved mailbox addresses.
`SendingEnabled` and `AiSendingEnabled` stay false. The health configuration
may set `owner_manual_sending_enabled: true`; that permits only the manual
flag to be enabled and continues to report general/AI transport drift.

The production migration stack has accumulated reviewed changes outside this
source template. Never overwrite it with this template. Derive deployments
from its fresh original template, preserve existing code/resources, inspect
the change set and verify the exact saved template after deployment.

The current ten sender identities are one through five at extraliferecords.com,
hello@extraliferecords.com, playloudr@extraliferecords.com,
adam@extraliferecords.com, hello@meandtherobots.com and
hello@lightspeedruinedtamworth.co.uk. Receiving aliases are not extra selectable
sender identities. Existing non-cloud SMTP accounts retain their own settings.
