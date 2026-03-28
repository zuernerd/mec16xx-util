# mec16xx-util

A Python script to read, write, and erase the internal Flash and EEPROM memory of Microchip MEC16xx embedded controllers via JTAG and OpenOCD. Useful for mainboard repair and EC firmware development without expensive dedicated programmers.

Tested with the **MEC1641**. Should be compatible with other MEC16xx chips sharing a similar Flash controller architecture (e.g. MEC1632, MEC1633, MEC1663, MEC1618/MEC1618i, MEC1609/MEC1609i).

> Since no datasheet was available for the MEC1641, all register information was reverse-engineered from the [MEC1632 datasheet](https://ww1.microchip.com/downloads/en/DeviceDoc/00001592B.pdf).

### Typical flash sizes by model

| Model | Flash |
|---|---|
| MEC1609(i), MEC1618(i), MEC1632, MEC1633 | 192 KiB |
| MEC1663 | 256 KiB |
| MEC1641 | 288 KiB |


## Requirements

### Hardware

Any JTAG adapter supported by OpenOCD should work. The following have been tested:

**FTDI-based adapters** (recommended)
- [Adafruit FT232H Breakout](https://www.adafruit.com/product/2264) - tested
- FT2232H / FT4232H boards

**Segger J-Link**
- J-Link Ultra - tested on Windows and Linux
- J-Link EDU Mini - tested on Linux only

**Other OpenOCD-compatible adapters** (untested)
- ST-Link V2/V3
- Bus Pirate
- Raspberry Pi GPIO

### Software
- Python 3.7+
- OpenOCD 0.11.0+

```bash
# Ubuntu/Debian
sudo apt install openocd

# Windows - download from https://openocd.org/pages/getting-openocd.html
```


## Usage

OpenOCD must be running before invoking the script. Use the config file matching your adapter:

```bash
# FT232H adapter
openocd -f mec16xx_ft232h.cfg

# J-Link
openocd -f mec16xx_jlink.cfg
```

Then run commands with:

```bash
python mec16xx_flash.py <command> [args...]
```

### Commands

| Command | Description |
|---|---|
| `info` | Show chip and flash/EEPROM status |
| `erase` | Emergency mass erase via JTAG |
| `erase-pages <addr> <size>` | Erase flash pages via controller |
| `write-flash <addr> <file> [--verify]` | Program flash from binary |
| `read-flash <addr> <size> [file] [--burst]` | Read flash (hex dump or save to file) |
| `verify <addr> <file>` | Verify flash against a binary |
| `read-eeprom [file]` | Dump full EEPROM (2 KB) |
| `read-eeprom <addr> <size> [file]` | Read EEPROM range |

### Verify connection

```bash
python mec16xx_flash.py info
```

### Erasing

There are two erase modes:

- **Page erase** (`erase-pages`) - erases specific flash pages via the flash controller. Use this for targeted erase before programming.
- **Emergency mass erase** (`erase`) - sends a special JTAG sequence that erases the entire flash and EEPROM, bypassing all protection. Useful when boot or data block protection is active, or the chip is in a bad state. **Requires a power cycle before programming.**

### Programming

```bash
python mec16xx_flash.py write-flash 0x1000 firmware.bin
```

Add `--verify` to automatically verify after programming:

```bash
python mec16xx_flash.py write-flash 0x1000 firmware.bin --verify
```

### Reading / Dumping

```bash
# Print hex dump to terminal
python mec16xx_flash.py read-flash 0x0000 0x48000

# Save to file
python mec16xx_flash.py read-flash 0x0000 0x48000 dump.bin

# Use burst mode for faster reads
python mec16xx_flash.py read-flash 0x0000 0x48000 dump.bin --burst
```

> **Note:** Boot-protected regions cannot be read if boot protection is active.

```bash
# Dump full EEPROM
python mec16xx_flash.py read-eeprom eeprom.bin
```

## Disclaimer

Use at your own risk. I am not responsible for any damage to hardware or data loss. Always back up existing firmware before making any changes.


## Credits

- [Glasgow Interface Explorer - mec16xx applet](https://github.com/GlasgowEmbedded/glasgow/blob/main/software/glasgow/applet/program/mec16xx/__init__.py)
- [dossalab/mec16xx-simple-flash](https://github.com/dossalab/mec16xx-simple-flash)
- Built with assistance from [Claude](https://claude.ai) (Anthropic)
