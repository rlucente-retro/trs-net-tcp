#!/usr/bin/env python3
"""
trs-netd.py - TRS-NET TCP/IP Network Server Daemon for TRS-OS

Listens for TCP/IP stream connections from TRS-OS clients (e.g., Olimex Agon Light 2
using MOD-WIFI-ESP8266 in transparent Wi-Fi streaming mode, or retrocomputing emulators).

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
from typing import BinaryIO, Final

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


class BufferedSocketReader:
    """
    Buffered reader over a TCP socket with precise framing helpers.

    Handles TCP segmentation, arbitrary chunk boundaries, and cleans stray
    inter-command linefeeds without dropping stream data.
    """

    def __init__(self, sock: socket.socket, timeout: float = 30.0) -> None:
        self.sock = sock
        self.timeout = timeout
        self._buffer = bytearray()

    def recv_exact(self, count: int) -> bytes:
        """Receive exactly count bytes from the socket."""
        while len(self._buffer) < count:
            ready, _, _ = select.select([self.sock], [], [], self.timeout)
            if not ready:
                raise TimeoutError(f"Timed out waiting for {count} bytes")
            chunk = self.sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise ConnectionResetError("Remote client closed connection during recv_exact")
            self._buffer.extend(chunk)

        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def readline(self) -> bytes:
        """Read bytes until newline (\\n), stripping trailing whitespace."""
        while b"\n" not in self._buffer:
            ready, _, _ = select.select([self.sock], [], [], self.timeout)
            if not ready:
                raise TimeoutError("Timed out waiting for line termination")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionResetError("Remote client closed connection during readline")
            self._buffer.extend(chunk)

        idx = self._buffer.index(b"\n")
        line = bytes(self._buffer[:idx])
        del self._buffer[: idx + 1]
        return line.rstrip(b"\r")

    def read_cmd_byte(self) -> bytes | None:
        """
        Read the next command prefix byte.

        Skips any leading whitespace (CR, LF, NUL, Space) that may have lingered
        from previous line terminators. Returns b"" on EOF, or None on idle timeout.
        """
        while True:
            while not self._buffer:
                ready, _, _ = select.select([self.sock], [], [], 1.0)
                if not ready:
                    return None  # 1-second idle tick
                chunk = self.sock.recv(4096)
                if not chunk:
                    return b""  # Clean EOF from peer
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
        conn: socket.socket,
        addr: tuple[str, int],
        volume: DiskVolume,
        printer_path: Path,
        stats: ServerStats,
        is_running: Callable[[], bool] = lambda: True,
    ) -> None:
        self.conn = conn
        self.addr = addr
        self.volume = volume
        self.printer_path = printer_path
        self.stats = stats
        self.is_running = is_running
        self.reader = BufferedSocketReader(conn, timeout=30.0)

        # Optimize for interactive request/response latency (disable Nagle algorithm)
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _send(self, data: bytes) -> None:
        self.conn.sendall(data)
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
        logger.info("Connection established with %s:%d", self.addr[0], self.addr[1])

        try:
            while self.is_running():
                cmd_byte = self.reader.read_cmd_byte()
                if cmd_byte == b"":
                    # Clean EOF from peer
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
                        "Unrecognized command byte from %s:%d: %r",
                        self.addr[0],
                        self.addr[1],
                        cmd_byte,
                    )

        except (ConnectionResetError, BrokenPipeError) as err:
            logger.debug("Connection closed by client: %s", err)
        except TimeoutError as err:
            logger.warning("Connection timed out: %s", err)
        except Exception as err:
            logger.error("Unexpected session error with %s:%d: %s", self.addr[0], self.addr[1], err, exc_info=True)
        finally:
            logger.info("Client %s:%d disconnected.", self.addr[0], self.addr[1])
            logger.info(self.stats.summary())


class TRSNetDaemon:
    """Main TCP Server Daemon lifecycle manager."""

    def __init__(
        self,
        volume_path: Path,
        printer_path: Path,
        host: str = "0.0.0.0",
        port: int = 65432,
    ) -> None:
        self.volume = DiskVolume(volume_path)
        self.printer_path = printer_path
        self.host = host
        self.port = port
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

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(5)
            self.server_sock = s

            logger.info("=" * 60)
            logger.info("  TRS-NET TCP Server Daemon (trs-netd)")
            logger.info("=" * 60)
            logger.info("Listening on:   %s:%d", self.host, self.port)
            logger.info("Volume Image:   %s", self.volume.path)
            logger.info("Printer Spool:  %s", self.printer_path)
            logger.info("Waiting for client connections (Ctrl+C to stop)...")
            logger.info("=" * 60)

            try:
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
                        session = ClientSession(
                            conn=conn,
                            addr=addr,
                            volume=self.volume,
                            printer_path=self.printer_path,
                            stats=self.stats,
                            is_running=lambda: self.running,
                        )
                        session.handle()

            finally:
                self.volume.close()
                if self.shutdown_signal is not None:
                    signame = signal.Signals(self.shutdown_signal).name
                    logger.info("Received %s, graceful shutdown complete.", signame)
                else:
                    logger.info("Server shutdown complete.")


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
        description="TRS-NET TCP/IP Network Server Daemon for TRS-OS (trs-netd)"
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=65432,
        help="TCP port to listen on (default: 65432)",
    )
    parser.add_argument(
        "--host",
        "-H",
        default="0.0.0.0",
        help="Host/IP address to bind to (default: 0.0.0.0)",
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

    configure_logging(verbose=args.verbose)

    daemon = TRSNetDaemon(
        volume_path=args.volume,
        printer_path=args.printer,
        host=args.host,
        port=args.port,
    )
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
