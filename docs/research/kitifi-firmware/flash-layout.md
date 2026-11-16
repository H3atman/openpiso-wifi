# Flash layout & artifacts — KiTifi vendo board firmware

All four `.bin` files in the gitignored `kitifi-subvendo-bins/` directory at the repo root (not committed), mapped. Derived by direct parsing of the
ESP image headers and partition tables (scripts: `esp2elf.py`, `carve.py` in the
analysis toolset; carved outputs under `rea-findings/artifacts/`).

## Identification

| File | What it is | Target MCU |
|---|---|---|
| `KiTifi-ESP8266-LAN.bin` | bare app image (0xE9 magic) | ESP8266 |
| `KiTifi-ESP8266-Wireless.bin` | bare app image (0xE9 magic) | ESP8266 |
| `KiTifi-ESP32-LAN.bin` | full flash dump (bootloader+ptable+app0+blank NVS) | ESP32 |
| `KiTifi-LAN8720.bin` | full flash dump (wired Ethernet build) | ESP32 |

## ESP32 full-flash layouts (identical partition table @ flash 0x8000)

| Partition | Offset | Size | Notes |
|---|---|---|---|
| (bootloader) | 0x1000 | 28 KB | entry 0x400805f0 |
| nvs | 0x9000 | 20 KB | **factory blank (all 0xFF)** — config at first boot |
| otadata | 0xe000 | 8 KB | blank → boots app0 |
| app0 | 0x10000 | 1280 KB | 5-segment app image; only ~830–886 KB used |
| app1 | 0x150000 | 1280 KB | **empty** (no OTA second slot in use) |
| spiffs | 0x290000 | 1408 KB | empty |
| coredump | 0x3f0000 | 64 KB | empty |

App images: `KiTifi-ESP32-LAN` entry `0x40082be0`, segments: DROM 0x3f400020
(0x1e8d8), DRAM 0x3ffbdb60, IROM/flash 0x400d0020 (0x94c6c — the ~608 KB of app
code), DRAM 0x3ffbf278, IRAM 0x40080000 (0x12698). `KiTifi-LAN8720` entry
`0x40082d3c`, slightly larger IROM (0x9d4bc) — the Ethernet stack adds ~35 KB.

Build fingerprints (esp_app_desc @ app+0x20): ESP32-LAN = IDF 4.4.5 / Arduino core
2.0.11, built **Jun 12 2023 16:41:45**; LAN8720 = IDF 4.4.4 / core 2.0.9, built
**Apr 20 2023 00:53:55**. Both project `arduino-lib-builder`.

## ESP8266 app images

Header: 2 memory segments (IRAM 0x4010f000 0xd60 B; DRAM 0x3fff20b8 0x28 B) +
checksum 0x2b at 0xdb5, 0xAA pad to 0x1000, then the **IROM section** (the bulk:
0x4e7cc B @ file 0x1010 mapped at 0x40201010 for LAN; 0x5543c for Wireless).
Entry 0x4010f480. IROM size difference: Wireless carries ~27 KB more code.

## Cross-references

- Server-side contract for every endpoint above: `../research/server-contract.md`
  and `kitifi-controller-handoff.md` (vendo/subvendo section).
- Ghidra decompiled functions: `../evidence/ghidra/<board>/funcs/`
- String tables with xrefs: `../evidence/ghidra/<board>/strings.tsv`
