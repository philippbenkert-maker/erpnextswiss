"""Read-only H005 adapter for the Apache-2.0 ebicsclient 2.0.0 release.

Keys are exported through PyFintech's supported API, encrypted with a random
in-memory password. No keys are generated, registered, rotated or written. The
old provider remains responsible for uploads. Positive receipts are intercepted
BEFORE transmission and require the caller's durable persistence callback.
"""

import re
import secrets
import ssl
import time
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from lxml import etree

from erpnextswiss.scripts.bank_file_admission import BankFileError, MAX_ARCHIVE_BYTES


NAMESPACE = "urn:org:ebics:H005"
NS = {"e": NAMESPACE}
MAX_RESPONSE = 8 * 1024 * 1024
MAX_WIRE_BYTES = 20 * 1024 * 1024
MAX_EXCHANGES = 66  # HPB, initialisation, at most 63 further segments, receipt
ORDER_SECONDS = 120
TRANSACTION_ID = re.compile(r"[A-Fa-f0-9]{32}\Z")


def _xml(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RESPONSE:
        raise BankFileError("EBICS protocol message exceeds its limit")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=False, remove_comments=False)
    try:
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError:
        raise BankFileError("Invalid EBICS protocol XML") from None
    if root.getroottree().docinfo.doctype or sum(1 for _ in root.iter()) > 4096:
        raise BankFileError("Forbidden EBICS protocol XML")
    return root


def _one(root, path):
    values = root.xpath(path, namespaces=NS)
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise BankFileError("Missing or ambiguous EBICS protocol field")
    return values[0]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReadOnlyTransport:
    """Bounded HTTPS; only HPB, exact CH statement BTD and its receipts.

    Bank response signatures are still checked by ebicsclient. This boundary
    does not turn an unsigned response or an uncertain receipt into success.
    No environment proxies, redirects, uploads or key-init calls are allowed.
    """

    def __init__(self, url, version, before_receipt, *, opener=None, clock=time.monotonic):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.query or parsed.port not in (None, 443)):
            raise BankFileError("Invalid configured EBICS HTTPS endpoint")
        if version not in ("04", "08"):
            raise BankFileError("Unsupported EBICS statement version")
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context))
        self.url, self.version, self.before_receipt = url, version, before_receipt
        self.clock = clock
        self.reset_budget()

    def reset_budget(self):
        self.deadline = self.clock() + ORDER_SECONDS
        self.exchanges = self.wire_bytes = 0
        self.transaction_id = None
        self.receipt_id = None

    def _admit(self, body):
        if len(body) > 1024 * 1024:
            raise BankFileError("EBICS request exceeds its limit")
        root = _xml(body)
        order = root.xpath("e:header/e:static/e:OrderDetails/e:AdminOrderType/text()", namespaces=NS)
        if order == ["HPB"] and etree.QName(root).localname == "ebicsNoPubKeyDigestsRequest":
            return "HPB"
        if root.tag != "{" + NAMESPACE + "}ebicsRequest":
            raise BankFileError("Forbidden EBICS request")
        phase = _one(root, "e:header/e:mutable/e:TransactionPhase/text()")
        if phase == "Initialisation" and order == ["BTD"]:
            prefix = "e:header/e:static/e:OrderDetails/e:BTDOrderParams/"
            expected = {"e:Service/e:ServiceName/text()": "EOP", "e:Service/e:Scope/text()": "CH",
                        "e:Service/e:Container/@containerType": "ZIP", "e:Service/e:MsgName/text()": "camt.053",
                        "e:Service/e:MsgName/@version": self.version}
            if (any(_one(root, prefix + path) != value for path, value in expected.items())
                    or root.xpath(".//e:DateRange | .//e:DataTransfer | .//e:ServiceOption", namespaces=NS)):
                raise BankFileError("Only undated Swiss statement downloads are permitted")
            return "BTD"
        transaction_id = _one(root, "e:header/e:static/e:TransactionID/text()")
        if not TRANSACTION_ID.fullmatch(transaction_id) or transaction_id != self.transaction_id:
            raise BankFileError("EBICS transaction identity changed")
        if phase == "Transfer" and not order and not root.xpath("e:body/*", namespaces=NS):
            return "Transfer"
        if phase == "Receipt" and not order:
            code = _one(root, "e:body/e:TransferReceipt/e:ReceiptCode/text()")
            if code not in ("0", "1"):
                raise BankFileError("Invalid EBICS receipt polarity")
            if code == "0":
                self.before_receipt(transaction_id)
                self.receipt_id = transaction_id
            return "Receipt"
        raise BankFileError("EBICS write or unsupported operation refused")

    def post(self, body):
        if self.exchanges >= MAX_EXCHANGES or self.clock() >= self.deadline:
            raise BankFileError("EBICS exchange budget exhausted")
        kind = self._admit(body)
        self.exchanges += 1
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise BankFileError("EBICS exchange deadline expired")
        request = Request(self.url, data=body, method="POST", headers={"Content-Type": "text/xml; charset=UTF-8"})
        try:
            with self.opener.open(request, timeout=min(30, remaining)) as response:
                if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                    raise BankFileError("Unexpected EBICS HTTP response")
                raw = response.read(min(MAX_RESPONSE, MAX_WIRE_BYTES - self.wire_bytes) + 1)
        except BankFileError:
            raise
        except Exception:
            raise BankFileError("EBICS HTTPS exchange failed; receipt may be unconfirmed") from None
        self.wire_bytes += len(raw)
        if self.wire_bytes > MAX_WIRE_BYTES or self.clock() >= self.deadline:
            raise BankFileError("EBICS response budget exhausted")
        root = _xml(raw)
        if kind == "BTD":
            ids = root.xpath("e:header/e:static/e:TransactionID/text()", namespaces=NS)
            if ids:
                if len(ids) != 1 or not TRANSACTION_ID.fullmatch(ids[0]):
                    raise BankFileError("Invalid EBICS transaction identity")
                self.transaction_id = ids[0]
            segments = root.xpath("e:header/e:static/e:NumSegments/text()", namespaces=NS)
            if segments and (len(segments) != 1 or not segments[0].isdigit() or not 1 <= int(segments[0]) <= 64):
                raise BankFileError("EBICS segment count exceeds its limit")
        return raw


class PythonEbicsReader:
    def __init__(self, connection):
        from ebicsclient import Bank, BankKeyHashes, Client, Keyring, User, VersionPolicy, public_key_hash

        if connection.ebics_version != "H005" or not connection.hpb_downloaded:
            raise BankFileError("An activated H005 connection with trusted bank keys is required")
        # Supported exports only. The existing encrypted key file is never changed.
        legacy = connection.get_client()
        password = secrets.token_urlsafe(48)
        private = legacy.user.export_keys(passphrase=password, pkcs=8)
        public = legacy.bank.export_keys()
        if set(private) != {"A006", "X002", "E002"} or set(public) != {"X002", "E002"}:
            raise BankFileError("Existing EBICS key versions are incompatible")
        def raw(value):
            return value.encode("ascii") if isinstance(value, str) else value
        def private_key(version):
            return load_pem_private_key(raw(private[version]), password=password.encode("utf-8"))
        self.keyring = Keyring(signature=private_key("A006"), authentication=private_key("X002"),
                               encryption=private_key("E002"))
        bank_auth = load_pem_public_key(raw(public["X002"]))
        bank_enc = load_pem_public_key(raw(public["E002"]))
        if any(key.key_size < 2048 for key in (self.keyring.signature, self.keyring.authentication,
                                              self.keyring.encryption, bank_auth, bank_enc)):
            raise BankFileError("EBICS key size is below the security minimum")
        self.bank = Bank(host_id=legacy.bank.hostid, url=legacy.bank.url)
        self.version = connection.statement_btf_version
        self.transport = ReadOnlyTransport(self.bank.url, self.version, self._before_receipt)
        self.client = Client(self.bank, User(partner_id=legacy.user.partnerid, user_id=legacy.user.userid),
                             self.keyring, transport=self.transport, version_policy=VersionPolicy.STRICT,
                             recovery_attempts=0)
        self.client.hpb(pinned=BankKeyHashes(authentication=public_key_hash(bank_auth),
                                            encryption=public_key_hash(bank_enc)))
        self._payload = self._persist = None

    def _before_receipt(self, transaction_id):
        if self._persist is None or self._payload is None:
            raise BankFileError("Positive receipt requires durable ERP persistence")
        self._persist(transaction_id, self._payload)

    def download(self, persist, *, keep=False):
        """Persist and independently verify before the SDK sends a positive receipt.

        keep=True is a diagnostic: validates but sends a negative receipt only.
        The caller must provide a read-only callback in that case.
        """
        from ebicsclient import BusinessTransactionFormat, ReceiptPolicy, ReturnCodeError

        self.transport.reset_budget()
        self._persist = persist
        self._payload = None
        def validate(payload):
            if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_ARCHIVE_BYTES:
                raise BankFileError("EBICS archive exceeds its limit")
            self._payload = payload
            if keep:
                persist(self.transport.transaction_id, payload)
        btf = BusinessTransactionFormat(service_name="EOP", message_name="camt.053", scope="CH",
                                        container="ZIP", message_version=self.version)
        try:
            self.client.download(btf, validate=validate,
                                 receipt_policy=ReceiptPolicy.KEEP if keep else ReceiptPolicy.ACKNOWLEDGE)
            if not keep and self.transport.receipt_id is None:
                raise BankFileError("Bank download completed without a durable receipt")
            return self.transport.transaction_id
        except ReturnCodeError as error:
            if error.code == "090005" and self._payload is None and self.transport.transaction_id is None:
                return None
            raise
        finally:
            self._persist = self._payload = None

    def confirm(self, transaction_id, verify):
        """Retry only the exact stored receipt after a lost reply/process restart."""
        from ebicsclient.protocol import h005

        if not isinstance(transaction_id, str) or not TRANSACTION_ID.fullmatch(transaction_id):
            raise BankFileError("Invalid stored EBICS transaction identity")
        self.transport.reset_budget()
        self.transport.transaction_id = transaction_id
        self._payload = b"stored"
        self._persist = lambda actual, _: verify(actual)
        try:
            request = h005.build_download_receipt_request(self.bank, self.keyring, transaction_id, positive=True)
            response = self.transport.post(request)
            h005.verify_response_signature(response, self.client.bank_keys.authentication)
            h005.parse_download_receipt_response(response, positive=True)
        finally:
            self._persist = self._payload = None
