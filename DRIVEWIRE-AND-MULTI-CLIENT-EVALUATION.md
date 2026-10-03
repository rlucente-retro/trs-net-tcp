# Architectural Evaluation: TRS-NET, Multi-Client Concurrency, and DriveWire Integration

**Target Systems:**
* **Host Daemon:** `trs-net-tcp` (`trs-netd.py`)
* **Operating System Target:** TRS-OS (TRSDOS 6.3.1 / LS-DOS 6.3 adapted for Zilog eZ80 on the Agon family of computers)
* **OS Source Code:** `trs-os-zds` (`TRSDOS_7`)
* **Hardware Interconnects:** 
  * Wi-Fi TCP/IP via Espressif ESP-AT (v1.7.x+) in transparent passthrough mode (`CIPMODE=1`)
  * Direct USB CDC-ACM / RS-232 serial via Olimex MOD-USB-RS232 on the Agon 10-pin UEXT expansion header

---

## Table of Contents

1. [Existing Codebase Architecture (trs-net-tcp)](#1-existing-codebase-architecture-trs-net-tcp)
   - [Host Server Architecture (`trs-netd.py`)](#host-server-architecture-trs-netdpy)
   - [TRS-NET Wire Protocol Specification](#trs-net-wire-protocol-specification)
   - [Disk Conversion (`jv1_to_dsk.py`)](#disk-conversion-jv1_to_dskpy)
   - [Integration Testing & Framing Edge Cases (`test_trs_netd.py`)](#integration-testing--framing-edge-cases-test_trs_netdpy)
   - [Hardware Integration (`MOD-USB-RS232.md`)](#hardware-integration-mod-usb-rs232md)
2. [Multi-Client Concurrency in trs-netd.py](#2-multi-client-concurrency-in-trs-netdpy)
   - [Single-Threaded Blocking Accept Loop](#single-threaded-blocking-accept-loop)
   - [Point-to-Point Physical Serial Constraints](#point-to-point-physical-serial-constraints)
   - [Shared File Descriptors and Caching Barriers](#shared-file-descriptors-and-caching-barriers)
3. [Dual-Transport Listening: Serial & TCP Simultaneously](#3-dual-transport-listening-serial--tcp-simultaneously)
   - [Asymmetric Connection Semantics](#asymmetric-connection-semantics)
   - [Cross-Platform I/O Multiplexing (POSIX vs. Windows)](#cross-platform-io-multiplexing-posix-vs-windows)
   - [Byte-Stealing and Line Noise on Serial](#byte-stealing-and-line-noise-on-serial)
   - [The Retrocomputing Inactivity Dilemma](#the-retrocomputing-inactivity-dilemma)
   - [Three Architectural Solutions Evaluated](#three-architectural-solutions-evaluated)
4. [DriveWire Protocol Analysis](#4-drivewire-protocol-analysis)
   - [Wire Protocol Specifications & Packet Formats](#wire-protocol-specifications--packet-formats)
   - [Point-to-Point Wire Semantics](#point-to-point-wire-semantics)
   - [Virtual Serial Channels vs. Multi-Client](#virtual-serial-channels-vs-multi-client)
   - [How DriveWire Servers Support Multiple Clients: The "Instance" Model](#how-drivewire-servers-support-multiple-clients-the-instance-model)
5. [Implementing DriveWire in trs-os-zds (TRS-OS)](#5-implementing-drivewire-in-trs-os-zds-trs-os)
   - [Block Storage Driver (`driver-FDCDVR.S`)](#block-storage-driver-driver-fdcdvrs)
   - [16-Bit Checksum Assembly Routine](#16-bit-checksum-assembly-routine)
   - [Line Printer Driver (`driver-PRDVR.S`)](#line-printer-driver-driver-prdvrs)
   - [Equates and Global Constants (`TRSDOS_GLOBAL/`)](#equates-and-global-constants-trsdos_global)
   - [What Remains Completely Unchanged](#what-remains-completely-unchanged)
6. [Post-Boot Dynamic Mounting vs. Pre-Boot IPL Binding](#6-post-boot-dynamic-mounting-vs-pre-boot-ipl-binding)
   - [Why Binding Was Originally Placed in IPL](#why-binding-was-originally-placed-in-ipl)
   - [Device Control Table (DCT) Mechanics in LS-DOS 6.3](#device-control-table-dct-mechanics-in-ls-dos-63)
   - [The `DW.CMD` / `MOUNT.CMD` Dynamic Workflow](#the-dwcmd--mountcmd-dynamic-workflow)
   - [Native LS-DOS Dynamic Media Sensing via GAT Sector](#native-ls-dos-dynamic-media-sensing-via-gat-sector)
   - [Advantages of Moving Mounting to OS-Level CLI](#advantages-of-moving-mounting-to-os-level-cli)
7. [Printer Spooling Under DriveWire](#7-printer-spooling-under-drivewire)
   - [Protocol Efficiency Comparison](#protocol-efficiency-comparison)
   - [Server-Side Spooling and PDF Capabilities](#server-side-spooling-and-pdf-capabilities)
   - [Assembly Driver Implementation](#assembly-driver-implementation)
8. [Comprehensive Comparison & Recommendations](#8-comprehensive-comparison--recommendations)

---

## 1. Existing Codebase Architecture (trs-net-tcp)

The `trs-net-tcp` repository provides a host daemon (`trs-netd.py`) that serves virtual floppy disk images (`.dsk`) and spools printer output for **TRS-OS** (TRSDOS 6.3.1 / LS-DOS 6.3 adapted for the Zilog eZ80 on the Agon family of computers).

```
+------------------------------------+                         +------------------------------------+
|            Agon Target             |                         |              Host PC               |
|                                    |                         |                                    |
|  +------------------------------+  |                         |  +------------------------------+  |
|  |     TRS-OS (Drive :6)        |  |                         |  |         trs-netd.py          |  |
|  |     driver-FDCDVR.S          |  |                         |  |         Server Daemon        |  |
|  +--------------+---------------+  |                         |  +--------------+---------------+  |
|                 | UART1            |                         |                 |                  |
|                 | (115200 8-N-1)   |   Wi-Fi TCP/IP (65432)  |   [TCP Mode]    |                  |
|  +--------------v---------------+  | <=======================> SocketTransport |                  |
|  | Wi-Fi Coprocessor (ESP8266)  |  |                         |   [Serial Mode] |                  |
|  | ESP-AT v1.7.x+ (CIPMODE=1)   |  |   USB CDC-ACM / Serial  | SerialTransport |                  |
|  +------------------------------+  | <=======================> (pyserial)      |                  |
|                 | UEXT Ribbon Cable|   (/dev/cu.usbmodem*)   +--------+--------+------------------+
|  +--------------v---------------+  |                                  |
|  | Olimex MOD-USB-RS232 Header  |  |                                  v
|  +------------------------------+  |                 +------------------------------------+
+------------------------------------+                 | BufferedStreamReader               |
                                                       +-----------------+------------------+
                                                                         |
                                                                         v
                                                       +------------------------------------+
                                                       | DiskVolume: Volumes/*.dsk (Locked) |
                                                       | Spooler:    printer/print_out.txt  |
                                                       +------------------------------------+
```

### Host Server Architecture (`trs-netd.py`)

* **Transport Abstraction Layer (`StreamTransport` Protocol):**
  * `SocketTransport`: Wraps standard TCP sockets. Sets `socket.IPPROTO_TCP, socket.TCP_NODELAY, 1` to disable the Nagle algorithm, eliminating transmission latency on small sector request packets. Uses `select.select()` for non-blocking operations.
  * `SerialTransport`: PySerial-based wrapper. Probes `in_waiting` to retrieve immediately available bytes, dynamically tuning read timeouts to prevent blocking.
* **Stream Framing (`BufferedStreamReader`):**
  * Solves TCP packet fragmentation, coalescing, and arbitrary serial chunk arrivals.
  * `recv_exact(count, timeout)`: Loops until the internal `_buffer` holds at least `count` bytes.
  * `readline(timeout)`: Reads until `\n`, stripping trailing `\r`.
  * `read_cmd_byte()`: Strips all inter-command whitespace (`\r`, `\n`, `\x00`, `' '`), preventing desynchronization caused by line delimiters. Handles 1.0-second idle ticks to poll daemon running state.
* **Disk Volume Management (`DiskVolume`):**
  * Handles `.dsk` files where offset `0..255` is the 256-byte `DiskDISK` geometry header, and each sector $S$ is addressed at `(S * 256) + 256`.
  * Acquires an exclusive advisory file lock on startup via `fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)` to prevent concurrent process corruption.
  * Forces physical disk sync on every sector write via `os.fsync(self.file.fileno())` to prevent data loss on unexpected power cuts.
* **Daemon Management (`TRSNetDaemon`):**
  * Signal safety: Signal handlers for `SIGINT` and `SIGTERM` are strictly async-signal-safe (assigning variables only, avoiding I/O or logging in signal context).
  * Auto-port detection (`find_auto_serial_port`): Scans COM ports, filters out virtual Bluetooth/debug devices, and on macOS prioritizes `/dev/cu.*` over `/dev/tty.*` to avoid blocking on carrier-detect (DCD).

---

### TRS-NET Wire Protocol Specification

The TRS-NET wire protocol uses single-character prefixes followed by decimal ASCII arguments or raw 256-byte binary payloads:

| Command | Wire Format (Client $\rightarrow$ Server) | Server Action | Server Response (Server $\rightarrow$ Client) |
| :--- | :--- | :--- | :--- |
| **Sector Read** | `<00012\n` | Reads sector 12 from offset `(12 * 256) + 256` | 256B raw payload + 4B big-endian checksum (`struct.pack('>i', sum(sector) % 256)`) |
| **Sector Re-Read** | `\00012\n` | Re-reads sector 12 (tracks retry statistic) | 256B raw payload + 4B big-endian checksum |
| **Sector Write** | `>00012\n` + 256B data + 4B checksum | Verifies 8-bit checksum (`sum % 256 == client_chk[3]`). Commits to disk only if valid. | 4B big-endian computed checksum (`struct.pack('>i', calc_chk)`) |
| **Sector Re-Write** | `/00012\n` + 256B data + 4B checksum | Re-writes sector 12 (tracks retry statistic) | 4B big-endian computed checksum |
| **Printer Spool** | `#00065\n` | Appends ASCII character `chr(65)` (`'A'`) | None |
| **Ping** | `@ping\n` | Liveness check | `@pong\n` |
| **Bind** | `@bind\n` | Binds drive and queries geometry | `@bound\n` followed by 256-byte `DiskDISK` header (offset `0..255`) |
| **Read Header / Dir** | `@rhdr\n` | Multi-stage boot query: sends header, then receives `<sec1\n`, then `<sec2\n` | 256B header, 256B dir sector 1, 256B dir sector 2 |
| **Echo Test** | `@echo\n` + 256B payload | Data line integrity check | Echoes identical 256B payload back |
| **Statistics** | `@stat\n` | Logs active server transaction statistics | None |
| **Reset** | `@rset\n` | Acknowledges client reset signal | None |

---

### Disk Conversion (`jv1_to_dsk.py`)

Raw TRS-80 JV1 floppy images contain only sector data without header metadata (e.g. 35 tracks $\times$ 10 sectors/track $\times$ 256 bytes = 89,600 bytes). `jv1_to_dsk.py` converts JV1 images into the 256-byte header `DiskDISK` format:

1. **256-Byte DiskDISK Header:**
   * Bytes `0..7`: ASCII `b"DiskDISK"`
   * Bytes `8..10`: Z80 jump instruction `0xC3 0x08 0x2E` (`JP 2E08h`)
   * Bytes `11..17`: 7 Device Control Table (DCT) parameters (`DCT+3` through `DCT+9`):
     * `DCT+3`: Density / write-protect flags (`0x00` = single-density 5.25", WP off).
     * `DCT+4`: Drive type & side flags (`0x10` = single-sided 5.25").
     * `DCT+5`: Current cylinder (`0x00`).
     * `DCT+6`: Max cylinder index (`tracks - 1`, e.g., 34 for 35 tracks).
     * `DCT+7`: Heads (bits 7..5) & sectors/track (bits 4..0): `((heads - 1) << 5) | (sec/trk - 1)` (`0x09`).
     * `DCT+8`: Granules/track (bits 7..5) & sectors/granule (bits 4..0): `((grans/trk - 1) << 5) | (sec/gran - 1)` (`0x24` = 2 grans/trk, 5 sec/gran).
     * `DCT+9`: Directory cylinder (`0x11` = Track 17).
   * Byte `18`: Granule size in sectors (`5`).
2. **LS-DOS 6 GAT Sector Patch:**
   * Locates Track 17, Sector 0 (offset `256 + 17 * 10 * 256 = 43776`).
   * Patches offset `+245..255` with `\x03LSI` + the 7 DCT parameters. LS-DOS 6.3 validates this block to mount the filesystem.

---

### Integration Testing & Framing Edge Cases (`test_trs_netd.py`)

The test suite validates both TCP and Serial transport modes:
* **TCP Integration (`TestTRSNetDaemonTCP`):** Spawns `trs-netd.py` against synthetic disk images, testing handshakes, sector reads/writes, printer spooling, multi-request sessions, CRLF/whitespace delimiter resilience, and write-checksum rejection.
* **Serial Integration (`TestTRSNetDaemonSerial`):** Allocates a POSIX pseudo-terminal pair via `pty.openpty()` and `tty.setraw()`, proving the server runs identically over raw serial streams.
* **Binary Integrity (`TestJV1ToDSKConversion`):** Tests synthetic conversion and parses TRS-80 directory extents and machine-code transfer address records (`rec_type == 2`) in `LIFE.CMD`.

> [!NOTE]
> **Client Framing Discovery in `test_trs_netd.py`:**
> During testing, an edge case was uncovered in the test client's `_recv_line` method: calling `sock.recv(64)` with a local buffer can consume bytes beyond `\n`. If the server sends `@bound\n` immediately followed by the 256-byte header, a client using an unbuffered `_recv_line` consumes part of the binary header and discards it, causing subsequent `_recv_exact(256)` calls to block and time out. The daemon's `BufferedStreamReader` avoids this by maintaining a persistent `_buffer`.

---

### Hardware Integration (`MOD-USB-RS232.md`)

Hardware reference for connecting the Olimex MOD-USB-RS232 to the Agon Light 2:
* **Signal Voltage:** Operates at **3.3V TTL/CMOS**. Despite the "RS232" branding, it does **not** output $\pm 12\text{V}$, making it electrically safe for direct connection to the eZ80.
* **UEXT Connector Pinout:**
  * Pin 1: 3.3V Rail (Target power)
  * Pin 2: Ground
  * Pin 3: TXD (eZ80 `PC0` TXD1 output) $\rightarrow$ PIC `RB5` RXD input
  * Pin 4: RXD (eZ80 `PC1` RXD1 input) $\leftarrow$ PIC `RB7` TXD output
  * Pins 5–10: I2C/SPI (unconnected on module)
* **Critical Solder Jumper:** `UEXT_PWR_3.3` **MUST REMAIN OPEN**. Closing it bridges the Agon Light 2 power supply regulator to the USB module's LM1117 regulator, causing power rail conflict.
* **Firmware Flashing Contingency:** Certain Olimex manufacturing batches shipped with factory test loopback firmware (`UEXT test ERROR!`). The guide details re-flashing the Microchip PIC18F14K50 with `Prebuilt.hex` using MPLAB X IPE / `ipecmd` via the 6-pin mini-ICSP header (`WU06S`, 1.27 mm pitch) powered strictly at 3.3V.

---

## 2. Multi-Client Concurrency in trs-netd.py

`trs-netd.py` **cannot handle multiple simultaneous clients**. It is strictly sequential for four reasons:

```
[Main Thread] -> s.accept() -> Client A connects -> ClientSession(A).handle() (BLOCKS HERE)
                                                          |
                                                          +--> Client B connects -> Queued in kernel listen(5) backlog
                                                          |    (Client B receives NO response until Client A exits!)
```

1. **Synchronous Blocking Accept Loop:**
   In [`_run_tcp()`](file:///Users/richardlucente/development/git/trs-net-tcp/trs-netd.py#L759-L780), `s.accept()` returns a client socket, and the main thread immediately invokes `session.handle()`. `session.handle()` loops reading commands until the client disconnects or times out. During this time, the thread never returns to `select.select()` to call `accept()` on incoming connections.
2. **Point-to-Point Physical Serial Constraints:**
   Serial connections (RS-232 / USB CDC-ACM) are physically 1:1 links.
3. **Shared File Descriptors:**
   In `DiskVolume`, sector I/O uses a single file handle (`self.file.seek` followed by `read` or `write`). If multiple threads accessed `DiskVolume` concurrently without locks or atomic `os.pread`/`os.pwrite`, seeking in one thread would race with I/O in another.
4. **Filesystem Caching Constraints in TRS-OS:**
   TRSDOS and LS-DOS 6.3 are single-user operating systems. The OS caches the Granule Allocation Table (GAT) and directory buffers in RAM. If two clients mount the same `.dsk` image with write access, they overwrite each other's allocation maps, resulting in immediate filesystem corruption.

---

## 3. Dual-Transport Listening: Serial & TCP Simultaneously

To allow clients to connect either via Wi-Fi (TCP) or USB Serial without restarting `trs-netd.py`, the server must listen on both interfaces simultaneously.

### Technical Challenges

1. **Asymmetric Connection Semantics:**
   * **TCP:** Has explicit connection signaling (SYN/ACK). The listening socket indicates readable in `select()` when a connection arrives.
   * **Serial:** Opening `/dev/cu.usbmodem*` opens the host USB driver, but does not indicate whether an Agon is attached or booted. On the Agon UEXT bus, only `TXD1`, `RXD1`, and `GND` are connected; there are **no DTR/DSR, RTS/CTS, or DCD lines**. The server can only detect a serial client when the **first byte of data** arrives.
2. **Cross-Platform I/O Multiplexing (POSIX vs. Windows):**
   * On macOS and Linux, `select.select([sock, ser], [], [])` can poll both socket and serial file descriptors (`ser.fileno()`).
   * On Windows, Winsock `select()` fails on COM ports (`OSError: [WinError 10038]`). A cross-platform implementation requires a **threaded listener arbiter**.
3. **Byte-Stealing and Line Noise on Serial:**
   * To detect a serial connection, the listener reads 1 byte from `ser.read(1)`. That byte is the command opcode (e.g. `@`, `<`, `>`, `#`). It must not be discarded; it must be prepended back into `BufferedStreamReader._buffer`.
   * Floating UART pins on power-up emit line noise (`0xFF`, `0x00`). The listener must only latch when receiving a valid command prefix.

---

### The Retrocomputing Inactivity Dilemma

In general network services, servers terminate inactive connections with an idle timeout (e.g., 30–60 seconds).

> [!WARNING]
> **Why Inactivity Timeouts Fail in Retrocomputing:**
> An 8-bit computer loads a program (such as a game, spreadsheet, or text editor) completely into RAM. The user may play a game for hours with **zero disk activity**.
> 
> * If the server terminates the serial connection due to an idle timer, a subsequent disk write (e.g., saving a high score) will fail.
> * If the server holds the serial session open indefinitely, it can never revert to listening for TCP, because the host USB adapter remains open at the OS level even if the Agon is switched off.

---

### Three Architectural Solutions Evaluated

#### Approach 1: Transaction-Level Multiplexing (Recommended)
The key insight is that **the TRS-NET wire protocol is essentially stateless RPC**. Every command embeds its parameters (e.g., `<00012\n` specifies sector 12 directly). `ClientSession` maintains no session state.

```
                  +-----------------------------------------------+
                  |                  trs-netd.py                  |
                  |          Dual-Listening Event Loop            |
                  +---------------+-------------------------------+
                                  |
               +------------------+------------------+
               |                                     |
               v                                     v
     [TCP Listener: Port 65432]             [Serial Listener: UART]
     Reads TCP Request Packet               Reads Serial Request Packet
               |                                     |
               +------------------+------------------+
                                  |
                                  v
                  +--------------------------------+
                  |     Transaction Mutex Lock     |
                  |    Executes Single Sector I/O  |
                  +---------------+----------------+
                                  |
                                  v
                  Send Response to originating transport
                  and immediately resume Dual-Listening!
```

* **Zero Timeouts:** The serial connection is never severed. A game can sit in RAM for 4 hours; when it sends `>00012\n`, the server responds immediately.
* **Dual Availability:** If the user resets the Agon and connects over Wi-Fi, the TCP socket is open and ready.
* **Atomic Protection:** Multi-stage commands (specifically `@rhdr`, which exchanges the header, then sector 1, then sector 2) hold the transaction mutex until all three phases complete.

#### Approach 2: Bind-Latching with Explicit Reset
* Whichever transport sends `@bind\n` acquires an exclusive lock.
* Releases only when TRS-OS sends `@rset\n` or when the USB cable is physically unplugged.
* *Drawback:* If the user powers off the Agon without unmounting, the server remains locked to Serial.

#### Approach 3: Handshake Preemption
* Serial client remains active with no timeout.
* If a new `@bind\n` or `@ping\n` handshake arrives on TCP, the server flushes serial buffers and transfers the active lease to TCP.

---

## 4. DriveWire Protocol Analysis

**DriveWire** (developed by Boisy Pitre, Aaron Wolfe, and the Color Computer community) defines a standard for vintage computers to access mass storage, printing, and networking on a host PC.

### Wire Protocol Specifications & Packet Formats

Unlike TRS-NET's ASCII lines, DriveWire uses **binary opcodes**:

```
DriveWire OP_READ (0x52):
Client: [ 0x52 ] [ Drive# (0-255) ] [ LSN_23..16 ] [ LSN_15..8 ] [ LSN_7..0 ]   (5 bytes)
Server: [ Status (0=OK) ] [ Checksum_15..8 ] [ Checksum_7..0 ] [ 256B Data ]   (259 bytes)

DriveWire OP_WRITE (0x57):
Client: [ 0x57 ] [ Drive# (0-255) ] [ LSN_23..16 ] [ LSN_15..8 ] [ LSN_7..0 ] [ 256B Data ] [ Checksum_15..8 ] [ Checksum_7..0 ] (263 bytes)
Server: [ Status (0=OK, 243=CRC Error) ]                                        (1 byte)
```

| Opcode | Hex | Description | Request Structure | Response Structure |
| :--- | :--- | :--- | :--- | :--- |
| `OP_NOP` | `0x00` | No Operation / Connection check | `[0x00]` | None |
| `OP_TIME` | `0x23` | Query host real-time clock | `[0x23]` | 4 bytes (Year, Month, Day, Hour, Min, Sec) |
| `OP_PRINTFLUSH` | `0x46` | Flush print buffer to spooler | `[0x46]` | None |
| `OP_PRINT` | `0x50` | Print single character byte | `[0x50] [char_byte]` | None |
| `OP_READ` | `0x52` | Read 256-byte sector | `[0x52] [Drive] [LSN (3B)]` | `[Status (1B)] [Checksum (2B)] [256B Data]` |
| `OP_WRITE` | `0x57` | Write 256-byte sector | `[0x57] [Drive] [LSN (3B)] [256B] [Chk (2B)]` | `[Status (1B)]` (`0` = OK, `243` = CRC error) |
| `OP_DWINIT` | `0x5A` | Query driver/server capabilities | `[0x5A] [Version (1B)]` | Capability response byte |
| `OP_REREAD` | `0x72` | Re-read sector on checksum retry | `[0x72] [Drive] [LSN (3B)]` | `[Status] [Checksum (2B)] [256B Data]` |
| `OP_REWRITE` | `0x77` | Re-write sector on error retry | `[0x77] [Drive] [LSN (3B)] [256B] [Chk (2B)]` | `[Status]` |
| `OP_RESET3` | `0xF8` | Hardware reset notification | `[0xF8]` | None (Server flushes caches) |

* **16-Bit Checksum Formula:** DriveWire computes a 16-bit big-endian sum across all 256 sector bytes:
  $$\text{Checksum} = \sum_{i=0}^{255} \text{byte}_i \pmod{65536}$$
* **Point-to-Point Semantics:** There are **no Client IDs, MAC addresses, or routing tokens** in DriveWire wire packets.

---

### Virtual Serial Channels vs. Multi-Client

DriveWire 4 introduced **Virtual Serial Channels** (`OP_SERREAD`, `OP_SERWRITE`, `OP_POLL`). These provide up to 256 logical sub-channels over a single serial line. 
* This is **internal multiplexing for a single client computer** (primarily for the multitasking OS-9 operating system), allowing window descriptors `/N0` through `/N14` to run a virtual modem, Telnet terminal, MIDI synthesizer, and disk I/O over one physical link.
* It does **not** allow multiple separate computers to share one wire.

---

### How DriveWire Servers Support Multiple Clients: The "Instance" Model

Because the wire protocol is 1:1, DriveWire servers (DriveWire 4 Java by Aaron Wolfe and `pyDriveWire` by Mike Furman) achieve simultaneous multi-client support using an **Instance Manager**:

```
                              +---------------------------------------+
                              |         DriveWire 4 Server            |
                              |          (Instance Manager)           |
                              +---+-------------------------------+---+
                                  |                               |
                     Instance 0   |                  Instance 1   |
                     (Serial)     |                  (TCP Socket) |
                                  v                               v
        +-----------------------------------+   +-----------------------------------+
        |       Instance 0 State Engine     |   |       Instance 1 State Engine     |
        | Endpoint: /dev/ttyUSB0 (115.2k)   |   | Endpoint: TCP Port 65504          |
        | Virtual Drives:                   |   | Virtual Drives:                   |
        |   Drive 0: os9_dev.dsk            |   |   Drive 0: coco_games.dsk         |
        |   Drive 1: scratch.dsk            |   |   Drive 1: empty                  |
        +-----------------------------------+   +-----------------------------------+
```

#### In `pyDriveWire` configuration syntax:
```ini
# Instance 0 (TCP Server)
option accept True
option port 65504
dw disk insert 0 /disks/coco_system.dsk

[serial_agon]
# Instance 1 (Serial Connection)
option port /dev/ttyUSB0
option speed 115200
dw disk insert 0 /disks/trsos_sys720k.dsk
```

1. **Independent Endpoints:** Each instance binds to its own dedicated serial device or TCP port.
2. **Isolated Drive Stacks:** Each instance has its own virtual drive table (Drives 0–255).
3. **Disk Isolation:** Concurrent writes to the same disk image across instances are forbidden to protect OS buffer caches.

---

## 5. Implementing DriveWire in trs-os-zds (TRS-OS)

Adapting the TRS-OS assembly codebase (`~/development/git/trs-os-zds`) to speak DriveWire is localized to a small set of files:

### Block Storage Driver (`driver-FDCDVR.S`)

#### Current TRS-NET Read vs. DriveWire Read:

```assembly
; =============================================================================
; CURRENT TRS-NET IMPLEMENTATION (driver-FDCDVR.S)
; =============================================================================
        LD      a, '<'
        ld      (net_cmd), a
        HEXDEC  (fdc_no_sum), net_cmd+1         ; Convert sector to ASCII (EXPENSIVE)
        ZEROS_RPL_SPACE net_cmd+1               ; Convert spaces to '0's
        out0    (UART1_FCTL), 07h               ; Flush FIFOs
        PUMP    DEVICE.serialnet, net_cmd       ; Send '<00012\n' (7 bytes)
        UART1.get.bytes 0, hl, fdc_rd_timeout   ; Receive 256 bytes data
        UART1.get.bytes 4, chk_sum, fdc_rd_timeout ; Receive 4 bytes checksum
        GOSUB   calc_cksum                      ; 8-bit sum (sum % 256)
        ld      b, a
        ld      a, (chk_sum+3)
        cp      b
        IF_NE_GOTO rd_retry_cksum               ; Retry with '\00012\n'

; =============================================================================
; PROPOSED DRIVEWIRE IMPLEMENTATION (driver-FDCDVR.S)
; =============================================================================
dw_cmd_buf:     db      52h, 00h, 00h, 00h, 00h ; [OP_READ, Drive, LSN_hi, LSN_mid, LSN_lo]
dw_chk_buf:     dw      0000h                   ; 16-bit big-endian checksum

RDIN_DRIVEWIRE:
        ld      a, 52h                          ; OP_READ (0x52)
        ld      (dw_cmd_buf), a
        ld      a, (LDRV$)                      ; Drive number
        ld      (dw_cmd_buf+1), a
        xor     a
        ld      (dw_cmd_buf+2), a               ; LSN bit 23..16 (always 0 on 720K/1.2M)
        ld      hl, (fdc_no_sum)                ; 16-bit sector number from calcsec
        ld      a, h
        ld      (dw_cmd_buf+3), a               ; LSN bit 15..8
        ld      a, l
        ld      (dw_cmd_buf+4), a               ; LSN bit 7..0

        out0    (UART1_FCTL), 07h               ; Flush UART1 FIFOs
        UART1.put.bytes 5, dw_cmd_buf, fdc_rd_err ; Send compact 5-byte request!

        UART1.get.byte  fdc_rd_timeout          ; Receive 1-byte Status (0 = OK)
        or      a
        jr      nz, rd_retry_error              ; Non-zero indicates error

        UART1.get.bytes 2, dw_chk_buf, fdc_rd_timeout ; Receive 2-byte 16-bit Checksum
        pop     hl                              ; Recover buffer pointer
        push    hl
        UART1.get.bytes 0, hl, fdc_rd_timeout   ; Receive 256-byte payload

        pop     hl
        push    hl
        call    calc_cksum_16                   ; Compute 16-bit sum in DE
        ld      hl, (dw_chk_buf)                ; Received checksum
        ; Compare DE (calculated) with HL (received big-endian)
        ld      a, d
        cp      h
        jr      nz, rd_retry_cksum
        ld      a, e
        cp      l
        jr      nz, rd_retry_cksum
        ; Success!
```

---

### 16-Bit Checksum Assembly Routine

Replace the current 8-bit checksum routine with this 16-bit addition loop on the eZ80:

```assembly
; =============================================================================
; 16-Bit Big-Endian Checksum (DriveWire Specification Appendix B)
; Inputs:  HL = 256-byte buffer pointer
; Outputs: DE = 16-bit sum (D = MSB, E = LSB)
; =============================================================================
calc_cksum_16:
        push    bc
        ld      b, 0                            ; Loop 256 times (DJNZ rolls over 0 -> 256)
        ld      de, 0                           ; Clear 16-bit accumulator
cksum_loop:
        ld      a, (hl)
        add     a, e                            ; Add byte to low accumulator
        ld      e, a
        jr      nc, no_carry
        inc     d                               ; Carry to high accumulator
no_carry:
        inc     hl
        djnz    cksum_loop
        pop     bc
        ret
```

---

### Line Printer Driver (`driver-PRDVR.S`)

```assembly
; =============================================================================
; CURRENT TRS-NET: 7 bytes ASCII with decimal conversion
; =============================================================================
PR_NET_CMD      db      "#00000", LF.asc
$?2             ld      h, 0
                ld      l, C
                HEXDEC  hl, PR_NET_CMD+1
                ZEROS_RPL_SPACE PR_NET_CMD+1
                UART1.put.bytes 7, PR_NET_CMD, pr_net_err
                ret

; =============================================================================
; PROPOSED DRIVEWIRE: 2 bytes Binary, zero math!
; =============================================================================
dw_prt_cmd:     db      50h, 00h                ; [OP_PRINT, character]
dw_flush_cmd:   db      46h                     ; [OP_PRINTFLUSH]

$?2             ld      a, C                    ; Character in C
                ld      (dw_prt_cmd+1), a
                UART1.put.bytes 2, dw_prt_cmd, pr_net_err
                ret

flush_printer:
                UART1.put.bytes 1, dw_flush_cmd, pr_net_err
                ret
```

---

### Equates and Global Constants (`TRSDOS_GLOBAL/`)

In [`equates-versions.s`](file:///Users/richardlucente/development/git/trs-os-zds/TRSDOS_GLOBAL/equates/equates-versions.s):
```assembly
OP_NOP          EQU     00h
OP_TIME         EQU     23h     ; '#'
OP_PRINTFLUSH   EQU     46h     ; 'F'
OP_PRINT        EQU     50h     ; 'P'
OP_READ         EQU     52h     ; 'R'
OP_WRITE        EQU     57h     ; 'W'
OP_DWINIT       EQU     5Ah     ; 'Z'
OP_REREAD       EQU     72h     ; 'r'
OP_REWRITE      EQU     77h     ; 'w'
OP_RESET3       EQU     F8h
```

---

### What Remains Completely Unchanged

* **Hardware & UART1 Driver (`driver-UART1.S`):** The Port C Mode 7 alternate function, 18.432 MHz BRG divisor 10 (115,200 baud), 16-byte hardware FIFOs, and timer interrupt masking during streaming blocks are **100% compatible** with DriveWire.
* **Boot Pipeline:** Launching TRS-OS from Quark MOS via `OSboot` remains identical.
* **Filesystem Internals:** Directory management, file allocation (`filposn.S`), and FCB logic remain unchanged.

---

## 6. Post-Boot Dynamic Mounting vs. Pre-Boot IPL Binding

### Why Binding Was Originally Placed in IPL

In original TRS-OS, binding was in `SYS0_IPL` solely for **Network Booting (Remote IPL)**—so an Agon with no local drive could boot TRSDOS over the serial cable.

When booting via `OSboot`, **TRSDOS is already loaded in RAM**. Halting at an IPL menu to bind drives is an unnecessary barrier.

---

### Device Control Table (DCT) Mechanics in LS-DOS 6.3

In LS-DOS 6.3, drives `:0` through `:7` are governed by 10-byte **Device Control Tables** at fixed address **`0x0470`** in low-core RAM ([`DCT.S`](file:///Users/richardlucente/development/git/trs-os-zds/TRSDOS_7/SYSRES/lowcore/DCT.S#L5)):

```
Address   Entry    State When Disabled    State When Enabled
0x0470    DCT0$    RET; DW FDCDVR         JP FDCDVR (0xC3 ...) + 7 DCT params
0x047A    DCT1$    RET; DW FDCDVR         JP FDCDVR (0xC3 ...) + 7 DCT params
...
0x04AC    DCT6$    RET; DW FDCDVR (0xC9)  JP FDCDVR (0xC3 ...) + 7 DCT params
0x04B6    DCT7$    RET; DW FDCDVR (0xC9)  JP FDCDVR (0xC3 ...) + 7 DCT params
```

* **To Enable a Drive:** Write `0xC3` (`JP`) into byte 0 of `DCT(n)`, followed by the 7 DCT parameters in bytes 3–9.
* **To Disable a Drive:** Write `0xC9` (`RET`) into byte 0.

Because `DCT$` is in standard read/write RAM, **any normal command file (.CMD) executed from the `TRSDOS Ready` prompt can mount, unmount, or modify drives dynamically**.

---

### The `DW.CMD` / `MOUNT.CMD` Dynamic Workflow

```text
TRSDOS Ready
DW MOUNT 0 :6
Mounting DriveWire Drive 0 as TRS-OS Drive :6... Ready.
DIR :6
```

```
[ User Types: DW MOUNT 0 :6 ]
              |
              v
1. Probe Server: Sends OP_DWINIT (0x5A) or OP_NOP (0x00) over UART1 to verify link.
              |
              v
2. Probe Geometry: Sends OP_READ for Track 17 Sector 0 (the GAT sector).
   * LS-DOS disk images contain the '\x03LSI' signature and the 7 DCT parameters
     at offset +245 of the GAT sector.
              |
              v
3. Update DCT6:
   * Writes the 7 geometry bytes into DCT6$ (0x04AF..0x04B5).
   * Sets (0x04AC) = 0xC3 (JP FDCDVR) to activate Drive :6.
              |
              v
4. Invalidate OS Buffers:
   * Calls TRSDOS system vector z_VDCTL (0x0D42) or flushes SBUFF$ to signal
     the filesystem that new media is mounted.
              |
              v
[ Returns to TRSDOS Ready Prompt — Drive :6 is immediately accessible! ]
```

---

### Native LS-DOS Dynamic Media Sensing via GAT Sector

LS-DOS 6.3 includes automatic media sensing:
* When a drive is accessed, if `DCT+4` has bit 7 set to 0, DOS reads Sector 0 of the directory cylinder (Track 17).
* It reads the `\x03LSI` signature block and auto-updates the DCT geometry in RAM.
* If a new disk image is mounted on the DriveWire server, the next `DIR :6` automatically detects the new geometry without requiring a system reboot.

---

### Advantages of Moving Mounting to OS-Level CLI

| Feature | Pre-Boot IPL Phase (Current) | Post-Boot Dynamic CLI (DriveWire) |
| :--- | :--- | :--- |
| **Boot Speed** | Pauses for user input, pinging, and binding menus | **Instant** (< 1s boot directly to `TRSDOS Ready`) |
| **Disk Swapping** | ❌ Requires system reboot into IPL | ✅ Hot-swap disk images on the fly via `DW MOUNT` |
| **Multi-Drive Mapping** | ❌ Hardcoded to Drive `:6` | ✅ Map DW Drive 0 to `:4`, DW Drive 1 to `:5`, DW Drive 2 to `:6` |
| **CLI Server Control** | ❌ Impossible from IPL | ✅ Execute `dw disk insert`, `dw disk show` from DOS prompt |
| **Error Handling** | If server is offline, IPL hangs or fails | Clean DOS error message; DOS remains fully operational |

---

## 7. Printer Spooling Under DriveWire

### Protocol Efficiency Comparison

```
TRS-NET Print:   [ '#' ] [ '0' ] [ '0' ] [ '0' ] [ '6' ] [ '5' ] [ '\n' ]   (7 bytes ASCII)
DriveWire Print: [ 0x50 (OP_PRINT) ] [ 0x41 ('A') ]                         (2 bytes Binary)
```

1. **71% Reduction in Wire Traffic:** 2 bytes per character vs. 7 bytes.
2. **Zero CPU Overhead on eZ80:** Eliminates decimal string formatting routines (`HEXDEC` and `ZEROS_RPL_SPACE`) from the printer driver.
3. **Explicit Job Completion:** Sending `0x46` (`OP_PRINTFLUSH`) explicitly signals when a print job ends.

### Server-Side Spooling and PDF Capabilities

Modern DriveWire servers provide advanced printing features:
* **PDF Generation:** Converts dot-matrix escape sequences (Epson FX-80 / Tandy DMP) into clean, paginated PDF files.
* **Host Queue Forwarding:** Pipes completed print jobs directly to host system printers via CUPS / `lpr` on macOS/Linux.
* **Auto-Flush Timeout:** If a vintage application does not send `OP_PRINTFLUSH`, the server flushes and closes the document after a configurable period of inactivity.

---

## 8. Comprehensive Comparison & Recommendations

| Evaluation Dimension | Current TRS-NET (`trs-netd.py`) | DriveWire Protocol |
| :--- | :--- | :--- |
| **Command Format** | ASCII text strings (`<00012\n`) | Compact binary opcodes (`0x52`, `0x57`) |
| **Sector Read Overhead** | 7B request + 260B response | **5B request + 259B response** |
| **Sector Write Overhead** | 267B request + 4B response | **263B request + 1B response** |
| **Checksum Integrity** | 8-bit modular sum (`sum % 256`) | **16-bit sum** ($\sum \text{byte} \pmod{65536}$) |
| **Printer Spooling** | 7 bytes/char (ASCII append) | **2 bytes/char + explicit `OP_PRINTFLUSH`** |
| **Multi-Client Architecture** | Sequential only (1 client at a time) | **Instance Model** (Multi-client across endpoints) |
| **Disk Image Headers** | Requires 256B `DiskDISK` header | Standard flat raw sector images (`.dsk`) |
| **Mounting Timing** | Pre-boot IPL menu (`ipl_net_bind.s`) | **Post-boot dynamic CLI** (`DW MOUNT`) |

### Key Recommendations

1. **Migrate to DriveWire on the eZ80:**
   Updating `driver-FDCDVR.S` and `driver-PRDVR.S` to DriveWire simplifies assembly code, reduces packet latency, eliminates ASCII formatting routines on the CPU, and provides robust 16-bit checksumming.
2. **Shift Drive Mounting from IPL to an OS-Level Utility (`DW.CMD`):**
   Bypassing pre-boot IPL menus allows TRS-OS to boot instantly into `TRSDOS Ready`. Managing `DCT$` dynamically from a command-line tool enables hot-swapping virtual disks and mounting multiple remote drives.
3. **Adopt DriveWire's Instance Model for Multi-Client Support:**
   To serve both Serial and TCP simultaneously, structure the server using DriveWire's **Multi-Instance Model**: run Instance 0 on Serial and Instance 1 on TCP, each managing its own virtual disk stack without timeouts or port contention.
