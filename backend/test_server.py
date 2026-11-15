import unittest
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import server


def provider(name):
    return {"name": name, "key": name.lower() + "-test-key", "model": name.lower() + "-test-model", "kind": "openai"}


class ProviderFallbackTests(unittest.TestCase):
    def setUp(self):
        self.providers = [provider("Gemini"), provider("Groq"), provider("OpenRouter")]
        server._cooldowns.clear()
        server._health_cache.update({"at": 0.0, "value": None})

    def test_tries_providers_in_order_until_one_succeeds(self):
        with patch.object(server, "provider_settings", return_value=self.providers), patch.object(
            server,
            "provider_reply",
            side_effect=[
                server.ProviderFailure("Gemini", "quota", True, True),
                "A working answer",
            ],
        ) as reply:
            result = server.complete_chat([{"role": "user", "content": "Hello"}], "Be helpful.", "casual")

        self.assertEqual(result, ("A working answer", "Groq"))
        self.assertEqual([call.args[0]["name"] for call in reply.call_args_list], ["Gemini", "Groq"])

    def test_reports_outage_only_when_all_providers_are_unavailable(self):
        failure = server.ProviderFailure("provider", "rate limited", True, True)
        with patch.object(server, "provider_settings", return_value=self.providers), patch.object(
            server, "provider_reply", side_effect=[failure, failure, failure]
        ):
            with self.assertRaises(server.AllProvidersUnavailable):
                server.complete_chat([{"role": "user", "content": "Hello"}], "", "casual")

    def test_does_not_bypass_a_safety_refusal_with_provider_fallback(self):
        with patch.object(server, "provider_settings", return_value=self.providers), patch.object(
            server, "provider_reply", side_effect=server.SafetyRefusal()
        ) as reply:
            with self.assertRaises(server.SafetyRefusal):
                server.complete_chat([{"role": "user", "content": "Unsafe request"}], "", "casual")
        reply.assert_called_once()

    def test_invalid_key_is_not_misreported_as_quota_outage(self):
        failure = server.ProviderFailure("provider", "invalid key", True, False)
        with patch.object(server, "provider_settings", return_value=self.providers), patch.object(
            server, "provider_reply", side_effect=[failure, failure, failure]
        ):
            with self.assertRaises(server.ProviderConfigurationError):
                server.complete_chat([{"role": "user", "content": "Hello"}], "", "casual")

    def test_previous_model_messages_remain_in_chat_context(self):
        messages, instruction, mode = server.clean_chat_payload({
            "messages": [
                {"role": "user", "content": "First"},
                {"role": "model", "content": "Earlier reply"},
                {"role": "user", "content": "Next"},
            ],
            "systemInstruction": "Be clear.",
            "mode": "expert",
        })

        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], "Earlier reply")
        self.assertEqual((instruction, mode), ("Be clear.", "expert"))


class BackendHttpTests(unittest.TestCase):
    def test_all_provider_limits_return_the_requested_outage_message(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BackendHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        providers = [provider("Gemini"), provider("Groq"), provider("OpenRouter")]
        failure = server.ProviderFailure("provider", "rate limited", True, True)
        body = json.dumps({
            "messages": [{"role": "user", "content": "Hello"}],
            "systemInstruction": "Be helpful.",
            "mode": "casual",
        }).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:%s/v1/chat" % httpd.server_address[1],
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-For": "198.51.100.27",
            },
            method="POST",
        )
        try:
            with patch.object(server, "provider_settings", return_value=providers), patch.object(
                server, "provider_reply", side_effect=[failure, failure, failure]
            ):
                with self.assertRaises(urllib.error.HTTPError) as response:
                    urllib.request.urlopen(request, timeout=5)
            self.assertEqual(response.exception.code, 503)
            payload = json.loads(response.exception.read().decode("utf-8"))
            self.assertEqual(payload["error"], server.OUTAGE_MESSAGE)
            self.assertTrue(payload["allProvidersUnavailable"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_serves_the_chat_website_and_its_assets(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BackendHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base_url = "http://127.0.0.1:%s" % httpd.server_address[1]
        try:
            for path, content_type in (
                ("/", "text/html"),
                ("/styles.css", "text/css"),
                ("/app.js", "text/javascript"),
            ):
                with urllib.request.urlopen(base_url + path, timeout=5) as response:
                    body = response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn(content_type, response.headers["Content-Type"])
                    self.assertTrue(body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_website_status_endpoint_matches_the_frontend_contract(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BackendHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, "available_providers", return_value={
                "configuredProviders": 1,
                "availableProviders": ["Gemini"],
                "available": True,
            }):
                with urllib.request.urlopen(
                    "http://127.0.0.1:%s/api/status" % httpd.server_address[1],
                    timeout=5,
                ) as response:
                    status = json.loads(response.read().decode("utf-8"))
            self.assertEqual(status, {
                "configured": True,
                "available": True,
                "availableProviders": ["Gemini"],
                "reason": "ready",
            })
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_website_chat_endpoint_uses_the_shared_backend_provider(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BackendHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        body = json.dumps({
            "messages": [{"role": "user", "content": "Hello"}],
            "systemInstruction": "Be helpful.",
            "mode": "casual",
        }).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:%s/api/chat" % httpd.server_address[1],
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-For": "198.51.100.28",
            },
            method="POST",
        )
        try:
            with patch.object(server, "provider_settings", return_value=[provider("Gemini")]), patch.object(
                server, "provider_reply", return_value="Website reply"
            ):
                with urllib.request.urlopen(request, timeout=5) as response:
                    result = json.loads(response.read().decode("utf-8"))
            self.assertEqual(result["text"], "Website reply")
            self.assertEqual(result["provider"], "Gemini")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
