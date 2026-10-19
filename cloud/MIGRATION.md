# Fasthosts mailbox preparation

Prepared 19 October 2026. This implementation is not a record of deployment,
incoming delivery, completed migration or provider cancellation. Keep all three
application send switches false: `SendingEnabled`, `AiSendingEnabled` and the new
`HumanSendingEnabled`. The owner has chosen receive-only replacement mail for now.

## Address and access model

`MailboxRegistry` is reviewed JSON, empty by default. The example file preserves
the original five mailbox IDs, generation partitions and internal transport
addresses. Each new native ID uses the `mail-*` namespace and a single exact
canonical address. Exact aliases map to that same partition. Neither headers nor
client input choose a sender or storage key. Duplicate address ownership, unknown
IDs and configured-address changes against provisioned metadata fail closed.

Use `mail-elr`, `mail-pippy`, `mail-playloudr`, `mail-robots` and `mail-lightspeed`
for the source mailboxes. Existing `elr` and `playloudr` external IDs stay intact.
New Reserved mailboxes are visible only to the owner. They are not automatically
visible to the robot reader or MCP clients. Existing grants are not changed by
deployment, provisioning or import. The reader needs an explicit `ReaderMailboxes`
entry for a new ID; MCP needs an owner-controlled exact grant.

Validate the exact proposed JSON before a deployment. Invalid registry JSON fails
closed at runtime, so deploying an unchecked mapping could interrupt existing
access. This local command makes no account change:

```powershell
python cloud/mail_migration.py registry cloud/migration-registry.example.json
```

Before retiring an external mailbox, reconcile any AI draft references and renew
only its existing read/draft permissions against the accepted replacement ID and
generation in the owner screen. Preserve the old grant snapshot and remove the
retired source binding after checking both clients. Do not enable send-approved
permission incidentally. A connector callback is not a successful mailbox read.

`AdditionalReceiveRecipients` defaults empty. Setting it adds a separate exact
recipient SES rule without replacing the original five-address rule or changing
the active ruleset. Its addresses must match the registry. Verify each receiving
domain/region and rule ordering before enabling routes; no domain wildcard or
catch-all is created. An unknown recipient encountered by the receiver now fails
for retry/dead-letter reconciliation instead of silently disappearing.

## Private backup format and verification

The importer consumes schema-version-1 archives produced by the read-only migration
exporter. Each archive contains `mailbox.json`, per-folder `folder.json`,
`messages.jsonl` and raw `.eml` files. Folder identity is the exact IMAP wire name,
UIDVALIDITY and UID under a stable source host/account hash. Raw MIME preserves
headers, HTML, attachments and original Date; the list ordering uses preserved
INTERNALDATE. A Message-ID is never a deduplication key. All folders, including
separate Sent and Sent Messages, retain their own identities.

The current exporter is in the migration task's `work/migration-backups/mail_backup.py`.
Re-run the export for catch-up, then independently reconcile source UIDs and hashes
with `verify_source.py`. Archive roots remain in the access-controlled
`D:\FasthostsMigration\2026-10-19\mail` directory. These contain private email;
never commit them. The repository ignores `.deployment/` for private operator logs.

Run from the repository with the environment containing the cloud test dependencies:

```powershell
python cloud/mail_migration.py verify 'D:\FasthostsMigration\2026-10-19\mail\hello@extraliferecords.com'
```

Verification requires all complete markers, matching current UID inventories,
per-folder and mailbox totals, every raw-file size and SHA256. Paths cannot escape
the archive. It reports message-size, attachment and MIME-draft compatibility gaps
before any destination write. Historical malware scanning is not fabricated: rows
carry `historical_source_not_rescanned`; live SES receipt quarantine is unchanged.

Two real archives verified offline on 19 October:

| Mailbox | Messages | Raw bytes | Folders | Compatibility exceptions |
|---|---:|---:|---:|---|
| hello@extraliferecords.com | 712 | 32,614,739 | 7 | None |
| playloudr@extraliferecords.com | 121 | 1,622,786 | 6 | None |

Both IMAP Drafts folders were empty. This does not prove that the other three
mailboxes have no drafts or that local desktop drafts are absent.

## Provision, sample import and catch-up

Keep the old provider and clients active. Deploy the reviewed registry with no
additional public recipients first. Preserve stack identity, owner pool, existing
parameter values, S3 versioning and retained data. Operator commands use a normal
temporary AWS profile, never mailbox credentials or the newsletter key.

```powershell
# Read-only preview. --apply is required for actual creation.
python cloud/mail_migration.py provision --profile OPERATOR --owner-sub OWNER_SUB --mailbox mail-elr
python cloud/mail_migration.py provision --profile OPERATOR --owner-sub OWNER_SUB --mailbox mail-elr --apply

# Use the generation returned by provisioning. Preview precedes a bounded sample.
python cloud/mail_migration.py import PRIVATE_ARCHIVE --profile OPERATOR --mailbox mail-elr --expected-generation GENERATION --limit 10
python cloud/mail_migration.py import PRIVATE_ARCHIVE --profile OPERATOR --mailbox mail-elr --expected-generation GENERATION --limit 10 --apply
python cloud/mail_migration.py import PRIVATE_ARCHIVE --profile OPERATOR --mailbox mail-elr --expected-generation GENERATION --apply
python cloud/mail_migration.py reconcile PRIVATE_ARCHIVE --profile OPERATOR --mailbox mail-elr --expected-generation GENERATION
```

All cloud operations require a stable stack with both legacy sending flags false
and the optional manual-human sending flag false.
New native targets must remain Reserved in the pinned generation and have the same
address as the source account. No import can touch one through five. Raw uploads
are immutable conditional puts; interrupted imports can resume without overwriting
bytes or duplicating rows. A rerun refreshes source flags only where the destination
still matches its last imported state. Owner edits, disappeared source items,
unexpected destination items and changed source bytes stop or produce explicit
conflicts. Source or destination mail is never deleted to force totals to match.

`reconcile` downloads and hashes destination bytes and compares folder, date and
state. `storage_reconciled=true` proves storage comparison only. It deliberately
never sets `ready_for_cutover=true`: real external receipt, desktop behaviour,
current-source catch-up, alias coverage and observation remain separate checks.

The rollback is to continue using the original provider, retain both datasets,
correct the isolated import and rerun comparison. Do not delete old mailboxes,
route MX or remove old credentials merely because an import command succeeded.

## Desktop folders, attachments and drafts

The API and desktop discovery expose imported custom folders. Exact case and
hierarchy names survive, and cross-folder search includes them. Flags remain in
the stored metadata; Seen and folder placement are supported by the current UI.
Desktop attachment downloads use authenticated 1 MB chunks with size, offset and
SHA256 checks up to the native 30 MB raw-message bound. Every chunk rechecks mailbox
access. No public file URL or broader S3 permission is needed. Existing inline MCP
attachment reads retain their smaller response bound.

The local compose database has separate unsent drafts that IMAP cannot export.
`migrate_local_drafts.py` copies pending drafts and their scoped attachments,
preserving HTML and threading metadata, without altering source IDs, grants,
credentials or status. It refuses uncertain/sending drafts and reports edits that
conflict with an earlier copy. Preview, apply against a backed-up database, and
verify the target draft in the installed app before hiding the old account:

```powershell
python cloud/migrate_local_drafts.py --database PRIVATE_SQLITE --source-account SOURCE_ID --target-account cloud:mail-elr
python cloud/migrate_local_drafts.py --database PRIVATE_SQLITE --source-account SOURCE_ID --target-account cloud:mail-elr --apply
```

An IMAP MIME draft in a later source archive is preserved as raw mail and reported
as a compatibility exception until its editable restore is checked. Do not call
that mailbox ready if a real draft remains stranded. The two verified sources
have no IMAP drafts; their one pending local draft still needs an actual owner-app
copy/restore check.

## Manual replies stay disabled

The prepared `/human/mailboxes/{id}/send` route is a separate Lambda created only
when `HumanSendingEnabled=true`. It requires the owner OAuth client, owner subject,
unexpired access token and openid scope. MCP clients cannot use it even with the
same subject. London SES receives only exact registry-derived addresses that are
also fixed in its IAM FromAddress allowlist. No SMTP credentials, arbitrary
senders or additional AI grants are exposed. Durable send claims prevent retry
after an uncertain provider response; SES acceptance does not mean delivery.

The user has explicitly kept this route disabled. The existing desktop adapter
also preserves pending drafts and blocks native sends. Enabling manual sending
later requires verified London sender identities, the deliberate flag change,
desktop integration, a controlled delivery check and operational reconciliation
for uncertain outcomes. Existing Fasthosts and Resend outbound accounts are not
modified by this preparation. Receive-only operation must be recorded explicitly
before retiring any required old SMTP route.

## Verification and remaining gates

Tests exercise exact aliases and multiple domains, cross-mailbox denial, unchanged
AI grants, UID/UIDVALIDITY collisions, partial/corrupt snapshots, idempotent retries,
source flag catch-up, owner edits, reassignment during import, custom folders,
chunked attachment integrity, local draft copy/attachment isolation, manual-send
disablement, MCP token denial and uncertain-send deduplication. CloudFormation lint
checks the additive template. These fixtures do not establish live IAM, delivery,
installed-app acceptance, observation or cancellation.

The reviewed account remains read-only until those gates pass. Do not mark Phase 2
complete while any mailbox, alias, pending draft, client or recovery check is missing.
