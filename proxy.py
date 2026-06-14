import socket
import threading
import os
import sys
import datetime

# ─────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────
PROXY_HOST = "0.0.0.0"
PROXY_PORT = 8080

SERVER_HOST = "127.0.0.1"   # Alamat web server (local)
SERVER_PORT = 8000          # port web server

UDP_HOST = "0.0.0.0"
UDP_PORT = 9091

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache") # Direktori untuk menyimpan cache

SERVER_TIMEOUT = 5          # Timeout (second) 


# ─────────────────────────────────────────
#  INISIALISASI CACHE
# ─────────────────────────────────────────
os.makedirs(CACHE_DIR, exist_ok=True)

# Lock untuk mencegah race condition saat tulis/baca cache
cache_lock = threading.Lock()


# ─────────────────────────────────────────
#  HELPER: LOGGING (OTOMATIS SIMPAN)
# ─────────────────────────────────────────
def log(tag, message):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{now}] [{tag}] {message}"
    print(log_entry)
    
    # Buat folder 'logs' jika belum ada bray
    os.makedirs("logs", exist_ok=True)
    
    try:
        # Simpan di dalam folder logs/
        with open(os.path.join("logs", "log_proxy.txt"), "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"Gagal menulis ke log file: {e}")

# ─────────────────────────────────────────
#  HELPER: KONVERSI URL PATH → NAMA FILE CACHE
# ─────────────────────────────────────────
def path_to_cache_filename(path):
    safe = path.lstrip("/").replace("/", "_")
    if not safe:
        safe = "index.html"
    return os.path.join(CACHE_DIR, safe)


# ─────────────────────────────────────────
#  HELPER: CEK CACHE
# ─────────────────────────────────────────
def get_from_cache(path):
    """Return bytes isi cache jika ada, None jika tidak."""
    cache_file = path_to_cache_filename(path)
    with cache_lock:
        if os.path.exists(cache_file):
            with open(cache_file, "rb") as f:
                return f.read()
    return None


# ─────────────────────────────────────────
#  HELPER: SIMPAN KE CACHE
# ─────────────────────────────────────────
def save_to_cache(path, data):
    """Simpan response bytes ke file cache."""
    cache_file = path_to_cache_filename(path)
    with cache_lock:
        with open(cache_file, "wb") as f:
            f.write(data)


# ─────────────────────────────────────────
#  HELPER: FORWARD REQUEST KE WEB SERVER
# ─────────────────────────────────────────
def forward_to_server(raw_request):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(SERVER_TIMEOUT)
        s.connect((SERVER_HOST, SERVER_PORT))
        s.sendall(raw_request)

        response = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                response += chunk
            except socket.timeout:
                break

        s.close()

        if not response:
            return None, "Empty response from server"
        return response, None

    except socket.timeout:
        return None, "timeout"
    except ConnectionRefusedError:
        return None, "connection refused"
    except Exception as e:
        return None, str(e)


# ─────────────────────────────────────────
#  HELPER: BANGUN ERROR RESPONSE
# ─────────────────────────────────────────
def build_error_response(status_code, status_text, message=""):
    body = f"<h1>{status_code} {status_text}</h1><p>{message}</p>".encode()
    header = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    return header.encode() + body


# ─────────────────────────────────────────
#  HANDLE SATU CLIENT
# ─────────────────────────────────────────
def handle_client(conn, addr):
    client_ip = addr[0]
    start_time = datetime.datetime.now()

    try:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk

        if not raw:
            return

        try:
            text = raw.decode("utf-8", errors="replace")
            request_line = text.split("\r\n")[0]
            parts = request_line.split(" ")
            method = parts[0]
            path   = parts[1] if len(parts) > 1 else "/"
        except Exception:
            conn.sendall(build_error_response(400, "Bad Request", "Malformed HTTP request"))
            log("PROXY", f"{client_ip} - 400 Bad Request")
            return

        if method != "GET":
            conn.sendall(build_error_response(405, "Method Not Allowed"))
            log("PROXY", f"{client_ip} {method} {path} - 405")
            return

        log("PROXY", f"{client_ip} GET {path}")

        # ── CEK CACHE ──
        cached = get_from_cache(path)
        if cached:
            elapsed = (datetime.datetime.now() - start_time).total_seconds() * 1000
            conn.sendall(cached)
            log("CACHE", f"HIT  | {path} | {len(cached)} bytes | {elapsed:.1f}ms")
            return

        # ── CACHE MISS → forward ke web server ──
        log("CACHE", f"MISS | {path} → forwarding ke server")

        clean_request = f"GET {path} HTTP/1.1\r\nHost: {SERVER_HOST}:{SERVER_PORT}\r\nConnection: close\r\n\r\n"
        response, error = forward_to_server(clean_request.encode())

        if error:
            if "timeout" in error:
                conn.sendall(build_error_response(504, "Gateway Timeout", f"Web server tidak merespons: {error}"))
                log("PROXY", f"{client_ip} GET {path} - 504 Gateway Timeout ({error})")
            else:
                conn.sendall(build_error_response(502, "Bad Gateway", f"Error dari server: {error}"))
                log("PROXY", f"{client_ip} GET {path} - 502 Bad Gateway ({error})")
            return

        try:
            response_text = response.split(b"\r\n")[0].decode("utf-8", errors="replace")
            status_code = int(response_text.split(" ")[1])
        except Exception:
            status_code = 0

        if status_code == 200:
            save_to_cache(path, response)
            log("CACHE", f"STORED | {path} | {len(response)} bytes")
        else:
            log("PROXY", f"Tidak di-cache (status {status_code})")

        elapsed = (datetime.datetime.now() - start_time).total_seconds() * 1000
        conn.sendall(response)
        log("PROXY", f"{client_ip} GET {path} - {status_code} | {elapsed:.1f}ms")

    except Exception as e:
        log("PROXY", f"Error handling {client_ip}: {e}")
        try:
            conn.sendall(build_error_response(500, "Internal Proxy Error", str(e)))
        except Exception:
            pass
    finally:
        conn.close()


# ─────────────────────────────────────────
#  UDP LISTENER (QoS Pass-through)
# ─────────────────────────────────────────
def start_udp_listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((UDP_HOST, UDP_PORT))
    log("UDP", f"QoS Listener aktif di port {UDP_PORT}")

    while True:
        try:
            data, addr = server.recvfrom(1024)
            log("UDP", f"Diterima {len(data)} bytes dari {addr[0]}:{addr[1]} | payload: {data.decode('utf-8', errors='replace')}")
            # Pantulkan kembali ke client (echo)
            server.sendto(data, addr)
            log("UDP", f"Echo {len(data)} bytes → {addr[0]}:{addr[1]}")
        except Exception as e:
            log("UDP", f"Error: {e}")


# ─────────────────────────────────────────
#  MAIN: PROXY SERVER
# ─────────────────────────────────────────
def start_proxy():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((PROXY_HOST, PROXY_PORT))
    server.listen(50)
    log("PROXY", f"Listening on port {PROXY_PORT}")
    log("PROXY", f"Forwarding ke Web Server {SERVER_HOST}:{SERVER_PORT}")
    log("PROXY", f"Cache dir: {CACHE_DIR}")

    while True:
        try:
            conn, addr = server.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
            log("PROXY", f"Koneksi baru dari {addr[0]} — thread spawned (active: {threading.active_count()-1})")
        except Exception as e:
            log("PROXY", f"Accept error: {e}")


if __name__ == "__main__":
    try:
        # UDP listener di thread terpisah
        udp_thread = threading.Thread(target=start_udp_listener, daemon=True)
        udp_thread.start()

        start_proxy()
    except KeyboardInterrupt:
        log("PROXY", "Proxy dihentikan.")
        sys.exit(0)