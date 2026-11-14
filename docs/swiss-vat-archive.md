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

1. Open **Swiss VAT Settings** for the company and review its enabled account
   mapping. Automatic extraction supports CHF accounting with the effective method
   and agreed consideration (basis 1). No account numbers are guessed at runtime.
2. Create a declaration with the exact company and period. **Get values** reads
   posted GL entries and returns all amounts, source evidence and a VAT
   reconciliation together. The same account allocation drives **Kontrolle MwSt**.
   Missing/ambiguous mappings stop the retrieval instead of filling silent zeros.
3. **Automatically from ledger** is on for new declarations. Submit re-reads the
   ledger, calculates the current amounts and validates against the bundled
   official eCH-0217/2 schema before archiving the private XML atomically. It saves
   the source fingerprint and reconciliation. It creates no closing journal entry.
4. Download the archived XML and upload it to the ESTV portal. Then record the
   completed external filing, optionally with its receipt and actual upload date.
5. Repeat downloads return the identical original bytes. Preserve the original
   and create an explicit correction for changes; duplicate original periods,
   cancellation and removal of archived evidence are blocked.

Existing archives are never recalculated by automatic extraction, including when
current ledger amounts differ from the historic filed XML. Other companies using
reviewed legacy company-scoped VAT queries retain that read-only endpoint. Manual
amounts and flat/cash-basis declarations must use their own reviewed workflow;
the new effective/agreed allocator does not pretend to support these methods.

## Read-only account allocation

The explicit account roles cover taxable/exempt turnover, reductions, input tax,
output-tax reconciliation, VAT settlements, excluded accounts, other funds and
acquisition tax. Ziffer 200 is net posted turnover before reductions, not gross
invoice totals. Invoice item rates split the actual GL income into 303/313/343
(or 302/312/342 for older rates). Zero-rate turnover needs an explicit classification.
Opted turnover is included in 200 and additionally disclosed in 205.

Reductions (235) also reduce the relevant rate base. Payment discounts inherit the
referenced invoice's rate/exempt box; mixed-rate payments need separate, reviewed
posting accounts. Signed credit notes, import input tax and reclassifications are
included. Input tax uses debit-minus-credit for 400/405/410 and credit-minus-debit
for correction boxes 415/420. Other funds in 900/910 do not increase 200.

A whole Journal Entry consisting only of mapped VAT and settlement accounts is
excluded as a closing; mixed closings fail for review. Payments of filed balances
are not new VAT events. Period-closing vouchers, opening and cancelled entries are
excluded. Unmapped income/tax postings and larger output-tax differences stop the
report. Cent differences within the configured tolerance are displayed explicitly.
No accounting balance is adjusted to force reconciliation.

`erpnextswiss.vat_setup.configure_company(company, apply=False)` previews the
reviewed setup for the standard Swiss account plan. Applying it creates missing
company settings and corrects only known **future purchase tax templates** to
1170/1171 with tax codes 400/405. Repeating it preserves existing account mappings.
It never changes account documents, invoice tax rows, payments, journals or GL.
The function is not a public RPC endpoint and requires an accounting manager role.

The signed payable amount represents either liability or credit. Monetary values
use Decimal with cent rounding. Effective and flat methods use separate fields.
The archive uses eCH-0217 version 2, not the obsolete 2017 namespace. Special tax
extensions unsupported by the form are rejected rather than silently dropped.

## Release acceptance

Run the pure XML and ledger-allocation tests and distribution/source verification. On a restored private
platform test site, run `scripts/vat_archive_native_check.py`. It is hard-guarded to
`kt-platform-test.local` and `platform-restore-db`; external data is supplied in a
private JSON source list outside Git. It tests both historical imports, repeat
imports/downloads, submission, corrections, permissions and immutability, verifies
all journal/GL/invoice rows unchanged, then rolls back every test change.
Never put original declarations, backups, credentials or source JSON into Git.

The additional `scripts/vat_automation_native_check.py` uses the same isolated
restore guard and private period expectations. It tests setup idempotency, live
GL allocation and XML/audit reconciliation, automatic refresh at submission,
permissions and unsupported-method failures. Hashes cover all account, ledger,
journal, invoice, invoice-tax and payment tables before/after. All test setup,
future templates and draft declarations are rolled back; original XML stays exact.
