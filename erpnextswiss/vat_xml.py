"""Swiss eCH-0217 v2 export, arithmetic and offline validation. No ERP writes."""
from __future__ import annotations
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
import re
from lxml import etree

NS = "http://www.ech.ch/xmlns/eCH-0217/2"
APP_NS = "http://www.ech.ch/xmlns/eCH-0058/5"
CENT = Decimal("0.01")
RESOURCES = Path(__file__).parent / "resources" / "ech0217_v2"
TURNOVER = {
    "total_revenue": "totalConsideration", "tax_free_services": "suppliesToForeignCountries",
    "revenue_abroad": "suppliesAbroad", "transfers": "transferNotificationProcedure",
    "non_taxable_services": "suppliesExemptFromTax", "losses": "reductionOfConsideration",
}
PRETAX = {
    "pretax_material": "inputTaxMaterialAndServices", "pretax_investments": "inputTaxInvestments",
    "missing_pretax": "subsequentInputTaxDeduction", "pretax_correction_mixed": "inputTaxCorrections",
    "pretax_correction_other": "inputTaxReductions",
}
RATES = {"normal": "8.1", "reduced": "2.6", "lodging": "3.8",
         "normal_old": "7.7", "reduced_old": "2.5", "lodging_old": "3.7"}


def money(value):
    try:
        result = Decimal(str(value or 0))
        if not result.is_finite():
            raise ValueError("Amounts must be finite")
        return result.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid amount") from exc


def uid(value):
    normalized = re.sub(r"[.\s-]", "", str(value or "").upper())
    normalized = re.sub(r"(MWST|TVA|IVA)$", "", normalized)
    if not re.fullmatch(r"CHE[0-9]{9}", normalized):
        raise ValueError("A Swiss UID such as CHE-123.456.789 is required")
    return normalized


def period(values):
    start = date.fromisoformat(str(values.get("start_date")))
    end = date.fromisoformat(str(values.get("end_date")))
    if start > end:
        raise ValueError("The period ends before it starts")
    return start, end


def supplies(values):
    effective = values.get("vat_type", "effective") == "effective"
    for suffix in ("_2024", ""):
        for prefix in (("normal", "reduced", "lodging") if effective else ("1", "2")):
            if effective:
                amount_field = prefix + "_amount" + suffix
                rate_field = prefix + "_rate" + suffix
                tax_field = prefix + "_tax" + suffix
                default = RATES[prefix if suffix else prefix + "_old"]
            else:
                amount_field = "amount_" + prefix + suffix
                rate_field = "rate_" + prefix + suffix
                tax_field = "tax_" + prefix + suffix
                default = 0
            amount = money(values.get(amount_field))
            rate = money(values.get(rate_field, default))
            if amount and not 0 < rate <= 100:
                raise ValueError("A valid tax rate is required for " + amount_field)
            yield amount_field, tax_field, amount, rate


def calculate(values):
    result, tax = {}, Decimal(0)
    rate_totals = {}
    for _, field, amount, rate in supplies(values):
        result[field] = money(amount * rate / 100)
        rate_totals[rate] = rate_totals.get(rate, Decimal(0)) + amount
    # XML combines equal rates; round each combined rate once to cents.
    tax = sum((money(amount * rate / 100) for rate, amount in rate_totals.items()), Decimal(0))
    for suffix, rate in (("_2024", Decimal("8.1")), ("", Decimal("7.7"))):
        result["additional_tax" + suffix] = money(money(values.get("additional_amount" + suffix)) * rate / 100)
        tax += result["additional_tax" + suffix]
    result["total_deductions"] = sum((money(values.get(f)) for f in (*tuple(TURNOVER)[1:], "misc")), Decimal(0))
    # Ziffer 205 is opted turnover included in 200, not a deduction.
    result["taxable_revenue"] = money(values.get("total_revenue")) - result["total_deductions"]
    result["total_tax"] = money(tax)
    if values.get("vat_type", "effective") == "effective":
        result["total_pretax_reductions"] = (money(values.get("pretax_material")) + money(values.get("pretax_investments"))
            + money(values.get("missing_pretax")) - money(values.get("pretax_correction_mixed"))
            - money(values.get("pretax_correction_other")))
        if money(values.get("form_1050")) or money(values.get("form_1055")):
            raise ValueError("Forms 1050/1055 apply to the net tax rate method")
    else:
        if any(money(values.get(f)) for f in PRETAX):
            raise ValueError("Input tax cannot be deducted with the net tax rate method")
        result["total_pretax_reductions"] = money(values.get("form_1050")) + money(values.get("form_1055"))
    result["payable_tax"] = money(tax - result["total_pretax_reductions"])
    result["balance"] = max(-result["payable_tax"], Decimal(0))
    return result


class LocalSchemas(etree.Resolver):
    def resolve(self, url, public_id, context):
        name = url.replace("\\", "/").rsplit("/", 1)[-1]
        path = RESOURCES / name
        if not re.fullmatch(r"eCH-[0-9-]+\.xsd", name) or not path.is_file():
            raise ValueError("Unbundled schema reference")
        return self.resolve_filename(str(path), context)


@lru_cache(maxsize=1)
def schema():
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    parser.resolvers.add(LocalSchemas())
    return etree.XMLSchema(etree.parse(str(RESOURCES / "eCH-0217-2-0-0.xsd"), parser))


def validate_xml(content):
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if not raw or len(raw) > 2_000_000:
        raise ValueError("XML is empty or too large")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(raw, parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError("DTDs are not accepted")
    schema().assertValid(root)
    return root


def build_xml(values, tax_id, now=None):
    start, end = period(values)
    method = values.get("vat_type", "effective")
    if method not in ("effective", "flat"):
        raise ValueError("Unsupported VAT method")
    totals = calculate(values)
    rows = list(supplies(values))
    if money(sum((r[2] for r in rows), Decimal(0))) != money(totals["taxable_revenue"]):
        raise ValueError("Taxable turnover does not match the turnover per tax rate")
    root = etree.Element("{" + NS + "}VATDeclaration", nsmap={None: NS, "eCH-0058": APP_NS})
    def add(parent, name, value):
        node = etree.SubElement(parent, "{" + NS + "}" + name)
        node.text = str(value)
        return node
    def amounts(parent, mapping):
        for field, name in mapping.items():
            amount = money(values.get(field))
            if amount or field == "total_revenue":
                add(parent, name, format(amount, ".2f"))
    info = add(root, "generalInformation", "")
    add(info, "uid", uid(tax_id))
    add(info, "organisationName", values.get("company"))
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("Generation time must have a time zone")
    add(info, "generationTime", timestamp.astimezone(timezone.utc).isoformat(timespec="seconds"))
    add(info, "reportingPeriodFrom", start.isoformat())
    add(info, "reportingPeriodTill", end.isoformat())
    add(info, "typeOfSubmission", values.get("submission_type") or "1")
    add(info, "formOfReporting", values.get("reporting_basis") or "1")
    add(info, "businessReferenceId", values.get("business_reference") or values.get("name") or values.get("title"))
    app = add(info, "sendingApplication", "")
    for name, value in (("manufacturer", "KT"), ("product", "ERPNextSwiss"), ("productVersion", "2.0.0")):
        etree.SubElement(app, "{" + APP_NS + "}" + name).text = value
    turnover = add(root, "turnoverComputation", "")
    amounts(turnover, TURNOVER)
    if money(values.get("misc")):
        description = str(values.get("misc_description") or "").strip()
        if not description or len(description) > 50:
            raise ValueError("Other deductions require a description of at most 50 characters")
        other = add(turnover, "variousDeduction", "")
        add(other, "amountVariousDeduction", format(money(values["misc"]), ".2f"))
        add(other, "descriptionVariousDeduction", description)
    tax = add(root, "effectiveReportingMethod" if method == "effective" else "netTaxRateMethod", "")
    if method == "effective":
        add(tax, "grossOrNet", "1")
        if money(values.get("non_taxable_revenue")):
            add(tax, "opted", format(money(values["non_taxable_revenue"]), ".2f"))
    groups = {}
    for _, _, amount, rate in rows:
        groups[rate] = groups.get(rate, Decimal(0)) + amount
    for rate, amount in groups.items():
        if amount:
            item = add(tax, "suppliesPerTaxRate", "")
            add(item, "taxRate", format(rate, ".2f"))
            add(item, "turnover", format(amount, ".2f"))
    for suffix, rate in (("_2024", "8.10"), ("", "7.70")):
        if money(values.get("additional_amount" + suffix)):
            item = add(tax, "acquisitionTax", "")
            add(item, "taxRate", rate)
            add(item, "turnover", format(money(values["additional_amount" + suffix]), ".2f"))
    amounts(tax, PRETAX if method == "effective" else {"form_1050": "compensationExport", "form_1055": "deemedInputTaxDeduction"})
    add(root, "payableTax", format(totals["payable_tax"], ".2f"))
    if money(values.get("grants")) or money(values.get("donations")):
        funds = add(root, "otherFlowsOfFunds", "")
        amounts(funds, {"grants": "subsidies", "donations": "donations"})
    content = etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True)
    validate_xml(content)
    return content


def read_xml(content):
    root = validate_xml(content)
    ns = {"v": NS}
    def text(path, default=None):
        return root.findtext(path, default, ns)
    info = "v:generalInformation/"
    values = {"start_date": text(info + "v:reportingPeriodFrom"),
              "end_date": text(info + "v:reportingPeriodTill"),
              "submission_type": text(info + "v:typeOfSubmission"),
              "reporting_basis": text(info + "v:formOfReporting"),
              "business_reference": text(info + "v:businessReferenceId")}
    method = "effectiveReportingMethod" if root.find("v:effectiveReportingMethod", ns) is not None else "netTaxRateMethod"
    tax = root.find("v:" + method, ns)
    if tax is None or (method == "effectiveReportingMethod" and tax.findtext("v:grossOrNet", namespaces=ns) != "1"):
        raise ValueError("Only effective net or net tax rate declarations can be imported")
    supported = set(PRETAX.values()) | {"grossOrNet", "opted", "suppliesPerTaxRate", "acquisitionTax", "compensationExport", "deemedInputTaxDeduction"}
    if any(etree.QName(child).localname not in supported for child in tax):
        raise ValueError("The XML contains a special reporting method requiring manual reconciliation")
    values["vat_type"] = "effective" if method == "effectiveReportingMethod" else "flat"
    for field, name in TURNOVER.items():
        values[field] = money(text("v:turnoverComputation/v:" + name))
    values["misc"] = money(text("v:turnoverComputation/v:variousDeduction/v:amountVariousDeduction"))
    values["misc_description"] = text("v:turnoverComputation/v:variousDeduction/v:descriptionVariousDeduction")
    values["non_taxable_revenue"] = money(tax.findtext("v:opted", namespaces=ns))
    for item in tax.findall("v:suppliesPerTaxRate", ns):
        rate, amount = money(item.findtext("v:taxRate", namespaces=ns)), money(item.findtext("v:turnover", namespaces=ns))
        if values["vat_type"] == "effective":
            matches = [key for key, value in RATES.items() if money(value) == rate]
            if not matches:
                raise ValueError("Unsupported historical VAT rate")
            key = matches[0]
            suffix = "" if key.endswith("_old") else "_2024"
            prefix = key.removesuffix("_old")
            amount_field, rate_field = prefix + "_amount" + suffix, prefix + "_rate" + suffix
        else:
            used = [i for i in ("1", "2") if values.get("rate_" + i + "_2024") == rate]
            free = [i for i in ("1", "2") if "rate_" + i + "_2024" not in values]
            if not used and not free:
                raise ValueError("More than two net tax rates require manual reconciliation")
            slot = (used or free)[0]
            amount_field, rate_field = "amount_" + slot + "_2024", "rate_" + slot + "_2024"
        values[amount_field] = money(values.get(amount_field)) + amount
        values[rate_field] = rate
    for item in tax.findall("v:acquisitionTax", ns):
        rate = money(item.findtext("v:taxRate", namespaces=ns))
        suffix = "_2024" if rate == money("8.1") else "" if rate == money("7.7") else None
        if suffix is None:
            raise ValueError("Unsupported acquisition tax rate")
        field = "additional_amount" + suffix
        values[field] = money(values.get(field)) + money(item.findtext("v:turnover", namespaces=ns))
    for field, name in {**PRETAX, "form_1050": "compensationExport", "form_1055": "deemedInputTaxDeduction"}.items():
        values[field] = money(tax.findtext("v:" + name, namespaces=ns))
    for field, name in (("grants", "subsidies"), ("donations", "donations")):
        values[field] = money(text("v:otherFlowsOfFunds/v:" + name))
    period(values)
    totals = calculate(values)
    if totals["payable_tax"] != money(text("v:payableTax")):
        raise ValueError("XML tax amount does not reconcile to its turnover and deductions")
    values.update(totals)
    if sum((r[2] for r in supplies(values)), Decimal(0)) != totals["taxable_revenue"]:
        raise ValueError("XML turnover does not reconcile")
    return {"values": values, "uid": uid(text(info + "v:uid")),
            "generated_at": text(info + "v:generationTime")}
