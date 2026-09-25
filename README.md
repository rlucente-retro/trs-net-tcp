# trs-net-tcp

**TRS-NET TCP/IP Network & Serial Server Daemon for TRS-OS**

`trs-net-tcp` provides a host-side network disk server daemon (`trs-netd.py`) that serves virtual floppy disk images (`.dsk`) and spools printer output for **TRS-OS** (TRSDOS / LS-DOS 6.3 adapted for the Zilog eZ80) over **TCP/IP sockets** or direct **Serial COM / TTY ports**.

This enables retrocomputing systems—such as the **Agon family** (Agon Light, Agon Light 2, etc.)—to mount remote disk drives (e.g., Drive `:6`) and perform file operations (`COPY`, `BACKUP`, `DIR`) seamlessly:
* **Over Wi-Fi (TCP/IP):** Equipped with a Wi-Fi coprocessor supporting the **Espressif ESP-AT (v1.7.x or later)** command set API in transparent passthrough mode.
* **Over Serial:** Connected directly via a USB-to-serial adapter, such as the **Olimex MOD-USB-RS232** module on the UEXT expansion header, or traditional RS-232 serial cables.

---

## Background & Architecture

In the original TRS-NET architecture developed by **Daniel Paul Martin** ([danielpaulmartin.com](https://danielpaulmartin.com/home/research/)), `TRS-NET.py` communicated with the eZ80 target through a local serial COM / TTY port.

`trs-net-tcp` provides a unified server supporting both transports:
1. **TCP/IP Socket Transport:** Listens on a TCP socket (default port `65432`). On the Agon family, the Wi-Fi coprocessor connects to the host server and enters transparent UART-WiFi passthrough mode (`AT+CIPMODE=1` & `AT+CIPSEND`).
2. **Serial Port Transport:** Connects directly to a local serial device (e.g., `/dev/cu.usbmodem*` on macOS, `/dev/ttyACM*` on Linux, `COM*` on Windows) at configurable baud rates (default `115200 8-N-1`).
3. **Zero Wire-Protocol Changes:** The eZ80 disk driver (`driver-FDCDVR.S` / `driver-NETDVR.S`) speaks the exact same TRS-NET block protocol across both transports without any driver modifications.
4. **Resilient Streaming:** Implements a unified `StreamTransport` and framed `BufferedStreamReader` to eliminate chunk fragmentation and handle stray line delimiters across packet networks and serial streams alike.

```
+--------------------------+                         +--------------------------+
|       Agon Family        |                         |         Host PC          |
|                          |                         |                          |
|  +--------------------+  |                         |  +--------------------+  |
|  |       TRS-OS       |  |                         |  |    trs-netd.py     |  |
|  |  (Drive :6 driver) |  |                         |  |  (TCP/Serial Svr)  |  |
|  +---------+----------+  |                         |  +---------+----------+  |
|            | UART1       |                         |            |             |
|            |             |   Wi-Fi / LAN Network   |            |             |
|  +---------v----------+  |   (TCP port 65432)      |            |             |
|  |  Wi-Fi Coprocessor | <==========================> [TCP Mode] |             |
|  | (ESP-AT v1.7.x+)   |  |                         |            |             |
|  +--------------------+  |                         |            |             |
|            |             |   USB CDC-ACM / Serial  |            |             |
|  +---------v----------+  |   (/dev/cu.usbmodem*)   |            |             |
|  |   MOD-USB-RS232    | <==========================> [Serial]   |             |
|  +--------------------+  |                         |  +---------v----------+  |
+--------------------------+                         |  | Volumes/sys720k.dsk|  |
                                                     |  | printer/print_out  |  |
                                                     +--------------------------+
```

---

## Quick Start

### 1. Prerequisites
* Python 3.9+
* macOS, Linux, or Windows
* Optional: `pyserial>=3.5` (required only when using Serial mode)

### 2. Setup Virtual Environment
```bash
make setup
# Or manually:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Run in TCP/IP Mode
```bash
# Run with default TCP options (port 65432, Volumes/sys720k.dsk)
make run

# Or run with verbose debug logging
make run-verbose
```

### 4. Run in Serial Mode (e.g. Olimex MOD-USB-RS232)
```bash
# List all detected serial ports
make list-ports

# Auto-detect connected USB serial adapter and run
make run SERIAL=auto

# Or explicitly specify the serial device
make run SERIAL=/dev/cu.usbmodem14101

# Or run directly with Python at 115200 baud
python3 trs-netd.py --serial /dev/cu.usbmodem* --baud 115200
```

> [!TIP]
> On macOS, always use `/dev/cu.*` instead of `/dev/tty.*` to avoid carrier-detect blocking when using adapters without DCD lines.

---

## Command-Line Options

```text
usage: trs-netd.py [-h] [--port PORT] [--host HOST] [--serial [SERIAL]]
                   [--baud BAUD] [--rtscts] [--list-ports] [--volume VOLUME]
                   [--printer PRINTER] [--verbose]

TRS-NET TCP/IP Network & Serial Server Daemon for TRS-OS (trs-netd)

options:
  -h, --help            show this help message and exit
  --port, -p PORT       TCP port to listen on (default: 65432, ignored if --serial is set)
  --host, -H HOST       Host/IP address to bind to (default: 0.0.0.0, ignored if --serial is set)
  --serial, -s [SERIAL] Serial port device (e.g. /dev/cu.usbmodem*, /dev/ttyACM0, COM3, or 'auto')
  --baud, -b BAUD       Serial baud rate (default: 115200)
  --rtscts              Enable RTS/CTS hardware flow control for serial (default: False)
  --list-ports          List detected serial ports and exit
  --volume, -v VOLUME   Path to TRS-80 / TRS-OS disk volume file (.dsk)
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
# TCP mode
python3 trs-netd.py --volume Volumes/sys12M.dsk --port 65432

# Serial mode
python3 trs-netd.py --volume Volumes/sys12M.dsk --serial auto
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
* **TCP Transport:** Handshake (`@ping`), binding (`@bind`), sector reads/writes with checksums, 256B echo, print spooling, and recovery from bad checksums or stray delimiters.
* **Serial Transport:** Simulated via a pseudo-terminal (`pty`) testing `@ping`, `@bind`, sector read/write, echo, and printer spooling.
* **CLI Options:** Verification of `--list-ports` and argument parsing.

---

## Repository Structure

```
trs-net-tcp/
├── Makefile            # Convenience run, fetch, test, and clean targets
├── README.md           # Project documentation and protocol specification
├── MOD-USB-RS232.md    # Hardware guide for Olimex MOD-USB-RS232 module
├── requirements.txt    # Optional dependencies (pyserial for serial mode)
├── .gitignore          # Git exclusion rules (.venv, Volumes/, caches, etc.)
├── trs-netd.py         # Main TCP/IP and Serial network server daemon
├── test_trs_netd.py    # Integration & unit test suite (TCP and Serial)
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
* **trs-net-tcp Daemon:** Modernized TCP/IP and Serial implementation for networked Agon family and retrocomputing platforms.
