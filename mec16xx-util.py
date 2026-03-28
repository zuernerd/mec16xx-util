#!/usr/bin/env python3
"""
MEC16xx Flash Programming Tool
==============================

Erase, program, and read MEC16xx embedded controller flash via JTAG.
Requires OpenOCD running separately.
"""

import os
import struct
import sys
import time

from openocd import Client

# =============================================================================
# CONFIGURATION
# =============================================================================

OPENOCD_PORT = 6666  # Tcl interface

# Flash controller registers
FLASH_DATA = 0xFF3900
FLASH_ADDRESS = 0xFF3904
FLASH_COMMAND = 0xFF3908
FLASH_STATUS = 0xFF390C
FLASH_CONFIG = 0xFF3910

# Flash commands (bit 8 = Reg_Ctl must be 1)
CMD_STANDBY = 0x100  # Mode 0: Standby
CMD_READ = 0x101  # Mode 1: Read
CMD_PROGRAM = 0x102  # Mode 2: Program
CMD_PROGRAM_BURST = 0x106  # Mode 2 + Burst (bit 2)
CMD_ERASE = 0x103  # Mode 3: Erase

# Flash status bits
STATUS_BUSY = 0x001  # Bit 0: Operation in progress
STATUS_DATA_FULL = 0x002  # Bit 1: Data FIFO full
STATUS_BOOT_BLOCK = 0x020  # Bit 5: Boot block protected
STATUS_DATA_BLOCK = 0x040  # Bit 6: Data block protected
STATUS_PROTECT_ERR = 0x080  # Bit 7: Protection error
STATUS_ERRORS = 0x700  # Bits 8-10: Error flags

# Flash memory regions
BOOT_BLOCK_END = 0x1000  # Boot block: 0x00000 - 0x00FFF (4KB)
FLASH_PAGE_SIZE = 0x1000  # 4KB pages

# JTAG DR_RESET_TEST bits (from Glasgow - order matters!)
BIT_ME = 0  # Mass Erase
BIT_VCC_POR = 1  # VCC Power-On Reset (active low)
BIT_VTR_POR = 2  # VTR Power-On Reset (active low)
BIT_POR_EN = 3  # Enable JTAG POR control

# EEPROM registers (base 0xF02C00, LDN 0Bh)
EEPROM_DATA = 0xF02C00
EEPROM_ADDRESS = 0xF02C04
EEPROM_COMMAND = 0xF02C08
EEPROM_STATUS = 0xF02C0C
EEPROM_CONFIG = 0xF02C10
EEPROM_UNLOCK = 0xF02C20

# EEPROM commands (bits [1:0] = mode, bit 2 = burst)
EEPROM_CMD_STANDBY = 0x00
EEPROM_CMD_READ_BURST = 0x05  # Read + Burst

# EEPROM status bits
EEPROM_STATUS_BUSY = 0x01
EEPROM_STATUS_BLOCK = 0x80  # Bit 7: EEPROM blocked
EEPROM_STATUS_ERRORS = 0x300  # Bits 8-9: Busy_Err + CMD_Err

EEPROM_SIZE = 2048  # 2KB


# =============================================================================
# HELPERS
# =============================================================================


def hex_dump(data, base_addr=0):
    """Print hex dump of data with address and ASCII columns."""
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        print(f"  {base_addr + offset:05X}: {hex_part:<48s} {ascii_part}")


# =============================================================================
# FLASH OPERATIONS
# =============================================================================


def wait_not_busy(ocd, timeout=5.0):
    """Wait for flash controller to finish operation."""
    start = time.time()
    while time.time() - start < timeout:
        status = ocd.read_memory(FLASH_STATUS, count=1, width=32)[0]
        if status & STATUS_ERRORS:
            return False
        if (status & STATUS_BUSY) == 0:
            return True
        time.sleep(0.001)
    return False


def wait_data_not_full(ocd, timeout=1.0):
    """Wait for flash data FIFO to have space."""
    start = time.time()
    while time.time() - start < timeout:
        status = ocd.read_memory(FLASH_STATUS, count=1, width=32)[0]
        if status & STATUS_ERRORS:
            return False
        if (status & STATUS_DATA_FULL) == 0:
            return True
        time.sleep(0.001)
    return False


# =============================================================================
# CHIP INFO
# =============================================================================


def do_chip_info(ocd):
    """Read and display chip status and flash controller state."""
    print("\nChip Info")
    print("=" * 40)

    ocd.halt()
    time.sleep(0.2)

    # JTAG IDCODE
    idcode_raw = ocd.execute("irscan mec16xx.cpu 0x1; drscan mec16xx.cpu 32 0x0")
    print(f"  JTAG IDCODE:    {idcode_raw.strip()}")

    # Check target is alive (unpowered = all reads return 0)
    vals = [
        ocd.read_memory(addr, count=1, width=32)[0]
        for addr in [0x0000, 0x1000, FLASH_STATUS]
    ]
    if all(v == 0 for v in vals):
        print("\n  [WARN] Target may not be powered or connected(all reads return 0).")
        return

    # Flash status
    status = ocd.read_memory(FLASH_STATUS, count=1, width=32)[0]
    print(f"  Flash Status:   0x{status:08X}")
    print(f"    Boot Protect: {'Yes' if status & STATUS_BOOT_BLOCK else 'No'}")
    print(f"    Data Protect: {'Yes' if status & STATUS_DATA_BLOCK else 'No'}")
    print(f"    Protect Err:  {'Yes' if status & STATUS_PROTECT_ERR else 'No'}")
    print(f"    Errors:       0x{(status & STATUS_ERRORS) >> 8:X}")

    # EEPROM status
    ee_status = ocd.read_memory(EEPROM_STATUS, count=1, width=32)[0]
    ee_blocked = bool(ee_status & EEPROM_STATUS_BLOCK)
    print(f"\n  EEPROM Status:  0x{ee_status:08X}")
    print(f"    Blocked:      {'Yes' if ee_blocked else 'No'}")
    print(f"    Busy:         {'Yes' if ee_status & EEPROM_STATUS_BUSY else 'No'}")
    if ee_blocked:
        print("    (EEPROM inaccessible - password unlock or VTR POR required)")

    # Quick flash content check
    print("\n  Flash Content:")
    boot_word = ocd.read_memory(0x0000, count=1, width=32)[0]
    app_word = ocd.read_memory(0x1000, count=1, width=32)[0]
    print(
        f"    Boot [0x00000]: 0x{boot_word:08X} {'(erased)' if boot_word == 0xFFFFFFFF else ''}"
    )
    print(
        f"    App  [0x01000]: 0x{app_word:08X} {'(erased)' if app_word == 0xFFFFFFFF else ''}"
    )


# =============================================================================
# EMERGENCY MASS ERASE
# =============================================================================


def do_emergency_erase(ocd):
    """
    Erase entire flash using JTAG emergency mass erase.

    This bypasses all protection and erases everything.
    Power cycle required after this before programming!

    Sequence (from Glasgow applet):
      1. Select RESET_TEST register (IR=0x2)
      2. Initialize PORs deasserted
      3. Enable POR control
      4. Assert VTR_POR (hold in reset)
      5. Set ME bit (mass erase)
      6. Deassert VTR_POR (triggers erase on rising edge)
      7. Wait for erase
      8. Clear ME, cycle POR, disable control
    """
    print("\nEmergency Mass Erase")
    print("=" * 40)

    # Select RESET_TEST register
    print("  Selecting JTAG RESET_TEST register...")
    ocd.execute("irscan mec16xx.cpu 0x2")

    # Initialize - both PORs deasserted (1 = deasserted, active low)
    print("  Initializing POR signals...")
    dr = (1 << BIT_VCC_POR) | (1 << BIT_VTR_POR)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    # Enable JTAG control of POR
    print("  Enabling POR control...")
    dr = (1 << BIT_VCC_POR) | (1 << BIT_VTR_POR) | (1 << BIT_POR_EN)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    # Assert VTR_POR (0 = asserted)
    print("  Asserting VTR_POR (reset)...")
    dr = (1 << BIT_VCC_POR) | (1 << BIT_POR_EN)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    # Set Mass Erase bit
    print("  Setting Mass Erase bit...")
    dr = (1 << BIT_ME) | (1 << BIT_VCC_POR) | (1 << BIT_POR_EN)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    # Deassert VTR_POR - rising edge triggers mass erase!
    print("  Triggering erase (VTR_POR rising edge)...")
    dr = (1 << BIT_ME) | (1 << BIT_VCC_POR) | (1 << BIT_VTR_POR) | (1 << BIT_POR_EN)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    # Wait for erase to complete
    print("  Waiting for erase (1 second)...")
    time.sleep(1.0)

    # Clear ME bit
    print("  Clearing Mass Erase bit...")
    dr = (1 << BIT_VCC_POR) | (1 << BIT_VTR_POR) | (1 << BIT_POR_EN)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    # Power cycle via POR
    print("  Power cycling chip...")
    dr = (1 << BIT_VCC_POR) | (1 << BIT_POR_EN)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")
    time.sleep(0.1)
    dr = (1 << BIT_VCC_POR) | (1 << BIT_VTR_POR) | (1 << BIT_POR_EN)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    # Disable POR control
    print("  Disabling POR control...")
    dr = (1 << BIT_VCC_POR) | (1 << BIT_VTR_POR)
    ocd.execute(f"drscan mec16xx.cpu 4 {dr}")

    print("\n[OK] Erase complete!")
    print("\n[!] POWER CYCLE THE BOARD before programming!")


# =============================================================================
# FLASH ERASE (PAGE ERASE VIA CONTROLLER)
# =============================================================================


def do_erase_flash(ocd, address, size):
    """
    Erase flash pages using the flash controller's erase mode.
    Address should be page-aligned. Erases all pages covering the given range.
    """
    page_count = (size + FLASH_PAGE_SIZE - 1) // FLASH_PAGE_SIZE
    end = address + page_count * FLASH_PAGE_SIZE

    print(f"\nErasing 0x{address:05X} - 0x{end:05X} ({page_count} pages)")
    print("=" * 40)

    # Halt CPU
    ocd.halt()
    time.sleep(0.2)

    # Enable flash controller
    ocd.write_memory(FLASH_CONFIG, [0x01], width=32)
    ocd.write_memory(FLASH_COMMAND, [CMD_STANDBY], width=32)
    ocd.write_memory(FLASH_STATUS, [STATUS_ERRORS], width=32)
    time.sleep(0.05)

    # Set erase mode
    ocd.write_memory(FLASH_COMMAND, [CMD_ERASE], width=32)

    start = time.time()

    for i in range(page_count):
        page_addr = address + i * FLASH_PAGE_SIZE

        ocd.write_memory(FLASH_STATUS, [STATUS_ERRORS], width=32)
        ocd.write_memory(FLASH_ADDRESS, [page_addr], width=32)

        if not wait_not_busy(ocd, timeout=5.0):
            status = ocd.read_memory(FLASH_STATUS, count=1, width=32)[0]
            print(f"  [FAIL] Erase failed at 0x{page_addr:05X} (status=0x{status:03X})")
            ocd.write_memory(FLASH_COMMAND, [CMD_STANDBY], width=32)
            return False

        print(f"  Page {i + 1}/{page_count} (0x{page_addr:05X}) [OK]")

    elapsed = time.time() - start

    ocd.write_memory(FLASH_COMMAND, [CMD_STANDBY], width=32)

    print(f"\n[OK] Erased {page_count} pages in {elapsed:.1f}s")
    return True


# =============================================================================
# PROGRAMMING
# =============================================================================


def do_write_flash(ocd, address, firmware_path):
    """
    Write firmware to flash at specified address.

    Requirements:
      - Flash must be erased (all 0xFFFFFFFF)
      - No boot protection active
      - Just use simple halt - no JTAG reset!
    """
    # Load firmware
    print(f"\nLoading {firmware_path}...")
    with open(firmware_path, "rb") as f:
        data = f.read()

    words = [struct.unpack("<I", data[i : i + 4])[0] for i in range(0, len(data), 4)]
    print(f"  Size: {len(data)} bytes ({len(words)} words)")

    print("\nProgramming")
    print("=" * 40)

    # Simple halt - DO NOT use JTAG reset (breaks register writes!)
    print("  Halting CPU...")
    ocd.halt()
    time.sleep(0.2)

    # Enable flash controller
    print("  Enabling flash controller...")
    ocd.write_memory(FLASH_CONFIG, [0x01], width=32)  # Reg_Ctl_En = 1
    ocd.write_memory(FLASH_COMMAND, [CMD_STANDBY], width=32)
    ocd.write_memory(FLASH_STATUS, [STATUS_ERRORS], width=32)  # Clear errors
    time.sleep(0.05)

    # Verify setup
    config = ocd.read_memory(FLASH_CONFIG, count=1, width=32)[0]
    status = ocd.read_memory(FLASH_STATUS, count=1, width=32)[0]

    if config != 0x01:
        print("  [FAIL] ERROR: Register writes not working!")
        print("    Kill any stale OpenOCD processes and retry.")
        return False

    if status & STATUS_BOOT_BLOCK and address < BOOT_BLOCK_END:
        print("  [FAIL] ERROR: Boot protection active!")
        print("    Run 'erase' command first, then power cycle.")
        return False

    # Verify target region is erased
    check_addr = address
    check_word = ocd.read_memory(check_addr, count=1, width=32)[0]
    if check_word != 0xFFFFFFFF:
        print(f"  [FAIL] ERROR: Target region not erased!")
        print(f"    [0x{check_addr:05X}]=0x{check_word:08X}")
        print(f"    Run 'erase-flash 0x{address:X} 0x{len(data):X}' first.")
        return False

    print("  [OK] Target region is erased")

    # Program using burst mode
    print(f"  Programming {len(words)} words at 0x{address:05X}...")

    ocd.write_memory(FLASH_COMMAND, [CMD_PROGRAM_BURST], width=32)
    ocd.write_memory(FLASH_ADDRESS, [address], width=32)
    wait_not_busy(ocd)

    start = time.time()

    for i, word in enumerate(words):
        # Wait for FIFO space
        if not wait_data_not_full(ocd):
            print(f"\n  [FAIL] FIFO timeout at word {i}")
            return False

        # Write data
        ocd.write_memory(FLASH_DATA, [word], width=32)

        # Progress
        if i % 1000 == 0:
            pct = (i * 100) // len(words)
            print(f"    {pct:3d}%", end="\r")

    # Wait for completion
    time.sleep(0.5)
    wait_not_busy(ocd, timeout=10.0)

    elapsed = time.time() - start
    print(f"  [OK] Programmed in {elapsed:.1f}s ({len(words) / elapsed:.0f} words/sec)")

    # Return to standby
    ocd.write_memory(FLASH_COMMAND, [CMD_STANDBY], width=32)

    print("\n[OK] Programming complete!")
    print("\n  Power cycle and test the board.")
    return True


def do_read_flash(ocd, address, size, output_path=None, burst=False):
    """
    Read flash memory. Saves to file if output_path given, otherwise prints hex dump.
    """
    mode = "burst" if burst else "normal"
    print(f"\nReading {size} bytes from 0x{address:05X} ({mode} mode)...")

    # Halt CPU
    print("  Halting CPU...")
    ocd.halt()
    time.sleep(0.2)

    # Enable flash controller
    print("  Enabling flash controller...")
    ocd.write_memory(FLASH_CONFIG, [0x01], width=32)

    if burst:
        ocd.write_memory(FLASH_COMMAND, [CMD_READ | 0x04], width=32)  # Read + Burst bit
    else:
        ocd.write_memory(FLASH_COMMAND, [CMD_READ], width=32)

    time.sleep(0.05)

    # Read data
    words = []
    word_count = (size + 3) // 4
    print(f"  Reading {word_count} words...")

    start = time.time()

    if burst:
        # Burst mode: write start address once, then just read data repeatedly
        ocd.write_memory(FLASH_ADDRESS, [address], width=32)
        wait_not_busy(ocd)

        for i in range(word_count):
            word = ocd.read_memory(FLASH_DATA, count=1, width=32)[0]
            words.append(word)

            if i % 1000 == 0:
                pct = (i * 100) // word_count
                print(f"    {pct:3d}%", end="\r")
    else:
        # Normal mode: write address for each word
        for i in range(word_count):
            ocd.write_memory(FLASH_ADDRESS, [address + i * 4], width=32)
            wait_not_busy(ocd)
            word = ocd.read_memory(FLASH_DATA, count=1, width=32)[0]
            words.append(word)

            if i % 1000 == 0:
                pct = (i * 100) // word_count
                print(f"    {pct:3d}%", end="\r")

    elapsed = time.time() - start
    print(f"  [OK] Read in {elapsed:.1f}s ({word_count / elapsed:.0f} words/sec)")

    # Return to standby
    ocd.write_memory(FLASH_COMMAND, [CMD_STANDBY], width=32)

    # Convert to bytes
    data = b"".join(struct.pack("<I", w) for w in words)[:size]

    if output_path:
        print(f"  Writing to {output_path}...")
        with open(output_path, "wb") as f:
            f.write(data)
        print(f"\n[OK] Read complete! Saved {len(data)} bytes.")
    else:
        print()
        hex_dump(data, address)

    return True


def do_verify(ocd, address, firmware_path):
    """
    Verify flash contents against a binary file.
    Uses normal (non-burst) read mode to safely handle protected regions.
    """
    print(f"\nLoading {firmware_path}...")
    with open(firmware_path, "rb") as f:
        data = f.read()

    words = [struct.unpack("<I", data[i : i + 4])[0] for i in range(0, len(data), 4)]
    print(f"  Size: {len(data)} bytes ({len(words)} words)")

    print(f"\nVerifying 0x{address:05X} - 0x{address + len(data):05X}")
    print("=" * 40)

    # Halt CPU
    ocd.halt()
    time.sleep(0.2)

    # Enable flash controller in read mode
    ocd.write_memory(FLASH_CONFIG, [0x01], width=32)
    ocd.write_memory(FLASH_COMMAND, [CMD_READ], width=32)
    time.sleep(0.05)

    mismatches = 0
    errors = 0
    start = time.time()

    for i, expected in enumerate(words):
        addr = address + i * 4

        # Clear errors before each read
        ocd.write_memory(FLASH_STATUS, [STATUS_ERRORS], width=32)
        ocd.write_memory(FLASH_ADDRESS, [addr], width=32)

        if not wait_not_busy(ocd):
            # Protection error - report and continue
            if errors == 0:
                print(f"  [WARN] Protection error starting at 0x{addr:05X}")
            errors += 1
            continue

        actual = ocd.read_memory(FLASH_DATA, count=1, width=32)[0]
        if actual != expected:
            if mismatches < 10:  # Print first 10 mismatches
                print(
                    f"  [FAIL] 0x{addr:05X}: expected 0x{expected:08X}, got 0x{actual:08X}"
                )
            mismatches += 1

        if i % 1000 == 0:
            pct = (i * 100) // len(words)
            print(f"    {pct:3d}%", end="\r")

    elapsed = time.time() - start

    ocd.write_memory(FLASH_COMMAND, [CMD_STANDBY], width=32)

    print(f"  Verified in {elapsed:.1f}s")
    if errors:
        print(f"  [WARN] {errors} words in protected regions (skipped)")
    if mismatches:
        print(f"\n[FAIL] Verification failed: {mismatches} mismatches")
        return False
    else:
        print(f"\n[OK] Verification passed! ({len(words)} words match)")
        return True


# =============================================================================
# EEPROM OPERATIONS
# =============================================================================


def do_read_eeprom(ocd, address, size, output_path=None):
    """Read EEPROM contents using burst read mode (byte-at-a-time)."""
    print(f"\nReading EEPROM: {size} bytes from 0x{address:03X}")
    print("=" * 40)

    ocd.halt()
    time.sleep(0.2)

    # Check if EEPROM is blocked
    status = ocd.read_memory(EEPROM_STATUS, count=1, width=32)[0]
    if status & EEPROM_STATUS_BLOCK:
        print("  [FAIL] EEPROM is blocked (EEPROM_BLOCK=1)")
        print("    EEPROM may be password-protected or locked via JTAG.")
        return False

    # Clear errors
    ocd.write_memory(EEPROM_STATUS, [EEPROM_STATUS_ERRORS], width=32)

    # Set burst read mode and write start address
    ocd.write_memory(EEPROM_COMMAND, [EEPROM_CMD_READ_BURST], width=32)
    ocd.write_memory(EEPROM_ADDRESS, [address], width=32)

    # Read bytes (EEPROM data register is 8-bit, auto-increments in burst)
    data = bytearray()
    start_time = time.time()

    for i in range(size):
        byte_val = ocd.read_memory(EEPROM_DATA, count=1, width=32)[0] & 0xFF
        data.append(byte_val)

        if i % 256 == 0:
            pct = (i * 100) // size
            print(f"    {pct:3d}%", end="\r")

    elapsed = time.time() - start_time

    # Return to standby (flushes FIFOs)
    ocd.write_memory(EEPROM_COMMAND, [EEPROM_CMD_STANDBY], width=32)

    print(f"  [OK] Read {len(data)} bytes in {elapsed:.1f}s")

    if output_path:
        print(f"  Writing to {output_path}...")
        with open(output_path, "wb") as f:
            f.write(data)
        print(f"\n[OK] Read complete! Saved {len(data)} bytes.")
    else:
        print()
        hex_dump(data, address)

    return True


# =============================================================================
# MAIN
# =============================================================================

USAGE = """
MEC16xx Flash Tool
==================
Usage: python mec16xx_flash.py <command> [args...]

Requires OpenOCD running separately:
  openocd -f mec16xx_ft232h.cfg
  openocd -f mec16xx_jlink.cfg

Commands:
  info                                       Show chip and flash status
  erase                                      Emergency mass erase (JTAG)
  erase-pages <addr> <size>                  Erase flash pages
  write-flash <addr> <file> [--verify]       Program flash from binary
  read-flash <addr> <size> [file] [--burst]  Read flash (hex dump or save)
  verify <addr> <file>                       Verify flash against binary
  read-eeprom [file]                         Dump EEPROM (2KB)
  read-eeprom <addr> <size> [file]           Read EEPROM range
""".strip()


def parse_int(s, name="value"):
    """Parse a hex/decimal string, exit on error."""
    try:
        return int(s, 0)
    except ValueError:
        sys.exit(f"ERROR: Invalid {name}: {s}")


def require_file(path):
    """Check file exists, exit on error."""
    if not os.path.exists(path):
        sys.exit(f"ERROR: File not found: {path}")
    return path


def main():
    args = sys.argv[1:]

    if not args:
        print(USAGE)
        return

    cmd = args[0].lower()

    try:
        with Client(host="localhost", port=OPENOCD_PORT) as ocd:
            if cmd == "info":
                do_chip_info(ocd)

            elif cmd == "erase":
                do_emergency_erase(ocd)

            elif cmd == "erase-pages":
                if len(args) < 3:
                    sys.exit("Usage: erase-pages <addr> <size>")
                addr = parse_int(args[1], "address")
                size = parse_int(args[2], "size")
                do_erase_flash(ocd, addr, size)

            elif cmd == "write-flash":
                if len(args) < 3:
                    sys.exit("Usage: write-flash <addr> <file> [--verify]")
                addr = parse_int(args[1], "address")
                fw_path = require_file(args[2])
                if do_write_flash(ocd, addr, fw_path) and "--verify" in args:
                    do_verify(ocd, addr, fw_path)

            elif cmd == "read-flash":
                if len(args) < 3:
                    sys.exit("Usage: read-flash <addr> <size> [file] [--burst]")
                addr = parse_int(args[1], "address")
                size = parse_int(args[2], "size")
                burst = "--burst" in args
                remaining = [a for a in args[3:] if a != "--burst"]
                do_read_flash(
                    ocd, addr, size, remaining[0] if remaining else None, burst
                )

            elif cmd == "verify":
                if len(args) < 3:
                    sys.exit("Usage: verify <addr> <file>")
                addr = parse_int(args[1], "address")
                fw_path = require_file(args[2])
                do_verify(ocd, addr, fw_path)

            elif cmd == "read-eeprom":
                if len(args) >= 3:
                    addr = parse_int(args[1], "address")
                    size = parse_int(args[2], "size")
                    output_path = args[3] if len(args) >= 4 else None
                elif len(args) == 2:
                    addr, size, output_path = 0, EEPROM_SIZE, args[1]
                else:
                    addr, size, output_path = 0, EEPROM_SIZE, None
                do_read_eeprom(ocd, addr, size, output_path)

            else:
                print(f"Unknown command: {cmd}")
                print(USAGE)

    except ConnectionRefusedError:
        sys.exit("ERROR: Cannot connect to OpenOCD on port 6666.\n")


if __name__ == "__main__":
    main()
