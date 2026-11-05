"""The receipt: same numbers everywhere, and a real PDF behind the button."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from frontend.receipt_pdf import build_receipt_pdf

RECEIPT = {
    "available": True,
    "number": "AM-20261031-8A1F2C",
    "reference": "PAY-6b0f2c93-1f2a-4a11-9b22-33cc44dd55ee",
    "status": "HELD",
    "status_label": "Paid",
    "issued_at": "2026-10-31T18:06:41+00:00",
    "issued_label": "31 October 2026, 19:06",
    "currency": "NGN",
    "amounts": {"service": 161.0, "fee": 3.22, "total": 164.22},
    "payer_name": "Kehinde Omoyayi",
    "payee_name": "Kehinde Omoyayi",
    "service": "Plumbing",
    "method": "Bank transfer",
    "provider_reference": "100004261031180609176004048227",
    "payment_id": "8a1f2c44-1111-4a11-9b22-33cc44dd55ee",
}


def render(**overrides):
    context = {
        "payment": {"id": RECEIPT["payment_id"]},
        "receipt": RECEIPT,
        "back_url": "/conversations/12345678-1234-4234-8234-123456789abc/",
        "csrf_token": "test-only",
        "nav_role": "customer",
        "role": "customer",
        "tour_role": "customer",
        "tour_user_key": "guest",
        "platform_manager": False,
        "request": type("Req", (), {"path": "/payments/x/receipt/"})(),
    }
    context.update(overrides)
    return render_to_string("payments/receipt.html", context)


class ReceiptPageTests(SimpleTestCase):
    def test_the_receipt_shows_the_money_the_provider_confirmed(self):
        html = render()
        self.assertIn("NGN 164.22", html)
        self.assertIn("NGN 161.00", html)
        self.assertIn("NGN 3.22", html)
        self.assertIn("Paid", html)
        self.assertIn("AM-20261031-8A1F2C", html)
        self.assertIn("Bank transfer", html)
        self.assertIn("31 October 2026, 19:06", html)

    def test_the_payer_can_view_print_and_download_it(self):
        html = render()
        self.assertIn(
            "/payments/8a1f2c44-1111-4a11-9b22-33cc44dd55ee/receipt.pdf/", html
        )
        self.assertIn("Download receipt", html)
        self.assertIn("data-receipt-print", html)

    def test_the_receipt_ends_at_the_reference(self):
        """No escrow explanation, no closing note — a receipt is a record, not a
        sales pitch."""
        html = render()
        for phrase in (
            "Held in escrow",
            "held by the platform",
            "paid to the artisan only after",
            "Keep this receipt",
            "What this is",
        ):
            self.assertNotIn(phrase, html)

    def test_the_pdf_carries_no_escrow_note(self):
        text = build_receipt_pdf(RECEIPT).decode("latin-1")
        for phrase in ("Held in escrow", "held by the platform", "Keep this receipt", "Generated"):
            self.assertNotIn(phrase, text)
        self.assertIn("Our reference", text)

    def test_the_payout_line_is_absent_for_the_payer(self):
        self.assertNotIn("Net to you", render())

    def test_the_artisan_receipt_shows_their_net(self):
        artisan = dict(RECEIPT, amounts={"service": 161.0, "fee": 3.22, "total": 164.22, "net_to_payee": 144.9})
        self.assertIn("Net to you", render(receipt=artisan))


class ReceiptPdfTests(SimpleTestCase):
    def test_the_document_is_a_pdf_a_reader_will_open(self):
        document = build_receipt_pdf(RECEIPT)

        self.assertTrue(document.startswith(b"%PDF-1.4"))
        self.assertTrue(document.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Type /Catalog", document)
        self.assertNotIn(b"\x00" * 8, document)

    def test_every_xref_offset_points_at_its_object(self):
        """A PDF reader jumps straight to these offsets; a wrong one opens blank."""
        document = build_receipt_pdf(RECEIPT)
        start = int(document.rsplit(b"startxref", 1)[1].split()[0])
        self.assertEqual(document[start:start + 4], b"xref")
        lines = document[start:].split(b"\n")
        offsets = [int(line.split()[0]) for line in lines[3:9]]
        self.assertEqual(len(offsets), 6)
        for number, offset in enumerate(offsets, start=1):
            self.assertTrue(
                document[offset:].startswith(f"{number} 0 obj".encode("latin-1")),
                f"object {number} is not at byte {offset}",
            )

    def test_the_amount_is_printed_without_a_naira_glyph(self):
        text = build_receipt_pdf(RECEIPT).decode("latin-1")
        self.assertIn("NGN 164.22", text)
        self.assertNotIn("₦", text)
