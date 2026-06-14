import socket
import argparse
import time
import datetime
import math
import sys

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────────────────────────────────────────
PROXY_PORT       = 8080
DEFAULT_UDP_PORT = 9000
PACKET_COUNT     = 10
UDP_PAYLOAD_SIZE = 64    # byte, termasuk overhead teks "Ping <seq> <ts>"
UDP_TIMEOUT      = 1.0   # detik, timeout per paket
INTERVAL         = 0.5   # detik, jeda antar paket


def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(label: str, msg: str):
    print(f"[{timestamp()}] [{label}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# MODE TCP: Kirim GET ke Proxy, cetak response HTML
# ─────────────────────────────────────────────────────────────────────────────
def run_tcp(proxy_ip: str, url_path: str):
    """
    Mengirim satu HTTP GET request ke proxy (port 8080) dan mencetak
    seluruh isi HTML response ke terminal.
    """
    proxy_port = PROXY_PORT

    print("=" * 60)
    print("  MODE TCP - HTTP GET via Proxy")
    print(f"  Proxy    : {proxy_ip}:{proxy_port}")
    print(f"  URL Path : {url_path}")
    print("=" * 60)

    t_start = time.perf_counter()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((proxy_ip, proxy_port))

        # Bangun HTTP/1.1 GET request secara manual
        request = (
            f"GET {url_path} HTTP/1.1\r\n"
            f"Host: {proxy_ip}:{proxy_port}\r\n"
            f"Connection: close\r\n"
            f"User-Agent: TubesBesar-Client/1.0\r\n"
            f"\r\n"
        )
        s.sendall(request.encode("utf-8"))

        # Terima response penuh
        response = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            response += chunk

        s.close()

        elapsed_ms = (time.perf_counter() - t_start) * 1000

        # Pisahkan header dan body
        if b"\r\n\r\n" in response:
            header_part, body_part = response.split(b"\r\n\r\n", 1)
            headers = header_part.decode("utf-8", errors="replace")
            body    = body_part.decode("utf-8", errors="replace")
        else:
            headers = response.decode("utf-8", errors="replace")
            body    = ""

        # Cetak header
        print("\n─── HTTP RESPONSE HEADERS ───────────────────────────────────")
        print(headers)
        print("─── HTTP RESPONSE BODY ──────────────────────────────────────")
        print(body)
        print("─────────────────────────────────────────────────────────────")
        log("TCP", f"Selesai | Total: {len(response)} bytes | RTT: {elapsed_ms:.2f}ms")

    except ConnectionRefusedError:
        log("TCP", f"GAGAL: Proxy di {proxy_ip}:{proxy_port} tidak bisa dikoneksi.")
        sys.exit(1)
    except socket.timeout:
        log("TCP", "GAGAL: Timeout saat menunggu response dari proxy.")
        sys.exit(1)
    except Exception as e:
        log("TCP", f"EXCEPTION: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# MODE UDP: QoS Test (RTT, Loss, Jitter, Throughput)
# ─────────────────────────────────────────────────────────────────────────────
def run_udp(server_ip: str, udp_port: int, count: int, interval: float):
    """
    Mengirim `count` paket UDP ke server (QoS Echo Server).
    Menghitung statistik: Min/Avg/Max RTT, Packet Loss, Jitter, Throughput.
    """
    print("=" * 60)
    print("  MODE UDP - QoS Test (Echo)")
    print(f"  Target  : {server_ip}:{udp_port}")
    print(f"  Paket   : {count} × interval {interval}s")
    print("=" * 60)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(UDP_TIMEOUT)

    rtts          = []      # daftar RTT (ms) per paket yang berhasil
    sent          = 0
    received      = 0
    total_bytes   = 0
    t_test_start  = time.perf_counter()

    for seq in range(1, count + 1):
        ts_send = time.perf_counter()
        ts_str  = f"{ts_send:.6f}"
        payload = f"Ping {seq} {ts_str}"

        # Pad payload agar mencapai UDP_PAYLOAD_SIZE byte (opsional, bisa lebih kecil)
        payload_bytes = payload.encode("utf-8")
        sent += 1

        try:
            s.sendto(payload_bytes, (server_ip, udp_port))
            echo, _ = s.recvfrom(65535)

            ts_recv = time.perf_counter()
            rtt_ms  = (ts_recv - ts_send) * 1000
            rtts.append(rtt_ms)
            received       += 1
            total_bytes    += len(echo)

            log("UDP",
                f"Paket {seq:>3}/{count} | "
                f"RTT={rtt_ms:.3f}ms | "
                f"{len(payload_bytes)} bytes | "
                f"RECV: '{echo.decode('utf-8', errors='replace')}'")

        except socket.timeout:
            log("UDP", f"Paket {seq:>3}/{count} | TIMEOUT (>{UDP_TIMEOUT}s) | LOST")

        if seq < count:
            time.sleep(interval)

    t_test_end    = time.perf_counter()
    total_time_s  = t_test_end - t_test_start
    s.close()

    # ── Hitung Statistik ──────────────────────────────────────────────────────
    loss_pct = ((sent - received) / sent * 100) if sent > 0 else 100.0

    if rtts:
        rtt_min = min(rtts)
        rtt_max = max(rtts)
        rtt_avg = sum(rtts) / len(rtts)

        # Jitter = standar deviasi dari selisih RTT antar paket berurutan
        if len(rtts) >= 2:
            diffs  = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
            avg_d  = sum(diffs) / len(diffs)
            jitter = math.sqrt(sum((d - avg_d) ** 2 for d in diffs) / len(diffs))
        else:
            jitter = 0.0

        # Throughput (kbps) = total bytes diterima × 8 bit ÷ total waktu (detik) ÷ 1000
        throughput_kbps = (total_bytes * 8) / total_time_s / 1000 if total_time_s > 0 else 0.0

    else:
        rtt_min = rtt_max = rtt_avg = jitter = throughput_kbps = 0.0

    # ── Cetak Laporan ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  HASIL QoS UDP - STATISTIK")
    print("=" * 60)
    print(f"  Target         : {server_ip}:{udp_port}")
    print(f"  Paket Dikirim  : {sent}")
    print(f"  Paket Diterima : {received}")
    print(f"  Packet Loss    : {loss_pct:.1f}%")
    print(f"  ─────────────────────────────")
    print(f"  RTT Min        : {rtt_min:.3f} ms")
    print(f"  RTT Avg        : {rtt_avg:.3f} ms")
    print(f"  RTT Max        : {rtt_max:.3f} ms")
    print(f"  Jitter (StdDev): {jitter:.3f} ms")
    print(f"  Throughput     : {throughput_kbps:.3f} kbps")
    print(f"  Total Waktu    : {total_time_s:.3f} s")
    print("=" * 60)

    if loss_pct == 100.0:
        print("\n[PERINGATAN] Semua paket hilang! Pastikan UDP QoS server aktif.")
        print(f"             Periksa apakah webserver.py berjalan di {server_ip}:{udp_port}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Client TCP/UDP - Tugas Besar Jaringan Komputer"
    )
    parser.add_argument(
        "-mode", required=True, choices=["tcp", "udp"],
        help="Mode operasi: 'tcp' untuk HTTP GET, 'udp' untuk QoS Test"
    )

    # Argumen TCP
    parser.add_argument(
        "-proxy_ip", default=None,
        help="[TCP] IP Address Proxy (Node 2), contoh: 192.168.1.20"
    )
    parser.add_argument(
        "-url", default="/index.html",
        help="[TCP] URL path yang diminta, contoh: /osi.html (default: /index.html)"
    )

    # Argumen UDP
    parser.add_argument(
        "-server_ip", default=None,
        help="[UDP] IP Address Server Utama atau Cadangan, contoh: 192.168.1.10"
    )
    parser.add_argument(
        "-udp_port", type=int, default=DEFAULT_UDP_PORT,
        help=f"[UDP] Port UDP tujuan (default: {DEFAULT_UDP_PORT})"
    )
    parser.add_argument(
        "-count", type=int, default=PACKET_COUNT,
        help=f"[UDP] Jumlah paket yang dikirim (default: {PACKET_COUNT})"
    )
    parser.add_argument(
        "-interval", type=float, default=INTERVAL,
        help=f"[UDP] Interval antar paket dalam detik (default: {INTERVAL})"
    )

    args = parser.parse_args()

    if args.mode == "tcp":
        if not args.proxy_ip:
            parser.error("-proxy_ip wajib diisi untuk mode TCP")
        run_tcp(args.proxy_ip, args.url)

    elif args.mode == "udp":
        if not args.server_ip:
            parser.error("-server_ip wajib diisi untuk mode UDP")
        run_udp(args.server_ip, args.udp_port, args.count, args.interval)


if __name__ == "__main__":
    main()
