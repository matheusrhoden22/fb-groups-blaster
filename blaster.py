"""
FB Groups Blaster — busca grupos no Facebook por palavra-chave, clica em
Participar em todos, e posta uma mensagem em cada grupo.

Usa o AdsPower perfil 16 (k17pnv2n) que ja esta logado no Facebook.

Uso:
  # 1) Varrer busca e participar de todos os grupos
  python blaster.py join --query "contabilidade" --scrolls 50

  # 2) Postar mensagem em todos os grupos salvos
  python blaster.py post --msg "Ola, oferecemos X..."
  python blaster.py post --msg-file mensagem.txt

  # 3) Fazer os dois (participa + posta)
  python blaster.py full --query "contabilidade" --scrolls 50 --msg-file msg.txt

  # 4) Ver a lista de grupos salvos
  python blaster.py list

Args globais:
  --min-delay / --max-delay   range de delay aleatorio entre posts (default 60-180s)
  --limit N                   limitar a N grupos (teste)
  --dry-run                   nao clica, so mostra o que faria

IMPORTANTE: postar em muitos grupos com mesma mensagem em sequencia e
padrao de spam. O Facebook bloqueia/bane rapidinho. Use com cabeca:
- textos variados (a skill aceita templates com variacoes)
- grupos segmentados
- limites baixos (5-10 posts por dia no inicio)
"""
import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import websocket

ADSPOWER_API = "http://local.adspower.net:50325"
USER_ID = "k17pnv2n"
OUT_DIR = Path(r"C:\Users\Matheus\fb-groups-blaster")
GROUPS_FILE = OUT_DIR / "grupos.json"
LOG_FILE = OUT_DIR / "log.json"


# ---------- AdsPower / CDP boilerplate (mesmo padrao do marketplace.py) ----------
def get_json(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_for_api(max_seconds=30):
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            if get_json(f"{ADSPOWER_API}/status").get("code") == 0:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def is_profile_active(user_id):
    r = get_json(f"{ADSPOWER_API}/api/v1/browser/active?user_id={user_id}")
    return r.get("code") == 0 and r.get("data", {}).get("status") == "Active", r.get("data", {})


def debug_port_alive(port):
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_profile(user_id):
    r = get_json(f"{ADSPOWER_API}/api/v1/browser/start?user_id={user_id}&open_tabs=1")
    if r.get("code") != 0:
        raise RuntimeError(f"Falha ao iniciar perfil: {r.get('msg')}")
    return r["data"]


def ensure_browser():
    if not wait_for_api(30):
        raise RuntimeError("AdsPower API nao respondeu. Abra o AdsPower.")
    active, data = is_profile_active(USER_ID)
    port = data.get("debug_port") if active else None
    if not (active and debug_port_alive(port)):
        print("[*] Iniciando perfil 16 no AdsPower...")
        data = start_profile(USER_ID)
        port = data.get("debug_port")
        if not port:
            ws = (data.get("ws", {}) or {}).get("puppeteer", "")
            if ws:
                port = ws.split(":")[2].split("/")[0]
        # o navegador do AdsPower pode demorar pra subir — espera ate 45s
        deadline = time.time() + 45
        while time.time() < deadline and not debug_port_alive(port):
            time.sleep(2)
            if not port:
                _, d2 = is_profile_active(USER_ID)
                port = d2.get("debug_port")
    if not debug_port_alive(port):
        raise RuntimeError(f"Porta de debug {port} nao esta acessivel.")
    print(f"[*] Perfil 16 no ar (porta {port}).")
    return port


def list_tabs(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def open_tab(port, target_url):
    encoded = urllib.parse.quote(target_url, safe="")
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{encoded}", method="PUT")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/new?{encoded}", timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))


def navigate_tab(port, tab, target_url):
    """Navega uma aba existente pra nova URL (mantem o WS)."""
    ws_url = tab["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
    try:
        cdp_send(ws, "Page.enable")
        cdp_send(ws, "Page.navigate", {"url": target_url}, msg_id=99)
    finally:
        ws.close()


def cdp_send(ws, method, params=None, msg_id=1, timeout=120):
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ws.settimeout(max(1, deadline - time.time()))
            resp = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            raise TimeoutError(f"CDP {method} timeout")
        if resp.get("id") == msg_id:
            return resp


def eval_js(ws, expr, timeout=180):
    """Avalia JS async (await) e retorna o valor JSON."""
    resp = cdp_send(ws, "Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True,
        "returnByValue": True,
        "timeout": timeout * 1000,
    }, msg_id=random.randint(1000, 999999), timeout=timeout + 5)
    result = resp.get("result", {}).get("result", {})
    if result.get("subtype") == "error":
        raise RuntimeError(result.get("description", "JS error"))
    return result.get("value")


# ---------- Fase 1: JOIN ----------
JOIN_JS = r"""
(async () => {
  const SCROLLS = __SCROLLS__;
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const collected = new Map();   // url -> {name, url, membros, posts}
  let clicked = 0;

  const collectCards = () => {
    // cards de grupo na busca: link /groups/<id-ou-slug>
    const links = document.querySelectorAll('a[href*="/groups/"]');
    for (const a of links) {
      const href = a.href.split('?')[0];
      const m = href.match(/\/groups\/([^/]+)\/?$/);
      if (!m) continue;
      const slug = m[1];
      if (['feed','create','search','discover','joins','my_groups'].includes(slug)) continue;
      const card = a.closest('div[role="article"]') || a.closest('div');
      if (!card) continue;
      const txt = (card.innerText || '').replace(/\s+/g, ' ').trim();
      // precisa ter botao Participar no card pra ser um resultado de busca
      if (!/participar|join/i.test(txt)) continue;
      if (!collected.has(href)) {
        const name = (a.innerText || '').trim() || slug;
        const mem = (txt.match(/([\d.,]+\s*(mil|k|m|M)?)\s*membros?/i) || [])[1] || '';
        collected.set(href, { name, url: href, membros: mem });
      }
    }
  };

  const clickJoinButtons = async () => {
    // Botoes "Participar" (PT) ou "Join" (EN). Evita "Participa" de eventos.
    const btns = Array.from(document.querySelectorAll('div[role="button"],button'))
      .filter(b => {
        const t = (b.innerText || '').trim();
        if (!/^(Participar|Join)$/i.test(t)) return false;
        // botao precisa estar visivel
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
    for (const b of btns) {
      try {
        b.scrollIntoView({block: 'center'});
        await sleep(120);
        b.click();
        clicked++;
        await sleep(250 + Math.random() * 400);
      } catch (e) {}
    }
    // fecha dialogs de confirmacao que possam abrir
    await sleep(600);
    const confirms = Array.from(document.querySelectorAll('div[role="dialog"] div[role="button"]'))
      .filter(b => /participar|enviar|join|submit/i.test((b.innerText || '').trim()));
    for (const c of confirms) {
      try { c.click(); await sleep(200); } catch (e) {}
    }
    // fecha "X" de qualquer dialog pendente
    const closes = Array.from(document.querySelectorAll('div[role="dialog"] div[aria-label="Fechar"],div[role="dialog"] div[aria-label="Close"]'));
    for (const c of closes) { try { c.click(); } catch (e) {} }
  };

  for (let i = 0; i < SCROLLS; i++) {
    collectCards();
    await clickJoinButtons();
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(1800 + Math.random() * 700);
  }
  collectCards();

  return { count: collected.size, clicked, groups: Array.from(collected.values()) };
})()
"""


def run_join(query, scrolls, dry_run=False):
    port = ensure_browser()
    search_url = (
        "https://www.facebook.com/search/groups/?q="
        + urllib.parse.quote(query)
    )
    print(f"[*] Abrindo busca: {search_url}")
    open_tab(port, search_url)
    time.sleep(6)

    # acha a aba
    tabs = [t for t in list_tabs(port)
            if t.get("type") == "page" and "search/groups" in t.get("url", "")]
    if not tabs:
        raise RuntimeError("Nao achei a aba de busca de grupos.")
    tab = tabs[0]

    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
    try:
        cdp_send(ws, "Page.enable")
        cdp_send(ws, "Runtime.enable")
        time.sleep(3)

        if dry_run:
            js = JOIN_JS.replace("__SCROLLS__", str(scrolls)).replace(
                "b.click();", "// b.click();"
            ).replace("c.click(); await sleep(200);", "")
        else:
            js = JOIN_JS.replace("__SCROLLS__", str(scrolls))

        print(f"[*] Scrollando e clicando em Participar ({scrolls} passes)...")
        data = eval_js(ws, js, timeout=max(300, scrolls * 10))
    finally:
        ws.close()

    groups = data.get("groups", [])
    print(f"[+] Coletados {len(groups)} grupos. Cliques em Participar: {data.get('clicked')}")

    # merge com lista ja existente
    existing = load_groups()
    by_url = {g["url"]: g for g in existing}
    for g in groups:
        by_url[g["url"]] = {**by_url.get(g["url"], {}), **g}
    save_groups(list(by_url.values()))
    print(f"[+] Lista total salva: {len(by_url)} grupos em {GROUPS_FILE}")
    return list(by_url.values())


# ---------- Fase 2: POST ----------
POST_JS = r"""
(async () => {
  const MSG = __MSG__;
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // espera feed do grupo carregar
  for (let i = 0; i < 20; i++) {
    if (document.querySelector('div[role="main"]')) break;
    await sleep(500);
  }
  await sleep(1500);

  // 1) clica na barra "Escreva algo..." / "Write something..."
  const triggers = Array.from(document.querySelectorAll('div[role="button"],div[role="textbox"]'))
    .filter(el => {
      const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
      return /escreva algo|write something|publicar algo|create post/i.test(t);
    });
  if (!triggers.length) return { ok: false, step: 'trigger_not_found' };
  triggers[0].scrollIntoView({block:'center'});
  await sleep(500);
  triggers[0].click();
  await sleep(2500);

  // 2) acha o textbox dentro do dialog
  const box = document.querySelector('div[role="dialog"] div[role="textbox"][contenteditable="true"]')
           || document.querySelector('div[role="textbox"][contenteditable="true"]');
  if (!box) return { ok: false, step: 'textbox_not_found' };
  box.focus();
  await sleep(300);

  // digita caractere por caractere (FB trava com insertText de uma vez)
  const lines = MSG.split('\n');
  for (let li = 0; li < lines.length; li++) {
    if (li > 0) {
      // quebra de linha via Shift+Enter
      document.execCommand('insertLineBreak');
    }
    document.execCommand('insertText', false, lines[li]);
    await sleep(50);
  }
  await sleep(1200);

  // 3) clica em "Publicar" / "Post"
  let postBtn = null;
  const dialog = document.querySelector('div[role="dialog"]');
  const scope = dialog || document;
  const candidates = Array.from(scope.querySelectorAll('div[role="button"],button'));
  const POST_RE = /^(Publicar|Postar|Post)$/i;
  for (const c of candidates) {
    const txt = (c.innerText||'').trim();
    const lbl = (c.getAttribute('aria-label')||'').trim();
    if (POST_RE.test(txt) || POST_RE.test(lbl)) {
      const dis = c.getAttribute('aria-disabled') === 'true' || c.disabled;
      if (!dis) { postBtn = c; break; }
    }
  }
  if (!postBtn) return { ok: false, step: 'post_button_not_found' };
  postBtn.click();

  // espera dialog fechar (sinal de sucesso)
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    if (!document.querySelector('div[role="dialog"] div[role="textbox"][contenteditable="true"]')) {
      return { ok: true };
    }
  }
  return { ok: false, step: 'dialog_did_not_close' };
})()
"""


def run_post(message, limit=None, min_delay=60, max_delay=180, dry_run=False):
    groups = load_groups()
    if not groups:
        print("[!] Nenhum grupo em grupos.json. Rode 'join' primeiro.")
        return
    if limit:
        groups = groups[:limit]

    print(f"[*] Vou tentar postar em {len(groups)} grupos.")
    print(f"[*] Delay entre posts: {min_delay}-{max_delay}s (aleatorio)")
    print(f"[*] Mensagem ({len(message)} chars):")
    print("    " + message.replace("\n", "\n    "))
    if not dry_run:
        resp = input("\nConfirma? [s/N] ").strip().lower()
        if resp != "s":
            print("[!] Cancelado.")
            return

    port = ensure_browser()
    log = load_log()

    for idx, g in enumerate(groups, 1):
        url = g["url"].rstrip("/")
        print(f"\n[{idx}/{len(groups)}] {g.get('name','?')}  {url}")
        if dry_run:
            print("   [dry-run] pularia post")
            continue

        try:
            open_tab(port, url)
            time.sleep(7)
            tabs = [t for t in list_tabs(port)
                    if t.get("type") == "page" and url.split("facebook.com")[1] in t.get("url","")]
            if not tabs:
                print("   [!] Aba nao achada")
                log.append({"url": url, "ok": False, "error": "tab_not_found",
                            "ts": datetime.now().isoformat()})
                save_log(log)
                continue
            tab = tabs[0]

            ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
            try:
                cdp_send(ws, "Page.enable")
                cdp_send(ws, "Runtime.enable")
                js = POST_JS.replace("__MSG__", json.dumps(message))
                result = eval_js(ws, js, timeout=120)
            finally:
                ws.close()

            ok = bool(result and result.get("ok"))
            step = (result or {}).get("step")
            print(f"   {'[+] OK' if ok else '[!] FALHA'}  {step or ''}")
            log.append({"url": url, "name": g.get("name"), "ok": ok, "step": step,
                        "ts": datetime.now().isoformat()})
            save_log(log)
        except Exception as e:
            print(f"   [!] Erro: {e}")
            log.append({"url": url, "ok": False, "error": str(e),
                        "ts": datetime.now().isoformat()})
            save_log(log)

        if idx < len(groups):
            wait = random.randint(min_delay, max_delay)
            print(f"   [zzz] aguardando {wait}s antes do proximo...")
            time.sleep(wait)

    ok_count = sum(1 for e in log[-len(groups):] if e.get("ok"))
    print(f"\n[+] Feito. Sucesso: {ok_count}/{len(groups)}. Log: {LOG_FILE}")


# ---------- persistencia ----------
def load_groups():
    if not GROUPS_FILE.exists():
        return []
    return json.loads(GROUPS_FILE.read_text(encoding="utf-8"))


def save_groups(groups):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_FILE.write_text(
        json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_log():
    if not LOG_FILE.exists():
        return []
    return json.loads(LOG_FILE.read_text(encoding="utf-8"))


def save_log(log):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cmd_list():
    gs = load_groups()
    if not gs:
        print("Nenhum grupo salvo.")
        return
    for i, g in enumerate(gs, 1):
        print(f"{i:3d}. {g.get('name','?')}   [{g.get('membros','')}]   {g.get('url')}")
    print(f"\nTotal: {len(gs)}")


# ---------- CLI ----------
def main():
    p = argparse.ArgumentParser(description="FB Groups Blaster — join e post em massa")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_join = sub.add_parser("join", help="busca grupos + clica em Participar")
    p_join.add_argument("--query", "-q", required=True)
    p_join.add_argument("--scrolls", "-s", type=int, default=50)
    p_join.add_argument("--dry-run", action="store_true")

    p_post = sub.add_parser("post", help="posta mensagem nos grupos salvos")
    g = p_post.add_mutually_exclusive_group(required=True)
    g.add_argument("--msg")
    g.add_argument("--msg-file")
    p_post.add_argument("--limit", type=int)
    p_post.add_argument("--min-delay", type=int, default=60)
    p_post.add_argument("--max-delay", type=int, default=180)
    p_post.add_argument("--dry-run", action="store_true")

    p_full = sub.add_parser("full", help="join + post (roda os dois)")
    p_full.add_argument("--query", "-q", required=True)
    p_full.add_argument("--scrolls", "-s", type=int, default=50)
    g2 = p_full.add_mutually_exclusive_group(required=True)
    g2.add_argument("--msg")
    g2.add_argument("--msg-file")
    p_full.add_argument("--limit", type=int)
    p_full.add_argument("--min-delay", type=int, default=60)
    p_full.add_argument("--max-delay", type=int, default=180)
    p_full.add_argument("--wait-approval", type=int, default=300,
                        help="segundos de espera apos 'join' para grupos aprovarem (default 300)")

    sub.add_parser("list", help="lista grupos salvos")

    args = p.parse_args()

    if args.cmd == "join":
        run_join(args.query, args.scrolls, args.dry_run)
    elif args.cmd == "post":
        msg = args.msg if args.msg else Path(args.msg_file).read_text(encoding="utf-8")
        run_post(msg, args.limit, args.min_delay, args.max_delay, args.dry_run)
    elif args.cmd == "full":
        msg = args.msg if args.msg else Path(args.msg_file).read_text(encoding="utf-8")
        run_join(args.query, args.scrolls, dry_run=False)
        print(f"\n[*] Aguardando {args.wait_approval}s pra grupos publicos aprovarem auto...")
        time.sleep(args.wait_approval)
        run_post(msg, args.limit, args.min_delay, args.max_delay, dry_run=False)
    elif args.cmd == "list":
        cmd_list()


if __name__ == "__main__":
    main()
