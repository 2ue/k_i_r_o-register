import unittest

from stripe_pay import card988_get_sms, normalize_card988_content, normalize_card_info, parse_card_line


class StripePayCardParserTest(unittest.TestCase):
    def test_parse_pipe_line_with_compact_expiry(self):
        card = parse_card_line("4242 4242 4242 4242|12/29|123|Amy Allen|1 Main St|New York|NY|10001|US")

        self.assertEqual(card["cardNumber"], "4242424242424242")
        self.assertEqual(card["expiryMonth"], "12")
        self.assertEqual(card["expiryYear"], "2029")
        self.assertEqual(card["cvv"], "123")
        self.assertEqual(card["nameOnCard"], "Amy Allen")
        self.assertEqual(card["billingAddress"], "1 Main St, New York, NY, 10001, US")

    def test_parse_pipe_line_with_split_expiry(self):
        card = parse_card_line("5555555555554444|01|2030|987|Bob Lee")

        self.assertEqual(card["cardNumber"], "5555555555554444")
        self.assertEqual(card["expiryMonth"], "01")
        self.assertEqual(card["expiryYear"], "2030")
        self.assertEqual(card["cvv"], "987")
        self.assertEqual(card["nameOnCard"], "Bob Lee")

    def test_normalize_rejects_invalid_card_number(self):
        with self.assertRaises(ValueError):
            normalize_card_info({"cardNumber": "123", "expiry": "12/29", "cvv": "123"})

    def test_normalize_card988_content(self):
        card = normalize_card988_content({
            "card_number": "4242424242424242",
            "expiry_date": "2030/1",
            "cvv": "123",
            "phone": "+15550101010",
            "sms_api": "http://example.test/api/get_sms?key=redacted",
            "name": "Test User",
            "address": "1 Main St,New York 10001,US",
        })

        self.assertEqual(card["cardNumber"], "4242424242424242")
        self.assertEqual(card["expiryMonth"], "01")
        self.assertEqual(card["expiryYear"], "2030")
        self.assertEqual(card["cvv"], "123")
        self.assertEqual(card["nameOnCard"], "Test User")
        self.assertEqual(card["billingAddress"], "1 Main St, New York, 10001, US")
        self.assertEqual(card["smsApi"], "http://example.test/api/get_sms?key=redacted")

    def test_card988_get_sms_ignores_empty_response(self):
        class Resp:
            status_code = 200
            text = "no|暂无验证码|到期时间：2026-05-29 00:00:00"

        class Requests:
            @staticmethod
            def get(*args, **kwargs):
                return Resp()

        import stripe_pay
        old_requests = stripe_pay.requests
        try:
            stripe_pay.requests = Requests
            self.assertIsNone(card988_get_sms("http://example.test/api?key=x", log=lambda *_: None))
        finally:
            stripe_pay.requests = old_requests

    def test_card988_get_sms_extracts_otp(self):
        class Resp:
            status_code = 200
            text = "ok|验证码：654321|到期时间：2026-05-29 00:00:00"

        class Requests:
            @staticmethod
            def get(*args, **kwargs):
                return Resp()

        import stripe_pay
        old_requests = stripe_pay.requests
        try:
            stripe_pay.requests = Requests
            self.assertEqual(
                card988_get_sms("http://example.test/api?key=x", log=lambda *_: None)["otp"],
                "654321",
            )
        finally:
            stripe_pay.requests = old_requests


if __name__ == "__main__":
    unittest.main()
