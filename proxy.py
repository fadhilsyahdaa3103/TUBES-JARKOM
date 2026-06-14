import socket
import threading
import argparse
import os
import sys
import time
import datetime

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURASI DEFAULT
# ─────────────────────────────────────────────────────────────────────────────
PROXY_HOST      = "0.0.0.0"
PROXY_PORT      = 8080

MAIN_SERVER_TCP_PORT   = 8000
BACKUP_SERVER_TCP_PORT = 8001

CONNECT_TIMEOUT = 5      # detik — timeout saat coba konek ke server
RECV_TIMEOUT    = 15     # detik — timeout saat tunggu response dari server

CACHE_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_cache")

# ─────────────────────────────────────────────────────────────────────────────
# LOCK GLOBAL untuk operasi tulis cache (mencegah race condition)
# ─────────────────────────────────────────────────────────────────────────────
cache_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# UTILITAS
# ─────────────────────────────────────────────────────────────────────────────
def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(label: str, msg: str):
    print(f"[{timestamp()}] [{label}] {msg}", flush=True)


def path_to_cache_key(path: str) -> str:
    """
    Mengubah URL path menjadi nama file cache yang aman.
    Contoh: '/index.html' → 'index.html'
             '/'           → '_root_'
    """
    safe = path.strip("/").replace("/", "__")
    if not safe:
        safe = "_root_"
    return safe + ".cache"


def read_cache(cache_key: str) -> bytes | None:
    """Membaca file cache. Return None jika tidak ada."""
    cache_path = os.path.join(CACHE_DIR, cache_key)
    try:
        with open(cache_path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception as e:
        log("CACHE", f"Error membaca cache '{cache_key}': {e}")
        return None


def write_cache(cache_key: str, data: bytes):
    """
    Menulis data ke file cache. Dilindungi threading.Lock untuk mencegah
    race condition saat beberapa thread menulis bersamaan.
    """
    cache_path = os.path.join(CACHE_DIR, cache_key)
    with cache_lock:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(data)
        except Exception as e:
            log("CACHE", f"Error menulis cache '{cache_key}': {e}")


def build_error_response(status_code: int, status_text: str, detail: str = "") -> bytes:
    """Membuat HTTP error response sederhana."""
    # Coba baca file HTML error kustom dari folder HTML/status/ milik server
    # Jika tidak ada, gunakan fallback teks biasa
    body = (
        f"<html><head><title>{status_code} {status_text}</title></head>"
        f"<body><h1>{status_code} {status_text}</h1>"
        f"<p>{detail}</p>"
        f"<hr><em>Proxy Server - Tugas Besar Jarkom</em></body></html>"
    ).encode("utf-8")

    headers = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    return headers.encode("utf-8") + body


# ─────────────────────────────────────────────────────────────────────────────
# FUNGSI FORWARD REQUEST KE BACKEND SERVER
# ─────────────────────────────────────────────────────────────────────────────
def forward_to_server(server_ip: str, server_port: int, raw_request: bytes) -> bytes:
    """
    Membuka koneksi TCP ke server backend, mengirim raw HTTP request,
    dan menerima full response (header + body).
    
    Raises:
        ConnectionRefusedError jika server tidak bisa dikoneksi.
        socket.timeout jika server tidak merespons dalam waktu CONNECT_TIMEOUT.
        Exception untuk error lainnya.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    s.connect((server_ip, server_port))   # Raise ConnectionRefusedError / timeout jika gagal
    s.settimeout(RECV_TIMEOUT)

    s.sendall(raw_request)

    # Terima response hingga koneksi ditutup server
    response = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        response += chunk

    s.close()
    return response


# ─────────────────────────────────────────────────────────────────────────────
# HANDLER PER CLIENT (dijalankan di thread terpisah)
# ─────────────────────────────────────────────────────────────────────────────
def handle_client(conn: socket.socket, addr: tuple,
                  main_ip: str, backup_ip: str):
    """
    Alur utama:
    1. Terima request dari client
    2. Parse URL path
    3. Cek Cache HIT → kirim dari cache (sangat cepat)
    4. Cache MISS → coba Server Utama → jika gagal, coba Server Cadangan
    5. Cache response baru, kirim ke client
    6. Jika kedua server gagal → 504 Gateway Timeout
    """
    client_ip, client_port = addr
    t_start = time.perf_counter()

    try:
        conn.settimeout(15)

        # ── 1. Terima raw HTTP request dari client ───────────────────────────
        raw_request = b""
        while b"\r\n\r\n" not in raw_request:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw_request += chunk

        if not raw_request:
            return

        # ── 2. Parse request line ────────────────────────────────────────────
        first_line = raw_request.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = first_line.split()
        if len(parts) < 2:
            conn.sendall(build_error_response(400, "Bad Request", "Request tidak valid."))
            return

        method = parts[0]
        path   = parts[1]

        # ── 3. Cek Cache ─────────────────────────────────────────────────────
        cache_key   = path_to_cache_key(path)
        cached_data = read_cache(cache_key)

        if cached_data is not None:
            # ─── CACHE HIT ───────────────────────────────────────────────────
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            conn.sendall(cached_data)
            log("PROXY",
                f"{client_ip}:{client_port} | GET {path} | "
                f"HIT (cache) | {elapsed_ms:.2f}ms")
            return

        # ── 4. CACHE MISS → Forward ke Server ────────────────────────────────
        log("PROXY",
            f"{client_ip}:{client_port} | GET {path} | MISS → menghubungi server...")

        response_data = None
        server_used   = None

        # Coba Server Utama terlebih dahulu
        try:
            response_data = forward_to_server(main_ip, MAIN_SERVER_TCP_PORT, raw_request)
            server_used   = f"Server Utama ({main_ip}:{MAIN_SERVER_TCP_PORT})"

        except (ConnectionRefusedError, socket.timeout, OSError) as e_main:
            log("PROXY",
                f"{client_ip}:{client_port} | Server Utama GAGAL ({type(e_main).__name__}: {e_main}) "
                f"→ FAILOVER ke Server Cadangan...")

            # Failover ke Server Cadangan
            try:
                response_data = forward_to_server(backup_ip, BACKUP_SERVER_TCP_PORT, raw_request)
                server_used   = f"Server Cadangan ({backup_ip}:{BACKUP_SERVER_TCP_PORT})"

            except (ConnectionRefusedError, socket.timeout, OSError) as e_backup:
                log("PROXY",
                    f"{client_ip}:{client_port} | Server Cadangan juga GAGAL "
                    f"({type(e_backup).__name__}: {e_backup}) → 504")
                # Kedua server mati → 504
                conn.sendall(
                    build_error_response(
                        504, "Gateway Timeout",
                        "Kedua server (Utama dan Cadangan) tidak dapat dijangkau."
                    )
                )
                elapsed_ms = (time.perf_counter() - t_start) * 1000
                log("PROXY",
                    f"{client_ip}:{client_port} | GET {path} | 504 | {elapsed_ms:.2f}ms")
                return

        # ── 5. Simpan ke cache & kirim ke client ─────────────────────────────
        write_cache(cache_key, response_data)
        conn.sendall(response_data)

        elapsed_ms = (time.perf_counter() - t_start) * 1000
        log("PROXY",
            f"{client_ip}:{client_port} | GET {path} | "
            f"MISS → {server_used} | {elapsed_ms:.2f}ms | "
            f"{len(response_data)} bytes → cached & sent")

    except socket.timeout:
        log("PROXY", f"{client_ip}:{client_port} | TIMEOUT menerima request dari client")
    except Exception as e:
        log("PROXY", f"{client_ip}:{client_port} | EXCEPTION: {e}")
        try:
            conn.sendall(build_error_response(500, "Internal Server Error", str(e)))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# PROXY SERVER UTAMA
# ─────────────────────────────────────────────────────────────────────────────
def run_proxy(main_ip: str, backup_ip: str):
    """Menjalankan proxy server: listen, accept, spawn thread per client."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((PROXY_HOST, PROXY_PORT))
    server.listen(100)

    print("=" * 65)
    print("  PROXY SERVER - Tugas Besar Jaringan Komputer")
    print(f"  Listen       : {PROXY_HOST}:{PROXY_PORT}")
    print(f"  Server Utama : {main_ip}:{MAIN_SERVER_TCP_PORT}")
    print(f"  Srv Cadangan : {backup_ip}:{BACKUP_SERVER_TCP_PORT}")
    print(f"  Cache Dir    : {CACHE_DIR}")
    print("=" * 65)
    log("PROXY", "Siap menerima koneksi...")

    while True:
        try:
            conn, addr = server.accept()
            t = threading.Thread(
                target=handle_client,
                args=(conn, addr, main_ip, backup_ip),
                daemon=True
            )
            t.start()
        except KeyboardInterrupt:
            log("PROXY", "Proxy dihentikan.")
            break
        except Exception as e:
            log("PROXY", f"Error accept: {e}")

    server.close()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Proxy Server dengan Caching & Failover - Tugas Besar Jarkom"
    )
    parser.add_argument(
        "--main_ip", required=True,
        help="IP Address Node 3 (Server Utama), contoh: 192.168.1.10"
    )
    parser.add_argument(
        "--backup_ip", required=True,
        help="IP Address Node 4 (Server Cadangan), contoh: 192.168.1.11"
    )
    args = parser.parse_args()

    run_proxy(args.main_ip, args.backup_ip)


if __name__ == "__main__":
    main()
