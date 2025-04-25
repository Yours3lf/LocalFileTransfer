Transfer files over LAN at maximum speed using TCP. Saturate gigabit connections.
This simple utility does the following:
- Discover other running scripts over LAN using UDP broadcasting
- Given a selected peer and a file/folder:
- Compress it into a .tar.zst file using zstandard compression
- Send over the compressed file using up to 32 TCP connections
Dependencies:
- zstandard for compression
- tqdm for progress bar
- Install deps using: pip install zstandard tqdm
