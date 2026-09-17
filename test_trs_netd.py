#!/usr/bin/env python3
"""
test_trs_netd.py - Comprehensive Unit & Integration Tests for trs-netd

Tests all protocol operations against a running trs-netd server:
  - @ping -> @pong
  - @bind -> @bound + 256B header
  - Sector Read ('<') with 8-bit checksum verification
  - Sector Write ('>') with round-trip checksum verification
  - @echo -> 256B payload echo
  - Printer byte spooling ('#')
  - Multi-request session handling
  - Inter-command delimiter resilience (CRLF, LFCR, stray newlines)
  - @rhdr multi-sector directory query handshake
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest


class TestTRSNetDaemon(unittest.TestCase):
    server_proc: subprocess.Popen | None = None
    temp_dir: Path
    test_vol: Path
    test_prt: Path
    test_port = 65433

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = Path(tempfile.mkdtemp(prefix="trs_net_test_"))
        cls.test_vol = cls.temp_dir / "test_vol.dsk"
        cls.test_prt = cls.temp_dir / "test_prt.txt"

        # Create a synthetic 720K disk image (256 bytes header + 2880 sectors * 256 bytes)
        # Header: fill with recognizable pattern (0xAA)
        header = b"\xaa" * 256
        # Sector 0 (offset 256): fill with 0x01
        sector0 = b"\x01" * 256
        # Sector 1 (offset 512): fill with 0x02
        sector1 = b"\x02" * 256
        # Sector 2 (offset 768): fill with 0x03
        sector2 = b"\x03" * 256
        # Fill rest with zeros up to 737,536 bytes
        padding = b"\x00" * (737536 - len(header) - len(sector0) - len(sector1) - len(sector2))

        with open(cls.test_vol, "wb") as f:
            f.write(header + sector0 + sector1 + sector2 + padding)

        # Launch trs-netd
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "trs-netd.py"),
            "--port",
            str(cls.test_port),
            "--host",
            "127.0.0.1",
            "--volume",
            str(cls.test_vol),
            "--printer",
            str(cls.test_prt),
            "--verbose",
        ]
        cls.server_proc = subprocess.Popen(cmd)

        # Wait for port to become available
        connected = False
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", cls.test_port), timeout=0.2):
                    connected = True
                    break
            except OSError:
                time.sleep(0.05)
        if not connected:
            raise RuntimeError("Failed to start trs-netd server for testing")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server_proc:
            cls.server_proc.terminate()
            try:
                cls.server_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                cls.server_proc.kill()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", self.test_port))
        sock.settimeout(5.0)
        return sock

    def _recv_exact(self, sock: socket.socket, count: int) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            chunk = sock.recv(count - len(buf))
            if not chunk:
                raise EOFError("Unexpected EOF while reading socket")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_line(self, sock: socket.socket) -> bytes:
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(64)
            if not chunk:
                raise EOFError("Unexpected EOF while waiting for line")
            buf.extend(chunk)
        idx = buf.index(b"\n")
        return bytes(buf[:idx]).rstrip(b"\r")

    def test_01_ping_pong(self) -> None:
        """Verify @ping returns @pong."""
        with self._connect() as s:
            s.sendall(b"@ping\n")
            reply = self._recv_line(s)
            self.assertEqual(reply, b"@pong")

    def test_02_bind(self) -> None:
        """Verify @bind returns @bound\\n and 256-byte volume header."""
        with self._connect() as s:
            s.sendall(b"@bind\n")
            reply = self._recv_line(s)
            self.assertEqual(reply, b"@bound")
            header = self._recv_exact(s, 256)
            self.assertEqual(header, b"\xaa" * 256)

    def test_03_read_sector(self) -> None:
        """Verify sector read '<00000\\n' returns 256 bytes + 4-byte checksum."""
        with self._connect() as s:
            s.sendall(b"<00000\n")
            data = self._recv_exact(s, 256)
            chk_raw = self._recv_exact(s, 4)
            self.assertEqual(data, b"\x01" * 256)
            checksum = struct.unpack(">i", chk_raw)[0]
            expected_chk = (sum(b"\x01" * 256)) % 256
            self.assertEqual(checksum, expected_chk)

    def test_04_read_sector_one(self) -> None:
        """Verify sector 1 read '<00001\\n' returns 0x02 * 256."""
        with self._connect() as s:
            s.sendall(b"<00001\n")
            data = self._recv_exact(s, 256)
            chk_raw = self._recv_exact(s, 4)
            self.assertEqual(data, b"\x02" * 256)
            checksum = struct.unpack(">i", chk_raw)[0]
            self.assertEqual(checksum, (sum(b"\x02" * 256)) % 256)

    def test_05_write_sector(self) -> None:
        """Verify sector write '>00005\\n' stores data and returns checksum."""
        write_payload = bytes(range(256))  # 0..255
        expected_chk = sum(write_payload) % 256
        dummy_client_chk = struct.pack(">i", expected_chk)

        with self._connect() as s:
            s.sendall(b">00005\n")
            s.sendall(write_payload)
            s.sendall(dummy_client_chk)

            # Server returns its computed checksum
            ret_chk_raw = self._recv_exact(s, 4)
            ret_chk = struct.unpack(">i", ret_chk_raw)[0]
            self.assertEqual(ret_chk, expected_chk)

            # Now read it back in same session
            s.sendall(b"<00005\n")
            readback = self._recv_exact(s, 256)
            self.assertEqual(readback, write_payload)

    def test_06_echo(self) -> None:
        """Verify @echo echoes back 256 bytes."""
        payload = (b"ECHO_TEST_PATTERN" * 16)[:256]
        self.assertEqual(len(payload), 256)

        with self._connect() as s:
            s.sendall(b"@echo\n")
            s.sendall(payload)
            echoed = self._recv_exact(s, 256)
            self.assertEqual(echoed, payload)

    def test_07_printer_spool(self) -> None:
        """Verify '#00065\\n' appends ASCII 'A' to print spool."""
        with self._connect() as s:
            s.sendall(b"#00065\n")
            s.sendall(b"#00066\n")
            s.sendall(b"#00067\n")
            time.sleep(0.1)

        with open(self.test_prt, "r", encoding="latin1") as f:
            content = f.read()
        self.assertIn("ABC", content)

    def test_08_crlf_and_stray_delimiters(self) -> None:
        """Verify server correctly ignores stray CR, LF, and whitespace between commands."""
        with self._connect() as s:
            # Send with mixed CRLF and stray newlines
            s.sendall(b"\r\n\r\n@ping\r\n\r\n\r\n<00000\n\r")
            pong = self._recv_line(s)
            self.assertEqual(pong, b"@pong")

            data = self._recv_exact(s, 256)
            chk_raw = self._recv_exact(s, 4)
            self.assertEqual(data, b"\x01" * 256)

    def test_09_rhdr_handshake(self) -> None:
        """Verify full @rhdr multi-stage query sequence used by TRS-OS IPL."""
        with self._connect() as s:
            s.sendall(b"@rhdr\n")
            header = self._recv_exact(s, 256)
            self.assertEqual(header, b"\xaa" * 256)

            # Client then requests sector 0 (query_rd_sector: <00000\n\r)
            s.sendall(b"<00000\n")
            sec0 = self._recv_exact(s, 256)
            self.assertEqual(sec0, b"\x01" * 256)

            # Client then requests sector 2
            s.sendall(b"<00002\n")
            sec2 = self._recv_exact(s, 256)
            self.assertEqual(sec2, b"\x03" * 256)

    def test_10_write_checksum_mismatch_protection(self) -> None:
        """Verify that a write with an invalid client checksum is rejected and not written to disk."""
        sec_num = 6
        bad_payload = b"\x77" * 256
        bad_chksum_bytes = b"\x00\x00\x00\xfe"  # Actual checksum of 256 * 0x77 is 0

        with self._connect() as s:
            # 1. Attempt write with mismatched client checksum
            s.sendall(f">{sec_num:05d}\n".encode("ascii"))
            s.sendall(bad_payload)
            s.sendall(bad_chksum_bytes)

            ret_chk = self._recv_exact(s, 4)
            self.assertEqual(ret_chk, b"\x00\x00\x00\x00")  # Server returned calculated sum (0)

            # 2. Read back sector 6 to verify it was NOT committed
            s.sendall(f"<{sec_num:05d}\n".encode("ascii"))
            read_back = self._recv_exact(s, 256)
            _ = self._recv_exact(s, 4)
            self.assertEqual(read_back, b"\x00" * 256, "Corrupted sector must NOT be written on checksum mismatch")

            # 3. Perform retry write ('/') with matching checksum
            good_chksum_bytes = b"\x00\x00\x00\x00"
            s.sendall(f"/{sec_num:05d}\n".encode("ascii"))
            s.sendall(bad_payload)
            s.sendall(good_chksum_bytes)

            ret_chk2 = self._recv_exact(s, 4)
            self.assertEqual(ret_chk2, b"\x00\x00\x00\x00")

            # 4. Read back sector 6 to verify successful write
            s.sendall(f"<{sec_num:05d}\n".encode("ascii"))
            read_back2 = self._recv_exact(s, 256)
            _ = self._recv_exact(s, 4)
            self.assertEqual(read_back2, bad_payload)

    def test_11_unrecognized_command_recovery(self) -> None:
        """Verify server gracefully ignores unknown command bytes without desyncing or crashing."""
        with self._connect() as s:
            # Send unknown command byte followed by valid ping
            s.sendall(b"Z\n@ping\n")
            pong = self._recv_line(s)
            self.assertEqual(pong, b"@pong")


if __name__ == "__main__":
    unittest.main()

