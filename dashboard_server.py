"""Dashboard server - serves dashboard.html + JSON API endpoints."""
import http.server
import socketserver
import json
import os
import glob
import urllib.request

PORT = 8877
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Handle each request in a separate thread."""
    daemon_threads = True
    allow_reuse_address = True


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Custom handler that reads files manually to avoid encoding issues."""

    def do_GET(self):
        try:
            if self.path == "/" or self.path == "/dashboard.html":
                self._serve_file("dashboard.html", "text/html")
            elif self.path == "/api/last_result":
                self._serve_json_file("last_result.json")
            elif self.path == "/api/forward_test":
                self._serve_json_file("forward_test.json")
            elif self.path == "/api/history":
                self._serve_history()
            elif self.path == "/api/window_trades":
                self._serve_json_file("window_trades.json")
            elif self.path == "/api/optimizer":
                self._serve_json_file("optimizer_dashboard.json")
            elif self.path.startswith("/api/btc_price"):
                self._proxy_btc_price()
            else:
                self.send_error(404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def _serve_file(self, filename, content_type):
        path = os.path.join(BASE_DIR, filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "File not found")

    def _serve_json_file(self, filename):
        path = os.path.join(BASE_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._send_json(data)
        except FileNotFoundError:
            self._send_json({"error": filename + " not found"}, 404)
        except json.JSONDecodeError:
            self._send_json({"error": filename + " parse error"}, 500)

    def _serve_history(self):
        history_dir = os.path.join(BASE_DIR, "history")
        experiments = []
        if os.path.isdir(history_dir):
            files = sorted(glob.glob(os.path.join(history_dir, "exp_*.json")))
            for fp in files:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        experiments.append(json.load(f))
                except Exception:
                    pass
        self._send_json(experiments)

    def _proxy_btc_price(self):
        try:
            url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _send_json(self, data, code=200):
        try:
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    print("Dashboard: http://localhost:" + str(PORT))
    httpd = ThreadedHTTPServer(("", PORT), DashboardHandler)
    httpd.serve_forever()
