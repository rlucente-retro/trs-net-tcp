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
from dataclasses import dataclass, field
import datetime
import io
import os
from pathlib import Path
import re
import select
import socket
import struct
import sys
import time
from typing import BinaryIO, Final

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
    rhdrs: int = 0
    stats: int = 0
    resets: int = 0
    total_bytes_rx: int = 0
    total_bytes_tx: int = 0
    start_time: float = field(default_factory=time.time)

    def summary(self) -> str:
        uptime = int(time.time() - self.start_time)
        return (
            f"[STATS] Uptime: {uptime}s | "
            f"GETS: {self.gets} (retry: {self.rd_retries}) | "
            f"PUTS: {self.puts} (retry: {self.wr_retries}) | "
            f"PING: {self.pings} | BIND: {self.binds} | RHDR: {self.rhdrs} | "
            f"PRNT: {self.prints} | ECHO: {self.echos} | "
            f"I/O: RX {self.total_bytes_rx} B, TX {self.total_bytes_tx} B"
        )


class BufferedSocketReader:
    """Buffered reader over a non-blocking or blocking TCP socket."""

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
            chunk = self.sock.recv(count - len(self._buffer))
            if not chunk:
                raise ConnectionResetError("Remote client disconnected")
            self._buffer.extend(chunk)

        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def readline(self) -> bytes:
        """Read bytes until newline (\\n), returning stripped bytes."""
        while b"\n" not in self._buffer:
            ready, _, _ = select.select([self.sock], [], [], self.timeout)
            if not ready:
                raise TimeoutError("Timed out waiting for line termination")
            chunk = self.sock.recv(256)
            if not chunk:
                raise ConnectionResetError("Remote client disconnected")
            self._buffer.extend(chunk)

        idx = self._buffer.index(b"\n")
        line = bytes(self._buffer[:idx])
        del self._buffer[: idx + 1]
        return line.rstrip(b"\r")

    def read_cmd_byte(self) -> bytes | None:
        """Read a single command character byte (blocking with select)."""
        while not self._buffer:
            ready, _, _ = select.select([self.sock], [], [], 1.0)
            if not ready:
                return None
            chunk = self.sock.recv(256)
            if not chunk:
                return None
            self._buffer.extend(chunk)

        b = bytes([self._buffer[0]])
        del self._buffer[0]
        return b


class TRSNetServer:
    """TCP Server implementing TRS-NET protocol for TRS-OS."""

    def __init__(
        self,
        volume_path: Path,
        printer_path: Path,
        host: str = "0.0.0.0",
        port: int = 65432,
        verbose: bool = False,
    ) -> None:
        self.volume_path = volume_path
        self.printer_path = printer_path
        self.host = host
        self.port = port
        self.verbose = verbose
        self.stats = ServerStats()
        self.vol_file: BinaryIO | None = None
        self.vol_size = 0
        self.vol_records = 0

    def log(self, message: str) -> None:
        """Log message with timestamp."""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {message}", flush=True)

    def log_debug(self, message: str) -> None:
        if self.verbose:
            self.log(f"[DEBUG] {message}")

    def open_volume(self) -> None:
        """Open the target disk image file."""
        if not self.volume_path.exists():
            raise FileNotFoundError(
                f"Volume image not found: {self.volume_path}. "
                "Run 'make fetch' to download standard disk volumes from upstream."
            )
        self.vol_file = open(self.volume_path, "r+b")
        self.vol_file.seek(0, os.SEEK_END)
        self.vol_size = self.vol_file.tell()
        self.vol_records = self.vol_size // SECTOR_SIZE
        self.log(
            f"Volume opened: {self.volume_path.name} "
            f"({self.vol_size:,} bytes, {self.vol_records} records of {SECTOR_SIZE}B)"
        )

    def close_volume(self) -> None:
        """Flush and close volume."""
        if self.vol_file:
            with io.open(self.vol_file.fileno(), "wb", closefd=False) as f:
                f.flush()
            self.vol_file.close()
            self.vol_file = None

    def read_sector_from_volume(self, rsector: int) -> bytes:
        """Read 256-byte sector from volume at offset (rsector * 256) + 256."""
        assert self.vol_file is not None
        offset = (rsector * SECTOR_SIZE) + HEADER_SIZE
        self.vol_file.seek(offset, os.SEEK_SET)
        data = self.vol_file.read(SECTOR_SIZE)
        if len(data) < SECTOR_SIZE:
            data = data.ljust(SECTOR_SIZE, b"\x00")
        return data

    def write_sector_to_volume(self, rsector: int, data: bytes) -> None:
        """Write 256-byte sector to volume at offset (rsector * 256) + 256."""
        assert self.vol_file is not None
        offset = (rsector * SECTOR_SIZE) + HEADER_SIZE
        self.vol_file.seek(offset, os.SEEK_SET)
        self.vol_file.write(data)
        self.vol_file.flush()

    def read_header_from_volume(self) -> bytes:
        """Read 256-byte volume header at offset 0."""
        assert self.vol_file is not None
        self.vol_file.seek(0, os.SEEK_SET)
        data = self.vol_file.read(HEADER_SIZE)
        if len(data) < HEADER_SIZE:
            data = data.ljust(HEADER_SIZE, b"\x00")
        return data

    def _parse_numeric_argument(self, raw_line: bytes) -> int:
        """Parse numeric ASCII argument (e.g. '00012' -> 12)."""
        text = raw_line.decode("ascii", errors="ignore").strip()
        match = re.search(r"\d+", text)
        if match:
            return int(match.group(0))
        raise ValueError(f"Cannot parse integer from: {raw_line!r}")

    def handle_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        """Handle communication loop with a connected client."""
        self.log(f"Client connected from {addr[0]}:{addr[1]}")
        reader = BufferedSocketReader(conn, timeout=30.0)

        def send_data(data: bytes) -> None:
            conn.sendall(data)
            self.stats.total_bytes_tx += len(data)

        try:
            while True:
                cmd_byte = reader.read_cmd_byte()
                if cmd_byte is None:
                    # Check if socket closed
                    try:
                        peek = conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                        if not peek:
                            break
                    except (BlockingIOError, InterruptedError):
                        pass
                    except OSError:
                        break
                    continue

                self.stats.total_bytes_rx += len(cmd_byte)

                # -------------------------------------------------------------
                # '#' Print Spooling Request: #<byte>\n
                # -------------------------------------------------------------
                if cmd_byte == b"#":
                    line = reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    try:
                        print_val = self._parse_numeric_argument(line)
                        self.printer_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(self.printer_path, "a", encoding="latin1") as prt:
                            prt.write(chr(print_val))
                        self.stats.prints += 1
                        self.stats.controls += 1
                        self.log_debug(f"Printer byte written: {print_val} ({chr(print_val)!r})")
                    except Exception as e:
                        self.log(f"Error handling print command: {e}")

                # -------------------------------------------------------------
                # '<' or '\' Read Sector Request: <00012\n
                # -------------------------------------------------------------
                elif cmd_byte in (b"<", b"\\"):
                    is_retry = (cmd_byte == b"\\")
                    if is_retry:
                        self.stats.rd_retries += 1
                    else:
                        self.stats.gets += 1

                    line = reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    rsector = self._parse_numeric_argument(line)

                    sector_data = self.read_sector_from_volume(rsector)
                    checksum = sum(sector_data) % 256
                    chk_bytes = struct.pack(">i", checksum)

                    send_data(sector_data)
                    send_data(chk_bytes)
                    self.log_debug(
                        f"Sector READ {'(retry) ' if is_retry else ''}sec={rsector}, "
                        f"chksum={checksum}"
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

                    line = reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    rsector = self._parse_numeric_argument(line)

                    sector_data = reader.recv_exact(SECTOR_SIZE)
                    self.stats.total_bytes_rx += SECTOR_SIZE

                    client_chk_bytes = reader.recv_exact(4)
                    self.stats.total_bytes_rx += 4

                    calc_chk = sum(sector_data) % 256
                    calc_chk_bytes = struct.pack(">i", calc_chk)

                    # Return calculated checksum to client
                    send_data(calc_chk_bytes)

                    # Commit to disk image
                    self.write_sector_to_volume(rsector, sector_data)
                    self.log_debug(
                        f"Sector WRITE {'(retry) ' if is_retry else ''}sec={rsector}, "
                        f"chksum={calc_chk}"
                    )

                # -------------------------------------------------------------
                # '@' Control Commands: @ping\n, @bind\n, @rhdr\n, @stat\n, etc.
                # -------------------------------------------------------------
                elif cmd_byte == b"@":
                    line = reader.readline()
                    self.stats.total_bytes_rx += len(line) + 1
                    cmd_str = line.decode("ascii", errors="ignore").strip().lower()

                    if cmd_str == "ping":
                        self.stats.pings += 1
                        self.stats.controls += 1
                        send_data(b"@pong\n")
                        self.log_debug("Control: @ping -> @pong")

                    elif cmd_str == "bind":
                        self.stats.binds += 1
                        self.stats.controls += 1
                        send_data(b"@bound\n")
                        header_data = self.read_header_from_volume()
                        send_data(header_data)
                        self.log_debug("Control: @bind -> @bound + header (256B)")

                    elif cmd_str == "rhdr":
                        self.stats.rhdrs += 1
                        self.stats.controls += 1
                        header_data = self.read_header_from_volume()
                        send_data(header_data)

                        # Followed by request for directory sector 1
                        sec1_line = reader.readline()
                        self.stats.total_bytes_rx += len(sec1_line) + 1
                        sec1 = self._parse_numeric_argument(sec1_line)
                        send_data(self.read_sector_from_volume(sec1))

                        # Followed by request for directory sector 2
                        sec2_line = reader.readline()
                        self.stats.total_bytes_rx += len(sec2_line) + 1
                        sec2 = self._parse_numeric_argument(sec2_line)
                        send_data(self.read_sector_from_volume(sec2))

                        self.log_debug(f"Control: @rhdr -> header + dir sec {sec1}, {sec2}")

                    elif cmd_str == "echo":
                        self.stats.echos += 1
                        self.stats.controls += 1
                        echo_buf = reader.recv_exact(SECTOR_SIZE)
                        self.stats.total_bytes_rx += len(echo_buf)
                        send_data(echo_buf)
                        self.log_debug("Control: @echo (256B)")

                    elif cmd_str == "stat":
                        self.stats.stats += 1
                        self.stats.controls += 1
                        self.log(self.stats.summary())

                    elif cmd_str == "rset":
                        self.stats.resets += 1
                        self.stats.controls += 1
                        self.log("Control: @rset received")

                    else:
                        self.log(f"Warning: Unknown control command '@{cmd_str}'")

        except (ConnectionResetError, TimeoutError, BrokenPipeError) as e:
            self.log_debug(f"Connection ended: {e}")
        except Exception as e:
            self.log(f"Connection error: {e}")
        finally:
            self.log(f"Client {addr[0]}:{addr[1]} disconnected.")
            self.log(self.stats.summary())

    def run(self) -> None:
        """Start listening for connections and service clients."""
        self.open_volume()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(5)

            self.log("=" * 60)
            self.log("  TRS-NET TCP Server Daemon (trs-netd)")
            self.log("=" * 60)
            self.log(f"Listening on:   {self.host}:{self.port}")
            self.log(f"Volume Image:   {self.volume_path}")
            self.log(f"Printer Spool:  {self.printer_path}")
            self.log("Waiting for incoming client connections (Ctrl+C to stop)...")
            self.log("=" * 60)

            try:
                while True:
                    conn, addr = server.accept()
                    with conn:
                        self.handle_client(conn, addr)
            except KeyboardInterrupt:
                self.log("\nShutting down server daemon...")
            finally:
                self.close_volume()


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

    server = TRSNetServer(
        volume_path=args.volume,
        printer_path=args.printer,
        host=args.host,
        port=args.port,
        verbose=args.verbose,
    )
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
