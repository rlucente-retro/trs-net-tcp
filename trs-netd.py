#!/usr/bin/env python3
"""
trs-netd.py - TRS-NET TCP/IP Network & Serial Server Daemon for TRS-OS

Serves virtual floppy disk images (.dsk) and spools printer output for TRS-OS
(TRSDOS / LS-DOS 6.3 adapted for the Zilog eZ80, such as the Agon family of computers).

Supports two communication transports:
  1. TCP/IP Socket: For networked systems (e.g., Agon family with an ESP-AT v1.7.x+
     Wi-Fi coprocessor in transparent passthrough mode, or retrocomputing emulators).
  2. Serial Port: For direct physical serial links (e.g., Olimex MOD-USB-RS232,
     USB CDC-ACM virtual COM ports, or legacy RS-232 serial cables).

Implements the TRS-NET remote block storage protocol:
  - Sector Read ('<') and Re-read ('\\') with 8-bit checksums
  - Sector Write ('>') and Re-write ('/') with round-trip verification
  - Printer Spooling ('#')
  - Control Commands ('@ping', '@bind', '@rhdr', '@stat', '@rset', '@echo')

Based on the original TRS-NET server concept by Daniel Paul Martin (www.TRSDOS.com).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
import fcntl
import logging
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
import sys
import time
from types import FrameType
from typing import Any, BinaryIO, Final, Protocol

# Optional pyserial dependency for serial transport
try:
    import serial
    import serial.tools.list_ports
    HAVE_PYSERIAL = True
except ImportError:
    serial = None  # type: ignore[assignment]
    HAVE_PYSERIAL = False

logger = logging.getLogger("trs-netd")

# Protocol Constants
SECTOR_SIZE: Final[int] = 256
HEADER_SIZE: Final[int] = 256  # Sector 0 is preceded by 256-byte disk header


@dataclass
class ServerStats:
    """Tracks protocol transaction statistics."""
    binds: int = 0
    controls: int = 0
    echos: int = 0
    pings: int = 0
    prints: int = 0
    gets: int = 0
    rd_retries: int = 0
    puts: int = 0
    wr_retries: int = 0
    wr_checksum_errors: int = 0
    rhdrs: int = 0
    stats: int = 0
    resets: int = 0
    total_bytes_rx: int = 0
    total_bytes_tx: int = 0
    start_time: float = field(default_factory=time.time)

    def summary(self) -> str:
        uptime = max(1, int(time.time() - self.start_time))
        return (
            f"Uptime: {uptime}s | "
            f"GETS: {self.gets} (retry: {self.rd_retries}) | "
            f"PUTS: {self.puts} (retry: {self.wr_retries}, bad_chk: {self.wr_checksum_errors}) | "
            f"PING: {self.pings} | BIND: {self.binds} | RHDR: {self.rhdrs} | "
            f"PRNT: {self.prints} | ECHO: {self.echos} | "
            f"I/O: RX {self.total_bytes_rx:,} B, TX {self.total_bytes_tx:,} B"
        )


class StreamTransport(Protocol):
    """Abstract bidirectional stream transport interface."""

    @property
    def name(self) -> str:
        """Human-readable identifier for logging."""
        ...

    def is_alive(self) -> bool:
        """Return True if connection is currently active."""
        ...

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Read up to max_bytes from transport. Returns b'' on timeout or EOF."""
        ...

    def write(self, data: bytes) -> None:
        """Send data through transport."""
        ...

    def close(self) -> None:
        """Close the transport."""
        ...


class SocketTransport:
    """Bidirectional stream transport over a TCP socket."""

    def __init__(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        self.sock = sock
        self.addr = addr
        self._alive = True
        self._name = f"{addr[0]}:{addr[1]}"
        # Optimize for interactive request/response latency (disable Nagle algorithm)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    @property
    def name(self) -> str:
        return self._name

    def is_alive(self) -> bool:
        return self._alive

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self._alive:
            return b""
        try:
            if timeout is not None:
                ready, _, _ = select.select([self.sock], [], [], timeout)
                if not ready:
                    return b""
            chunk = self.sock.recv(max_bytes)
            if not chunk:
                self._alive = False
                return b""
            return chunk
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._alive = False
            return b""

    def write(self, data: bytes) -> None:
        if not self._alive:
            raise ConnectionResetError("Socket is closed")
        try:
            self.sock.sendall(data)
        except (ConnectionResetError, BrokenPipeError, OSError) as err:
            self._alive = False
            raise ConnectionResetError(f"Socket write failed: {err}") from err

    def close(self) -> None:
        self._alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class SerialTransport:
    """Bidirectional stream transport over a local serial port."""

    def __init__(self, ser: Any) -> None:
        self.ser = ser
        self._alive = True
        self._name = f"{getattr(ser, 'port', 'serial')} ({getattr(ser, 'baudrate', 'unknown')} baud)"

    @property
    def name(self) -> str:
        return self._name

    def is_alive(self) -> bool:
        return self._alive and (self.ser is not None) and getattr(self.ser, "is_open", False)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self.is_alive():
            return b""
        try:
            waiting = getattr(self.ser, "in_waiting", 0)
            if waiting > 0:
                return bytes(self.ser.read(min(max_bytes, waiting)))

            if timeout is not None and timeout <= 0:
                return b""

            old_timeout = self.ser.timeout
            try:
                if timeout is not None and timeout != old_timeout:
                    self.ser.timeout = timeout
                first_byte = bytes(self.ser.read(1))
                if not first_byte:
                    return b""
                more = getattr(self.ser, "in_waiting", 0)
                if more > 0:
                    extra = bytes(self.ser.read(min(max_bytes - 1, more)))
                    return first_byte + extra
                return first_byte
            finally:
                if timeout is not None and timeout != old_timeout:
                    self.ser.timeout = old_timeout
        except Exception as err:
            logger.warning("Serial read error on %s: %s", self.name, err)
            self._alive = False
            return b""

    def write(self, data: bytes) -> None:
        if not self.is_alive():
            raise ConnectionResetError("Serial port is closed")
        try:
            self.ser.write(data)
        except Exception as err:
            self._alive = False
            raise ConnectionResetError(f"Serial write failed: {err}") from err

    def close(self) -> None:
        self._alive = False
        try:
            if self.ser and getattr(self.ser, "is_open", False):
                self.ser.close()
        except Exception:
            pass


class BufferedStreamReader:
    """
    Buffered reader over any StreamTransport (Socket or Serial) with precise framing helpers.

    Handles TCP segmentation, arbitrary serial chunk boundaries, and cleans stray
    inter-command linefeeds without dropping stream data.
    """

    def __init__(self, transport: StreamTransport, timeout: float = 30.0) -> None:
        self.transport = transport
        self.timeout = timeout
        self._buffer = bytearray()

    def recv_exact(self, count: int, timeout: float | None = None) -> bytes:
        """Receive exactly count bytes from the transport."""
        effective_timeout = self.timeout if timeout is None else timeout
        deadline = time.time() + effective_timeout
        while len(self._buffer) < count:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for {count} bytes")
            chunk = self.transport.read(
                max(4096, count - len(self._buffer)),
                timeout=min(remaining, 1.0),
            )
            if not chunk:
                if not self.transport.is_alive():
                    raise ConnectionResetError("Remote peer closed connection during recv_exact")
                continue
            self._buffer.extend(chunk)

        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def readline(self, timeout: float | None = None) -> bytes:
        """Read bytes until newline (\\n), stripping trailing whitespace."""
        effective_timeout = self.timeout if timeout is None else timeout
        deadline = time.time() + effective_timeout
        while b"\n" not in self._buffer:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for line termination")
            chunk = self.transport.read(4096, timeout=min(remaining, 1.0))
            if not chunk:
                if not self.transport.is_alive():
                    raise ConnectionResetError("Remote peer closed connection during readline")
                continue
            self._buffer.extend(chunk)

        idx = self._buffer.index(b"\n")
        line = bytes(self._buffer[:idx])
        del self._buffer[: idx + 1]
        return line.rstrip(b"\r")

    def read_cmd_byte(self) -> bytes | None:
        """
        Read the next command prefix byte.

        Skips any leading whitespace (CR, LF, NUL, Space) that may have lingered
        from previous line terminators. Returns b"" on EOF/disconnect, or None on idle tick.
        """
        while True:
            while not self._buffer:
                if not self.transport.is_alive():
                    return b""
                chunk = self.transport.read(4096, timeout=1.0)
                if not chunk:
                    if not self.transport.is_alive():
                        return b""
                    return None  # 1-second idle tick: check loop condition and continue
                self._buffer.extend(chunk)

            b = bytes([self._buffer[0]])
            del self._buffer[0]

            # Discard inter-command whitespace / delimiters (CR, LF, NUL, Space)
            if b not in (b"\r", b"\n", b"\x00", b" "):
                return b


class DiskVolume:
    """Manages virtual .dsk image access with file locking and buffer synchronization."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: BinaryIO | None = None
        self.size: int = 0
        self.records: int = 0

    def open(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                f"Volume image not found: {self.path}. "
                "Run 'make fetch' to download standard disk volumes from upstream."
            )
        self.file = open(self.path, "r+b")
        try:
            # Acquire non-blocking exclusive lock to prevent concurrent corruption
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.warning(
                "Could not acquire exclusive lock on %s; another process may be accessing it.",
                self.path.name,
            )

        self.file.seek(0, os.SEEK_END)
        self.size = self.file.tell()
        self.records = self.size // SECTOR_SIZE
        logger.info(
            "Volume loaded: %s (%s bytes, %d records of %dB)",
            self.path.name,
            f"{self.size:,}",
            self.records,
            SECTOR_SIZE,
        )

    def close(self) -> None:
        if self.file:
            try:
                self.file.flush()
                os.fsync(self.file.fileno())
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                self.file.close()
                self.file = None

    def read_header(self) -> bytes:
        """Read the 256-byte disk header at offset 0."""
        if not self.file:
            raise RuntimeError("Volume not open")
        self.file.seek(0, os.SEEK_SET)
        data = self.file.read(HEADER_SIZE)
        return data.ljust(HEADER_SIZE, b"\x00")

    def read_sector(self, sector_num: int) -> bytes:
        """Read a 256-byte sector at offset (sector_num * 256) + 256."""
        if not self.file:
            raise RuntimeError("Volume not open")
        if sector_num < 0:
            raise ValueError(f"Invalid negative sector index: {sector_num}")

        offset = (sector_num * SECTOR_SIZE) + HEADER_SIZE
        self.file.seek(offset, os.SEEK_SET)
        data = self.file.read(SECTOR_SIZE)
        return data.ljust(SECTOR_SIZE, b"\x00")

    def write_sector(self, sector_num: int, data: bytes) -> None:
        """Write a 256-byte sector at offset (sector_num * 256) + 256."""
        if not self.file:
            raise RuntimeError("Volume not open")
        if sector_num < 0:
            raise ValueError(f"Invalid negative sector index: {sector_num}")
        if len(data) != SECTOR_SIZE:
            raise ValueError(f"Sector payload must be exactly {SECTOR_SIZE} bytes (got {len(data)})")

        offset = (sector_num * SECTOR_SIZE) + HEADER_SIZE
        self.file.seek(offset, os.SEEK_SET)
        self.file.write(data)
        self.file.flush()
        # Force kernel sync to prevent data loss on sudden power-off
        os.fsync(self.file.fileno())


class ClientSession:
    """Handles protocol transaction state for an active client connection."""

    def __init__(
        self,
        transport: StreamTransport,
        volume: DiskVolume,
        printer_path: Path,
        stats: ServerStats,
        is_running: Callable[[], bool] = lambda: True,
    ) -> None:
        self.transport = transport
        self.volume = volume
        self.printer_path = printer_path
        self.stats = stats
        self.is_running = is_running
        self.reader = BufferedStreamReader(transport, timeout=30.0)

    def _send(self, data: bytes) -> None:
        self.transport.write(data)
        self.stats.total_bytes_tx += len(data)

    @staticmethod
    def _parse_sector_arg(raw: bytes) -> int:
        """Extract integer argument from ASCII string (e.g. b'<00012' -> 12)."""
        text = raw.decode("ascii", errors="ignore").strip()
        match = re.search(r"\d+", text)
        if match:
            return int(match.group(0))
        raise ValueError(f"Cannot parse integer from: {raw!r}")

    def handle(self) -> None:
        logger.info("Connection established with %s", self.transport.name)

        try:
            while self.is_running():
                cmd_byte = self.reader.read_cmd_byte()
                if cmd_byte == b"":
                    # Clean EOF / disconnect from peer
                    break
                if cmd_byte is None:
                    # 1.0s idle tick: check loop condition and continue
                    continue

                self.stats.total_bytes_rx += len(cmd_byte)

                # -------------------------------------------------------------
                # '#' Print Spooling Request: #<byte>\n
                # -------------------------------------------------------------
                if cmd_byte == b"#":
                    line = self.reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    try:
                        byte_val = self._parse_sector_arg(line)
                        self.printer_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(self.printer_path, "a", encoding="latin1") as prt:
                            prt.write(chr(byte_val))
                        self.stats.prints += 1
                        self.stats.controls += 1
                        logger.debug("Printer byte spooled: %d (%r)", byte_val, chr(byte_val))
                    except Exception as err:
                        logger.warning("Failed to process printer byte %r: %s", line, err)

                # -------------------------------------------------------------
                # '<' or '\' Read Sector Request: <00012\n
                # -------------------------------------------------------------
                elif cmd_byte in (b"<", b"\\"):
                    is_retry = (cmd_byte == b"\\")
                    if is_retry:
                        self.stats.rd_retries += 1
                    else:
                        self.stats.gets += 1

                    line = self.reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    rsector = self._parse_sector_arg(line)

                    sector_data = self.volume.read_sector(rsector)
                    checksum = sum(sector_data) % 256
                    chk_bytes = struct.pack(">i", checksum)

                    self._send(sector_data)
                    self._send(chk_bytes)
                    logger.debug(
                        "Sector READ %ssec=%d, chksum=%d",
                        "(retry) " if is_retry else "",
                        rsector,
                        checksum,
                    )

                # -------------------------------------------------------------
                # '>' or '/' Write Sector Request: >00012\n + 256B data + 4B chksum
                # -------------------------------------------------------------
                elif cmd_byte in (b">", b"/"):
                    is_retry = (cmd_byte == b"/")
                    if is_retry:
                        self.stats.wr_retries += 1
                    else:
                        self.stats.puts += 1

                    line = self.reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    rsector = self._parse_sector_arg(line)

                    sector_data = self.reader.recv_exact(SECTOR_SIZE)
                    self.stats.total_bytes_rx += SECTOR_SIZE

                    client_chk_bytes = self.reader.recv_exact(4)
                    self.stats.total_bytes_rx += 4

                    calc_chk = sum(sector_data) % 256
                    calc_chk_bytes = struct.pack(">i", calc_chk)

                    # Verify client checksum byte (client sends 4-byte int with 8-bit sum in byte 3)
                    client_chk = client_chk_bytes[3]
                    if client_chk != calc_chk:
                        self.stats.wr_checksum_errors += 1
                        logger.warning(
                            "Checksum mismatch on sector %d write: client=%d, server=%d (data not committed)",
                            rsector,
                            client_chk,
                            calc_chk,
                        )
                    else:
                        # Commit sector to disk volume only if checksum verified
                        self.volume.write_sector(rsector, sector_data)

                    # Return computed checksum to client
                    self._send(calc_chk_bytes)

                    logger.debug(
                        "Sector WRITE %ssec=%d, chksum=%d (client_chk=%d)",
                        "(retry) " if is_retry else "",
                        rsector,
                        calc_chk,
                        client_chk,
                    )

                # -------------------------------------------------------------
                # '@' Control Commands: @ping\n, @bind\n, @rhdr\n, @stat\n, etc.
                # -------------------------------------------------------------
                elif cmd_byte == b"@":
                    line = self.reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    cmd_name = line.decode("ascii", errors="ignore").strip().lower()

                    if cmd_name == "ping":
                        self.stats.pings += 1
                        self.stats.controls += 1
                        self._send(b"@pong\n")
                        logger.debug("Control: @ping -> @pong")

                    elif cmd_name == "bind":
                        self.stats.binds += 1
                        self.stats.controls += 1
                        self._send(b"@bound\n")
                        header = self.volume.read_header()
                        self._send(header)
                        logger.debug("Control: @bind -> @bound + header (256B)")

                    elif cmd_name == "rhdr":
                        self.stats.rhdrs += 1
                        self.stats.controls += 1
                        header = self.volume.read_header()
                        self._send(header)

                        # Client then requests sector 1
                        sec1_line = self.reader.readline()
                        self.stats.total_bytes_rx += len(sec1_line) + 1
                        sec1 = self._parse_sector_arg(sec1_line)
                        self._send(self.volume.read_sector(sec1))

                        # Client then requests sector 2
                        sec2_line = self.reader.readline()
                        self.stats.total_bytes_rx += len(sec2_line) + 1
                        sec2 = self._parse_sector_arg(sec2_line)
                        self._send(self.volume.read_sector(sec2))

                        logger.debug("Control: @rhdr -> header + dir sec %d, %d", sec1, sec2)

                    elif cmd_name == "echo":
                        self.stats.echos += 1
                        self.stats.controls += 1
                        echo_buf = self.reader.recv_exact(SECTOR_SIZE)
                        self.stats.total_bytes_rx += len(echo_buf)
                        self._send(echo_buf)
                        logger.debug("Control: @echo (256B)")

                    elif cmd_name == "stat":
                        self.stats.stats += 1
                        self.stats.controls += 1
                        logger.info(self.stats.summary())

                    elif cmd_name == "rset":
                        self.stats.resets += 1
                        self.stats.controls += 1
                        logger.info("Control: @rset received from client")

                    else:
                        logger.warning("Unrecognized control command: '@%s'", cmd_name)

                else:
                    logger.warning(
                        "Unrecognized command byte from %s: %r",
                        self.transport.name,
                        cmd_byte,
                    )

        except (ConnectionResetError, BrokenPipeError) as err:
            logger.debug("Connection closed by peer: %s", err)
        except TimeoutError as err:
            logger.warning("Connection timed out: %s", err)
        except Exception as err:
            logger.error("Unexpected session error with %s: %s", self.transport.name, err, exc_info=True)
        finally:
            logger.info("Client %s disconnected.", self.transport.name)
            logger.info(self.stats.summary())


def list_serial_ports() -> None:
    """Print detected serial ports to standard output."""
    if not HAVE_PYSERIAL:
        print(
            "Error: 'pyserial' is not installed. Install it with: pip install pyserial",
            file=sys.stderr,
        )
        return

    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports detected.")
        return

    print("Detected serial ports:")
    for p in ports:
        desc = p.description or "n/a"
        hwid = p.hwid or "n/a"
        print(f"  {p.device:30} - {desc} [{hwid}]")


def find_auto_serial_port() -> str | None:
    """
    Search for connected USB-to-serial adapters.

    Filters out Bluetooth ports and virtual consoles, preferring /dev/cu.* on macOS.
    Returns the port device path, or None if none or ambiguous.
    """
    if not HAVE_PYSERIAL:
        logger.error(
            "'pyserial' is required for serial communication. Install it with: pip install pyserial"
        )
        return None

    ports = list(serial.tools.list_ports.comports())
    if not ports:
        logger.error("No serial ports detected on the system.")
        return None

    candidates = []
    for p in ports:
        dev_lower = p.device.lower()
        desc_lower = (p.description or "").lower()
        # Ignore virtual Bluetooth and debug console devices
        if "bluetooth" in dev_lower or "bluetooth" in desc_lower or "debug-console" in dev_lower:
            continue
        # Look for typical USB-serial drivers or USB VID/PID
        if (
            any(marker in dev_lower for marker in ("usbmodem", "usbserial", "ttyacm", "ttyusb", "com"))
            or getattr(p, "vid", None) is not None
        ):
            candidates.append(p)

    # On macOS, prefer /dev/cu.* over /dev/tty.* to avoid carrier-detect blocking
    if sys.platform == "darwin":
        cu_candidates = [p for p in candidates if p.device.startswith("/dev/cu.")]
        if cu_candidates:
            candidates = cu_candidates

    if len(candidates) == 1:
        chosen = candidates[0]
        logger.info("Auto-detected serial port: %s (%s)", chosen.device, chosen.description)
        return chosen.device
    elif len(candidates) > 1:
        logger.error("Multiple USB serial ports detected. Please specify one with --serial <port>:")
        for p in candidates:
            logger.error("  %s - %s", p.device, p.description)
        return None
    else:
        logger.error("No USB serial devices detected. Available ports:")
        for p in ports:
            logger.error("  %s - %s", p.device, p.description)
        return None


class TRSNetDaemon:
    """Main TCP & Serial Server Daemon lifecycle manager."""

    def __init__(
        self,
        volume_path: Path,
        printer_path: Path,
        host: str = "0.0.0.0",
        port: int = 65432,
        serial_port: str | None = None,
        baud_rate: int = 115200,
        rtscts: bool = False,
    ) -> None:
        self.volume = DiskVolume(volume_path)
        self.printer_path = printer_path
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.rtscts = rtscts
        self.stats = ServerStats()
        self.running = False
        self.shutdown_signal: int | None = None
        self.server_sock: socket.socket | None = None

    def _sig_handler(self, signum: int, frame: FrameType | None) -> None:
        # Strictly async-signal-safe: setting variables only, no I/O or logging!
        self.shutdown_signal = signum
        self.running = False

    def run(self) -> None:
        # Register signal handlers for clean teardown
        signal.signal(signal.SIGINT, self._sig_handler)
        signal.signal(signal.SIGTERM, self._sig_handler)

        self.volume.open()
        self.running = True

        try:
            if self.serial_port:
                self._run_serial()
            else:
                self._run_tcp()
        finally:
            self.volume.close()
            if self.shutdown_signal is not None:
                signame = signal.Signals(self.shutdown_signal).name
                logger.info("Received %s, graceful shutdown complete.", signame)
            else:
                logger.info("Server shutdown complete.")

    def _run_tcp(self) -> None:
        """Run TCP/IP server accept loop."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(5)
            self.server_sock = s

            logger.info("=" * 60)
            logger.info("  TRS-NET TCP Server Daemon (trs-netd)")
            logger.info("=" * 60)
            logger.info("Listening on:   %s:%d (TCP)", self.host, self.port)
            logger.info("Volume Image:   %s", self.volume.path)
            logger.info("Printer Spool:  %s", self.printer_path)
            logger.info("Waiting for client connections (Ctrl+C to stop)...")
            logger.info("=" * 60)

            while self.running:
                try:
                    ready, _, _ = select.select([s], [], [], 1.0)
                    if not ready:
                        continue
                    conn, addr = s.accept()
                except OSError:
                    if not self.running:
                        break
                    continue

                with conn:
                    transport = SocketTransport(conn, addr)
                    session = ClientSession(
                        transport=transport,
                        volume=self.volume,
                        printer_path=self.printer_path,
                        stats=self.stats,
                        is_running=lambda: self.running,
                    )
                    session.handle()

    def _run_serial(self) -> None:
        """Run Serial port server session loop."""
        if not HAVE_PYSERIAL:
            logger.error(
                "'pyserial' is required for serial communication. Install it with: pip install pyserial"
            )
            return

        port_name = self.serial_port
        if port_name == "auto":
            detected = find_auto_serial_port()
            if not detected:
                return
            port_name = detected

        if sys.platform == "darwin" and port_name.startswith("/dev/tty."):
            cu_name = port_name.replace("/dev/tty.", "/dev/cu.")
            logger.warning(
                "On macOS, '%s' may hang waiting for carrier detect (DCD). Consider using '%s' instead.",
                port_name,
                cu_name,
            )

        logger.info("=" * 60)
        logger.info("  TRS-NET Serial Server Daemon (trs-netd)")
        logger.info("=" * 60)
        logger.info("Serial Port:    %s", port_name)
        logger.info("Baud Rate:      %d baud (8-N-1, RTS/CTS: %s)", self.baud_rate, self.rtscts)
        logger.info("Volume Image:   %s", self.volume.path)
        logger.info("Printer Spool:  %s", self.printer_path)
        logger.info("Listening for TRS-OS requests (Ctrl+C to stop)...")
        logger.info("=" * 60)

        while self.running:
            try:
                ser = serial.Serial(
                    port=port_name,
                    baudrate=self.baud_rate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    rtscts=self.rtscts,
                    timeout=1.0,
                    write_timeout=5.0,
                )
                with ser:
                    try:
                        ser.reset_input_buffer()
                        ser.reset_output_buffer()
                    except Exception:
                        pass

                    transport = SerialTransport(ser)
                    session = ClientSession(
                        transport=transport,
                        volume=self.volume,
                        printer_path=self.printer_path,
                        stats=self.stats,
                        is_running=lambda: self.running,
                    )
                    session.handle()
            except serial.SerialException as err:
                if not self.running:
                    break
                logger.warning(
                    "Serial communication error on %s: %s. Retrying in 2 seconds...",
                    port_name,
                    err,
                )
                time.sleep(2.0)


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TRS-NET TCP/IP Network & Serial Server Daemon for TRS-OS (trs-netd)"
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=65432,
        help="TCP port to listen on (default: 65432, ignored if --serial is set)",
    )
    parser.add_argument(
        "--host",
        "-H",
        default="0.0.0.0",
        help="Host/IP address to bind to (default: 0.0.0.0, ignored if --serial is set)",
    )
    parser.add_argument(
        "--serial",
        "-s",
        nargs="?",
        const="auto",
        default=None,
        help="Serial port device (e.g. /dev/cu.usbmodem*, /dev/ttyACM0, COM3, or 'auto')",
    )
    parser.add_argument(
        "--baud",
        "-b",
        type=int,
        default=115200,
        help="Serial baud rate (default: 115200)",
    )
    parser.add_argument(
        "--rtscts",
        action="store_true",
        default=False,
        help="Enable RTS/CTS hardware flow control for serial (default: False)",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List detected serial ports and exit",
    )
    parser.add_argument(
        "--volume",
        "-v",
        type=Path,
        default=Path("Volumes/sys720k.dsk"),
        help="Path to TRS-80 / TRS-OS disk volume file (.dsk) (default: Volumes/sys720k.dsk)",
    )
    parser.add_argument(
        "--printer",
        type=Path,
        default=Path("printer/print_out.txt"),
        help="Path to printer spool output file (default: printer/print_out.txt)",
    )
    parser.add_argument(
        "--verbose",
        "-V",
        action="store_true",
        help="Enable verbose debug logging of sector transfers and commands",
    )
    args = parser.parse_args(argv)

    if args.list_ports:
        list_serial_ports()
        return 0

    configure_logging(verbose=args.verbose)

    daemon = TRSNetDaemon(
        volume_path=args.volume,
        printer_path=args.printer,
        host=args.host,
        port=args.port,
        serial_port=args.serial,
        baud_rate=args.baud,
        rtscts=args.rtscts,
    )
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
