"""
FB Groups Blaster — servidor web local + orquestrador dos jobs.

Sobe um HTTP server em http://localhost:5050 que serve a UI (index.html) e
expoe uma API JSON pra disparar jobs de join/post/recheck. Usa o AdsPower
perfil 16 (k17pnv2n) que ja esta logado no Facebook.

Uso:
    python server.py
"""
import json
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websocket

from blaster import (
    GROUPS_FILE, LOG_FILE, OUT_DIR,
    cdp_send, ensure_browser, list_tabs, load_groups, load_log, open_tab,
    save_groups, save_log,
)

PORT = 5050
HERE = Path(__file__).parent
STATE_LOCK = threading.Lock()
STATE = {
    "phase": "idle",
    "phase_msg": "aguardando",
    "join": {"running": False, "clicked": 0, "total": 0, "limit": 0, "query": ""},
    "post": {"running": False, "current": 0, "total": 0, "active_url": None,
             "active_name": None, "ok": 0, "fail": 0},
    "recheck": {"running": False, "current": 0, "total": 0},
    "groups": [],
    "events": [],
}
WORKER_TAB = {"id": None}


# ---------- util ----------
def log_event(msg):
    with STATE_LOCK:
        STATE["events"].append({"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg})
        STATE["events"] = STATE["events"][-250:]
    print(msg, flush=True)


def set_state(**kw):
    with STATE_LOCK:
        STATE.update(kw)


def sync_groups_from_disk():
    with STATE_LOCK:
        STATE["groups"] = load_groups()


def upsert_groups(new_items):
    existing = load_groups()
    by_url = {g["url"]: g for g in existing}
    for g in new_items:
        merged = {**by_url.get(g["url"], {}), **g}
        by_url[g["url"]] = merged
    save_groups(list(by_url.values()))
    sync_groups_from_disk()


# ---------- aba worker (reutilizada pra nao abrir 50 abas) ----------
def get_or_create_worker_tab(port, initial_url="about:blank"):
    tabs = list_tabs(port)
    if WORKER_TAB["id"]:
        for t in tabs:
            if t.get("id") == WORKER_TAB["id"] and t.get("type") == "page":
                return t
    resp = open_tab(port, initial_url)
    time.sleep(3)
    tabs = list_tabs(port)
    new_id = resp.get("id") if isinstance(resp, dict) else None
    if new_id:
        for t in tabs:
            if t.get("id") == new_id:
                WORKER_TAB["id"] = new_id
                return t
    # fallback: aba mais recente
    pages = [t for t in tabs if t.get("type") == "page"]
    if pages:
        WORKER_TAB["id"] = pages[0]["id"]
        return pages[0]
    raise RuntimeError("Sem aba de trabalho disponivel")


def navigate_worker(port, url):
    tab = get_or_create_worker_tab(port, url)
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
    try:
        cdp_send(ws, "Page.enable")
        cdp_send(ws, "Page.navigate", {"url": url}, msg_id=99)
    finally:
        ws.close()
    time.sleep(5)
    # re-pega a aba (pode ter mudado o webSocketDebuggerUrl apos navegar)
    tabs = list_tabs(port)
    for t in tabs:
        if t.get("id") == WORKER_TAB["id"]:
            return t
    return tab


# ---------- JS helpers ----------
JOIN_KICKOFF_JS = r"""
(() => {
  window.__bl_progress__ = { clicked: 0, total: 0, done: false, error: null, groups: [] };
  (async () => {
    try {
      const SCROLLS = __SCROLLS__;
      const LIMIT = __LIMIT__;
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const seen = new Map();
      let clicked = 0;

      const collectCards = () => {
        const out = [];
        const links = document.querySelectorAll('a[href*="/groups/"]');
        for (const a of links) {
          const href = a.href.split('?')[0];
          const m = href.match(/\/groups\/([^/]+)\/?$/);
          if (!m) continue;
          const slug = m[1];
          if (['feed','create','search','discover','joins','my_groups'].includes(slug)) continue;
          if (seen.has(href)) continue;
          const card = a.closest('div[role="article"]');
          if (!card) continue;
          const txt = (card.innerText || '').replace(/\s+/g, ' ').trim();
          const btn = Array.from(card.querySelectorAll('div[role="button"],button'))
            .find(b => /^(Participar|Join)$/i.test((b.innerText||'').trim()));
          if (!btn) continue;
          const name = (a.innerText||'').trim() || slug;
          const memMatch = txt.match(/([\d.,]+\s*(mil|k|m|M)?)\s*membros?/i);
          out.push({ href, name, membros: memMatch ? memMatch[1] : '', card, btn });
        }
        return out;
      };

      for (let s = 0; s < SCROLLS; s++) {
        if (clicked >= LIMIT) break;
        const cards = collectCards();
        for (const g of cards) {
          if (clicked >= LIMIT) break;
          if (seen.has(g.href)) continue;
          try {
            g.btn.scrollIntoView({block: 'center'});
            await sleep(250);
            g.btn.click();
            clicked++;
            await sleep(1100 + Math.random() * 500);

            // confirma/fecha dialog se abrir
            const dlg = document.querySelector('div[role="dialog"]');
            if (dlg) {
              const confirm = Array.from(dlg.querySelectorAll('div[role="button"]'))
                .find(b => /participar|enviar|join|submit/i.test((b.innerText||'').trim()));
              if (confirm) { confirm.click(); await sleep(800); }
              const close = dlg.querySelector('div[aria-label="Fechar"], div[aria-label="Close"]');
              if (close) { close.click(); await sleep(400); }
            }

            // re-classifica olhando o estado do card
            const relink = document.querySelector(`a[href="${g.href}"], a[href="${g.href}/"]`);
            const rcard = relink ? relink.closest('div[role="article"]') : null;
            const rtxt = (rcard ? rcard.innerText : '').toLowerCase();

            let status = 'member';
            if (/cancelar solicita|solicita..o pendente|cancel request|pending/i.test(rtxt)) {
              status = 'pending';
            } else if (rcard) {
              const stillJoin = Array.from(rcard.querySelectorAll('div[role="button"],button'))
                .some(b => /^(Participar|Join)$/i.test((b.innerText||'').trim()));
              if (stillJoin) status = 'not_joined';
            }

            seen.set(g.href, {
              url: g.href, name: g.name, membros: g.membros,
              status, canPost: status === 'member',
              joinedAt: new Date().toISOString()
            });
            window.__bl_progress__.clicked = clicked;
            window.__bl_progress__.total = seen.size;
            window.__bl_progress__.groups = Array.from(seen.values());
          } catch (e) {
            seen.set(g.href, {
              url: g.href, name: g.name, membros: g.membros,
              status: 'error', canPost: false, error: String(e),
              joinedAt: new Date().toISOString()
            });
          }
        }
        if (clicked >= LIMIT) break;
        window.scrollTo(0, document.body.scrollHeight);
        await sleep(1800 + Math.random() * 500);
      }

      window.__bl_progress__.groups = Array.from(seen.values());
      window.__bl_progress__.done = true;
    } catch (e) {
      window.__bl_progress__.error = String(e);
      window.__bl_progress__.done = true;
    }
  })();
  return true;
})()
"""


STATUS_CHECK_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 40; i++) {
    if (document.querySelector('div[role="main"]')) break;
    await sleep(400);
  }
  await sleep(1500);

  const hasWriteBox = Array.from(document.querySelectorAll('div[role="button"],div[role="textbox"]'))
    .some(el => /escreva algo|write something|publicar algo|create post/i
      .test(((el.innerText||'') + ' ' + (el.getAttribute('aria-label')||'')).trim()));

  const mainTxt = (document.querySelector('div[role="main"]') || document.body).innerText.toLowerCase();
  const pending = /cancelar solicita|solicita..o pendente|cancel request|request pending/.test(mainTxt);
  const joinVisible = Array.from(document.querySelectorAll('div[role="button"],button'))
    .some(el => /^(Participar|Join)$/i.test((el.innerText||'').trim()));

  let status = 'unknown';
  if (hasWriteBox) status = 'member';
  else if (pending) status = 'pending';
  else if (joinVisible) status = 'not_joined';

  return { status, canPost: hasWriteBox };
})()
"""


POST_JS = r"""
(async () => {
  const MSG = __MSG__;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 40; i++) {
    if (document.querySelector('div[role="main"]')) break;
    await sleep(400);
  }
  await sleep(1500);

  const triggers = Array.from(document.querySelectorAll('div[role="button"],div[role="textbox"]'))
    .filter(el => {
      const t = ((el.innerText||'') + ' ' + (el.getAttribute('aria-label')||'')).trim();
      return /escreva algo|write something|publicar algo|create post/i.test(t);
    });
  if (!triggers.length) return { ok: false, step: 'trigger_not_found' };
  triggers[0].scrollIntoView({block:'center'});
  await sleep(500);
  triggers[0].click();
  await sleep(2800);

  const box = document.querySelector('div[role="dialog"] div[role="textbox"][contenteditable="true"]')
           || document.querySelector('div[role="textbox"][contenteditable="true"]');
  if (!box) return { ok: false, step: 'textbox_not_found' };
  box.focus();
  await sleep(300);

  const lines = MSG.split('\n');
  for (let li = 0; li < lines.length; li++) {
    if (li > 0) document.execCommand('insertLineBreak');
    document.execCommand('insertText', false, lines[li]);
    await sleep(50);
  }
  await sleep(1200);

  const dialog = document.querySelector('div[role="dialog"]');
  const scope = dialog || document;
  let postBtn = null;
  for (const c of scope.querySelectorAll('div[role="button"],button')) {
    const t = ((c.innerText||'') + ' ' + (c.getAttribute('aria-label')||'')).trim();
    if (/^(Publicar|Post)$/i.test(t) && c.getAttribute('aria-disabled') !== 'true') {
      postBtn = c; break;
    }
  }
  if (!postBtn) return { ok: false, step: 'post_button_not_found' };
  postBtn.click();

  for (let i = 0; i < 25; i++) {
    await sleep(500);
    if (!document.querySelector('div[role="dialog"] div[role="textbox"][contenteditable="true"]')) {
      return { ok: true };
    }
  }
  return { ok: false, step: 'dialog_did_not_close' };
})()
"""


def eval_js_async(ws, expr, timeout=120):
    resp = cdp_send(ws, "Runtime.evaluate", {
        "expression": expr, "awaitPromise": True, "returnByValue": True,
        "timeout": timeout * 1000,
    }, msg_id=random.randint(1000, 9999999), timeout=timeout + 5)
    result = resp.get("result", {}).get("result", {})
    if result.get("subtype") == "error":
        raise RuntimeError(result.get("description", "JS error"))
    return result.get("value")


def eval_js_fire_and_poll(ws, kickoff_expr, poll_expr, is_done, on_tick,
                         poll_interval=2.0, timeout=900):
    """Dispara um job JS sem await (kickoff) e polla variavel global."""
    cdp_send(ws, "Runtime.evaluate", {
        "expression": kickoff_expr, "returnByValue": True,
    }, msg_id=random.randint(1000, 9999999), timeout=30)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = cdp_send(ws, "Runtime.evaluate", {
            "expression": poll_expr, "returnByValue": True,
        }, msg_id=random.randint(1000, 9999999), timeout=10)
        val = resp.get("result", {}).get("result", {}).get("value") or {}
        on_tick(val)
        if is_done(val):
            return val
        time.sleep(poll_interval)
    raise TimeoutError("poll timeout")


# ---------- JOBS ----------
def job_join(query, limit, scrolls=80, clear_before=True):
    try:
        if clear_before:
            save_groups([])
            sync_groups_from_disk()

        with STATE_LOCK:
            STATE["join"].update({"running": True, "clicked": 0, "total": 0,
                                  "limit": limit, "query": query})
            STATE["phase"] = "joining"
            STATE["phase_msg"] = f"buscando '{query}'..."
        log_event(f"[JOIN] iniciando — query='{query}' limite={limit}")

        port = ensure_browser()
        search_url = "https://www.facebook.com/search/groups/?q=" + urllib.parse.quote(query)
        navigate_worker(port, search_url)
        time.sleep(6)

        tab = get_or_create_worker_tab(port)
        ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        try:
            cdp_send(ws, "Page.enable")
            cdp_send(ws, "Runtime.enable")
            time.sleep(2)
            kickoff = (JOIN_KICKOFF_JS
                       .replace("__SCROLLS__", str(scrolls))
                       .replace("__LIMIT__", str(limit)))

            def tick(progress):
                with STATE_LOCK:
                    STATE["join"]["clicked"] = progress.get("clicked", 0)
                    STATE["join"]["total"] = progress.get("total", 0)
                    STATE["phase_msg"] = f"clicado em {progress.get('clicked',0)}/{limit}"
                # salva parciais
                groups = progress.get("groups") or []
                if groups:
                    upsert_groups(groups)

            final = eval_js_fire_and_poll(
                ws,
                kickoff_expr=kickoff,
                poll_expr="window.__bl_progress__",
                is_done=lambda v: bool(v.get("done")),
                on_tick=tick,
                poll_interval=1.5,
                timeout=60 * 20,
            )
        finally:
            ws.close()

        groups = final.get("groups") or []
        upsert_groups(groups)
        log_event(f"[JOIN] concluido — {final.get('clicked',0)} cliques, {len(groups)} grupos")
    except Exception as e:
        log_event(f"[JOIN] ERRO: {e}")
    finally:
        with STATE_LOCK:
            STATE["join"]["running"] = False
            STATE["phase"] = "idle"
            STATE["phase_msg"] = "pronto"


def job_recheck():
    try:
        groups = load_groups()
        if not groups:
            log_event("[RECHECK] nenhum grupo salvo")
            return
        with STATE_LOCK:
            STATE["recheck"].update({"running": True, "current": 0, "total": len(groups)})
            STATE["phase"] = "rechecking"
            STATE["phase_msg"] = "rechecando status..."
        log_event(f"[RECHECK] rodando pra {len(groups)} grupos")

        port = ensure_browser()
        for i, g in enumerate(groups, 1):
            url = g["url"].rstrip("/")
            with STATE_LOCK:
                STATE["recheck"]["current"] = i
                STATE["phase_msg"] = f"rechecando {i}/{len(groups)}: {g.get('name','?')}"
            try:
                navigate_worker(port, url)
                time.sleep(5)
                tab = get_or_create_worker_tab(port)
                ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
                try:
                    cdp_send(ws, "Page.enable")
                    cdp_send(ws, "Runtime.enable")
                    res = eval_js_async(ws, STATUS_CHECK_JS, timeout=40)
                finally:
                    ws.close()
                if res:
                    g["status"] = res.get("status", g.get("status", "unknown"))
                    g["canPost"] = bool(res.get("canPost"))
                    g["checkedAt"] = datetime.now().isoformat()
            except Exception as e:
                g["status"] = "error"
                g["canPost"] = False
                g["error"] = str(e)
            save_groups(groups)
            sync_groups_from_disk()
        log_event("[RECHECK] concluido")
    except Exception as e:
        log_event(f"[RECHECK] ERRO: {e}")
    finally:
        with STATE_LOCK:
            STATE["recheck"]["running"] = False
            STATE["phase"] = "idle"
            STATE["phase_msg"] = "pronto"


def job_post(message, urls, min_delay=60, max_delay=180):
    try:
        groups = load_groups()
        by_url = {g["url"]: g for g in groups}
        targets = [by_url[u] for u in urls if u in by_url]
        if not targets:
            log_event("[POST] nenhum alvo")
            return

        with STATE_LOCK:
            STATE["post"].update({"running": True, "current": 0, "total": len(targets),
                                  "active_url": None, "active_name": None, "ok": 0, "fail": 0})
            STATE["phase"] = "posting"
            STATE["phase_msg"] = f"vai postar em {len(targets)} grupos"
        log_event(f"[POST] iniciando — {len(targets)} grupos, delay {min_delay}-{max_delay}s")

        port = ensure_browser()
        log = load_log()

        for i, g in enumerate(targets, 1):
            url = g["url"].rstrip("/")
            with STATE_LOCK:
                STATE["post"].update({"current": i, "active_url": url,
                                      "active_name": g.get("name", "?")})
                STATE["phase_msg"] = f"postando {i}/{len(targets)}: {g.get('name','?')}"
            log_event(f"[POST {i}/{len(targets)}] {g.get('name','?')}  {url}")

            ok = False
            step = None
            err = None
            try:
                navigate_worker(port, url)
                time.sleep(6)
                tab = get_or_create_worker_tab(port)
                ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
                try:
                    cdp_send(ws, "Page.enable")
                    cdp_send(ws, "Runtime.enable")
                    js = POST_JS.replace("__MSG__", json.dumps(message))
                    res = eval_js_async(ws, js, timeout=120)
                finally:
                    ws.close()
                ok = bool(res and res.get("ok"))
                step = (res or {}).get("step")
            except Exception as e:
                err = str(e)

            g["lastPost"] = {"ok": ok, "step": step, "error": err,
                             "ts": datetime.now().isoformat()}
            # persiste no grupo principal tambem
            for gg in groups:
                if gg["url"] == g["url"]:
                    gg["lastPost"] = g["lastPost"]
            save_groups(groups)
            sync_groups_from_disk()
            log.append({"url": url, "name": g.get("name"), "ok": ok,
                        "step": step, "error": err,
                        "ts": datetime.now().isoformat()})
            save_log(log)
            with STATE_LOCK:
                if ok:
                    STATE["post"]["ok"] += 1
                else:
                    STATE["post"]["fail"] += 1

            if i < len(targets):
                wait = random.randint(min_delay, max_delay)
                log_event(f"[POST] aguardando {wait}s antes do proximo...")
                for _ in range(wait):
                    time.sleep(1)

        log_event(f"[POST] concluido — {STATE['post']['ok']} OK, {STATE['post']['fail']} falhas")
    except Exception as e:
        log_event(f"[POST] ERRO: {e}")
    finally:
        with STATE_LOCK:
            STATE["post"]["running"] = False
            STATE["phase"] = "idle"
            STATE["phase_msg"] = "pronto"


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencia access log padrao

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            data = Path(path).read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(HERE / "index.html", "text/html; charset=utf-8")
            return
        if self.path == "/api/state":
            with STATE_LOCK:
                self._send_json(200, STATE)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/join":
            body = self._read_json()
            query = (body.get("query") or "").strip()
            limit = int(body.get("limit") or 20)
            scrolls = int(body.get("scrolls") or 80)
            clear = bool(body.get("clear", True))
            if not query:
                self._send_json(400, {"error": "query vazia"})
                return
            if STATE["join"]["running"]:
                self._send_json(409, {"error": "join ja esta rodando"})
                return
            threading.Thread(target=job_join, args=(query, limit, scrolls, clear),
                             daemon=True).start()
            self._send_json(200, {"ok": True})
            return

        if self.path == "/api/post":
            body = self._read_json()
            msg = (body.get("message") or "").strip()
            urls = body.get("urls") or []
            mn = int(body.get("min_delay") or 60)
            mx = int(body.get("max_delay") or 180)
            if not msg or not urls:
                self._send_json(400, {"error": "message e urls obrigatorios"})
                return
            if STATE["post"]["running"]:
                self._send_json(409, {"error": "post ja esta rodando"})
                return
            threading.Thread(target=job_post, args=(msg, urls, mn, mx),
                             daemon=True).start()
            self._send_json(200, {"ok": True})
            return

        if self.path == "/api/recheck":
            if STATE["recheck"]["running"]:
                self._send_json(409, {"error": "recheck ja esta rodando"})
                return
            threading.Thread(target=job_recheck, daemon=True).start()
            self._send_json(200, {"ok": True})
            return

        if self.path == "/api/clear-log":
            save_log([])
            with STATE_LOCK:
                STATE["events"] = []
            self._send_json(200, {"ok": True})
            return

        if self.path == "/api/remove-group":
            body = self._read_json()
            url = body.get("url")
            groups = [g for g in load_groups() if g.get("url") != url]
            save_groups(groups)
            sync_groups_from_disk()
            self._send_json(200, {"ok": True})
            return

        self.send_error(404)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sync_groups_from_disk()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n  FB Groups Blaster rodando em {url}")
    print("  (Ctrl+C pra parar)\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  servidor parado.")


if __name__ == "__main__":
    main()
