"""LocalFileTransfer v2: fast LAN file/folder transfer with authentication and encryption.

Usage
  python LocalFileTransfer.py                      interactive (discovery + menu), like v1
  python LocalFileTransfer.py receive [--once] [--timeout S] [--out DIR] [--port N]
  python LocalFileTransfer.py send PATH --to HOST [--port N]
  python LocalFileTransfer.py genkey [--force]     create the pre-shared key (%USERPROFILE%\\.lft\\psk)
  python LocalFileTransfer.py lockkey              restrict the key file to the current user

Both PCs need the SAME key file (copy it once over a trusted channel, e.g. scp over SSH).

Security (v2)
  Every TCP connection starts with a mutual challenge-response:
    server -> MAGIC | ver | Ns(32)
    client -> Nc(32) | HMAC(psk, "lft2-c" | Ns | Nc)
    server -> HMAC(psk, "lft2-s" | Nc | Ns)
  Per-connection, per-direction keys: HKDF-SHA256(psk, salt=Ns|Nc, info="lft2 c2s" / "lft2 s2c").
  Records: type(1) | len(4) | AES-256-GCM(nonce = 0000 | counter64, aad = type). Counters make
  reordering/replay/truncation detectable; an END record closes each chunk; the receiver answers
  with an ACK record ("ok", or "done" + extracted paths once the whole transfer is unpacked).
  Peers without the key are dropped after the handshake; nothing they send is written to disk.

Performance: unchanged from v1 (2 parallel flows, 2 MB records, 16-deep producer/consumer queues,
zstd level 3 multi-threaded). Encryption runs on the producer (disk) thread, decryption on the
writer thread, so the socket threads only move bytes. cryptography releases the GIL in AES-GCM.
"""
import argparse
import hashlib
import hmac
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

import zstandard as zstd
from tqdm import tqdm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ───────── CONFIG ─────────
UDP_PORT = 55500
TCP_PORT = 55510
BUFFER_SIZE = 2 * 1024 * 1024	# 2 MB user-space chunk (= plaintext record size)
SOCK_BUF	= 2 * 1024 * 1024	# 2 MB SO_SNDBUF / SO_RCVBUF — matches Wi-Fi BDP without bufferbloat
QUEUE_DEPTH	= 16				# producer/consumer queue size (≈ 32 MB in flight)
MAX_CONNECTIONS = 2				# 2 flows grab slightly more airtime than 1 on dual-Wi-Fi
PEER_TIMEOUT = 10
HANDSHAKE_TIMEOUT = 15			# seconds for the handshake + header
IO_TIMEOUT = 120				# seconds without progress on a data connection
ACK_TIMEOUT = 3600				# seconds the sender waits for "done" (decompress + extract)
# ─────────────────────────

PROTO_MAGIC = b"LFT2"
PROTO_VER = 2
NONCE_LEN = 32
TAG_LEN = 32
REC_HEADER = 1
REC_DATA = 2
REC_END = 3
REC_ACK = 4
MAX_RECORD = BUFFER_SIZE + 16 + 64 * 1024	# ciphertext upper bound (data + GCM tag, header slack)

QUIET = False
peers = {}
received_chunks = {}
received_chunks_lock = threading.Lock()
file_creation_locks = {}
file_creation_guard = threading.Lock()


def log(msg):
	if not QUIET:
		print(msg, flush=True)


def emit(event, **kw):
	"""Machine-readable progress line on stdout (used by the testpc skill in headless modes)."""
	print(json.dumps({"event": event, **kw}), flush=True)


# ───────── KEY ─────────
def default_psk_path() -> Path:
	return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".lft" / "psk"


def lock_key_file(path: Path):
	"""Restrict the key file to the current user (Windows ACL / POSIX mode)."""
	if os.name == "nt":
		sid = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
		user_sid = sid.stdout.strip().split(",")[-1].strip('"')
		subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{user_sid}:F"],
					   capture_output=True, check=True)
	else:
		os.chmod(path, 0o600)


def load_psk(path: Path) -> bytes:
	if not path.exists():
		raise SystemExit(
			f"❌ No pre-shared key at {path}.\n"
			f"   Create one with:  python {Path(__file__).name} genkey\n"
			f"   then copy that file to the same place on the other PC (e.g. scp over SSH).")
	key = bytes.fromhex(path.read_text().strip())
	if len(key) < 32:
		raise SystemExit(f"❌ Key in {path} is too short ({len(key)} bytes, need 32).")
	return key


def cmd_genkey(path: Path, force: bool):
	if path.exists() and not force:
		raise SystemExit(f"Key already exists at {path} (use --force to replace it; the other PC then needs the new one).")
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(os.urandom(32).hex())
	lock_key_file(path)
	print(f"✅ New key written to {path} (readable only by you). Copy it to the same path on the other PC.")


# ───────── CRYPTO SESSION ─────────
class Channel:
	"""An authenticated, encrypted record stream over one TCP connection."""

	def __init__(self, sock: socket.socket, send_key: bytes, recv_key: bytes):
		self.sock = sock
		self.send_aead = AESGCM(send_key)
		self.recv_aead = AESGCM(recv_key)
		self.send_ctr = 0
		self.recv_ctr = 0

	@staticmethod
	def _nonce(ctr: int) -> bytes:
		return b"\x00\x00\x00\x00" + ctr.to_bytes(8, "big")

	# Sealing/opening are split from socket I/O so they can run on producer/consumer threads.
	def seal(self, rtype: int, payload: bytes):
		ct = self.send_aead.encrypt(self._nonce(self.send_ctr), payload, bytes([rtype]))
		self.send_ctr += 1
		return struct.pack("!BI", rtype, len(ct)), ct

	def open(self, rtype: int, ct, ctr: int) -> bytes:
		return self.recv_aead.decrypt(self._nonce(ctr), bytes(ct), bytes([rtype]))

	def send_sealed(self, hdr: bytes, ct: bytes):
		self.sock.sendall(hdr)
		self.sock.sendall(ct)

	def send_record(self, rtype: int, payload: bytes):
		self.send_sealed(*self.seal(rtype, payload))

	def read_raw(self):
		"""Read one record without decrypting. Returns (rtype, ciphertext, counter)."""
		rtype, ln = struct.unpack("!BI", recv_exact(self.sock, 5))
		if ln > MAX_RECORD:
			raise ConnectionError(f"Record too large ({ln} bytes)")
		ct = recv_exact(self.sock, ln)
		ctr = self.recv_ctr
		self.recv_ctr += 1
		return rtype, ct, ctr

	def recv_record(self):
		rtype, ct, ctr = self.read_raw()
		return rtype, self.open(rtype, ct, ctr)


def recv_exact(sock: socket.socket, n: int) -> bytearray:
	buf = bytearray(n)
	view = memoryview(buf)
	got = 0
	while got < n:
		r = sock.recv_into(view[got:], n - got)
		if r == 0:
			raise ConnectionError("Connection closed by peer")
		got += r
	return buf


def _derive(psk: bytes, ns: bytes, nc: bytes, info: bytes) -> bytes:
	return HKDF(algorithm=hashes.SHA256(), length=32, salt=ns + nc, info=info).derive(psk)


def server_handshake(sock: socket.socket, psk: bytes) -> Channel:
	ns = os.urandom(NONCE_LEN)
	sock.sendall(PROTO_MAGIC + bytes([PROTO_VER]) + ns)
	msg = bytes(recv_exact(sock, NONCE_LEN + TAG_LEN))
	nc, tag = msg[:NONCE_LEN], msg[NONCE_LEN:]
	expect = hmac.new(psk, b"lft2-c" + ns + nc, hashlib.sha256).digest()
	if not hmac.compare_digest(tag, expect):
		raise PermissionError("authentication failed (wrong or missing key, or a v1 client)")
	sock.sendall(hmac.new(psk, b"lft2-s" + nc + ns, hashlib.sha256).digest())
	return Channel(sock, _derive(psk, ns, nc, b"lft2 s2c"), _derive(psk, ns, nc, b"lft2 c2s"))


def client_handshake(sock: socket.socket, psk: bytes) -> Channel:
	hello = bytes(recv_exact(sock, len(PROTO_MAGIC) + 1 + NONCE_LEN))
	if hello[:4] != PROTO_MAGIC or hello[4] != PROTO_VER:
		raise ConnectionError("peer is not a LocalFileTransfer v2 receiver")
	ns = hello[5:]
	nc = os.urandom(NONCE_LEN)
	sock.sendall(nc + hmac.new(psk, b"lft2-c" + ns + nc, hashlib.sha256).digest())
	try:
		server_tag = bytes(recv_exact(sock, TAG_LEN))
	except ConnectionError:
		raise PermissionError("receiver rejected us during authentication (the two PCs have different keys)")
	expect = hmac.new(psk, b"lft2-s" + nc + ns, hashlib.sha256).digest()
	if not hmac.compare_digest(server_tag, expect):
		raise PermissionError("receiver failed authentication (it does not have our key)")
	return Channel(sock, _derive(psk, ns, nc, b"lft2 c2s"), _derive(psk, ns, nc, b"lft2 s2c"))


# ───────── NETWORK HELPERS ─────────
def local_ip() -> str:
	ip_addresses = socket.gethostbyname_ex(socket.gethostname())[2]
	class_a_ips = [ip for ip in ip_addresses if (not ip.startswith("127.") and not ip.startswith("172.") and not ip.startswith("192."))]
	class_b_ips = [ip for ip in ip_addresses if (not ip.startswith("127.") and not ip.startswith("10.") and not ip.startswith("192."))]
	class_c_ips = [ip for ip in ip_addresses if (not ip.startswith("127.") and not ip.startswith("172.") and not ip.startswith("10."))]
	log(f"Class A IPs: {class_a_ips}")
	log(f"Class B IPs: {class_b_ips}")
	log(f"Class C IPs: {class_c_ips}")
	for group in (class_c_ips, class_b_ips, class_a_ips):
		if group:
			return group[0]
	return ""


# ───────── SOCKET TUNING ─────────
def tune_socket(sock: socket.socket):
	# MUST be called BEFORE connect() on client and AFTER accept() on server
	# (or before bind/listen, in which case the listening socket passes opts to accepted ones).
	try:
		sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
	except OSError as e:
		log(f"⚠️  Socket tuning failed: {e}")


# ───────── DISCOVERY (interactive mode only) ─────────
def discover_peers(my_ip: str):
	broadcast_ip = my_ip[:my_ip.rfind(".")] + ".255"
	log(f"Broadcast IP: {broadcast_ip}")

	def send_broadcast():
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
		while True:
			message = json.dumps({"port": TCP_PORT, "host": socket.gethostname(), "v": PROTO_VER})
			sock.sendto(message.encode(), (broadcast_ip, UDP_PORT))
			time.sleep(3)

	def receive_broadcast():
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		sock.bind(('', UDP_PORT))
		while True:
			data, addr = sock.recvfrom(1024)
			ip = addr[0]
			if ip == my_ip or ip.startswith("127."): continue
			try:
				info = json.loads(data.decode())
				peers[ip] = {
					"port": int(info['port']),
					"host": str(info.get('host', ip))[:64],
					"v": int(info.get('v', 1)),
					"last_seen": time.time()
				}
			except Exception:
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
	# Temp files get unique names (never a user's own x.tar), in LFT_TMP if set, else next to the source.
	tmp_dir = Path(os.environ.get("LFT_TMP", "") or path.parent)
	tmp_dir.mkdir(parents=True, exist_ok=True)
	tar_path = tmp_dir / f".lft_{uuid.uuid4().hex}_{path.name}.tar"
	with tarfile.open(tar_path, 'w') as tar:
		tar.add(path, arcname=path.name)

	output_path = Path(str(tar_path) + '.zst')
	# threads=-1 uses all logical cores so compression doesn't bottleneck a fast link
	cctx = zstd.ZstdCompressor(level=3, threads=-1)

	with open(tar_path, 'rb') as src, open(output_path, 'wb') as dst, tqdm(
		total=os.path.getsize(tar_path),
		desc="📦 Compressing",
		unit='B',
		unit_scale=True,
		disable=QUIET
	) as pbar:
		with cctx.stream_writer(dst) as compressor:
			while chunk := src.read(BUFFER_SIZE):
				compressor.write(chunk)
				pbar.update(len(chunk))

	tar_path.unlink()
	return output_path


def decompress_received_file(zst_path: Path):
	"""Decompress + extract next to the archive. Returns the extracted top-level paths."""
	tar_path = zst_path.with_suffix('')		# <id>__<name>.tar.zst -> <id>__<name>.tar
	dctx = zstd.ZstdDecompressor()

	with open(zst_path, 'rb') as src, open(tar_path, 'wb') as dst, tqdm(
		total=os.path.getsize(zst_path),
		desc="📦 Decompressing",
		unit='B',
		unit_scale=True,
		disable=QUIET
	) as pbar:
		last_pos = 0
		with dctx.stream_reader(src) as reader:
			while chunk := reader.read(BUFFER_SIZE):
				dst.write(chunk)
				pos = src.tell()
				pbar.update(pos - last_pos)
				last_pos = pos

	with tarfile.open(tar_path, 'r') as tar:
		top = sorted({n.split('/')[0] for n in tar.getnames() if n and not n.startswith('/')})
		tar.extractall(path=zst_path.parent, filter='data')

	tar_path.unlink()
	zst_path.unlink()
	log(f"✅ Decompressed and extracted to {zst_path.parent}")
	return [str(zst_path.parent / t) for t in top]


# ───────── RECEIVER ─────────
class tqdmWrapper:
	pbar = None

	def init(self, total_size, desc_str):
		if self.pbar is None:
			self.pbar = tqdm(total=total_size, desc=desc_str, unit='B', unit_scale=True, disable=QUIET)

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


def _validate_header(h: dict) -> dict:
	file_id = str(h["file_id"])
	if len(file_id) != 32 or any(c not in "0123456789abcdef" for c in file_id):
		raise ValueError("bad file_id")
	filename = Path(str(h["filename"])).name		# never a path from the peer
	if not filename or filename in (".", ".."):
		raise ValueError("bad filename")
	total_size, chunk_start, chunk_size = int(h["total_size"]), int(h["chunk_start"]), int(h["chunk_size"])
	num_chunks = int(h.get("num_chunks", MAX_CONNECTIONS))
	if total_size < 0 or chunk_start < 0 or chunk_size < 0 or chunk_start + chunk_size > total_size or not (1 <= num_chunks <= 64):
		raise ValueError("bad chunk range")
	return {"file_id": file_id, "filename": filename, "total_size": total_size,
			"chunk_start": chunk_start, "chunk_size": chunk_size, "num_chunks": num_chunks}


def start_receiver(psk: bytes, port: int, out_dir: Path, on_done=None):
	"""Start the listener thread. on_done(paths, nbytes) is called once a whole transfer is unpacked."""
	pbar = tqdmWrapper()
	pbar_lock = threading.Lock()
	out_dir.mkdir(mode=0o777, parents=True, exist_ok=True)

	def handle_client(conn, addr):
		chan = None
		target = None
		try:
			tune_socket(conn)
			conn.settimeout(HANDSHAKE_TIMEOUT)
			chan = server_handshake(conn, psk)
			rtype, payload = chan.recv_record()
			if rtype != REC_HEADER:
				raise ConnectionError("expected header record")
			header = _validate_header(json.loads(payload.decode()))
			conn.settimeout(IO_TIMEOUT)

			file_id		= header["file_id"]
			total_size	= header["total_size"]
			chunk_start = header["chunk_start"]
			chunk_size	= header["chunk_size"]
			target = out_dir / f"{file_id}__{header['filename']}"

			with pbar_lock:
				pbar.init(total_size, "📥 Receiving")

			_ensure_file(target, total_size)

			# Producer/consumer split: this thread does network recv only and
			# pushes records to a writer thread that decrypts and owns the file. Disk hiccups
			# (AV scan, page-cache flush, sparse-file allocation) no longer pause
			# the network — they just back-pressure via the queue.
			write_q = queue.Queue(maxsize=QUEUE_DEPTH)
			EOF = object()
			writer_state = {"error": None, "written": 0, "ended": False}

			def writer():
				try:
					with open(target, 'r+b') as f:
						f.seek(chunk_start)
						while True:
							item = write_q.get()
							if item is EOF:
								break
							if writer_state["error"]:
								continue			# drain so the network thread never blocks
							rt, ct, ctr = item
							data = chan.open(rt, ct, ctr)
							if rt == REC_END:
								writer_state["ended"] = True
								continue
							if writer_state["written"] + len(data) > chunk_size:
								raise ValueError("more data than announced")
							f.write(data)
							writer_state["written"] += len(data)
				except Exception as e:
					writer_state["error"] = e
					while write_q.get() is not EOF:	# keep draining until the producer stops
						pass

			wt = threading.Thread(target=writer, daemon=True)
			wt.start()

			try:
				while True:
					rt, ct, ctr = chan.read_raw()
					if rt not in (REC_DATA, REC_END):
						raise ConnectionError(f"unexpected record type {rt}")
					write_q.put((rt, ct, ctr))
					if rt == REC_END:
						break
					with pbar_lock:
						pbar.update(len(ct) - 16)
			finally:
				write_q.put(EOF)
				wt.join()

			if writer_state["error"]:
				raise writer_state["error"]
			if not writer_state["ended"] or writer_state["written"] != chunk_size:
				raise ConnectionError(f"chunk incomplete ({writer_state['written']}/{chunk_size} bytes)")

			with received_chunks_lock:
				received_chunks.setdefault(file_id, set()).add(chunk_start)
				done = len(received_chunks[file_id]) >= header["num_chunks"]
				if done:
					del received_chunks[file_id]

			if not done:
				chan.send_record(REC_ACK, json.dumps({"status": "ok"}).encode())
				return

			with pbar_lock:
				pbar.close()
				log("✅ Transfer complete.")
			with file_creation_guard:
				file_creation_locks.pop(str(target), None)
			paths = decompress_received_file(target)
			conn.settimeout(IO_TIMEOUT)
			chan.send_record(REC_ACK, json.dumps({"status": "done", "paths": paths}).encode())
			if on_done:
				on_done(paths, total_size)

		except PermissionError as e:
			log(f"⛔ Rejected connection from {addr[0]}: {e}")
		except Exception as e:
			log(f"❌ Error receiving chunk from {addr[0]}: {e}")
			try:
				if chan is not None:
					chan.send_record(REC_ACK, json.dumps({"status": "error", "msg": str(e)[:300]}).encode())
			except Exception:
				pass
			# don't leave a half-written archive behind (best effort; the sibling chunk may hold it open)
			if target is not None:
				with received_chunks_lock:
					received_chunks.pop(target.name.split("__", 1)[0], None)
				try:
					target.unlink()
				except OSError:
					pass
		finally:
			conn.close()

	server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	# Exclusive bind: a second listener on the same port must fail loudly, not silently split connections.
	if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
		server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
	else:
		server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	# Setting buffer sizes on the listening socket makes accepted sockets inherit them.
	tune_socket(server)
	try:
		server.bind(('', port))
	except OSError as e:
		raise SystemExit(f"❌ Cannot listen on TCP/{port}: {e} (another receiver running?)")
	server.listen()
	log(f"📥 Receiver listening on TCP/{port} → {out_dir}")

	def listener():
		while True:
			conn, addr = server.accept()
			threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

	threading.Thread(target=listener, daemon=True).start()
	return server


# ───────── SENDER ─────────
def send_file(ip: str, port: int, file_path: Path, psk: bytes):
	"""Send a file or folder. Returns the receiver's extracted paths. Raises on any failure."""
	log(f"Sending {file_path} to {ip}:{port}")
	file = compress_path(file_path)
	try:
		total_size = file.stat().st_size
		file_id = uuid.uuid4().hex

		chunk_size = total_size // MAX_CONNECTIONS
		ranges = [(i * chunk_size, chunk_size) for i in range(MAX_CONNECTIONS)]
		ranges[-1] = (ranges[-1][0], total_size - ranges[-1][0])

		pbar = tqdm(total=total_size, desc="📤 Sending", unit='B', unit_scale=True, disable=QUIET)
		pbar_lock = threading.Lock()
		results = [None] * len(ranges)

		def send_chunk(idx: int, start: int, size: int):
			try:
				header = {
					"file_id": file_id,
					"filename": file.name,
					"total_size": total_size,
					"chunk_start": start,
					"chunk_size": size,
					"num_chunks": len(ranges),
				}
				conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
				tune_socket(conn)
				conn.settimeout(HANDSHAKE_TIMEOUT)
				try:
					conn.connect((ip, port))
					chan = client_handshake(conn, psk)
					chan.send_record(REC_HEADER, json.dumps(header).encode())
					conn.settimeout(IO_TIMEOUT)

					# Producer/consumer split: a reader thread reads + encrypts from disk
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
										raise IOError("file shrank while sending")
									read_q.put((chan.seal(REC_DATA, data), len(data)))
									remaining -= len(data)
							read_q.put((chan.seal(REC_END, b""), 0))
						except Exception as e:
							reader_error.append(e)
						finally:
							read_q.put(EOF)

					rt = threading.Thread(target=reader, daemon=True)
					rt.start()
					try:
						while True:
							item = read_q.get()
							if item is EOF:
								break
							(hdr, ct), n = item
							chan.send_sealed(hdr, ct)
							if n:
								with pbar_lock:
									pbar.update(n)
					finally:
						rt.join()
					if reader_error:
						raise reader_error[0]

					conn.settimeout(ACK_TIMEOUT)
					rtype, payload = chan.recv_record()
					if rtype != REC_ACK:
						raise ConnectionError("expected ACK")
					ack = json.loads(payload.decode())
					if ack.get("status") not in ("ok", "done"):
						raise ConnectionError(f"receiver reported: {ack.get('msg', ack)}")
					results[idx] = ack
				finally:
					conn.close()
			except Exception as e:
				results[idx] = e

		threads = []
		for i, (start, size) in enumerate(ranges):
			t = threading.Thread(target=send_chunk, args=(i, start, size))
			t.start()
			threads.append(t)
		for t in threads:
			t.join()
		pbar.close()

		errors = [r for r in results if isinstance(r, Exception)]
		if errors:
			raise errors[0]
		done = [r for r in results if r.get("status") == "done"]
		if not done:
			raise ConnectionError("receiver never confirmed extraction")
		log("✅ Transfer complete.")
		return done[0].get("paths", [])
	finally:
		try:
			file.unlink()
		except OSError:
			pass


# ───────── MODES ─────────
def run_interactive(psk: bytes, port: int):
	my_ip = local_ip()
	log(f"My IP: {my_ip}")
	discover_peers(my_ip)
	start_receiver(psk, port, Path(__file__).resolve().parent / "received")
	time.sleep(2)

	while True:
		print("\n🌐 Available peers:")
		live_peers = list(peers.items())
		for i, (ip, info) in enumerate(live_peers):
			note = "" if info.get("v", 1) >= PROTO_VER else "  (v1 — update it, can't talk to v2)"
			print(f"{i+1}. {info['host']} ({ip}){note}")
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
			send_file(target_ip, peer["port"], Path(path), psk)
		except Exception as e:
			print(f"❌ Error: {e}")


def run_receive(psk: bytes, port: int, out_dir: Path, once: bool, timeout: float):
	finished = threading.Event()
	result = {}

	def on_done(paths, nbytes):
		result.update(paths=paths, bytes=nbytes)
		emit("done", paths=paths, bytes=nbytes)
		finished.set()

	start_receiver(psk, port, out_dir, on_done=on_done)
	emit("listening", port=port, out=str(out_dir))
	if not once:
		finished.wait(timeout) if timeout else threading.Event().wait()
		return 0
	if not finished.wait(timeout if timeout else None):
		emit("timeout", seconds=timeout)
		return 2
	time.sleep(0.5)		# let the ACK leave before the process exits
	return 0


def run_send(psk: bytes, host: str, port: int, path: Path):
	if not path.exists():
		emit("error", msg=f"not found: {path}")
		return 1
	t0 = time.time()
	try:
		paths = send_file(host, port, path, psk)
	except Exception as e:
		emit("error", msg=str(e))
		return 1
	dt = time.time() - t0
	emit("sent", paths=paths, seconds=round(dt, 2))
	return 0


def main(argv=None):
	global QUIET
	ap = argparse.ArgumentParser(description="LocalFileTransfer v2 (authenticated + encrypted)")
	ap.add_argument("--psk", type=Path, default=default_psk_path(), help="pre-shared key file")
	sub = ap.add_subparsers(dest="cmd")
	p_recv = sub.add_parser("receive", help="headless receiver")
	p_recv.add_argument("--port", type=int, default=TCP_PORT)
	p_recv.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "received")
	p_recv.add_argument("--once", action="store_true", help="exit after one complete transfer")
	p_recv.add_argument("--timeout", type=float, default=0, help="seconds to wait (0 = forever)")
	p_recv.add_argument("--quiet", action="store_true")
	p_send = sub.add_parser("send", help="headless sender")
	p_send.add_argument("path", type=Path)
	p_send.add_argument("--to", required=True, help="receiver host/IP")
	p_send.add_argument("--port", type=int, default=TCP_PORT)
	p_send.add_argument("--quiet", action="store_true")
	p_gen = sub.add_parser("genkey", help="create the pre-shared key")
	p_gen.add_argument("--force", action="store_true")
	sub.add_parser("lockkey", help="restrict the key file to the current user")
	p_int = sub.add_parser("interactive", help="discovery + menu (default)")
	p_int.add_argument("--port", type=int, default=TCP_PORT)
	args = ap.parse_args(argv)

	if args.cmd == "genkey":
		return cmd_genkey(args.psk, args.force)
	if args.cmd == "lockkey":
		lock_key_file(args.psk)
		print(f"✅ {args.psk} is now readable only by you.")
		return 0
	QUIET = getattr(args, "quiet", False)
	psk = load_psk(args.psk)
	if args.cmd == "receive":
		return run_receive(psk, args.port, args.out.resolve(), args.once, args.timeout)
	if args.cmd == "send":
		return run_send(psk, args.to, args.port, args.path)
	return run_interactive(psk, getattr(args, "port", TCP_PORT))


if __name__ == "__main__":
	sys.exit(main() or 0)
