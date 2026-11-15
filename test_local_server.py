import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server


class FakeBackendHandler(BaseHTTPRequestHandler):
    received_payloads = []

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_json(200, {
            "configuredProviders": 3,
            "availableProviders": ["Gemini"],
            "available": True,
        })

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        self.received_payloads.append(json.loads(self.rfile.read(length).decode("utf-8")))
        self.send_json(200, {"text": "Test reply", "provider": "Gemini"})


class LocalProxyTests(unittest.TestCase):
    def setUp(self):
        self.original_url = os.environ.get("UHH_BACKEND_URL")
        FakeBackendHandler.received_payloads.clear()
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackendHandler)
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        os.environ["UHH_BACKEND_URL"] = "http://127.0.0.1:%s" % self.backend.server_address[1]

        self.app = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        self.app_thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.app_thread.start()
        self.base_url = "http://127.0.0.1:%s" % self.app.server_address[1]

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.backend.shutdown()
        self.backend.server_close()
        self.app_thread.join(timeout=5)
        self.backend_thread.join(timeout=5)
        if self.original_url is None:
            os.environ.pop("UHH_BACKEND_URL", None)
        else:
            os.environ["UHH_BACKEND_URL"] = self.original_url

    def test_status_reports_backend_provider_health(self):
        with urllib.request.urlopen(self.base_url + "/api/status", timeout=5) as response:
            status = json.loads(response.read().decode("utf-8"))
        self.assertTrue(status["configured"])
        self.assertTrue(status["available"])
        self.assertEqual(status["availableProviders"], ["Gemini"])

    def test_chat_is_forwarded_without_provider_keys(self):
        payload = {
            "messages": [{"role": "user", "content": "Hello"}],
            "mode": "casual",
            "systemInstruction": "Be clear.",
        }
        request = urllib.request.Request(
            self.base_url + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.base_url,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertEqual(result["text"], "Test reply")
        self.assertEqual(FakeBackendHandler.received_payloads, [payload])
        self.assertNotIn("apiKey", FakeBackendHandler.received_payloads[0])

    def test_app_page_has_no_new_image_controls(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertNotIn("Make an image", page)
        self.assertIn("Terms of Use &amp; Privacy Notice", page)

    def test_dotenv_loads_only_the_public_backend_url(self):
        names = ("UHH_BACKEND_URL", "GEMINI_API_KEY")
        previous = {name: os.environ.pop(name, None) for name in names}
        try:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, ".env").write_text(
                    "UHH_BACKEND_URL=https://example.invalid\n"
                    "GEMINI_API_KEY=must-not-be-loaded\n",
                    encoding="utf-8",
                )
                with patch.object(server, "APP_DIR", Path(directory)):
                    server.load_dotenv()
            self.assertEqual(os.environ.get("UHH_BACKEND_URL"), "https://example.invalid")
            self.assertNotIn("GEMINI_API_KEY", os.environ)
        finally:
            for name in names:
                os.environ.pop(name, None)
                if previous[name] is not None:
                    os.environ[name] = previous[name]


if __name__ == "__main__":
    unittest.main()
