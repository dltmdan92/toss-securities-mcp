"""One-use, loopback-only credential form. Never logs form values."""
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs

from vault import Vault
import sys

ROOT = Path(__file__).resolve().parent
NONCE = secrets.token_urlsafe(32)
PATH = "/setup/" + secrets.token_urlsafe(24)


def page(content, title="토스증권 MCP 연결"):
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>" + title + "</title><style>"
            "*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#17212f;font:15px/1.6 system-ui,sans-serif}"
            "main{max-width:540px;margin:40px auto;padding:32px;background:white;border-radius:20px}"
            "h1{font-size:26px;line-height:1.3;margin:8px 0 16px}p{color:#596577}"
            "label{display:block;font-weight:600;margin-top:18px}input:not([type=checkbox]){width:100%;"
            "padding:12px;border:1px solid #ccd3df;border-radius:9px;margin-top:7px;font:inherit}"
            "button{background:#2478ff;color:white;border:0;border-radius:10px;padding:13px;width:100%;"
            "font:600 16px system-ui;margin-top:24px;cursor:pointer}.note{font-size:13px}.tag{color:#2478ff;font-weight:700}"
            "</style><main>" + content + "</main></html>").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalSetup"

    def log_message(self, *args):
        pass

    def send_page(self, content, status=200):
        body = page(content)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def valid_request(self):
        return self.path == PATH and self.headers.get("Host") == self.server.expected_host

    def do_GET(self):
        if not self.valid_request():
            self.send_page("<h1>페이지를 찾을 수 없습니다.</h1>", 404)
            return
        self.send_page("<span class='tag'>LOCAL SETUP</span><h1>토스증권 MCP 연결</h1>"
            f"<p>발급받은 API 키를 입력하세요. {'macOS 키체인' if sys.platform == 'darwin' else 'Windows 사용자 DPAPI'}에 안전하게 저장합니다.</p>"
            "<form method='post' autocomplete='off'>"
            f"<input type='hidden' name='csrf' value='{NONCE}'>"
            "<label>Client ID<input name='client_id' required spellcheck='false' autocomplete='off'></label>"
            "<label>Client Secret<input type='password' name='client_secret' required autocomplete='new-password'></label>"
            "<label>기본 accountSeq <small>(선택)</small><input name='account_seq' inputmode='numeric' placeholder='비워 두어도 됩니다'></label>"
            "<label><input type='checkbox' name='enable_trading' value='yes'> 실제 주문 기능 사용</label>"
            "<p class='note'>기본값은 조회만 허용합니다. 이 설정을 저장하는 것만으로 주문이 실행되지는 않습니다.</p>"
            "<button type='submit'>암호화하여 저장</button></form>"
            "<p class='note'>토스증권 WTS → 설정 → Open API → 허용 IP 관리에 현재 공인 IP도 등록해 주세요.</p>")

    def do_POST(self):
        expected_origin = "http://" + self.server.expected_host
        if not self.valid_request() or self.headers.get("Origin") != expected_origin:
            self.send_page("<h1>요청을 확인할 수 없습니다.</h1>", 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError()
            data = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=8)
            def field(name):
                values = data.get(name, [""])
                if len(values) != 1:
                    raise ValueError()
                return values[0].strip()
            if not secrets.compare_digest(field("csrf"), NONCE):
                self.send_page("<h1>요청을 확인할 수 없습니다.</h1>", 403)
                return
            client_id, client_secret, account = field("client_id"), field("client_secret"), field("account_seq")
            if not client_id or not client_secret or any(ord(c) < 32 for c in client_id + client_secret):
                raise ValueError()
            if account and (not re.fullmatch(r"[1-9][0-9]{0,18}", account) or int(account) > 9223372036854775807):
                raise ValueError()
            Vault(ROOT / ".local").write("credentials", {
                "client_id": client_id, "client_secret": client_secret,
                "account_seq": int(account) if account else "",
                "enable_trading": field("enable_trading") == "yes"})
        except Exception:
            self.send_page("<h1>저장하지 못했습니다.</h1><p>필수값과 계좌 식별값을 확인한 뒤 브라우저의 뒤로 가기로 다시 입력하세요.</p>", 400)
            return
        self.send_page("<span class='tag'>SAVED</span><h1>연결 설정을 저장했습니다.</h1>"
                       "<p>이 페이지를 닫아도 됩니다. 채팅에서 연결 확인을 진행할 수 있습니다.</p>")
        threading.Thread(target=self.server.shutdown, daemon=True).start()


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.expected_host = f"127.0.0.1:{server.server_port}"
    url = "http://" + server.expected_host + PATH
    print(json.dumps({"url": url}), flush=True)
    # The setup page automatically expires even if it is never submitted.
    timeout = threading.Timer(1200, server.shutdown)
    timeout.daemon = True
    timeout.start()
    try:
        server.serve_forever()
    finally:
        timeout.cancel()
        server.server_close()
