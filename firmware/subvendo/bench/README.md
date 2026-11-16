# Subvendo bench procedure

Answers the hardware open questions of `docs/research/esp8266-lan-juanfi-qr-parity-and-controller-design.md` (Q3 to Q5) and bench items B4 to B7 of `../TEST-PLAN.md`. Run everything from `firmware/subvendo/`. Commands work in bash and fish.

Pinned toolchain: PlatformIO 6.2.0 in `.venv`, platform `espressif8266@4.2.1`, `framework-arduinoespressif8266 @ 3.30102.0` (Arduino core 3.1.2), xtensa GCC 10.3.0. `esptool` 5.6.0 is also in the venv for backup and restore.

## What to buy or have

- A 3.3 V USB-serial adapter (CP2102, CH340 or FT232) if the board has no USB. 3.3 V logic only; 5 V on an ESP8266 pin kills it.
- Dupont jumper wires (female-female or whatever matches the headers): 3 for serial (TX, RX, GND), 1 for the GPIO0 flash-mode strap, 1 for the loopback, 1 spare for `RST` to GND.
- The board's own 5 V supply. Do not power a LAN board from the adapter's 3.3 V: the W5100/W5500 plus the module can pull more than the adapter gives, and resets show up as bogus results.
- For the power-cut soak: a way to cut power quickly and repeatedly, such as a switched power strip or unplugging the barrel jack. Keep the serial adapter powered separately so the log survives (common GND only).
- No CH-926, scope or logic analyzer is needed.

You need access to these board signals: `GPIO1` (TX), `GPIO3` (RX), `GND`, `GPIO0`, `RST`, `GPIO2` (COIN terminal) and one of `GPIO5` / `GPIO4` (a module pad is fine). Find the programming header or solder to the module pads.

## Serial access

`/dev/ttyUSB*` belongs to group `uucp` on Arch/CachyOS and you are not in it. Either:

```sh
sudo usermod -aG uucp $USER   # then log out and back in; check with: groups
```

or a udev rule (no re-login needed after `sudo udevadm control --reload && sudo udevadm trigger`):

```sh
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4|1a86|0403", MODE="0660", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/99-usb-serial.rules
```

## BACKUP FIRST, before any flashing

**Flashing erases the vendor firmware, including its license and config (`config.json` / `system.data` on the FS, the admin token, the paired Controller address). Without a backup you cannot get the board back to how you got it.**

Enter flash mode: tie `GPIO0` to GND, power-cycle (or tap `RST`), leave the strap on while esptool runs. `GPIO15` must be low and `GPIO2` high at boot (the board already does this). On a KiTifi LAN board `GPIO0` is also the Ethernet chip-select; that is harmless in flash mode. Remove the strap and reset to run normally.

```sh
mkdir -p bench/out
PORT=/dev/ttyUSB0   # fish: set PORT /dev/ttyUSB0 ; use $PORT in both
.venv/bin/esptool --port $PORT --baud 460800 flash-id
.venv/bin/esptool --port $PORT --baud 460800 read-flash 0 ALL bench/out/backup-<board>-$(date +%F)-a.bin
.venv/bin/esptool --port $PORT --baud 460800 read-flash 0 ALL bench/out/backup-<board>-$(date +%F)-b.bin
sha256sum bench/out/backup-<board>-*-a.bin bench/out/backup-<board>-*-b.bin
```

Use `<board>` = `kitifi` or `juanfi`. The two hashes MUST match. If they differ, or the sizes are not 4194304 bytes, re-read (try `--baud 115200`) and do not flash until they agree. Copy the backup out of `bench/out/` (it is git-ignored): to another disk. Keep one copy.

Restore the original firmware exactly:

```sh
.venv/bin/esptool --port $PORT --baud 460800 write-flash --flash-mode keep --flash-freq keep --flash-size keep 0 bench/out/backup-<board>-<date>-a.bin
```

Then verify with `.venv/bin/esptool --port $PORT verify-flash 0 bench/out/backup-<board>-<date>-a.bin`.

## Build, flash, capture

Each test is one env. `pio run` builds, `-t upload` flashes (GPIO0 strapped, then reset), and capture logs it. `bench/capture.py <test>` writes `bench/out/<test>-<UTC>.log` and appends every `RESULT ...` line to `bench/out/results.md` (commit that file as the artifact; logs stay local).

```sh
.venv/bin/pio run -e <env> -t upload --upload-port $PORT
# remove the GPIO0 strap, tap RST, then:
.venv/bin/python bench/capture.py <test> --port $PORT
```

Pins are build flags (see `common/bench_pins.h`). Defaults are the KiTifi preset: COIN GPIO2, SET GPIO15, W5100 with CS GPIO0. For JuanFi LAN use `PLATFORMIO_BUILD_FLAGS="-DBENCH_BOARD_JUANFI"` (W5500; its CS pin is not documented, default 0 is a guess). Override one value with `-DETH_CS=15`, `-DETH_W5500`, `-DETH_W5100`, `-DGEN_PIN=4`, `-DCOIN_PIN=..`. A `static_assert` refuses a generator pin that collides with SPI, UART, flash, COIN, SET or CS.

The sketch holds SET low, so an attached acceptor stays inhibited.

### Pins to avoid in every test

| GPIO | Why |
|---|---|
| 12, 13, 14 | HSPI to the Ethernet chip (MISO, MOSI, SCK) |
| 0 | Ethernet CS (KiTifi), flash-mode strap |
| 1, 3 | UART0, the console |
| 2, 15 | COIN and SET (boot-strap pins: 2 high, 15 low at boot) |
| 6 to 11 | the SPI flash |
| 16 | free, but it is the deep-sleep wake line for `rtc_retention` |

Free for the generator: GPIO5 (default) or GPIO4.

### 1. `flash_id` (open question 7)

Wiring: serial only.

```sh
.venv/bin/pio run -e flash_id -t upload --upload-port $PORT
.venv/bin/python bench/capture.py flash_id --port $PORT
```

Cross-check with the host (flash-mode strap on): `.venv/bin/esptool --port $PORT flash-id` prints manufacturer, device and detected size; `jedec=` here must agree (`0x1640EF` = manufacturer `EF`, type `40`, capacity `16`). `.venv/bin/esptool --port $PORT read-flash-sfdp 0 4` may help tell W25Q32FV from JV; untested.

Expected: `RESULT flash_id jedec=0x1640ef vendor=Winbond part=W25Q32 (FV or JV-IQ/JQ) real_bytes=4194304 w25q32_family=YES`.

Design impact: `w25q32_family=NO` or `real_bytes` below 4194304 invalidates the wear projection (I6, F11) and the datasheet figures for page program and sector erase time used for hold-up (open question 6); re-derive them from that part's datasheet. With a smaller flash the ring tests refuse to run (the region is beyond the chip); set `-DRING_BASE`.

### 2. `rtc_retention` (open question 4, bench item B4)

Wiring: serial only. For the optional deep sleep step also wire GPIO16 to RST and uncomment the `RTC_TEST_DEEPSLEEP` flag in `platformio.ini`.

```sh
.venv/bin/pio run -e rtc_retention -t upload --upload-port $PORT
.venv/bin/python bench/capture.py rtc_retention --port $PORT
```

The sketch walks six steps, resuming through every reset (the step is journaled via an explicit EEPROM instance at scratch sector `0x310000`, so it continues even when RTC is lost): `restart`, `reset`, `swwdt`, `hwwdt` run by themselves after 3 s; `ext_rst` waits for you to press RST; `power` waits for you to cut and restore board power (adapter stays up, or capture.py reopens the port). Type `n` to skip a step, `r` to start over.

Expected (this is the point of the test, no answer is assumed): per-step lines `STEPRESULT step=hwwdt rtc=KEPT|LOST reason=1 reason_expected=1 ok` and a final `RESULT rtc_retention restart=.. reset=.. swwdt=.. hwwdt=.. ext_rst=.. power=..`. A `MISMATCH` means the reset you caused was not the reset the chip reported: redo that step. `power` must be LOST; if it is KEPT the supply was not really cut.

Design impact: the sim has `warm_rtc_kept` and `warm_rtc_lost`. If `restart`, `reset` and both WDTs are KEPT, the RTC fast path of D9/D10 is real and checkpoint-in-RTC is worth keeping. If any is LOST, that reset type must be treated as flash-only restore (the RTC copy cannot be relied on), and the cost is the flash ring, not RTC, absorbing every reset. `ext_rst` and `power` results tell whether RTC helps after a brownout or button reset at all.

### 3. `flash_ring` (open questions 6 and 7, bench items B6, B7)

Wiring: serial only, plus a way to cut power. Uses the real `lib/subvendo_core/ring.{h,cpp}` on real flash at 0x300000 to 0x301FFF (the start of the 1 MB FS area of the `4m1m` layout; the sketch has no FS, and is far below). The sector at 0x310000 is used by the `isr_loopback` flash load. If the vendor FS lives there it is already gone after the backup-and-flash step.

Timing run (once):

```sh
.venv/bin/pio run -e flash_ring -t upload --upload-port $PORT
.venv/bin/python bench/capture.py flash_ring --port $PORT --send t
```

Expected: `TIMING erase ...`, `TIMING program32 ...`, `TIMING program256 ...` with min/avg/max in microseconds, and `RESULT flash_ring_timing ...`. Datasheet limits for the W25Q32JV: sector erase up to 400 ms, page program up to 3 ms. Real values are usually far lower. Values above the limits mean a different or failing flash part.

Power-cut soak:

```sh
.venv/bin/python bench/capture.py flash_ring --port $PORT --send w      # first run wipes the region
# ... cut board power at random instants, 30+ times, wait a second, restore power ...
# later runs: the same command without --send to keep the history
```

The sketch checkpoints an incrementing counter in a tight loop (flash write first, then `V <n> seq=.. vcc=..` on serial). Each boot prints one `RESULT flash_ring_soak found=.. restored=.. seq=.. boot=.. valid=.. torn=.. blank=.. last_vcc_mv=.. min_vcc_mv=..`; `capture.py` adds `last_printed` and `printed_minus_restored` (last counter printed before the cut, minus the restored one). `torn` counts slots with partial data that the CRC rejected. The soak runs the flash flat out and wears the two ring sectors (rated about 100K erases each), so do not leave it running unattended. Add `-DSOAK_DELAY_MS=10` to slow it.

Expected: `printed_minus_restored` is never above 0 and `restored` is never below an earlier boot's value; `torn` is above 0 in some boots (that is the proof the cut hit a write and the CRC caught it).

Design impact: any `printed_minus_restored > 0` means a write completed from the sketch's view but was not durable: checkpoint-before-report (D9) does not hold on this board without more hold-up, so the board needs a supply supervisor or bulk capacitor. A restored value that goes backwards means a torn erase or program damaged a valid record: the ring needs a second copy.

Hold-up time cannot be measured without a scope. Estimate only: with `ADC_MODE(ADC_VCC)` each record stores `ESP.getVcc()` in mV (uncalibrated, +-10 %), and `min_vcc_mv` is the lowest supply at which a program still finished. The `V ... vcc=` lines in the log, timestamped by the host, show the supply sag in the last tens of ms before the cut. This works only if the module's ADC pin (TOUT) is floating; if the board wires it, build with `-DUSE_VCC=0`. Treat any hold-up number derived from this as an estimate.

### 4. `isr_loopback` (open question 5, bench item B5)

Wiring: one jumper from **GPIO5 (generator) to GPIO2 (COIN)**. Disconnect the real acceptor from the COIN terminal first. GPIO2 must be high at boot; the jumper is safe because GPIO5 is an input until the sketch starts. Keep clear of the pins in the table above.

```sh
.venv/bin/pio run -e isr_loopback -t upload --upload-port $PORT
.venv/bin/python bench/capture.py isr_loopback --port $PORT
```

The sketch runs 24 coins of 5 pulses, with 20, 50 and 100 ms pulse widths, under four loads in order: `idle`, `flash` (continuous erase and program of one sector), `wifi`, `eth`. Pulse gap defaults to the width, coin gap to 400 ms (open question 2 is unresolved; change with `-DPULSE_GAP_MS=..`, `-DCOIN_GAP_MS=..`, `-DPULSES_PER_COIN=..`).

- `wifi` runs only if you create `bench/wifi.local.ini` from `bench/wifi.local.example` (git-ignored; never commit it). Without it the sketch prints `SKIP load=wifi`.
- `eth` needs the LAN cable. When it prints `SINK load=eth ip=<ip> port=9000` it waits 15 s: start the flood from another terminal: `python3 bench/flood.py <ip> --pps 2000 --size 512 --seconds 120`. The same works for the WiFi load.

Expected: one line per case: `RESULT isr_loopback load=idle width_ms=20 ppc=5 sent=120 got=120 missed=0 extra=0 lat_min_us=.. lat_avg_us=.. lat_max_us=.. lat_p99_us=.. load_ops=..`, then `load=ALL` with `missed_total` and the worst latencies. `load_ops` is flash operations (flash load) or UDP packets the board received (compare with flood.py's `sent`); if it is 0 the load did not happen. The timer1 ISR stamps its own edge, so the numbers include a small constant floor (the tail of the timer ISR).

Design impact: `missed_total > 0` under any load means GPIO-interrupt counting is not safe next to that activity: counting needs hardware support (not an ISR) or that activity must be blocked while a Coin Window is open. `extra > 0` is a double count (the debounce in the core is 3 ms and does not see it here). If `lat_max_us` under WiFi is above about 3000 (3 ms, the core's `debounceMs`) or above half the shortest pulse (10 ms), the debounce and quiet values in D13 need review. Flat low numbers under all loads confirm they hold.

### 5. `provisioning` / `provisioning_w5500` (#43, ADR 0005)

**Hardware execution is Rafael's job. No board was attached during implementation. DHCP option 12, real flash/MAC access and first-contact adoption are NOT yet verified on hardware. Do not treat a successful build or HTTP 503 from the offline test as a bench pass.**

This is a first-contact probe, not production coin firmware: it holds SET low, reads credentials, obtains one DHCP lease and sends **one** signed 57-byte Report, then prints `DONE`. It never writes flash, counts coins, or opens the slot. There is no production sketch yet to wire into; that remains #21.

#### Config layout and flash reservation

`lib/subvendo_core/provisioning.h` owns the format. Reserve physical flash **`0x3FB000..0x3FBFFF`** (the 4096-byte `_EEPROM_start` sector) on the **4 MB / `eagle.flash.4m1m.ld`** layout. Arduino core 3.1.2 stages sketch OTA below FS start (`0x300000`), and FS updates end at `0x3FA000`; neither writes this sector. RF calibration starts at `0x3FC000`, followed by SDK sectors. The bench reader refuses a chip whose detected size is not 4 MB; the script only accepts firmware fitting the linked sketch region.

Do not use the default Arduino `EEPROM` instance: it owns this same sector. The only existing EEPROM user, the destructive `rtc_retention` bench, now explicitly journals in scratch sector `0x310000` (also borrowed by the ISR load bench). The ring bench borrows `0x300000..0x30FFFF`; these scratch regions are inside FS and are not production storage. Do not change flash size, linker layout or these reservations without revisiting the boundaries. Boards provisioned at the old OTA-unsafe address `0x2FF000` must be unpaired and re-provisioned; there is no fallback read of the old sector.

`provisioning.h` rejects unsafe addresses at compile time in both the firmware and host sim. `config.guard.ld` wraps the SDK linker script and asserts that its OTA, FS and EEPROM boundaries still match the header. This is an exclusive reservation of the EEPROM sector, not permission to share it with Arduino EEPROM. The host sees the same constants, so no runtime fallback is needed.

Build-only red/green checks (from `firmware/subvendo`; none access a board):

```sh
# Red: old config address is inside OTA staging; compilation must fail.
PLATFORMIO_BUILD_FLAGS='-DBENCH_BOARD_KITIFI -Ibench/common -Ilib/subvendo_core -DSV_CONFIG_ADDRESS=0x2FF000' .venv/bin/pio run -e provisioning
make -B -C sim run CXX='g++ -DSV_CONFIG_ADDRESS=0x2FF000'
# Green: default reserved address; firmware builds and all sim scenarios pass.
.venv/bin/pio run -e provisioning
make -B -C sim run
```

`-B` forces the sim to rebuild after changing flags. `SV_CONFIG_ADDRESS` is a build-test override only; `flash.py` always provisions `0x3FB000`.

The image is 4096 bytes, with a 312-byte little-endian record followed by `0xFF` padding:

| Byte offset | Field |
|---|---|
| 0 | `uint32` magic `0x3150434F` (bytes `OCP1`, format version 1) |
| 4 | `uint32` Vendo ID, nonzero |
| 8 | 32 raw secret bytes (decode the endpoint's hex, not its ASCII text) |
| 40 | Controller URL, 256-byte NUL-terminated ASCII buffer, zero padded |
| 296 | Hostname, 12-byte NUL-terminated buffer, zero padded (`opnw-XXXX`) |
| 308 | IEEE reflected CRC-32 over bytes 0..307, same as Python `zlib.crc32` |

`ConfigStorage` is the read-only HAL seam. The reader distinguishes valid, erased/missing, corrupt and read-error records, and clears its output on failure. A failed read means a clear `RESULT provisioning_config error=... no_reports=YES`, no DHCP and no Report. CRC detects damage, not hostile modification; the flash holds the HMAC key in plaintext, as required for Report signing.

#### Build and prepare (no hardware access)

1. Build the correct chip variant; the default KiTifi preset uses W5100/CS GPIO0. JuanFi's CS is still unconfirmed: inspect the board and override `ETH_CS` if needed. `provisioning_w5500` selects W5500 but does not guess a different CS.

   ```sh
   .venv/bin/pio run -e core_target -e provisioning -e provisioning_w5500
   ```

2. Have the Controller running on a trusted management network, with its portal URL reachable by an unpaid HotSpot host. The management origin may differ from the returned `controller_url`. Basic auth sends `admin` plus the password; use HTTPS management with a system-trusted certificate where available, or a physically trusted HTTP management link. No redirects or proxies are followed. The current bench transport supports **HTTP board URLs only**, up to 255 ASCII bytes, without query/fragment/credentials; HTTPS board traffic needs a trust-anchor design and is deliberately refused, not sent with TLS verification disabled.

3. Optional no-flash provisioning, with a supplied MAC. This calls the real endpoint and reserves a Vendo, but never invokes esptool or opens a port. It reports only ID, MAC, hostname, URL and CRC; no image is saved.

   ```sh
   .venv/bin/python -I flash.py --controller http://CONTROLLER-MANAGEMENT:8080 \
     --mac A0:20:A6:11:0C:01 --firmware .pio/build/provisioning/firmware.bin \
     --no-flash
   ```

   The password is prompted without echo; automation may supply `OPENPISO_ADMIN_PASSWORD` in the environment (not a CLI argument). The secret-bearing config image never touches disk in either mode. Ordinary flashing passes firmware and config as bytes to esptool 5.6.0's Python `write_flash` API; tool output and errors are suppressed so credentials cannot leak through diagnostics. No-flash mode discards the credentials on exit, so unpair this test reservation before real provisioning.

#### Rafael's hardware procedure

1. **Back up the vendor firmware twice and compare hashes using the BACKUP FIRST procedure above. Copy the matching backup to another disk. Do not continue without it.** Disconnect the coin acceptor or keep it inhibited. Choose the correct chip/pins and build as above. Real flashing is destructive and is never an agent verification step.
2. Configure the Router/HotSpot so an unpaid board can reach `controller_url`. Preserve the board's source IP all the way to the Controller: no source NAT or HTTP reverse proxy on that path. For CHR, plug the board through a USB-Ethernet adapter bridged to `virbr-hs`; for another readable DHCP server, hostname observation works but first-contact adoption still requires a configured RouterOS HotSpot. Confirm the Controller's configured Router is reachable and the board MAC is not already reserved. Use a fresh board lease so an old DHCP hostname does not mask the test.
3. Strap GPIO0 for flash mode and reset. Run the one provisioning-and-flashing command (substitute the W5500 firmware path if appropriate). This reads the chip MAC with esptool `read-mac`, calls `POST /admin/vendos/pair` with form `mac`, then writes firmware at `0x0` and config at `0x3FB000`.

   ```sh
   .venv/bin/python -I flash.py --controller http://CONTROLLER-MANAGEMENT:8080 \
     --port $PORT --firmware .pio/build/provisioning/firmware.bin
   ```

   Do not reuse the optional no-flash MAC reservation in this command: a second pair request is **409**, not secret rotation. Unpair in admin first, then run this command. A failed/lost provisioning response or failed flash likewise requires admin unpair before re-provisioning. `provisioned` unpair needs no Router.
4. Remove the GPIO0 strap. Start capture, then reset the board so capture sees the whole boot (it exits on `DONE`).

   ```sh
   .venv/bin/python bench/capture.py provisioning --port $PORT
   ```

   Expect `CONFIG vendo_id=... mac=... hostname=opnw-0C01`, then `RESULT provisioning_dhcp ip=... mask=... gateway=... hostname_requested=opnw-0C01`, and `RESULT provisioning_report ... bytes=57`. No config or key bytes should appear. The Ethernet MAC must match the printed chip MAC exactly: this sketch explicitly supplies it because the stock library otherwise derives a different MAC.
5. Read the Router lease **itself**, not just the board's requested hostname. On RouterOS run `/ip/dhcp-server/lease/print detail where mac-address="A0:20:A6:11:0C:01"`. Save the MAC, address and exact `host-name` in the bench artifact. It should be `opnw-0C01` (uppercase suffix). The pinned lwIP library has no pre-allocation hostname setter: the adapter suppresses automatic DHCP with a temporary TEST-NET static address, clears that address, sets the interface hostname, then starts the first DHCP allocation. Physical option-12 delivery is still the question this test answers. If the lease has no hostname, record that result explicitly; the hostname is only a label, never pairing authority.
6. In admin confirm the same Vendo changes from **waiting for first contact** to **paired**, with the lease IP, correct HotSpot server and MAC; no admin pairing action after plugging in. On RouterOS also inspect the static DHCP lease and `/ip/hotspot/ip-binding/print detail where comment="openpiso-vendo-ID"` for the MAC/IP bypass. Save those observations. A Report's HTTP 200 is useful but not proof on its own; Router cleanup during adoption may close the TCP response. A 502 may leave `pending`: fix Router reachability and reset this probe to send another signed Report from the pinned IP (do not call pair again). A 503 means no Router client; a 401 means bad HMAC or MAC/IP host mismatch. Rejection must not count as a bench pass.
7. `capture.py` appends the serial `RESULT` rows to **`bench/out/results.md`**. Add a dated manual row there with board/chip, MAC, exact observed Router `host-name` (or explicitly absent), lease IP, Vendo ID, adopted HotSpot server/status and the lease/bypass check. Keep the raw serial log locally. Commit the non-secret results only after running the hardware check. No result is fabricated in this change.

## Offline checks (no board)

From `firmware/subvendo/`, run serially:

```sh
make -C sim run               # config/hostname cases plus all existing fault scenarios
.venv/bin/pio run -e core_target -e provisioning -e provisioning_w5500
# core_target compiles the core under -Werror; the other two cover both Ethernet chips
make -C ../../controller build
.venv/bin/python -I bench/provisioning_e2e.py --binary ../../controller/bin/openpiso
```

The last check starts/stops a local native Controller with a fresh temporary ledger, no Router or CHR. It imports the builder to inspect the CLI config in memory, checks CRC/fields and absent secret logging, verifies the decoded key via signed/forged Reports (503/401), and verifies duplicate provisioning is 409. An audit guard rejects all file writes, tool launches and device-node opens during no-flash provisioning; a scan also checks for secret bytes/hex outside the Controller ledger (which must retain its own key). The repeatable non-secret artifact is `bench/out/provisioning-e2e.json`; no credential image is created, and the temporary Controller ledger is deleted after the test. This artifact is not the hardware `results.md`.
