import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))
HOST = "127.0.0.1"
MAX_BODY_SIZE = 1_000_000
BACKEND_TIMEOUT = 90
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def load_dotenv():
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name == "UHH_BACKEND_URL" and name not in os.environ:
            os.environ[name] = value


def backend_url():
    configured_url = os.environ.get("UHH_BACKEND_URL", "").strip()
    if not configured_url:
        url_file = APP_DIR / "backend_url.txt"
        if url_file.exists():
            configured_url = url_file.read_text(encoding="utf-8").strip()
    if not configured_url:
        return ""
    parsed = urlparse(configured_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return ""
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        return ""
    return configured_url.rstrip("/")


def backend_request(path, payload=None):
    base_url = backend_url()
    if not base_url:
        raise RuntimeError("The _UHH backend URL has not been configured.")
    headers = {"Accept": "application/json"}
    body = None
    method = "GET"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=BACKEND_TIMEOUT) as response:
            response_body = response.read()
            return response.status, json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {"error": "The _UHH backend returned an unreadable response."}
        return error.code, data
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError(
            "The _UHH backend could not be reached. It may be starting up; please try again shortly."
        ) from None


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/status":
            if not backend_url():
                self.send_json(200, {
                    "configured": False,
                    "available": False,
                    "reason": "backend_not_configured",
                    "availableProviders": [],
                })
                return
            try:
                status_code, result = backend_request("/health")
                if status_code != 200:
                    raise RuntimeError("The _UHH backend is temporarily unavailable.")
                self.send_json(200, {
                    "configured": result.get("configuredProviders", 0) > 0,
                    "available": bool(result.get("available")),
                    "availableProviders": result.get("availableProviders", []),
                    "reason": "ready" if result.get("available") else "providers_unavailable",
                })
            except RuntimeError as error:
                self.send_json(200, {
                    "configured": True,
                    "available": False,
                    "reason": "backend_unreachable",
                    "availableProviders": [],
                    "error": str(error),
                })
            return
        static_file = STATIC_FILES.get(path)
        if not static_file:
            self.send_error(404, "Not found")
            return
        filename, content_type = static_file
        try:
            body = (RESOURCE_DIR / filename).read_bytes()
        except OSError:
            self.send_error(500, "Could not read app file")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request size.") from None
        if content_length <= 0 or content_length > MAX_BODY_SIZE:
            raise ValueError("Request is empty or too large.")
        raw_body = self.rfile.read(content_length)
        try:
            return json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request must contain valid JSON.") from None

    def origin_is_local(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in ("localhost", "127.0.0.1")
            and parsed.port == self.server.server_address[1]
        )

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if not self.origin_is_local():
                self.send_json(403, {"error": "Requests must come from this local _UHH app."})
                return
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise ValueError("Request must be a JSON object.")
            if path == "/api/chat":
                self.handle_chat(payload)
            else:
                self.send_json(404, {"error": "Not found."})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except RuntimeError as error:
            message = str(error)
            status = 503 if "isn't configured" in message else 502
            self.send_json(status, {"error": message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            print(f"Request failed: {error}")
            self.send_json(500, {"error": "Something went wrong in _UHH. Please try again."})

    def handle_chat(self, payload):
        if not backend_url():
            self.send_json(503, {
                "code": "backend_not_configured",
                "error": "The _UHH AI service hasn't been connected yet.",
            })
            return
        status_code, result = backend_request("/v1/chat", payload)
        self.send_json(status_code, result)

def main():
    load_dotenv()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((HOST, port), AppHandler)
    app_url = f"http://localhost:{port}"
    print(f"_UHH is ready at {app_url}")
    if not backend_url():
        print("Set UHH_BACKEND_URL in .env or add a backend_url.txt next to UHH.exe.")
    webbrowser.open(app_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping _UHH.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
