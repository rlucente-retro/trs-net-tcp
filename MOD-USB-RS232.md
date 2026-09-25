# Olimex MOD-USB-RS232 Guide for Agon Light 2

Comprehensive guide for configuring, connecting, operating, and flashing the **Olimex MOD-USB-RS232** module as a serial link between the **Olimex Agon Light 2** and a host computer (macOS / Linux / Windows).

---

## Table of Contents

1. [Hardware Overview](#1-hardware-overview)
2. [Signal Levels & Safety](#2-signal-levels--safety)
3. [Physical Connections & Cabling](#3-physical-connections--cabling)
4. [Solder Jumpers & Configuration](#4-solder-jumpers--configuration)
5. [Host Laptop Setup (macOS / Linux / Windows)](#5-host-laptop-setup)
6. [Using MOD-USB-RS232 with trs-netd](#6-using-mod-usb-rs232-with-trs-netd)
7. [Agon Light 2 Software & Communication (UART1)](#7-agon-light-2-software--communication-uart1)
8. [Comprehensive Firmware Flashing Guide](#8-comprehensive-firmware-flashing-guide)
   - [Why Flashing May Be Required](#why-flashing-may-be-required)
   - [Firmware Download](#firmware-download)
   - [Hardware Requirements](#hardware-requirements)
   - [ICSP Connector Pinout & Adapters](#icsp-connector-pinout--adapters)
   - [Power Supply Considerations During Flashing](#power-supply-considerations-during-flashing)
   - [Step-by-Step Flashing via MPLAB X IPE](#step-by-step-flashing-via-mplab-x-ipe)
   - [Command-Line Flashing Alternative (`ipecmd`)](#command-line-flashing-alternative-ipecmd)
9. [Quick Troubleshooting Checklist](#9-quick-troubleshooting-checklist)

---

## 1. Hardware Overview

* **Manufacturer:** Olimex Ltd.
* **Microcontroller:** Microchip PIC18F14K50 (20-pin USB Flash Microcontroller with nanoWatt XLP Technology).
* **Connectors:**
  * **USB:** Mini-B USB female connector (connects to host PC/laptop).
  * **UEXT:** 10-pin (2x5) shrouded male IDC header (connects to target development board).
  * **ICSP:** 6-pin mini ICSP connector (0.05" / 1.27 mm pitch) for programming the PIC microcontroller.
* **Status Indicator:** Red LED (`STAT`) on PIC pin 14 (RC2).
* **Documentation & Schematics:**
  * [Official User Manual (PDF)](https://www.olimex.com/Products/Modules/Interface/MOD-USB-RS232/resources/MOD-USB-RS232.pdf)
  * [Official Schematic (PDF)](https://www.olimex.com/Products/Modules/Interface/MOD-USB-RS232/resources/MOD-USB-RS232-schematic.pdf)

---

## 2. Signal Levels & Safety

> [!IMPORTANT]
> Despite having **"RS232"** in its product name, the **MOD-USB-RS232 does NOT output true RS-232 voltages (±12V)**.
> It is an on-board **3.3V TTL/CMOS UART-to-USB converter**.

* **Logic Voltage:** 3.3V DC.
* **Compatibility:** The Zilog eZ80F92 processor on the Olimex Agon Light 2 runs at 3.3V logic. Because the MOD-USB-RS232 operates natively at 3.3V, it is **100% electrically compatible and safe** to plug directly into the Agon Light 2 UEXT port without level shifters.

---

## 3. Physical Connections & Cabling

### Agon Light 2 $\leftrightarrow$ MOD-USB-RS232
* **Cable:** Standard 10-wire flat ribbon cable with 10-pin IDC female connectors on both ends (supplied with the module).
* **Orientation:** Both the Agon Light 2 and the MOD-USB-RS232 use shrouded, keyed IDC headers. The key notch ensures Pin 1 matches Pin 1 on both ends.

### MOD-USB-RS232 $\leftrightarrow$ Laptop
* **Cable:** USB-A (or USB-C) to **USB Mini-B** cable.
* **Power Source:** The module is bus-powered through the Mini-USB port (5V from USB stepped down to 3.3V by an on-board LM1117 linear regulator).

---

## 4. Solder Jumpers & Configuration

The module features four surface-mount solder jumpers on the PCB. Verify their states prior to use:

| Jumper Name | Default State | Required Setting for Agon Light 2 | Purpose & Notes |
| :--- | :--- | :--- | :--- |
| **`UEXT_PWR_3.3`** | **OPEN** | **MUST REMAIN OPEN** | Connects module 3.3V rail to UEXT Pin 1. **Do not close.** Agon Light 2 is powered by its own PSU; closing this jumper bridges two separate active 3.3V regulators together. |
| **`3_RX/3_TX`** | **3_RX** (shorted) | **3_RX** (default) | Routes UEXT Pin 3 to PIC RXD (RB5). Matches Agon Light 2 UEXT Pin 3 (eZ80 TXD1 output). |
| **`4_TX/4_RX`** | **4_TX** (shorted) | **4_TX** (default) | Routes UEXT Pin 4 to PIC TXD (RB7). Matches Agon Light 2 UEXT Pin 4 (eZ80 RXD1 input). |
| **`USB_PWR_3.3`** | **OPEN** | **OPEN** (default) | Connects PIC VUSB to 3.3V. Leave open for standard bus operation. |

### UEXT Pin Mapping (Straight 10-pin Ribbon Cable)

| Pin # | Olimex UEXT Standard | Agon Light 2 Function (Host) | MOD-USB-RS232 Function (Device) | Connection Result |
| :---: | :--- | :--- | :--- | :--- |
| **1** | 3.3V | +3.3V Rail (Agon power) | Isolated (when `UEXT_PWR_3.3` is OPEN) | Safe isolation |
| **2** | GND | Ground | Ground | Common Ground |
| **3** | TXD | eZ80 `PC0` (UART1 TX Output) | PIC `RB5` (UART RX Input) | Agon TX $\rightarrow$ PC RX |
| **4** | RXD | eZ80 `PC1` (UART1 RX Input) | PIC `RB7` (UART TX Output) | Agon RX $\leftarrow$ PC TX |
| **5** | SCL | I2C Clock | Not Connected on module | N/A |
| **6** | SDA | I2C Data | Not Connected on module | N/A |
| **7** | MISO | SPI MISO | Not Connected on module | N/A |
| **8** | MOSI | SPI MOSI | Not Connected on module | N/A |
| **9** | SCK | SPI Clock | Not Connected on module | N/A |
| **10**| CS | SPI Chip Select | Not Connected on module | N/A |

---

## 5. Host Laptop Setup

The PIC18F14K50 firmware exposes a standard **USB CDC-ACM** (Abstract Control Model) Virtual COM Port.

### macOS
* **Driver:** Built into macOS (`AppleUSBACM`). No drivers needed.
* **Device Node:** Enumerates under `/dev/cu.usbmodem*` (e.g. `/dev/cu.usbmodem14101`).
  > Always connect to `/dev/cu.*` instead of `/dev/tty.*` to avoid carrier-detect blocking.
* **Connect using Terminal Emulator:**
  ```bash
  # Using screen:
  screen /dev/cu.usbmodem* 115200

  # Exit screen: Press Ctrl-A, then Ctrl-\, then press 'y'

  # Using Python miniterm:
  python3 -m serial.tools.miniterm /dev/cu.usbmodem* 115200
  ```

### Linux
* **Driver:** Built-in `cdc_acm` kernel driver.
* **Device Node:** `/dev/ttyACM0` (or similar).
* **Permissions:** Ensure your user is part of the `dialout` or `uucp` group:
  ```bash
  sudo usermod -a -G dialout $USER
  ```
* **Connect:**
  ```bash
  minicom -D /dev/ttyACM0 -b 115200
  ```

### Windows (10 / 11)
* **Driver:** Automatically handled by `usbser.sys`.
* If manual driver binding is requested, use the `.inf` driver file provided in the official Olimex software package (`Demo/inf/`).
* **Connect:** Use PuTTY, Tera Term, or ExtraPuTTY pointing to the assigned `COMx` port at `115200 8-N-1`.

---

## 6. Using MOD-USB-RS232 with `trs-netd`

`trs-netd.py` can serve virtual floppy disks directly to TRS-OS through the MOD-USB-RS232 module plugged into the Agon Light 2 UEXT connector.

### Step 1: Connect Hardware
1. Connect the 10-pin ribbon cable between the Agon Light 2 UEXT connector and the MOD-USB-RS232 UEXT connector (the keyed notches ensure Pin 1 matches on both ends).
2. Connect the Mini-USB cable from MOD-USB-RS232 to your host computer.
3. Power on the Agon Light 2.

### Step 2: Identify the Serial Port
Run the port scanner to detect the serial port assigned to the module:
```bash
make list-ports
```
On macOS, the module enumerates as `/dev/cu.usbmodem*` (e.g. `/dev/cu.usbmodem14101`).

### Step 3: Start the Daemon
Run `trs-netd` targeting the serial port at the standard 115200 baud:
```bash
# Auto-detect connected USB serial port:
make run SERIAL=auto

# Or explicitly specify the port device:
make run SERIAL=/dev/cu.usbmodem14101

# Or with verbose debug logging:
make run-verbose SERIAL=/dev/cu.usbmodem14101
```

### Step 4: Mount Remote Drive in TRS-OS
From the TRS-OS prompt on your Agon Light 2:
```text
SYSTEM (DRIVE=6, DRIVER="NETDVR")
DIR :6
```
Virtual disk images from `Volumes/` are now accessible as local disk drives over the direct MOD-USB-RS232 serial connection!

---

## 7. Agon Light 2 Software & Communication (UART1)

On the Olimex Agon Light 2, the UEXT connector is mapped directly to **eZ80 UART1** (`PC0` = TXD1, `PC1` = RXD1).

### Quark MOS C API Example
```c
#include <mos_api.h>

// UART1 configuration: 115200 baud, 8 data bits, 1 stop bit, no parity
static UART uart_cfg = {
    .baudRate = 115200,
    .dataBits = 8,
    .stopBits = 1,
    .parity = 0,
    .flowControl = 0,
    .interrupts = 0
};

void init_serial(void) {
    // Open UART1 (port index 1)
    mos_uopen(&uart_cfg);
}

void serial_send_byte(char c) {
    mos_uputc(c);
}

char serial_recv_byte(void) {
    return mos_ugetc();
}

void close_serial(void) {
    mos_uclose();
}
```

### BBC BASIC Example
```basic
10 REM Open serial port at 115200 baud
20 SYS &00, 115200
30 PRINT "Sending test message..."
40 *FX 21, 1
```

---

## 8. Comprehensive Firmware Flashing Guide

### Why Flashing May Be Required
A known manufacturing issue in select Olimex production batches resulted in some MOD-USB-RS232 boards shipping with an internal **factory loopback test routine** rather than the functional USB-to-UART CDC bridge firmware.

* **Symptom:** When plugged into a computer, the device is recognized, but when opened in a serial terminal, it continuously outputs:
  ```text
  UEXT test ERROR!
  ```
  or fails to pass serial characters between USB and UEXT.
* **Resolution:** Re-flash the PIC18F14K50 with Olimex's precompiled CDC bridge firmware (`Prebuilt.hex`).

> [!TIP]
> **Do NOT purchase a programmer in advance!**
> The vast majority of MOD-USB-RS232 units ship with proper CDC firmware and work immediately out of the box. Flashing is strictly a contingency procedure for the rare units with the factory testing glitch. If your board does arrive with this defect, Olimex support will typically provide a free replacement upon request, avoiding the need to purchase external programming hardware.

---

### Firmware Download

Download the official software archive directly from Olimex:
* **Package URL:** [Demo_MOD-USB-RS232.zip](https://www.olimex.com/Products/Modules/Interface/MOD-USB-RS232/resources/Demo_MOD-USB-RS232.zip)
* **Archive Contents:**
  * `Demo/Prebuilt.hex` — The official compiled production firmware.
  * `Demo/USB2RS232/` — Complete C18 source code project for MPLAB.
  * `Demo/inf/` — Windows USB CDC installation INF file.

---

### Hardware Requirements

#### What is an ICSP Programmer?
An **ICSP Programmer** is a **separate, active physical hardware device (a dedicated electronic tool or dongle)**, not just a passive cable or adapter.

Microcontrollers like the PIC18F14K50 cannot be programmed through standard USB unless they already have a specialized USB bootloader installed. Flashing the raw chip via **ICSP** (In-Circuit Serial Programming) requires a specialized low-level protocol—clock pulses on `PGC`, serial bitstreams on `PGD`, and a controlled reset/programming voltage on `VPP/MCLR`. Your laptop's USB ports cannot produce these signals on their own.

The programmer acts as the bridge between your laptop's USB port and the chip's flashing interface:

```text
[Laptop]  <-- Standard USB Cable -->  [Hardware Programmer]  <-- 6-wire ICSP Cable / Adapter -->  [MOD-USB-RS232]
```

#### Supported Programmers
* **Microchip Official:**
  * **PICkit (PICkit 3, 4, or 5):** A small dongle (pack-of-gum sized) with a USB port on one end and a 6-pin header on the other. Official versions cost ~$40–$80; generic clones on Amazon/eBay are ~$15.
  * **MPLAB SNAP:** An inexpensive, bare-PCB programmer (~$25–$35).
  * **ICD 3 / 4:** Higher-end in-circuit debuggers/programmers.
* **Olimex:** PIC-KIT3 or PIC-ICD2.
* **Universal Programmers:** XGecu TL866II Plus / T48 (using the 6-pin ICSP header port).
* **DIY / Raspberry Pi:** An open-source tool like `pickle` running on a Raspberry Pi (directly driving ICSP signals via GPIO pins) or an Arduino.

#### Physical Connector & Adapter
* The ICSP port on the MOD-USB-RS232 board is a **compact 6-pin mini header (0.05" / 1.27 mm pitch)** labeled `ICSP` (`WU06S`).
* Standard programmers use a **6-pin 0.10" (2.54 mm pitch)** header.
* To bridge the different pin pitches, you need:
  * The official **Olimex PIC-ICSP** adapter board, or
  * Micro grabber test clips (e.g., Pomona or Saleae-style logic analyzer clips), or
  * 1.27 mm pitch female flyleads, or temporarily soldering 30 AWG kynar wires directly to the ICSP pads.

---

### ICSP Connector Pinout & Adapters

#### MOD-USB-RS232 Mini-ICSP Header Pinout (`WU06S`)

Looking at the connector with pin 1 indicated by the silkscreen/PCB indicator:

| Pin # | Signal Name | PIC18F14K50 Pin | Function | Notes |
| :---: | :--- | :---: | :--- | :--- |
| **1** | **VPP / MCLR** | Pin 4 (`RA3/MCLR/VPP`) | Programming Voltage / Reset | Pulled up via 4.7k $\Omega$ to 3.3V |
| **2** | **VDD (+3.3V)** | Pin 1 (`VDD`) | Target Power Supply | **Do NOT apply 5V!** |
| **3** | **GND (VSS)** | Pin 20 (`VSS`) | Ground Reference | Common ground |
| **4** | **PGD** | Pin 19 (`RA0/D+/PGD`) | ICSP Data line | Shared with USB D+ |
| **5** | **PGC** | Pin 18 (`RA1/D-/PGC`) | ICSP Clock line | Shared with USB D- |
| **6** | **NC** | — | Not connected | Leave floating |

#### Programmer (PICkit 3/4/5) 6-pin 0.1" Header Mapping

| PICkit Pin # | Signal Name | Connects to MOD-USB-RS232 Pin |
| :---: | :--- | :---: |
| **1** (Arrow) | VPP / MCLR | Pin 1 |
| **2** | VDD Target | Pin 2 |
| **3** | VSS (GND) | Pin 3 |
| **4** | PGD (ICSPDAT) | Pin 4 |
| **5** | PGC (ICSPCLK) | Pin 5 |
| **6** | AUX / LVP | Pin 6 (Leave unconnected) |

---

### Power Supply Considerations During Flashing

> [!WARNING]
> **VOLTAGE LIMIT WARNING:** The PIC18F14K50 and on-board circuitry operate at **3.3V**.
> Never configure your programmer to power the target at 5.0V!

Choose one of two powering methods:

* **Method A (Recommended — Self-Powered via USB):**
  1. Connect the MOD-USB-RS232 to a standard USB port/charger via its Mini-USB cable. The on-board LM1117 regulator supplies clean 3.3V to the PIC.
  2. Connect **VPP, GND, PGD, and PGC** from the programmer.
  3. Connect **VDD** from the programmer to Pin 2 solely for voltage sensing (do not enable power output from the programmer in software).
* **Method B (Programmer-Powered):**
  1. Leave the Mini-USB cable unplugged.
  2. In your programmer configuration software, enable target power output and **set voltage strictly to 3.3V**.

---

### Step-by-Step Flashing via MPLAB X IPE

Microchip provides the standalone **MPLAB X IPE (Integrated Programming Environment)** as part of the free MPLAB X suite (available for macOS, Linux, and Windows).

#### Step 1: Install Software & Prepare Hex
1. Download and install **MPLAB X IDE / IPE** from Microchip's website.
2. Download `Demo_MOD-USB-RS232.zip` from Olimex and extract `Prebuilt.hex`.

#### Step 2: Configure MPLAB X IPE
1. Launch **MPLAB X IPE**.
2. Set **Family:** `Advanced 8-bit MCUs (PIC18)`.
3. Set **Device:** `PIC18F14K50`.
4. Set **Tool:** Select your programmer (e.g. `PICkit 3`, `PICkit 4`, `SNAP`).
5. Click **Apply**.

#### Step 3: Check Power Settings
1. Click the **Settings** menu at the top, select **Advanced Mode**, and enter the password (default: `microchip`).
2. Navigate to the **Power** tab on the left.
3. If powering via USB (Method A): Ensure *Power Target Circuit from Tool* is **unchecked**.
4. If powering from the tool (Method B): Check *Power Target Circuit from Tool* and set the voltage slider to **`3.3V`**.

#### Step 4: Connect to the Target
1. Connect your programmer to the 6-pin ICSP connector on the board.
2. In the main IPE screen, click **Connect**.
3. In the output console, verify the target signature:
   ```text
   Target device was detected: PIC18F14K50
   Device ID revision: ...
   ```
   *(If Device ID reads `0x000000`, re-check wiring continuity on VPP, PGD, PGC, and GND).*

#### Step 5: Program the Hex File
1. Under **Hex File**, click **Browse** and select `Prebuilt.hex`.
2. Click **Program**.
3. The console will display programming and verification progress:
   ```text
   Programming...
   The following memory area(s) will be programmed:
   program memory: start address = 0x0, end address = 0x27ff
   configuration memory
   Program Memory Programming...
   Configuration Memory Programming...
   Verifying...
   Program Memory Verify Complete
   Configuration Memory Verify Complete
   Programming complete
   ```
4. Disconnect the ICSP connector and power-cycle the board.

---

### Command-Line Flashing Alternative (`ipecmd`)

For headless or automated environments, MPLAB X installs the `ipecmd` command-line utility.

#### Flashing with PICkit 3 (macOS/Linux):
```bash
# Path to ipecmd on macOS:
IPECMD="/Applications/microchip/mplabx/v6.20/mplab_platform/bin/ipecmd.sh"

# Program Prebuilt.hex into PIC18F14K50 using self-powered board:
$IPECMD -P18F14K50 -TPPK3 -M -F/path/to/Prebuilt.hex
```

Parameters:
* `-P18F14K50`: Specifies the target chip.
* `-TPPK3`: Uses PICkit 3 (`-TPPK4` for PICkit 4, `-TPSNAP` for MPLAB SNAP).
* `-M`: Program all memories (Flash, Config words).
* `-F<file>`: Specifies the input HEX file.

---

## 9. Quick Troubleshooting Checklist

| Problem | Root Cause | Solution |
| :--- | :--- | :--- |
| **Terminal prints repetitive `UEXT test ERROR!`** | Module contains factory loopback test code | Re-flash PIC18F14K50 with `Prebuilt.hex` following Section 8. |
| **No serial characters transmitted or received** | Jumper configuration incorrect | Verify jumpers `3_RX/3_TX` and `4_TX/4_RX` are both set to default (device mode). |
| **Agon Light 2 resets or browns out when connected** | Power loop / regulator conflict | Ensure `UEXT_PWR_3.3` jumper on MOD-USB-RS232 is **OPEN**. |
| **Device not appearing under `/dev/cu.*` on Mac** | Bad USB cable or port | Mini-USB cables can be charge-only. Verify cable data lines with another device; try a direct USB port. |
| **ICSP programmer cannot detect target (`Device ID 0x0`)** | Loose ICSP wiring or incorrect voltage | Ensure common GND is connected; verify 3.3V rail is active; keep ICSP jumper wires under 15 cm. |
