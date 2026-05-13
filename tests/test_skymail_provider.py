import unittest

from mail_providers.skymail import SkyMailProvider


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.requests = []

    def request(self, method, url, headers=None, json=None, timeout=None, verify=None):
        self.requests.append({
            "method": method,
            "url": url,
            "headers": headers or {},
            "json": json or {},
        })
        if url.endswith("/api/public/genToken"):
            return FakeResponse({"code": 200, "message": "success", "data": {"token": "token-1"}})
        if url.endswith("/api/public/addUser"):
            return FakeResponse({"code": 200, "message": "success", "data": None})
        if url.endswith("/api/public/emailList"):
            return FakeResponse({
                "code": 200,
                "message": "success",
                "data": [{
                    "emailId": 10,
                    "subject": "Verification code",
                    "text": "Your verification code is 123456",
                    "content": "",
                    "createTime": "2026-05-13 12:00:00",
                    "type": 0,
                    "isDel": 0,
                }],
            })
        raise AssertionError(f"Unexpected URL: {url}")


class SkyMailProviderTest(unittest.TestCase):
    def test_create_mailbox_uses_generated_token_and_adds_user(self):
        session = FakeSession()
        provider = SkyMailProvider(
            base_url="https://mail.example.com",
            admin_email="admin@example.com",
            admin_password="secret",
            domain_id="example.com",
            session=session,
        )

        address = provider.create_mailbox()

        self.assertTrue(address.endswith("@example.com"))
        self.assertEqual(session.requests[0]["url"], "https://mail.example.com/api/public/genToken")
        self.assertEqual(session.requests[0]["json"], {"email": "admin@example.com", "password": "secret"})
        self.assertEqual(session.requests[1]["url"], "https://mail.example.com/api/public/addUser")
        self.assertEqual(session.requests[1]["headers"]["Authorization"], "token-1")
        self.assertEqual(session.requests[1]["json"], {"list": [{"email": address}]})

    def test_wait_otp_queries_inbox_and_extracts_code(self):
        session = FakeSession()
        provider = SkyMailProvider(
            base_url="https://mail.example.com",
            token="token-1",
            domain="example.com",
            session=session,
        )
        provider.address = "user@example.com"

        code = provider.wait_otp(timeout=1, poll_interval=0.01)

        self.assertEqual(code, "123456")
        self.assertEqual(session.requests[-1]["url"], "https://mail.example.com/api/public/emailList")
        self.assertEqual(session.requests[-1]["headers"]["Authorization"], "token-1")
        self.assertEqual(session.requests[-1]["json"]["toEmail"], "user@example.com")

    def test_list_domains_uses_domain_or_admin_email_suffix(self):
        provider = SkyMailProvider(admin_email="admin@example.com", session=FakeSession())
        self.assertEqual(provider.list_domains(), [{"id": "example.com", "domain": "example.com"}])


if __name__ == "__main__":
    unittest.main()
