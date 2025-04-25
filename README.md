
# LocalFileTransfer python3 utility

Transfer files over LAN at maximum speed using TCP. Saturate gigabit connections.

This simple utility does the following:
- Discover other running scripts over LAN using UDP broadcasting
- Given a selected peer and a file/folder:
- Compress it into a .tar.zst file using zstandard compression
- Send over the compressed file using up to 32 TCP connections

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
