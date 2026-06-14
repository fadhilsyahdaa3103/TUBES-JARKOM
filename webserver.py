import socket
import threading
import argparse
import os
import sys
import datetime
import mimetypes

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────────────────────────────────────────
BIND_HOST   = "0.0.0.0"       # Bind ke semua interface agar bisa diakses lewat LAN
HTML_ROOT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HTML")

# Peta URL path → nama file relatif terhadap HTML_ROOT
ROUTE_MAP = {
    "/":                  "index.html",
    "/index.html":        "index.html",
    "/osi.html":          "osi.html",
    "/tcpip.html":        "tcpip.html",
    "/qos.html":          "qos.html",
    "/implementation.html": "implementation.html",
}

# ─────────────────────────────────────────────────────────────────────────────
# UTILITAS
# ─────────────────────────────────────────────────────────────────────────────
def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(label: str, msg: str):
    print(f"[{timestamp()}] [{label}] {msg}", flush=True)


def build_response(status_code: int, status_text: str, body: bytes,
                   content_type: str = "text/html; charset=utf-8") -> bytes:
    """Membangun HTTP/1.1 response secara manual."""
    headers = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"Server: PythonWebServer/1.0\r\n"
        f"Date: {datetime.datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')}\r\n"
        f"\r\n"
    )
    return headers.encode("utf-8") + body


def read_file(filepath: str) -> bytes:
    """Membaca file biner dari disk."""
    with open(filepath, "rb") as f:
        return f.read()


# ─────────────────────────────────────────────────────────────────────────────
# TCP: HANDLER PER KONEKSI (dijalankan di thread terpisah)
# ─────────────────────────────────────────────────────────────────────────────
def handle_tcp_client(conn: socket.socket, addr: tuple, tcp_port: int):
    """Memparsing HTTP GET request dan mengembalikan response."""
    client_ip, client_port = addr
    try:
        # Terima request (maks 4096 byte cukup untuk request GET sederhana)
        raw = b""
        conn.settimeout(10)
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk

        if not raw:
            return

        # ── Parsing manual HTTP request ──────────────────────────────────────
        request_line = raw.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = request_line.split()
        if len(parts) < 2:
            # Request tidak valid
            response = build_response(400, "Bad Request", b"<h1>400 Bad Request</h1>")
            conn.sendall(response)
            log("TCP", f"{client_ip}:{client_port} | BAD REQUEST | port={tcp_port}")
            return

        method = parts[0]
        path   = parts[1]

        # Hanya layani GET
        if method != "GET":
            body = b"<h1>405 Method Not Allowed</h1>"
            response = build_response(405, "Method Not Allowed", body)
            conn.sendall(response)
            log("TCP", f"{client_ip}:{client_port} | 405 | {method} {path}")
            return

        # ── Routing: URL → file ──────────────────────────────────────────────
        status_code = 200
        status_text = "OK"

        # Cek apakah path ada di route map
        if path in ROUTE_MAP:
            filename = ROUTE_MAP[path]
            filepath = os.path.join(HTML_ROOT, filename)
        else:
            # Coba langsung sebagai file (untuk aset, CSS, dll.)
            # Pastikan tidak ada path traversal (keamanan dasar)
            safe_path = os.path.normpath(path.lstrip("/"))
            filepath  = os.path.join(HTML_ROOT, safe_path)
            filename  = safe_path

        # ── Baca file & kirim response ────────────────────────────────────────
        try:
            body = read_file(filepath)

            # Deteksi Content-Type otomatis
            mime, _ = mimetypes.guess_type(filepath)
            if mime is None:
                mime = "application/octet-stream"
            if "text" in mime:
                mime += "; charset=utf-8"

            response = build_response(200, "OK", body, content_type=mime)
            status_code = 200
            status_text = "OK"

        except FileNotFoundError:
            # Coba kirim halaman 404 kustom
            err_file = os.path.join(HTML_ROOT, "status", "404.html")
            try:
                body = read_file(err_file)
            except FileNotFoundError:
                body = b"<h1>404 Not Found</h1>"
            response   = build_response(404, "Not Found", body)
            status_code = 404
            status_text = "Not Found"

        except Exception as e:
            # Error membaca file → 500
            err_file = os.path.join(HTML_ROOT, "status", "500.html")
            try:
                body = read_file(err_file)
            except FileNotFoundError:
                body = b"<h1>500 Internal Server Error</h1>"
            response   = build_response(500, "Internal Server Error", body)
            status_code = 500
            status_text = "Internal Server Error"
            log("TCP", f"ERROR membaca file '{filepath}': {e}")

        conn.sendall(response)
        log("TCP",
            f"{client_ip}:{client_port} | {status_code} {status_text} | "
            f"GET {path} | file={filename} | port={tcp_port}")

    except socket.timeout:
        log("TCP", f"{client_ip}:{client_port} | TIMEOUT saat menerima request")
    except Exception as e:
        log("TCP", f"{client_ip}:{client_port} | EXCEPTION: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# TCP SERVER
# ─────────────────────────────────────────────────────────────────────────────
def run_tcp_server(tcp_port: int):
    """Menjalankan TCP server dengan multithreading."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND_HOST, tcp_port))
    server.listen(50)
    log("TCP", f"Server TCP siap di {BIND_HOST}:{tcp_port} | HTML_ROOT={HTML_ROOT}")

    while True:
        try:
            conn, addr = server.accept()
            t = threading.Thread(
                target=handle_tcp_client,
                args=(conn, addr, tcp_port),
                daemon=True
            )
            t.start()
        except KeyboardInterrupt:
            log("TCP", "Server dihentikan.")
            break
        except Exception as e:
            log("TCP", f"Error saat menerima koneksi: {e}")

    server.close()


# ─────────────────────────────────────────────────────────────────────────────
# UDP QoS ECHO SERVER
# ─────────────────────────────────────────────────────────────────────────────
def run_udp_server(udp_port: int):
    """UDP Echo Server: memantulkan kembali setiap paket tanpa mengubah payload."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND_HOST, udp_port))
    log("UDP", f"QoS Echo Server siap di {BIND_HOST}:{udp_port}")

    while True:
        try:
            data, addr = server.recvfrom(65535)
            # Echo balik tanpa modifikasi
            server.sendto(data, addr)
            log("UDP", f"{addr[0]}:{addr[1]} | ECHO | {len(data)} bytes | '{data.decode('utf-8', errors='replace')}'")
        except KeyboardInterrupt:
            log("UDP", "Server dihentikan.")
            break
        except Exception as e:
            log("UDP", f"Error: {e}")

    server.close()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Web Server (TCP/HTTP + UDP QoS Echo) - Tugas Besar Jarkom"
    )
    parser.add_argument(
        "--tcp_port", type=int, default=8000,
        help="Port TCP/HTTP (default: 8000 untuk Server Utama, 8001 untuk Cadangan)"
    )
    parser.add_argument(
        "--udp_port", type=int, default=9000,
        help="Port UDP QoS (default: 9000 untuk Server Utama, 9001 untuk Cadangan)"
    )
    args = parser.parse_args()

    if not os.path.isdir(HTML_ROOT):
        print(f"[ERROR] Folder HTML tidak ditemukan: {HTML_ROOT}")
        print("Pastikan folder 'HTML/' ada di direktori yang sama dengan webserver.py")
        sys.exit(1)

    print("=" * 60)
    print("  WEB SERVER - Tugas Besar Jaringan Komputer")
    print(f"  TCP Port : {args.tcp_port}")
    print(f"  UDP Port : {args.udp_port}")
    print(f"  HTML Root: {HTML_ROOT}")
    print("=" * 60)

    # Jalankan TCP dan UDP server secara paralel di thread terpisah
    tcp_thread = threading.Thread(
        target=run_tcp_server, args=(args.tcp_port,), daemon=True
    )
    udp_thread = threading.Thread(
        target=run_udp_server, args=(args.udp_port,), daemon=True
    )

    tcp_thread.start()
    udp_thread.start()

    try:
        tcp_thread.join()
        udp_thread.join()
    except KeyboardInterrupt:
        print("\n[INFO] Server dihentikan oleh user (Ctrl+C).")
        sys.exit(0)


if __name__ == "__main__":
    main()
