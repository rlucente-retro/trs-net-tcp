# trs-net-tcp

**TRS-NET TCP/IP Network Server Daemon for TRS-OS**

`trs-net-tcp` provides a host-side network disk server daemon (`trs-netd.py`) that serves virtual floppy disk images (`.dsk`) and spools printer output for **TRS-OS** (TRSDOS / LS-DOS 6.3 adapted for the Zilog eZ80) over **TCP/IP sockets** instead of legacy physical RS-232 serial cables.

This enables retrocomputing systems—such as the **Agon family** (Agon Light, Agon Light 2, etc.) equipped with a **MOD-WIFI-ESP8266** module—to mount remote disk drives (e.g., Drive `:6`) and perform file operations (`COPY`, `BACKUP`, `DIR`) seamlessly over Wi-Fi.

---

## Background & Architecture

In the original TRS-NET architecture developed by **Daniel Paul Martin** ([danielpaulmartin.com](https://danielpaulmartin.com/home/research/)), `TRS-NET.py` acted as a host-side server that communicated with the eZ80 target exclusively through a local serial COM / TTY port (`pyserial`) with hardware RTS/CTS flow control.

`trs-net-tcp` modernizes this architecture:
1. **TCP/IP Socket Transport:** Instead of opening a local serial port, `trs-netd.py` listens on a TCP socket (default port `65432` or configurable).
2. **Transparent Wi-Fi Passthrough:** On the Agon family, the connected MOD-WIFI-ESP8266 connects to the host server via TCP and enters transparent UART-WiFi passthrough mode (`AT+CIPMODE=1` & `AT+CIPSEND`).
3. **Zero Wire-Protocol Changes:** The eZ80 disk driver (`driver-FDCDVR.S` / `driver-NETDVR.S`) continues to speak the exact same TRS-NET block protocol; the ESP8266 and `trs-netd.py` transparently tunnel the stream across TCP/IP.
4. **Resilient Streaming:** Implements framed `recv_exact()` and buffered socket parsing to eliminate fragmentation issues common when tunneling serial protocols over packet networks.

```
+--------------------------+                         +--------------------------+
|       Agon Family        |                         |         Host PC          |
|                          |                         |                          |
|  +--------------------+  |                         |  +--------------------+  |
|  |       TRS-OS       |  |                         |  |    trs-netd.py     |  |
|  |  (Drive :6 driver) |  |                         |  |    (TCP Server)    |  |
|  +---------+----------+  |                         |  +---------+----------+  |
|            | UART1       |                         |            | TCP Socket  |
|  +---------v----------+  |   Wi-Fi / LAN Network   |  +---------v----------+  |
|  |  MOD-WIFI-ESP8266  | <===========================> |   port 65432 / TCP |  |
|  | (Transparent Mode) |  |                         |  +--------------------+  |
|  +--------------------+  |                         |  | Volumes/sys720k.dsk|  |
+--------------------------+                         |  | printer/print_out  |  |
                                                     +--------------------------+
```

---

## Quick Start

### 1. Prerequisites
* Python 3.9+
* macOS, Linux, or Windows

### 2. Setup Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Run the Server
```bash
# Run with default options (port 65432, Volumes/sys720k.dsk)
make run

# Or run with verbose debug logging
make run-verbose
```

### 4. Command-Line Options
```text
usage: trs-netd.py [-h] [--port PORT] [--host HOST] [--volume VOLUME]
                   [--printer PRINTER] [--verbose]

TRS-NET TCP/IP Network Server Daemon for TRS-OS (trs-netd)

options:
  -h, --help            show this help message and exit
  --port PORT, -p PORT  TCP port to listen on (default: 65432)
  --host HOST, -H HOST  Host/IP address to bind to (default: 0.0.0.0)
  --volume VOLUME, -v VOLUME
                        Path to TRS-80 / TRS-OS disk volume file (.dsk)
                        (default: Volumes/sys720k.dsk)
  --printer PRINTER     Path to printer spool output file
                        (default: printer/print_out.txt)
  --verbose, -V         Enable verbose debug logging of sector transfers and commands
```

---

## Fetching Disk Volumes (`make fetch`)

To keep this repository lean and avoid duplicating upstream binary assets in git, disk images are not tracked directly in the repository. Instead, the `Makefile` automatically downloads and unpacks them from Daniel Paul Martin's official distribution:

```bash
# Fetch and unpack disk images into Volumes/
make fetch
```

*(Note: `make run` will also automatically invoke `make fetch` if the default volume is not yet present).*

### Available Upstream Volumes:

| Image | Size | Format / Purpose |
| :--- | :--- | :--- |
| `Volumes/sys720k.dsk` | 720 KB | Default disk image. Standard 3.5" 720 KB TRS-OS disk with utilities. |
| `Volumes/sys12M.dsk` | 1.2 MB | 5.25" High-Density (1.2 MB) virtual disk image. |
| `Volumes/sys180k.dsk` | 180 KB | 5.25" Single-Sided Double-Density (180 KB) virtual disk image. |
| `Volumes/sys631.dsk` | 1.2 MB | TRSDOS 6.3.1 system disk image. |
| `Volumes/sys631.X.dsk` | 1.2 MB | TRSDOS 6.3.1 expanded distribution image. |
| `Volumes/bldtools.dsk` | 196 KB | Build tools and developer utilities disk. |

To serve an alternate volume:
```bash
python3 trs-netd.py --volume Volumes/sys12M.dsk --port 65432
```

To remove all downloaded assets and return the repo to its minimal footprint:
```bash
make distclean
```

---

## Protocol Reference

The TRS-NET wire protocol uses single-character prefixes followed by optional decimal parameters or raw binary blocks:

### 1. Sector Read (`<`) and Re-read (`\`)
* **Client Request:** `<00012\n` (command `<` or `\`, 5-digit ASCII sector index, `\n`)
* **Server Response:**
  * `256` bytes of raw sector payload (read from file offset `(rsector * 256) + 256`)
  * `4` bytes of big-endian 32-bit checksum (`struct.pack('>i', sum(sector) % 256)`)

### 2. Sector Write (`>`) and Re-write (`/`)
* **Client Request:** `>00012\n` followed by `256` bytes payload, followed by `4` bytes checksum.
* **Server Response:**
  * `4` bytes computed checksum (`struct.pack('>i', sum(sector) % 256)`) returned to client for verification.
  * Server commits the 256 bytes to disk image at offset `(rsector * 256) + 256`.

### 3. Printer Spooling (`#`)
* **Client Request:** `#00065\n` (character code in decimal ASCII)
* **Server Action:** Appends the character (`A`) to `printer/print_out.txt`.

### 4. Control Commands (`@`)
* `@ping\n` &rarr; Server responds with `@pong\n`. Used to verify connection liveness.
* `@bind\n` &rarr; Server responds with `@bound\n` followed by the 256-byte disk header (file offset `0..255`).
* `@rhdr\n` &rarr; Server returns 256-byte header, followed by requests for directory sectors.
* `@echo\n` &rarr; Client sends 256 bytes; server immediately echoes all 256 bytes back.
* `@stat\n` &rarr; Triggers the server to log active transfer statistics.
* `@rset\n` &rarr; Acknowledges client reset signal.

---

## Testing

Run the automated test suite:
```bash
make test
```

The test suite validates:
* `@ping` &rarr; `@pong` handshake
* `@bind` &rarr; `@bound` + 256-byte header delivery
* Sector reads with 8-bit checksum calculation
* Sector writes with readback verification
* 256-byte `@echo` transmission
* Printer byte spooling
* Session disconnect and reconnect handling

---

## Repository Structure

```
trs-net-tcp/
├── Makefile            # Convenience run, fetch, test, and clean targets
├── README.md           # Project documentation and protocol specification
├── .gitignore          # Git exclusion rules (.venv, Volumes/, caches, etc.)
├── trs-netd.py         # Main TCP/IP network server daemon
├── test_trs_netd.py    # Integration & unit test suite
├── Volumes/            # Virtual floppy disk images (fetched via 'make fetch')
│   ├── sys720k.dsk
│   ├── sys12M.dsk
│   └── ...
└── printer/            # Printer spool output directory (created on demand)
    └── print_out.txt
```

---

## Credits & License

* **TRS-OS & TRS-NET Protocol:** Created and maintained by **Daniel Paul Martin** ([danielpaulmartin.com](https://danielpaulmartin.com/home/research/)).
* **trs-net-tcp Daemon:** Modernized TCP/IP implementation for networked Agon family and retrocomputing platforms.
