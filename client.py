import socket
import time
import sys
import datetime
import math
import os
import threading

# ─────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────
PROXY_HOST = "127.0.0.1"   
PROXY_PORT = 8080

SERVER_HOST = "127.0.0.1"  
UDP_PORT_SERVER = 9090
UDP_PORT_PROXY  = 9091

UDP_PACKET_COUNT = 10       
UDP_TIMEOUT = 1.0           
TCP_DEFAULT_PATH = "/HTML/index.html"  

# Lock untuk mencegah output bertabrakan saat multi-request
print_lock = threading.Lock()

# ─────────────────────────────────────────
#  HELPER: LOGGING (OTOMATIS SIMPAN)
# ─────────────────────────────────────────
def log(tag, message):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{now}] [{tag}] {message}"
    
    with print_lock:
        print(log_entry)
        os.makedirs("logs", exist_ok=True)
        try:
            with open(os.path.join("logs", "log_client.txt"), "a", encoding="utf-8") as f:
                f.write(log_entry + "\n")
        except Exception as e:
            print(f"Gagal menulis ke log file: {e}")

def save_output_to_file(text_block):
    with print_lock:
        os.makedirs("logs", exist_ok=True)
        try:
            with open(os.path.join("logs", "hasil_client.txt"), "a", encoding="utf-8") as f:
                f.write(text_block + "\n")
        except Exception as e:
            print(f"Gagal menyimpan output ke file: {e}")

# ─────────────────────────────────────────
#  MODE TCP — HTTP CLIENT (CLIENT 1)
# ─────────────────────────────────────────
def mode_tcp(path, thread_id=""):
    tag = f"TCP {thread_id}".strip()
    log(tag, f"Mengirim GET {path} ke proxy {PROXY_HOST}:{PROXY_PORT}")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((PROXY_HOST, PROXY_PORT))

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {PROXY_HOST}:{PROXY_PORT}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        t_send = time.time()
        s.sendall(request.encode("utf-8"))

        response = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                response += chunk
            except socket.timeout:
                break

        t_recv = time.time()
        s.close()

        rtt_ms = (t_recv - t_send) * 1000

        if not response:
            log(tag, "Tidak ada response dari proxy.")
            return

        if b"\r\n\r\n" in response:
            header_part, body_part = response.split(b"\r\n\r\n", 1)
            header_text = header_part.decode("utf-8", errors="replace")
            status_line = header_text.split("\r\n")[0]
        else:
            header_text = ""
            status_line = "(tidak ada header)"
            body_part = response

        output_tcp = f"""
{"═" * 60}
  [{tag}] STATUS  : {status_line}
  [{tag}] RTT     : {rtt_ms:.2f} ms
  [{tag}] UKURAN  : {len(response)} bytes (header + body)
{"═" * 60}
── HEADER ──
{header_text}
── BODY (500 karakter pertama) ──
{body_part[:500].decode("utf-8", errors="replace")}
{"─" * 60}
"""
        with print_lock:
            print(output_tcp)
        save_output_to_file(output_tcp)

    except ConnectionRefusedError:
        log(tag, f"Koneksi ditolak — pastikan proxy berjalan di {PROXY_HOST}:{PROXY_PORT}")
    except socket.timeout:
        log(tag, "Timeout — proxy tidak merespons dalam 10 detik")
    except Exception as e:
        log(tag, f"Error: {e}")

# ─────────────────────────────────────────
#  MODE TCP MULTI — KONKUREN (CLIENT 2)
# ─────────────────────────────────────────
def mode_tcp_multi(path, num_requests):
    log("TCP_MULTI", f"Memulai {num_requests} request konkuren secara bersamaan ke {PROXY_HOST}:{PROXY_PORT}...")
    
    threads = []
    for i in range(1, num_requests + 1):
        t = threading.Thread(target=mode_tcp, args=(path, f"(Req-{i})"))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    log("TCP_MULTI", "Semua request konkuren selesai dieksekusi.")

# ─────────────────────────────────────────
#  MODE UDP — QoS PINGER
# ─────────────────────────────────────────
def mode_udp(target_host=None, count=None, target_port=None):
    host  = target_host or SERVER_HOST
    n     = count or UDP_PACKET_COUNT
    port = target_port or UDP_PORT_SERVER

    log("UDP", f"Memulai QoS ping ke {host}:{port} — {n} paket")
    print()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(UDP_TIMEOUT)

    rtt_list      = []  
    lost          = 0
    total_payload = 0
    t_start       = time.time()

    for seq in range(1, n + 1):
        timestamp = time.time()
        payload   = f"Ping {seq} {timestamp}".encode("utf-8")
        total_payload += len(payload)

        try:
            s.sendto(payload, (host, port))
            t_send = time.time()

            data, _ = s.recvfrom(1024)
            t_recv  = time.time()

            rtt_ms = (t_recv - t_send) * 1000
            rtt_list.append(rtt_ms)

            echo_ok = (data == payload)
            status  = "OK" if echo_ok else "ECHO MISMATCH"
            
            loop_log = f"  Paket {seq:>3}: RTT = {rtt_ms:7.3f} ms  [{status}]"
            with print_lock:
                print(loop_log)
            save_output_to_file(loop_log) 

        except socket.timeout:
            lost += 1
            loop_log = f"  Paket {seq:>3}: Request timed out"
            with print_lock:
                print(loop_log)
            save_output_to_file(loop_log)

        time.sleep(0.1)

    t_end    = time.time()
    duration = t_end - t_start
    s.close()

    received   = len(rtt_list)
    loss_pct   = (lost / n) * 100
    throughput = (total_payload * 8) / duration / 1000  

    if received > 0:
        rtt_min = min(rtt_list)
        rtt_avg = sum(rtt_list) / received
        rtt_max = max(rtt_list)

        if received > 1:
            diffs   = [abs(rtt_list[i] - rtt_list[i-1]) for i in range(1, received)]
            mean_d  = sum(diffs) / len(diffs)
            variance = sum((d - mean_d) ** 2 for d in diffs) / len(diffs)
            jitter  = math.sqrt(variance)
        else:
            jitter = 0.0
    else:
        rtt_min = rtt_avg = rtt_max = jitter = 0.0

    output_udp = f"""
{"═" * 60}
  QoS STATISTIK — {host}:{port}
{"═" * 60}
  Paket dikirim   : {n}
  Paket diterima  : {received}
  Packet Loss     : {loss_pct:.1f}%
  RTT min         : {rtt_min:.3f} ms
  RTT avg         : {rtt_avg:.3f} ms
  RTT max         : {rtt_max:.3f} ms
  Jitter          : {jitter:.3f} ms
  Throughput      : {throughput:.3f} kbps
{"═" * 60}
"""
    with print_lock:
        print(output_udp)
    save_output_to_file(output_udp)

# ─────────────────────────────────────────
#  HELPER: USAGE
# ─────────────────────────────────────────
def print_usage():
    print("""
Cara penggunaan:
  Client 1 (Single Request):
    python client.py -mode tcp
  Client 2 (Multi Request/Konkuren):
    python client.py -mode tcp_multi -n 10
  Mode UDP (QoS):
    python client.py -mode udp
""")

if __name__ == "__main__":
    args = sys.argv[1:]

    def get_arg(flag, default=None):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
        return default

    mode = get_arg("-mode", "").lower()
    path = get_arg("-path", TCP_DEFAULT_PATH)
    host = get_arg("-host", None)
    count_str = get_arg("-count", None)
    
    # Untuk multi request
    n_multi_str = get_arg("-n", "5")
    try:
        n_multi = int(n_multi_str)
    except ValueError:
        n_multi = 5

    try:
        count = int(count_str) if count_str and count_str.isdigit() else None
    except ValueError:
        count = None

    target = get_arg("-target", "server") 

    if mode == "tcp":
        mode_tcp(path)
    elif mode == "tcp_multi":
        mode_tcp_multi(path, n_multi)
    elif mode == "udp":
        if target == "proxy":
            mode_udp(target_host=PROXY_HOST, count=count, target_port=UDP_PORT_PROXY)
        else:
            mode_udp(target_host=SERVER_HOST, count=count, target_port=UDP_PORT_SERVER)
    else:
        print_usage()