#!/usr/bin/env python3
"""Close CRM proxy — reporting API, no pagination, fast. python3 close_proxy.py"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.parse import parse_qs
import base64, json, os, threading, time, webbrowser
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

API_KEY = "YOUR_CLOSE_API_KEY_HERE"  # Settings → API Keys → generate one
PORT = 8765
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN = base64.b64encode(f"{API_KEY}:".encode()).decode()
BASE = "https://api.close.com/api/v1"

_me_cache = {}

def req_get(path):
    r = Request(f"{BASE}{path}", headers={"Authorization": f"Basic {TOKEN}", "Accept": "application/json"})
    with urlopen(r, timeout=30) as res: return json.loads(res.read())

def req_post(path, body):
    d = json.dumps(body).encode()
    r = Request(f"{BASE}{path}", data=d, headers={"Authorization": f"Basic {TOKEN}", "Content-Type": "application/json", "Accept": "application/json"})
    with urlopen(r, timeout=30) as res: return json.loads(res.read())

def get_me():
    if not _me_cache:
        _me_cache.update(req_get("/me/"))
    return _me_cache

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): print(f"  {args[1]} {args[0]}")
    def do_OPTIONS(self): self.send_response(200); self._cors(); self.end_headers()
    def do_GET(self):
        if self.path in ("/", "/index.html"): self._serve("close_dashboard_proxy.html")
        elif self.path.startswith("/api/dashboard"): self._dashboard()
        elif self.path.startswith("/api/calls"):     self._calls_page()
        elif self.path.startswith("/api/metrics"):   self._metrics()
        elif self.path.startswith("/api/"):          self._proxy()
        else: self.send_response(404); self.end_headers()

    def _serve(self, fn):
        fp = os.path.join(SCRIPT_DIR, fn)
        try:
            data = open(fp, "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors(); self.end_headers(); self.wfile.write(data)
        except FileNotFoundError: self.send_response(404); self.end_headers()

    def _dashboard(self):
        qs = parse_qs(self.path.split('?',1)[1]) if '?' in self.path else {}
        days = int(qs.get('days', ['30'])[0])
        today_only = qs.get('today', ['0'])[0] == '1'
        now = datetime.utcnow()
        if today_only:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now - timedelta(days=days)
        start_str = start.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_str   = now.strftime('%Y-%m-%dT23:59:59Z')
        print(f"\n  Fetching dashboard: {days}d ({start.strftime('%Y-%m-%d')} -> today)")
        try:
            me = get_me()
            my_id = me.get("id", "")
            dr = {"start": start_str, "end": end_str}

            with ThreadPoolExecutor(max_workers=4) as ex:
                f_users    = ex.submit(req_get, "/user/?_limit=100")
                f_comp     = ex.submit(req_post, "/report/activity/", {
                    "type": "comparison", "datetime_range": dr,
                    "metrics": ["calls.outbound.all.count", "calls.inbound.all.count",
                                "calls.all.all.count", "calls.all.all.sum_duration",
                                "calls.all.all.avg_duration"]
                })
                f_overview = ex.submit(req_post, "/report/activity/", {
                    "type": "overview", "datetime_range": dr,
                    "metrics": ["calls.outbound.all.count", "calls.inbound.all.count",
                                "calls.all.all.count", "calls.all.all.sum_duration",
                                "calls.all.all.avg_duration"]
                })
                users_r    = f_users.result()
                comp_r     = f_comp.result()
                overview_r = f_overview.result()

            users = {u["id"]: f"{u.get('first_name','')} {u.get('last_name','')}".strip()
                     for u in (users_r.get("data") or [])}

            body = json.dumps({
                "me":       {"id": my_id, "name": users.get(my_id, "You"),
                             "org": (me.get("organizations") or [{}])[0].get("name","")},
                "users":      users,
                "comparison": comp_r,
                "overview":   overview_r,
                "since":      start.strftime('%Y-%m-%d'),
                "start_str":  start_str,
                "end_str":    end_str,
                "days":       days
            }).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self._cors(); self.end_headers(); self.wfile.write(body)
            print(f"  Done (no calls — loaded progressively).")
        except HTTPError as e:
            b = e.read(); print(f"  !! {e.code}: {b[:300]}")
            self.send_response(e.code); self._cors(); self.end_headers(); self.wfile.write(b)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.send_response(500); self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _calls_page(self):
        """Progressive calls endpoint: /api/calls?skip=N&days=D"""
        qs = parse_qs(self.path.split('?',1)[1]) if '?' in self.path else {}
        days = int(qs.get('days', ['30'])[0])
        skip = int(qs.get('skip', ['0'])[0])
        now = datetime.utcnow()
        start = now - timedelta(days=days)
        start_str = start.strftime('%Y-%m-%dT00:00:00Z')
        end_str   = now.strftime('%Y-%m-%dT23:59:59Z')
        try:
            me = get_me()
            my_id = me.get("id", "")
            result = req_get(
                f"/activity/call/?user_id={my_id}"
                f"&date_created__gt={start_str}&date_created__lt={end_str}"
                f"&_limit=100&_skip={skip}"
            )
            calls = result.get("data") or []

            # Fetch lead names for meaningful calls on this page in parallel
            meaningful_here = [c for c in calls if (c.get("duration") or 0) >= 300]
            lead_ids = list({c["lead_id"] for c in meaningful_here if c.get("lead_id")})
            lead_names = {}
            def fetch_lead(lid):
                try:
                    l = req_get(f"/lead/{lid}/?_fields=id,display_name,name")
                    return lid, l.get("display_name") or l.get("name") or lid
                except: return lid, lid
            if lead_ids:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    for lid, name in ex.map(fetch_lead, lead_ids):
                        lead_names[lid] = name

            body = json.dumps({
                "calls":      calls,
                "lead_names": lead_names,
                "has_more":   len(calls) == 100,
                "skip":       skip,
            }).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self._cors(); self.end_headers(); self.wfile.write(body)
            print(f"  calls page skip={skip}: {len(calls)} calls, {len(lead_names)} leads")
        except HTTPError as e:
            b = e.read()
            self.send_response(e.code); self._cors(); self.end_headers(); self.wfile.write(b)
        except Exception as e:
            import traceback; traceback.print_exc()
            self.send_response(500); self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _metrics(self):
        try:
            result = req_get("/report/activity/metrics/")
            body = json.dumps(result).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self._cors(); self.end_headers(); self.wfile.write(body)
        except HTTPError as e:
            b = e.read()
            self.send_response(e.code); self._cors(); self.end_headers(); self.wfile.write(b)
        except Exception as e:
            self.send_response(500); self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _proxy(self):
        path = self.path[4:]
        try:
            r = Request(f"{BASE}{path}", headers={"Authorization": f"Basic {TOKEN}", "Accept": "application/json"})
            with urlopen(r, timeout=30) as res: raw = res.read()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self._cors(); self.end_headers(); self.wfile.write(raw)
        except HTTPError as e:
            b = e.read(); self.send_response(e.code); self._cors(); self.end_headers(); self.wfile.write(b)
        except Exception as e:
            self.send_response(500); self._cors(); self.end_headers()
            self.wfile.write(json.dumps({"error":str(e)}).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.send_header("Access-Control-Allow-Methods","GET,OPTIONS")

def open_browser():
    time.sleep(1.2); webbrowser.open(f"http://localhost:{PORT}/")

if __name__ == "__main__":
    print(f"\nProxy -> http://localhost:{PORT}  (Ctrl+C to stop)")
    print()
    threading.Thread(target=open_browser, daemon=True).start()
    try: HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
