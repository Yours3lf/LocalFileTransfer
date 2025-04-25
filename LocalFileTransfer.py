import socket
import threading
import time
import os
import uuid
import json
import struct
import tarfile
from pathlib import Path
import zstandard as zstd
from tqdm import tqdm

# ───────── CONFIG ─────────
UDP_PORT = 55500
TCP_PORT = 55510
BUFFER_SIZE = 1024 * 1024
MAX_CONNECTIONS = 32
PEER_TIMEOUT = 10
# ─────────────────────────

peers = {}
received_chunks = {}
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
	#print(f"Compression input path: {path}")
	if path.is_file():
		tar_path = path.with_suffix('.tar')
		#print(f"Tar path: {tar_path}")
		with tarfile.open(tar_path, 'w') as tar:
			tar.add(path, arcname=path.name)
	else:
		tar_path = Path(f"{path.parent}\\{path.name}_{uuid.uuid4().hex}.tar")
		#print(f"Tar path: {tar_path}")
		with tarfile.open(tar_path, 'w') as tar:
			tar.add(path, arcname=path.name)

	output_path = tar_path.with_suffix('.tar.zst')
	#print(f"Output path: {tar_path}")
	cctx = zstd.ZstdCompressor(level=3)

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

	with open(zst_path, 'rb') as src, open(tar_path, 'wb') as dst, tqdm(
		total=os.path.getsize(tar_path),
		desc="📦 Decompressing",
		unit='B',
		unit_scale=True
	) as pbar:
		with dctx.stream_reader(src) as reader:
			while chunk := reader.read(BUFFER_SIZE):
				dst.write(chunk)
				pbar.update(len(chunk))
				
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

def start_receiver():
	pbar = tqdmWrapper()
	lock = threading.Lock()
	
	def handle_client(conn, pbar):
		try:
			hlen_data = conn.recv(4)
			hlen = struct.unpack("!I", hlen_data)[0]
			header = json.loads(conn.recv(hlen).decode())

			file_id		= header["file_id"]
			filename	= header["filename"]
			total_size	= header["total_size"]
			chunk_start = header["chunk_start"]
			chunk_size	= header["chunk_size"]

			curr_path = os.path.dirname(__file__)
			out_dir = Path(f"{curr_path}/received")
			#print(f"Out Dir: {out_dir}")
			out_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
			target = out_dir / f"{file_id}__{filename}"
			#print(f"Target {target}")
			
			with lock:
				pbar.init(total_size, "📥 Receiving")

			with open(target, 'r+b' if target.exists() else 'wb') as f:
				f.seek(chunk_start)
				remaining = chunk_size
				while remaining > 0:
					data = conn.recv(min(BUFFER_SIZE, remaining))
					if not data:
						break
					f.write(data)
					remaining -= len(data)
					with lock:
						pbar.update(len(data))
			
			# Track chunks
			received_chunks.setdefault(file_id, set()).add(chunk_start)

			expected = set(i * (total_size // MAX_CONNECTIONS) for i in range(MAX_CONNECTIONS))
			last_chunk_start = (MAX_CONNECTIONS - 1) * (total_size // MAX_CONNECTIONS)
			expected.remove(last_chunk_start)
			expected.add(total_size - (total_size % MAX_CONNECTIONS or MAX_CONNECTIONS))

			if len(received_chunks[file_id]) >= len(expected):
				with lock:
					pbar.close()
					print("✅ Transfer complete.")
				decompress_received_file(target)
				del received_chunks[file_id]

		except Exception as e:
			print(f"❌ Error receiving chunk: {e}")
		finally:
			conn.close()

	def listener():
		server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		#server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
		server.bind(('', TCP_PORT))
		server.listen()
		print(f"📥 Receiver listening on TCP/{TCP_PORT}")
		while True:
			conn, _ = server.accept()
			#print(f"Accepted {conn} {_}")
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
	lock = threading.Lock()

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

		with open(file, 'rb') as f:
			f.seek(start)
			conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			conn.connect((ip, port))
			conn.sendall(hdr_len + hdr_bytes)

			remaining = size
			while remaining > 0:
				chunk = f.read(min(BUFFER_SIZE, remaining))
				if not chunk: break
				conn.sendall(chunk)
				with lock:
					pbar.update(len(chunk))
				remaining -= len(chunk)
			conn.close()

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
