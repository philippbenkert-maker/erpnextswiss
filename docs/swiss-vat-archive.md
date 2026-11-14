# Swiss VAT declaration and original XML archive

The declaration is a reporting archive. It never posts journal entries and never
transmits a filing to the ESTV. Existing closing entries stay submitted and unchanged.

## Historical filings

From the declaration list choose **Link existing XML and closing entry**. Select
an original XML attached to its submitted closing Journal Entry and inspect the
preview. The import checks eCH-0217/2, company UID, dates, original SHA-256 and the
CHF settlement on account 2201. It links the same private file without moving or
rewriting the original. Repeated import returns the same declaration.

Record that an external filing was completed only with an explicit confirmation
and its evidence. Leave the actual filing date blank when unknown. The ERP records
who confirmed it and when, separately from the actual external upload date.

## Future periods

1. Create a declaration with the company, exact period, effective/flat method and
   agreed/received reporting basis. Inspect the source accounts and amounts.
2. **Get values** requires configured, company-scoped VAT queries for every box.
   Missing queries stop the complete retrieval; no partial values or silent zeros
   are accepted. A query must use `{company}` as a bound placeholder and expose
   `posting_date`, `base_grand_total` and, where needed,
   `total_taxes_and_charges`. Configuration/classification requires accounting review.
   Reviewed amounts may also be entered directly from the accounting evidence.
3. Submit validates amounts and the XML against the bundled official schema and
   archives the private XML atomically. Submission here does not post or externally file.
4. Download the archived XML and upload it to the ESTV portal. Then record the
   completed external filing, optionally with the receipt and actual upload date.
5. Repeated download returns the identical original bytes. Preserve the original
   and create an explicit correction for changes; duplicate original periods,
   cancellation and removal of archived evidence are blocked.

The signed payable amount represents either liability or credit. Monetary values
use Decimal with cent rounding. Effective and flat methods use separate fields.
The archive uses eCH-0217 version 2, not the obsolete 2017 namespace. Special tax
extensions unsupported by the form are rejected rather than silently dropped.

## Release acceptance

Run the pure XML tests and distribution/source verification. On a restored private
platform test site, run `scripts/vat_archive_native_check.py`. It is hard-guarded to
`kt-platform-test.local` and `platform-restore-db`; external data is supplied in a
private JSON source list outside Git. It tests both historical imports, repeat
imports/downloads, submission, corrections, permissions and immutability, verifies
all journal/GL/invoice rows unchanged, then rolls back every test change.
Never put original declarations, backups, credentials or source JSON into Git.
