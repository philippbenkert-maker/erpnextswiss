"""Real pinned SDK, synthetic keys, signed bank responses, no network or ERP writes."""
import base64
from io import BytesIO
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

from ebicsclient import Bank, BankKeys, Client, User, VersionPolicy, generate_keyring, crypto
from ebicsclient.errors import MessageVersionMismatchError, ReceiptMismatchError, ResponseAuthenticationError
from ebicsclient.protocol import h005
from lxml import etree

from erpnextswiss.erpnextswiss.python_ebics_reader import (
    PythonEbicsReader, ReadOnlyTransport, BankFileError, MAX_RESPONSE, NAMESPACE,
)


class Response(BytesIO):
    status = 200
    headers = {}


class Opener:
    def __init__(self, responses, events):
        self.responses, self.events, self.requests = list(responses), events, []

    def open(self, request, timeout):
        self.requests.append(request.data)
        self.events.append("http")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result(request.data) if callable(result) else result)


class TestPythonEbicsReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.subscriber = generate_keyring()
        cls.bank_ring = generate_keyring()

    def signed(self, static="", mutable="", body="", code="000000"):
        root = etree.fromstring((f'<ebicsResponse xmlns="{NAMESPACE}">'
            f'<header authenticate="true"><static>{static}</static><mutable>{mutable}'
            f'<ReturnCode>{code}</ReturnCode></mutable></header>'
            f'<body>{body}<ReturnCode authenticate="true">000000</ReturnCode></body>'
            '</ebicsResponse>').encode())
        root.insert(1, crypto.build_auth_signature(root, self.bank_ring.authentication, NAMESPACE))
        return etree.tostring(root)

    def payload(self, version="08"):
        out = BytesIO()
        with ZipFile(out, "w") as archive:
            archive.writestr("statement.xml", f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.{version}"><BkToCstmrStmt/></Document>')
        return out.getvalue()

    def responses(self, payload=None, segments=1):
        key = crypto.new_transaction_key()
        encrypted_key = base64.b64encode(crypto.encrypt_transaction_key(self.subscriber.encryption.public_key(), key)).decode()
        encrypted = base64.b64encode(crypto.encrypt_with_transaction_key(key, payload or self.payload())).decode()
        return [self.signed(static=f'<TransactionID>{"A"*32}</TransactionID><NumSegments>{segments}</NumSegments>',
                            mutable='<SegmentNumber lastSegment="true">1</SegmentNumber>',
                            body=f'<DataTransfer><DataEncryptionInfo><TransactionKey>{encrypted_key}</TransactionKey></DataEncryptionInfo><OrderData>{encrypted}</OrderData></DataTransfer>'),
                lambda request: self.signed(code="011000" if b"<ReceiptCode>0</ReceiptCode>" in request else "011001")]

    def reader(self, responses):
        events = []
        reader = PythonEbicsReader.__new__(PythonEbicsReader)
        reader.bank = Bank(host_id="TEST", url="https://bank.invalid/ebics")
        reader.version, reader.keyring = "08", self.subscriber
        opener = Opener(responses, events)
        reader.transport = ReadOnlyTransport(reader.bank.url, "08", reader._before_receipt, opener=opener)
        reader.client = Client(reader.bank, User(partner_id="P1", user_id="U1"), self.subscriber,
                               transport=reader.transport, version_policy=VersionPolicy.STRICT, recovery_attempts=0)
        # Test fixture only: production obtains these via hash-pinned HPB.
        reader.client._bank_keys = BankKeys(authentication=self.bank_ring.authentication.public_key(),
                                            encryption=self.bank_ring.encryption.public_key())
        reader._payload = reader._persist = None
        return reader, opener, events

    def test_real_sdk_persists_before_positive_receipt_without_dates(self):
        reader, opener, events = self.reader(self.responses())
        result = reader.download(lambda tx, raw: events.append("persist"))
        self.assertEqual(result, "A" * 32)
        self.assertEqual(events, ["http", "persist", "http"])
        self.assertNotIn(b"DateRange", opener.requests[0])
        self.assertIn(b"<ReceiptCode>0</ReceiptCode>", opener.requests[-1])

    def test_persistence_failure_never_sends_positive_receipt(self):
        reader, opener, _ = self.reader(self.responses())
        with self.assertRaises(OSError):
            reader.download(Mock(side_effect=OSError("disk full")))
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_bank_signature_never_reaches_persistence(self):
        responses = self.responses()
        responses[0] = responses[0].replace(b"<NumSegments>1", b"<NumSegments>2")
        reader, opener, _ = self.reader(responses)
        persist = Mock()
        with self.assertRaises(ResponseAuthenticationError): reader.download(persist)
        persist.assert_not_called()
        self.assertEqual(len(opener.requests), 1)

    def test_wrong_message_version_is_not_acknowledged(self):
        reader, opener, _ = self.reader(self.responses(self.payload("04")))
        persist = Mock()
        with self.assertRaises(MessageVersionMismatchError): reader.download(persist)
        persist.assert_not_called()
        self.assertNotIn(b"<ReceiptCode>0</ReceiptCode>", b"".join(opener.requests))

    def test_keep_probe_does_not_consume(self):
        reader, opener, events = self.reader(self.responses())
        reader.download(lambda tx, raw: events.append("preview"), keep=True)
        self.assertEqual(events, ["http", "preview", "http"])
        self.assertIn(b"<ReceiptCode>1</ReceiptCode>", opener.requests[-1])

    def test_lost_receipt_answer_does_not_retry(self):
        reader, opener, events = self.reader([self.responses()[0], TimeoutError()])
        with self.assertRaises(BankFileError): reader.download(lambda tx, raw: events.append("persist"))
        self.assertEqual(events, ["http", "persist", "http"])
        self.assertEqual(len(opener.requests), 2)

    def test_negative_confirmation_is_not_success(self):
        reader, _, _ = self.reader([self.responses()[0], self.signed(code="011001")])
        with self.assertRaises(ReceiptMismatchError): reader.download(Mock())

    def test_resume_verifies_archive_before_sending_exact_receipt(self):
        reader, opener, events = self.reader([self.signed(code="011000")])
        reader.confirm("A" * 32, lambda tx: events.append("readback"))
        self.assertEqual(events, ["readback", "http"])
        self.assertIn(("<TransactionID>" + "A" * 32).encode(), opener.requests[0])

    def test_failed_resume_readback_sends_nothing(self):
        reader, opener, _ = self.reader([])
        with self.assertRaises(BankFileError): reader.confirm("A" * 32, Mock(side_effect=BankFileError("corrupt")))
        self.assertEqual(opener.requests, [])

    def test_receipt_for_another_transaction_is_not_success(self):
        reader, _, _ = self.reader([self.responses()[0], self.signed(
            static=f'<TransactionID>{"B"*32}</TransactionID>', code="011000")])
        with self.assertRaises(BankFileError): reader.download(Mock())

    def test_drip_feed_is_stopped_at_deadline(self):
        reader, opener, _ = self.reader(self.responses())
        moments = iter([1, 1, 121])
        reader.transport.deadline = 120
        reader.transport.clock = lambda: next(moments)
        request = h005.build_download_receipt_request(reader.bank, reader.keyring, "A" * 32, positive=False)
        reader.transport.transaction_id = "A" * 32
        with self.assertRaises(BankFileError): reader.transport.post(request)
        self.assertEqual(len(opener.requests), 1)

    def test_no_data_is_authenticated_and_returns_none(self):
        reader, _, _ = self.reader([self.signed(code="090005")])
        persist = Mock()
        self.assertIsNone(reader.download(persist))
        persist.assert_not_called()

    def test_response_size_bound(self):
        reader, opener, _ = self.reader([b"x" * (MAX_RESPONSE + 1)])
        with self.assertRaises(BankFileError): reader.download(Mock())
        self.assertEqual(len(opener.requests), 1)

    def test_excessive_segments_rejected(self):
        reader, opener, _ = self.reader(self.responses(segments=65))
        with self.assertRaises(BankFileError): reader.download(Mock())
        self.assertEqual(len(opener.requests), 1)

    def test_uploads_and_key_initialisation_refused(self):
        for order in ("BTU", "INI", "HIA", "SPR"):
            reader, opener, _ = self.reader([])
            body = f'<ebicsRequest xmlns="{NAMESPACE}"><header><static><OrderDetails><AdminOrderType>{order}</AdminOrderType></OrderDetails></static><mutable><TransactionPhase>Initialisation</TransactionPhase></mutable></header><body/></ebicsRequest>'.encode()
            with self.assertRaises(BankFileError): reader.transport.post(body)
            self.assertEqual(opener.requests, [])

    def test_unsafe_endpoints_rejected(self):
        for url in ("http://bank.invalid", "https://user:pass@bank.invalid", "https://bank.invalid/#secret", "https://bank.invalid:444/"):
            with self.assertRaises(BankFileError): ReadOnlyTransport(url, "08", Mock())

    def test_deadline_prevents_another_http_request(self):
        reader, opener, _ = self.reader([])
        reader.transport.deadline = 0
        with self.assertRaises(BankFileError): reader.transport.post(b"x")
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
