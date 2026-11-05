from unittest.mock import patch, Mock
from django.test import SimpleTestCase, RequestFactory
from frontend.api import APIClient
from frontend.views import _private_conversation_messages


class PrivateMediaTransportTests(SimpleTestCase):
    def test_http_raw_headers_are_preserved(self):
        request = RequestFactory().get("/")
        request.session = {}
        client = APIClient(request)
        with patch.object(
            client,
            "_send",
            return_value=(200, b"file bytes", {"Content-Type": "audio/webm"}),
        ):
            body, mime, headers = client.get_file(
                "/conversations/example/media/example/"
            )
        self.assertEqual(mime, "audio/webm")
        self.assertEqual(body, b"file bytes")

    def test_backend_urls_become_session_authenticated_same_origin_routes(self):
        cid = "12345678-1234-4234-8234-123456789abc"
        aid = "23456789-1234-4234-8234-123456789abc"
        payload = {
            "results": [
                {
                    "attachments": [
                        {
                            "id": aid,
                            "url": "https://backend.invalid/private-media",
                            "content_type": "audio/webm",
                        },
                        {"id": aid, "url": None},
                    ]
                }
            ]
        }
        result = _private_conversation_messages(payload, cid)
        self.assertTrue(
            result["results"][0]["attachments"][0]["url"].startswith(
                "/discover/conversations/"
            )
        )
        self.assertIsNone(result["results"][0]["attachments"][1]["url"])
