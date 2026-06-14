import socket
import threading
import os
import sys
import datetime

# ─────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────
TCP_HOST = "0.0.0.0"
TCP_PORT = 8000
UDP_HOST = "0.0.0.0"
UDP_PORT = 9090

# Direktori tempat file HTML disimpan
BASE_DIR = "HTML"


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
        with open(os.path.join("logs", "log_server.txt"), "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"Gagal menulis ke log file: {e}")


# ─────────────────────────────────────────
#  HELPER: MIME TYPE
# ─────────────────────────────────────────
def get_mime_type(path):
    if path.endswith(".html") or path.endswith(".htm"):
        return "text/html; charset=utf-8"
    elif path.endswith(".css"):
        return "text/css"
    elif path.endswith(".js"):
        return "application/javascript"
    elif path.endswith(".png"):
        return "image/png"
    elif path.endswith(".jpg") or path.endswith(".jpeg"):
        return "image/jpeg"
    elif path.endswith(".ico"):
        return "image/x-icon"
    else:
        return "application/octet-stream"


# ─────────────────────────────────────────
#  HELPER: BANGUN HTTP RESPONSE
# ─────────────────────────────────────────
def build_response(status_code, status_text, body_bytes, content_type="text/html; charset=utf-8"):
    header = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    return header.encode("utf-8") + body_bytes


# ─────────────────────────────────────────
#  HANDLE SATU KONEKSI CLIENT (TCP)
# ─────────────────────────────────────────
def handle_tcp_client(conn, addr):
    client_ip = addr[0]
    try:
        # Terima request (maksimal 4KB untuk header)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk

        if not raw:
            return

        # Parse request line
        try:
            request_text = raw.decode("utf-8", errors="replace")
            request_line = request_text.split("\r\n")[0]
            parts = request_line.split(" ")
            method = parts[0]
            path = parts[1] if len(parts) > 1 else "/"
        except Exception:
            # Request malformed
            body = b"<h1>400 Bad Request</h1>"
            conn.sendall(build_response(400, "Bad Request", body))
            log("TCP", f"{client_ip} - 400 Bad Request (malformed)")
            return

        # Hanya support GET
        if method != "GET":
            body = b"<h1>405 Method Not Allowed</h1>"
            conn.sendall(build_response(405, "Method Not Allowed", body))
            log("TCP", f"{client_ip} {method} {path} - 405")
            return

        # Normalisasi path
        if path == "/":
            path = "/index.html"

        # Keamanan: cegah path traversal (../../)
        safe_path = os.path.normpath(path.lstrip("/"))
        if safe_path.startswith(".."):
            body = b"<h1>403 Forbidden</h1>"
            conn.sendall(build_response(403, "Forbidden", body))
            log("TCP", f"{client_ip} GET {path} - 403 Forbidden (path traversal)")
            return

        file_path = os.path.join(BASE_DIR, safe_path)

        # Coba baca file
        try:
            with open(file_path, "rb") as f:
                body = f.read()
            mime = get_mime_type(file_path)
            response = build_response(200, "OK", body, mime)
            conn.sendall(response)
            log("TCP", f"{client_ip} GET {path} - 200 OK ({len(body)} bytes)")

        except FileNotFoundError:
            body = b"<h1>404 Not Found</h1><p>File tidak ditemukan.</p>"
            conn.sendall(build_response(404, "Not Found", body))
            log("TCP", f"{client_ip} GET {path} - 404 Not Found")

        except Exception as e:
            body = f"<h1>500 Internal Server Error</h1><p>{e}</p>".encode()
            conn.sendall(build_response(500, "Internal Server Error", body))
            log("TCP", f"{client_ip} GET {path} - 500 Internal Server Error: {e}")

    except Exception as e:
        log("TCP", f"Error handling client {client_ip}: {e}")
    finally:
        conn.close()


# ─────────────────────────────────────────
#  TCP SERVER (HTTP)
# ─────────────────────────────────────────
def start_tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((TCP_HOST, TCP_PORT))
    server.listen(50)
    log("TCP", f"HTTP Server running on port {TCP_PORT}")

    while True:
        try:
            conn, addr = server.accept()
            t = threading.Thread(target=handle_tcp_client, args=(conn, addr), daemon=True)
            t.start()
            log("TCP", f"New connection from {addr[0]} — thread spawned (active: {threading.active_count()-1})")
        except Exception as e:
            log("TCP", f"Accept error: {e}")


# ─────────────────────────────────────────
#  UDP SERVER (QoS Echo)
# ─────────────────────────────────────────
def start_udp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((UDP_HOST, UDP_PORT))
    log("UDP", f"QoS Echo Server running on port {UDP_PORT}")

    while True:
        try:
            data, addr = server.recvfrom(1024)
            server.sendto(data, addr)
            log("UDP", f"Echo {len(data)} bytes -> {addr[0]}:{addr[1]} | payload: {data.decode('utf-8', errors='replace')}")
        except Exception as e:
            log("UDP", f"Error: {e}")


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    log("MAIN", "Starting Web Server (TCP + UDP)...")

    # UDP server di thread terpisah
    udp_thread = threading.Thread(target=start_udp_server, daemon=True)
    udp_thread.start()

    # TCP server di main thread (blocking)
    try:
        start_tcp_server()
    except KeyboardInterrupt:
        log("MAIN", "Server dihentikan.")
        sys.exit(0)