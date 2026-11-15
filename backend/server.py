import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
MAX_BODY_SIZE = 64_000
MAX_MESSAGE_LENGTH = 5_000
MAX_MESSAGES = 20
MAX_OUTPUT_TOKENS = 1_024
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_REQUESTS = 15
PROVIDER_TIMEOUT = 20
HEALTH_CACHE_SECONDS = 20
OUTAGE_MESSAGE = "SORRY!, _uhh is not working right now please try again later."
WEB_ROOT = Path(__file__).resolve().parent.parent
WEB_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class ProviderFailure(Exception):
    def __init__(self, provider, message, retryable, quota_or_outage=False, cooldown=30):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.quota_or_outage = quota_or_outage
        self.cooldown = cooldown


class SafetyRefusal(Exception):
    pass


class ClientRequestError(Exception):
    pass


class AllProvidersUnavailable(Exception):
    pass


class ProviderConfigurationError(Exception):
    pass


_cooldowns = {}
_cooldowns_lock = threading.Lock()
_health_cache = {"at": 0.0, "value": None}
_health_lock = threading.Lock()
_request_times = defaultdict(deque)
_rate_lock = threading.Lock()


def provider_settings():
    return [
        {
            "name": "Gemini",
            "key": os.environ.get("GEMINI_API_KEY", "").strip(),
            "model": os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip(),
            "kind": "gemini",
        },
        {
            "name": "Groq",
            "key": os.environ.get("GROQ_API_KEY", "").strip(),
            "model": os.environ.get("GROQ_MODEL", "").strip(),
            "kind": "openai",
        },
        {
            "name": "OpenRouter",
            "key": os.environ.get("OPENROUTER_API_KEY", "").strip(),
            "model": os.environ.get("OPENROUTER_MODEL", "openrouter/free").strip(),
            "kind": "openai",
        },
    ]


def load_json_response(response):
    try:
        return json.loads(response.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderFailure("provider", "The AI provider returned an unreadable response.", True, True) from None


def read_error_body(error):
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    details = payload.get("error", {})
    if isinstance(details, dict):
        return str(details.get("message", ""))[:500]
    return str(details)[:500]


def parse_retry_after(error, default=30):
    value = error.headers.get("Retry-After", "")
    try:
        return max(1, min(int(value), 900))
    except (TypeError, ValueError):
        return default


def make_provider_request(provider, messages, system_instruction, mode):
    if provider["kind"] == "gemini":
        contents = []
        for message in messages:
            contents.append({
                "role": "model" if message["role"] == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            })
        payload = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
        }
        if mode == "expert":
            payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "high"}
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + urllib.parse.quote(provider["model"], safe="-.")
            + ":generateContent"
        )
        headers = {"Content-Type": "application/json", "x-goog-api-key": provider["key"]}
    else:
        payload = {
            "model": provider["model"],
            "messages": [{"role": "system", "content": system_instruction}] + messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        url = "https://api.groq.com/openai/v1/chat/completions"
        if provider["name"] == "OpenRouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + provider["key"],
        }
        if provider["name"] == "OpenRouter":
            headers["X-OpenRouter-Title"] = "_UHH by Plunger Studios"
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def extract_text(provider, payload):
    if provider["kind"] == "gemini":
        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback", {})
            if feedback.get("blockReason"):
                raise SafetyRefusal()
            raise ProviderFailure(provider["name"], "No response was returned.", True, True)
        candidate = candidates[0]
        if candidate.get("finishReason") == "SAFETY":
            raise SafetyRefusal()
        parts = candidate.get("content", {}).get("parts", [])
        text = "\n".join(
            part.get("text", "") for part in parts if isinstance(part.get("text"), str)
        ).strip()
    else:
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderFailure(provider["name"], "No response was returned.", True, True)
        choice = choices[0]
        if choice.get("finish_reason") == "content_filter":
            raise SafetyRefusal()
        content = choice.get("message", {}).get("content", "")
        text = content.strip() if isinstance(content, str) else ""
    if not text:
        raise ProviderFailure(provider["name"], "The provider returned an empty reply.", True, True)
    return text


def provider_reply(provider, messages, system_instruction, mode):
    request = make_provider_request(provider, messages, system_instruction, mode)
    try:
        with urllib.request.urlopen(request, timeout=PROVIDER_TIMEOUT) as response:
            payload = load_json_response(response)
    except urllib.error.HTTPError as error:
        detail = read_error_body(error)
        if error.code == 400 and any(
            marker in detail.lower()
            for marker in ("safety", "blocked", "harmful", "prohibited content")
        ):
            raise SafetyRefusal() from None
        if error.code in (400, 413, 422):
            raise ClientRequestError(
                "That request couldn't be processed. Try shortening or rephrasing it."
            ) from None
        quota_or_outage = error.code in (402, 429) or 500 <= error.code <= 599
        cooldown = parse_retry_after(error, 300 if error.code == 402 else 30)
        if error.code in (401, 403):
            cooldown = 300
        with _cooldowns_lock:
            _cooldowns[provider["name"]] = time.monotonic() + cooldown
        raise ProviderFailure(
            provider["name"],
            detail or "The provider is unavailable.",
            retryable=True,
            quota_or_outage=quota_or_outage,
            cooldown=cooldown,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        with _cooldowns_lock:
            _cooldowns[provider["name"]] = time.monotonic() + 30
        raise ProviderFailure(
            provider["name"],
            "The provider could not be reached.",
            retryable=True,
            quota_or_outage=True,
            cooldown=30,
        ) from None
    return extract_text(provider, payload)


def clean_chat_payload(payload):
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ClientRequestError("Send a message to start chatting.")
    cleaned = []
    for message in messages[-MAX_MESSAGES:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant", "model") or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            cleaned.append({
                "role": "assistant" if role == "model" else role,
                "content": content[:MAX_MESSAGE_LENGTH],
            })
    if not cleaned or cleaned[-1]["role"] != "user":
        raise ClientRequestError("The last message must be from you.")
    instruction = payload.get("systemInstruction", "")
    if not isinstance(instruction, str):
        raise ClientRequestError("The assistant instructions must be text.")
    instruction = instruction[:2_000]
    mode = payload.get("mode", "casual")
    if mode not in ("casual", "formal", "expert"):
        mode = "casual"
    return cleaned, instruction, mode


def complete_chat(messages, instruction, mode):
    providers = [provider for provider in provider_settings() if provider["key"] and provider["model"]]
    if not providers:
        raise ProviderConfigurationError()

    failures = []
    for provider in providers:
        try:
            text = provider_reply(provider, messages, instruction, mode)
            with _cooldowns_lock:
                _cooldowns.pop(provider["name"], None)
            with _health_lock:
                _health_cache["value"] = None
            return text, provider["name"]
        except SafetyRefusal:
            raise
        except ClientRequestError:
            raise
        except ProviderFailure as error:
            failures.append(error)
            with _health_lock:
                _health_cache["value"] = None
            if not error.retryable:
                raise ProviderConfigurationError() from None

    if len(failures) == len(providers) and all(error.quota_or_outage for error in failures):
        raise AllProvidersUnavailable()
    raise ProviderConfigurationError()


def peer_address(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return handler.client_address[0]


def allow_request(client_address):
    now = time.monotonic()
    with _rate_lock:
        requests = _request_times[client_address]
        while requests and now - requests[0] >= RATE_LIMIT_WINDOW:
            requests.popleft()
        if len(requests) >= RATE_LIMIT_REQUESTS:
            return False
        requests.append(now)
        return True


def available_providers():
    now = time.monotonic()
    with _health_lock:
        cached = _health_cache["value"]
        if cached is not None and now - _health_cache["at"] < HEALTH_CACHE_SECONDS:
            return cached

    configured = [provider for provider in provider_settings() if provider["key"] and provider["model"]]
    healthy = []
    for provider in configured:
        with _cooldowns_lock:
            if _cooldowns.get(provider["name"], 0) > time.monotonic():
                continue
        if check_provider_model_endpoint(provider):
            healthy.append(provider["name"])

    result = {
        "configuredProviders": len(configured),
        "availableProviders": healthy,
        "available": bool(healthy),
    }
    with _health_lock:
        _health_cache["at"] = time.monotonic()
        _health_cache["value"] = result
    return result


def check_provider_model_endpoint(provider):
    if provider["kind"] == "gemini":
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = {"x-goog-api-key": provider["key"]}
    elif provider["name"] == "Groq":
        url = "https://api.groq.com/openai/v1/models"
        headers = {"Authorization": "Bearer " + provider["key"]}
    else:
        url = "https://openrouter.ai/api/v1/models"
        headers = {"Authorization": "Bearer " + provider["key"]}
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return False
        if provider["name"] == "Gemini":
            models = payload.get("models", [])
            model_name = "models/" + provider["model"]
            return any(
                item.get("name") == model_name
                and "generateContent" in item.get("supportedGenerationMethods", [])
                for item in models
            )
        models = payload.get("data", [])
        return any(item.get("id") == provider["model"] for item in models) or provider["name"] == "OpenRouter" and provider["model"] == "openrouter/free"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return False


class BackendHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in WEB_FILES:
            filename, content_type = WEB_FILES[path]
            try:
                body = (WEB_ROOT / filename).read_bytes()
            except OSError:
                self.send_json(500, {"error": "The _UHH website files could not be read."})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in ("/health", "/api/status"):
            self.send_json(404, {"error": "Not found."})
            return
        try:
            health = available_providers()
            if path == "/health":
                self.send_json(200, health)
            else:
                self.send_json(200, {
                    "configured": health["configuredProviders"] > 0,
                    "available": health["available"],
                    "availableProviders": health["availableProviders"],
                    "reason": "ready" if health["available"] else "providers_unavailable",
                })
        except (BrokenPipeError, ConnectionResetError):
            return

    def read_payload(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ClientRequestError("Invalid request size.") from None
        if size <= 0 or size > MAX_BODY_SIZE:
            raise ClientRequestError("Request is empty or too large.")
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ClientRequestError("Request must contain valid JSON.") from None
        if not isinstance(payload, dict):
            raise ClientRequestError("Request must be a JSON object.")
        return payload

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path not in ("/v1/chat", "/api/chat"):
            self.send_json(404, {"error": "Not found."})
            return
        if not allow_request(peer_address(self)):
            self.send_json(429, {
                "code": "client_rate_limited",
                "error": "Too many requests. Please wait a minute and try again.",
            })
            return
        try:
            messages, instruction, mode = clean_chat_payload(self.read_payload())
            providers = [provider for provider in provider_settings() if provider["key"] and provider["model"]]
            if not providers:
                self.send_json(503, {
                    "code": "providers_not_configured",
                    "error": "The _UHH AI service hasn't been configured yet.",
                })
                return

            try:
                text, provider = complete_chat(messages, instruction, mode)
                self.send_json(200, {"text": text, "provider": provider})
            except SafetyRefusal:
                self.send_json(200, {
                    "text": "I can't help with that request. Try rephrasing it in a safe, constructive way.",
                    "blocked": True,
                })
            except ClientRequestError as error:
                self.send_json(400, {"code": "invalid_request", "error": str(error)})
            except ProviderConfigurationError:
                self.send_json(502, {
                    "code": "provider_configuration_error",
                    "error": "The AI providers couldn't process this request. Check the backend's provider keys and model settings.",
                })
            except AllProvidersUnavailable:
                self.send_json(503, {
                    "code": "all_providers_unavailable",
                    "allProvidersUnavailable": True,
                    "error": OUTAGE_MESSAGE,
                })
        except ClientRequestError as error:
            self.send_json(400, {"code": "invalid_request", "error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            print("Chat request failed: %s" % error)
            self.send_json(500, {
                "code": "backend_error",
                "error": "Something went wrong in the _UHH service. Please try again.",
            })


def main():
    server = ThreadingHTTPServer((HOST, PORT), BackendHandler)
    print("_UHH backend listening on 0.0.0.0:%s" % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping _UHH backend.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
