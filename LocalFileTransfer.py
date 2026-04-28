import socket
import threading
import time
import os
import uuid
import json
import struct
import tarfile
import queue
from pathlib import Path
import zstandard as zstd
from tqdm import tqdm

# ───────── CONFIG ─────────
UDP_PORT = 55500
TCP_PORT = 55510
BUFFER_SIZE = 2 * 1024 * 1024	# 2 MB user-space chunk
SOCK_BUF	= 2 * 1024 * 1024	# 2 MB SO_SNDBUF / SO_RCVBUF — matches Wi-Fi BDP without bufferbloat
QUEUE_DEPTH	= 16				# producer/consumer queue size (≈ 32 MB in flight)
MAX_CONNECTIONS = 2				# 2 flows grab slightly more airtime than 1 on dual-Wi-Fi
PEER_TIMEOUT = 10
# ─────────────────────────

peers = {}
received_chunks = {}
received_chunks_lock = threading.Lock()
file_creation_locks = {}
file_creation_guard = threading.Lock()

ip_addresses = socket.gethostbyname_ex(socket.gethostname())[2]
class_a_ips = [ip for ip in ip_addresses if (not ip.startswith("127.") and not ip.startswith("172.") and not ip.startswith("192."))]
class_b_ips = [ip for ip in ip_addresses if (not ip.startswith("127.") and not ip.startswith("10.") and not ip.startswith("192."))]
class_c_ips = [ip for ip in ip_addresses if (not ip.startswith("127.") and not ip.startswith("172.") and not ip.startswith("10."))]
my_ip = ""

print(f"Class A IPs: {class_a_ips}")
print(f"Class B IPs: {class_b_ips}")
print(f"Class C IPs: {class_c_ips}")

if len(class_c_ips) > 0:
	my_ip = class_c_ips[:1][0]
elif len(class_b_ips) > 0:
	my_ip = class_b_ips[:1][0]
elif len(class_a_ips) > 0:
	my_ip = class_a_ips[:1][0]

print(f"My IP: {my_ip}")

broadcast_ip = my_ip[:my_ip.rfind(".")] + ".255"

print(f"Broadcast IP: {broadcast_ip}")

# ───────── SOCKET TUNING ─────────
def tune_socket(sock: socket.socket):
	# MUST be called BEFORE connect() on client and AFTER accept() on server
	# (or before bind/listen, in which case the listening socket passes opts to accepted ones).
	try:
		sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
	except OSError as e:
		print(f"⚠️  Socket tuning failed: {e}")

# ───────── DISCOVERY ─────────
def discover_peers():
	def send_broadcast():
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
		while True:
			message = json.dumps({"port": TCP_PORT, "host": socket.gethostname()})
			sock.sendto(message.encode(), (broadcast_ip, UDP_PORT))
			time.sleep(3)

	def receive_broadcast():
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		#sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
		sock.bind(('', UDP_PORT))
		while True:
			data, addr = sock.recvfrom(1024)
			#print(f"Rcv broadcast: {data} {addr}")
			ip = addr[0]
			if ip == my_ip or ip.startswith("127."): continue
			try:
				info = json.loads(data.decode())
				peers[ip] = {
					"port": info['port'],
					"host": info.get('host', ip),
					"last_seen": time.time()
				}
			except:
				pass

	def cleanup_peers():
		while True:
			time.sleep(5)
			now = time.time()
			for ip in list(peers):
				if now - peers[ip]["last_seen"] > PEER_TIMEOUT:
					del peers[ip]

	threading.Thread(target=send_broadcast, daemon=True).start()
	threading.Thread(target=receive_broadcast, daemon=True).start()
	threading.Thread(target=cleanup_peers, daemon=True).start()

# ───────── COMPRESSION ─────────
def compress_path(path: Path) -> Path:
	if path.is_file():
		tar_path = path.with_suffix('.tar')
		with tarfile.open(tar_path, 'w') as tar:
			tar.add(path, arcname=path.name)
	else:
		tar_path = Path(f"{path.parent}\\{path.name}_{uuid.uuid4().hex}.tar")
		with tarfile.open(tar_path, 'w') as tar:
			tar.add(path, arcname=path.name)

	output_path = tar_path.with_suffix('.tar.zst')
	# threads=-1 uses all logical cores so compression doesn't bottleneck a fast link
	cctx = zstd.ZstdCompressor(level=3, threads=-1)

	with open(tar_path, 'rb') as src, open(output_path, 'wb') as dst, tqdm(
		total=os.path.getsize(tar_path),
		desc="📦 Compressing",
		unit='B',
		unit_scale=True
	) as pbar:
		with cctx.stream_writer(dst) as compressor:
			while chunk := src.read(BUFFER_SIZE):
				compressor.write(chunk)
				pbar.update(len(chunk))

	tar_path.unlink()
	return output_path

def decompress_received_file(zst_path: Path):
	tar_path = zst_path.with_suffix('.tar')
	dctx = zstd.ZstdDecompressor()

	# Track progress against the COMPRESSED file size (the uncompressed size is unknown
	# without scanning frames). The original code used getsize(tar_path) which was 0
	# because the file had just been opened with 'wb'.
	with open(zst_path, 'rb') as src, open(tar_path, 'wb') as dst, tqdm(
		total=os.path.getsize(zst_path),
		desc="📦 Decompressing",
		unit='B',
		unit_scale=True
	) as pbar:
		last_pos = 0
		with dctx.stream_reader(src) as reader:
			while chunk := reader.read(BUFFER_SIZE):
				dst.write(chunk)
				pos = src.tell()
				pbar.update(pos - last_pos)
				last_pos = pos

	with tarfile.open(tar_path, 'r') as tar:
		tar.extractall(path=zst_path.parent, filter='data')

	tar_path.unlink()
	zst_path.unlink()
	print(f"✅ Decompressed and extracted to {zst_path.parent}")

# ───────── RECEIVER ─────────
class tqdmWrapper:
	pbar = None

	def init(self, total_size, desc_str):
		if self.pbar is None:
			self.pbar = tqdm(total=total_size, desc=desc_str, unit='B', unit_scale=True)

	def update(self, size_int):
		if not self.pbar is None:
			self.pbar.update(size_int)

	def close(self):
		if not self.pbar is None:
			self.pbar.close()
			self.pbar = None

def _ensure_file(target: Path, total_size: int):
	# Avoid the TOCTOU race where multiple receiver threads each evaluate
	# `target.exists()` as False and each open with 'wb' (which truncates),
	# clobbering chunks already written by sibling threads.
	with file_creation_guard:
		lock = file_creation_locks.setdefault(str(target), threading.Lock())
	with lock:
		if not target.exists() or target.stat().st_size != total_size:
			with open(target, 'wb') as f:
				f.truncate(total_size)

def start_receiver():
	pbar = tqdmWrapper()
	pbar_lock = threading.Lock()

	def handle_client(conn, pbar):
		try:
			tune_socket(conn)

			hlen_data = conn.recv(4)
			hlen = struct.unpack("!I", hlen_data)[0]

			# recv() may return fewer bytes than requested — read header in a loop.
			hdr_buf = b""
			while len(hdr_buf) < hlen:
				part = conn.recv(hlen - len(hdr_buf))
				if not part:
					raise ConnectionError("Header truncated")
				hdr_buf += part
			header = json.loads(hdr_buf.decode())

			file_id		= header["file_id"]
			filename	= header["filename"]
			total_size	= header["total_size"]
			chunk_start = header["chunk_start"]
			chunk_size	= header["chunk_size"]

			curr_path = os.path.dirname(__file__)
			out_dir = Path(f"{curr_path}/received")
			out_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
			target = out_dir / f"{file_id}__{filename}"

			with pbar_lock:
				pbar.init(total_size, "📥 Receiving")

			_ensure_file(target, total_size)

			# Producer/consumer split: this thread does network recv only and
			# pushes buffers to a writer thread that owns the file. Disk hiccups
			# (AV scan, page-cache flush, sparse-file allocation) no longer pause
			# the network — they just back-pressure via the queue.
			write_q = queue.Queue(maxsize=QUEUE_DEPTH)
			EOF = object()
			writer_error = []

			def writer():
				try:
					with open(target, 'r+b') as f:
						f.seek(chunk_start)
						while True:
							item = write_q.get()
							if item is EOF:
								break
							f.write(item)
				except Exception as e:
					writer_error.append(e)

			wt = threading.Thread(target=writer, daemon=True)
			wt.start()

			try:
				remaining = chunk_size
				while remaining > 0:
					data = conn.recv(min(BUFFER_SIZE, remaining))
					if not data:
						break
					write_q.put(data)
					remaining -= len(data)
					with pbar_lock:
						pbar.update(len(data))
			finally:
				write_q.put(EOF)
				wt.join()

			if writer_error:
				raise writer_error[0]

			with received_chunks_lock:
				received_chunks.setdefault(file_id, set()).add(chunk_start)
				done = len(received_chunks[file_id]) >= MAX_CONNECTIONS
				if done:
					del received_chunks[file_id]

			if done:
				with pbar_lock:
					pbar.close()
					print("✅ Transfer complete.")
				with file_creation_guard:
					file_creation_locks.pop(str(target), None)
				decompress_received_file(target)

		except Exception as e:
			print(f"❌ Error receiving chunk: {e}")
		finally:
			conn.close()

	def listener():
		server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		# Setting buffer sizes on the listening socket makes accepted sockets inherit them.
		tune_socket(server)
		server.bind(('', TCP_PORT))
		server.listen()
		print(f"📥 Receiver listening on TCP/{TCP_PORT}")
		while True:
			conn, _ = server.accept()
			threading.Thread(target=handle_client, args=(conn, pbar), daemon=True).start()

	threading.Thread(target=listener, daemon=True).start()

# ───────── SENDER ─────────
def send_file(ip: str, port: int, file_path: Path):
	print(f"Sending file to {ip}:{port}")
	file = compress_path(file_path)
	total_size = file.stat().st_size
	file_id = uuid.uuid4().hex

	chunk_size = total_size // MAX_CONNECTIONS
	ranges = [(i * chunk_size, chunk_size) for i in range(MAX_CONNECTIONS)]
	ranges[-1] = (ranges[-1][0], total_size - ranges[-1][0])

	pbar = tqdm(total=total_size, desc="📤 Sending", unit='B', unit_scale=True)
	pbar_lock = threading.Lock()

	def send_chunk(start: int, size: int):
		header = {
			"file_id": file_id,
			"filename": file.name,
			"total_size": total_size,
			"chunk_start": start,
			"chunk_size": size
		}
		hdr_bytes = json.dumps(header).encode()
		hdr_len = struct.pack("!I", len(hdr_bytes))

		# Producer/consumer split: a reader thread fills the queue from disk
		# while this thread drains it to the socket. On Windows, socket.sendfile()
		# falls back to a synchronous read→send loop, so without this split the
		# NIC sits idle during reads and the disk sits idle during sends.
		read_q = queue.Queue(maxsize=QUEUE_DEPTH)
		EOF = object()
		reader_error = []

		def reader():
			try:
				with open(file, 'rb') as f:
					f.seek(start)
					remaining = size
					while remaining > 0:
						data = f.read(min(BUFFER_SIZE, remaining))
						if not data:
							break
						read_q.put(data)
						remaining -= len(data)
			except Exception as e:
				reader_error.append(e)
			finally:
				read_q.put(EOF)

		rt = threading.Thread(target=reader, daemon=True)
		rt.start()

		conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		tune_socket(conn)
		try:
			conn.connect((ip, port))
			conn.sendall(hdr_len + hdr_bytes)

			while True:
				item = read_q.get()
				if item is EOF:
					break
				conn.sendall(item)
				with pbar_lock:
					pbar.update(len(item))
		finally:
			conn.close()
			rt.join()

		if reader_error:
			raise reader_error[0]

	threads = []
	for start, size in ranges:
		t = threading.Thread(target=send_chunk, args=(start, size))
		t.start()
		threads.append(t)
	for t in threads:
		t.join()

	pbar.close()
	print("✅ Transfer complete.")
	file.unlink()

# ───────── MAIN ─────────
def main():
	discover_peers()
	start_receiver()
	time.sleep(2)

	while True:
		print("\n🌐 Available peers:")
		live_peers = list(peers.items())
		for i, (ip, info) in enumerate(live_peers):
			print(f"{i+1}. {info['host']} ({ip})")
		if not live_peers:
			print("No peers found. Waiting…")
			time.sleep(5)
			continue

		choice = input("Select peer to send file/folder to (# or q): ")
		if choice.lower() == 'q':
			break
		try:
			target_ip, peer = live_peers[int(choice)-1]
			path = input("Enter path to file or folder: ").strip().strip('"')
			send_file(target_ip, peer["port"], Path(path))
		except Exception as e:
			print(f"❌ Error: {e}")

if __name__ == "__main__":
	main()
