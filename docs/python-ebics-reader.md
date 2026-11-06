# Python EBICS statement reader

Provider: `ebicsclient==2.0.0`, Apache-2.0, upstream tag
`ea0d293fafdae8917f21a26e8d13163f7bc036bc`. Earlier releases had different licence
terms and are not accepted substitutes. Python >=3.11 is required.

The only switch is site config `ebics_statement_reader: "ebicsclient"`.
Omitting it or selecting `"fintech"` keeps the old provider. Unknown values fail
closed. Never activate before an exact-image offline test and a non-consuming
AKB pilot pass. There is no automatic fallback to the restricted old provider.
The existing scheduler, account bindings, file admission, durable EBICS Download
archive and strict invoice matching remain authoritative. Outgoing payments,
key initialization and key rotation are not part of this change.

## Keys and network boundary

The reader uses supported encrypted in-memory exports of the existing A006,
X002 and E002 keys; it does not generate, replace, register or persist keys.
PyFintech remains installed for that export and the unchanged payment path.
The new client's HPB download must match BOTH previously trusted bank public
key hashes. Unknown bank keys require separate verification, never auto-trust.

The injected HTTPS transport accepts HPB and undated BTD/EOP/CH/ZIP/camt.053
only, in the configured 04/08 format. Transfers/receipts must retain the same
transaction ID. Upload/key-init orders, environment proxies and redirects are
refused. TLS >=1.2 with hostname/CA verification and SDK bank signatures remain
mandatory. HTTP response 8 MiB, aggregate response 20 MiB, <=64 segments, <=66
exchanges and a 120-second order budget bound the network. Reads are checked
between chunks with a socket timeout <=30s (one blocked read can overrun the
budget by that timeout); a drip-feed cannot keep a single read alive forever.
The SDK's bounded outer inflation has a 512 MiB cap; our decrypted archive cap
is 32 MiB and inner ZIP/XML admission applies its tighter existing limits. The
512 MiB SDK allocation limit is not a 32 MiB process-memory guarantee.

## Durability and recovery

The SDK validates/decrypts before preparing its receipt. Immediately before a
positive receipt is sent, the adapter admits the archive and all accounts,
stores the exact UTF-8 XML envelope and file hashes, commits, and reads it back
for verification. This matches the existing archive representation; it is not
an archive of the bank's encrypted wire bytes or original ZIP container.
Only a signed positive bank acknowledgement marks the receipt Confirmed.

Persistence/readback failure sends no positive receipt. A lost acknowledgement
leaves the committed record Pending. The next run retries only that exact
receipt, after integrity verification, before any new BTD. Unknown/expired
bank transactions and mismatched acknowledgements remain failures requiring
review; they are not guessed successful. Confirmed Pending processing resumes
using the existing duplicate and open-invoice guards. Ambiguous, partial,
aggregated or reference-free matches are not automatically booked.

## Release and rollback

Build `scripts/Containerfile.ebics-reader` from a pinned production image ID
and an exact Git archive in `app/`; pass the full SOURCE_COMMIT. It replaces
only this app, installs its declared dependencies and requires `pip check`.
Run `scripts/test_ebics_reader_offline.py` in that image with network disabled.
Also verify all app source hashes against the same archive before/after tests.

Pilot: initialize Frappe with a read-only SQL transaction; construct
`PythonEbicsReader` for the approved connection and call `download(callback,
keep=True)`. The callback may only admit/preview data and report counts/dates,
never write/submit. KEEP sends a negative receipt and leaves files at the bank.
Do not expose keys, statements, account numbers or unfiltered errors in logs.

Before deployment: verified backup/rollback path, no pending old receipts,
no concurrent deployment, exact source integration through v16. After release:
activate only the provider switch, run sync, repeat sync, inspect persistent
receipts/Payment Entries and scheduler health. No-data is success only when
the signed bank response says so. Ygnis/VAT period changes are out of scope.

Rollback: stop this scheduler path, resolve any Pending bank receipt explicitly,
restore the previous image and the previous provider setting; preserve all
durable archives and posted Payment Entries. An image rollback is not a ledger
rollback. Do not blindly re-import or delete already booked payments.
