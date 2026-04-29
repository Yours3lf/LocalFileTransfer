
# LocalFileTransfer python3 utility

Transfer files over LAN at maximum speed using TCP. Saturate gigabit connections.

This simple utility does the following:
- Discover other running scripts over LAN using UDP broadcasting
- Given a selected peer and a file/folder:
- Compress it into a .tar.zst file using zstandard compression
- Send over the compressed file using up to 32 TCP connections
- Decompress on the other side back to the original files/folders

## Dependencies

- zstandard for compression
- tqdm for progress bar

Install deps using: 
> pip install zstandard tqdm

## Usage

Either double click the .py file
Or run
> python LocalFileTransfer.py

Then select a peer by typing the number of it and pressing enter.
Then drag and drop a file or folder into the cmdline window to paste the path of it.
Then press enter in the cmdline window to send that file/folder.

On the receiving end it'll be decompressed into a folder next to the python file called "received"

## Max throughput settings for Intel Wi-Fi (Advanced Settings)

- 802 a/b/g Wireless Mode:        5GHz 802.11a (don't use low throughput 2.4Ghz)
- 802 n/ac/ax/be Wireless Mode:   802.11be (highest)
- Channel Width for 2.4Ghz:       Auto
- Channel Width for 5GHz:         Auto
- Channel Width for 6Ghz:         Auto
- Fat channel intolerant:         Disabled
- Packet Coalescing:              Enabled
- Preferred Band:                 Prefer 6GHz (highest)
- Throughput booster:             Enabled (crucial for high throughput)
- Transmit power:                 Highest
- Ultra High Band (6GHz):         Enabled
