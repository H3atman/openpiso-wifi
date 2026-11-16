// Open questions 6 and 7: flash timing and power-cut behaviour of the real checkpoint ring
// (lib/subvendo_core/ring.{h,cpp}) on real SPI flash.
//
// Region: RING_BASE (default flash 0x300000, the start of the FS area of the 4m1m layout), RING_SECTORS
// sectors. The sketch uses no FS and sits far below, so nothing else lives there.
//
// Boot: scan the ring and report what survived. Then a 2 s window for a command:
//   t = measure erase / program times        w = wipe the region, then soak
//   (nothing) = soak: checkpoint an incrementing counter as fast as possible; pull power at random.
// Each checkpoint is written to flash first and printed after, so a printed value that is higher
// than the restored one means a lost durable write. bench/capture.py computes that gap.
//
// Vcc estimate: each record stores ESP.getVcc() (mV, uncalibrated, +-10%) in `reserved`. The lowest
// value among valid records is the lowest supply at which a program still completed.
#include <string.h>
#include "bench.h"
#include "ring.h"

#ifndef RING_BASE
#define RING_BASE 0x300000
#endif
#ifndef RING_SECTORS
#define RING_SECTORS 2
#endif
#ifndef SOAK_DELAY_MS
#define SOAK_DELAY_MS 0  // slow the soak down to spare the flash
#endif
#ifndef USE_VCC
#define USE_VCC 1  // 0 if the board wires the ADC pin (TOUT) to something
#endif
#if USE_VCC
ADC_MODE(ADC_VCC);
#endif

namespace {

// sv::Flash on the real chip. Addresses are relative to RING_BASE. Times every op in microseconds.
struct RealFlash : sv::Flash {
  Stat erase, prog32, prog256;
  uint32_t errors = 0;

  void read(uint32_t a, void* dst, size_t n) override {
    alignas(4) uint32_t buf[sv::kPageSize / 4];
    if (spi_flash_read(RING_BASE + a, buf, (n + 3) & ~3u) != SPI_FLASH_RESULT_OK) errors++;
    memcpy(dst, buf, n);
  }
  void eraseSector(uint32_t s) override {
    uint32_t t0 = micros();
    if (spi_flash_erase_sector(RING_BASE / sv::kSectorSize + s) != SPI_FLASH_RESULT_OK) errors++;
    erase.add(micros() - t0);
  }
  void program(uint32_t a, const void* src, size_t n) override {
    alignas(4) uint32_t buf[sv::kPageSize / 4];
    memcpy(buf, src, n);
    uint32_t t0 = micros();
    if (spi_flash_write(RING_BASE + a, buf, (n + 3) & ~3u) != SPI_FLASH_RESULT_OK) errors++;
    (n >= 256 ? prog256 : prog32).add(micros() - t0);
  }
};

RealFlash flash;
sv::Ring ring(flash, RING_SECTORS);
sv::Rec rec;  // the last record written (or restored)

uint32_t vcc() {
#if USE_VCC
  return ESP.getVcc();
#else
  return 0;
#endif
}

void printStat(const char* name, const Stat& s) {
  Serial.printf("TIMING %s n=%u min_us=%u avg_us=%u max_us=%u\n", name, s.n, s.min, s.avg(), s.max);
}

// Measures sector erase and 256 B / 32 B program times (the real slot size is 32 B).
void timing() {
  uint8_t page[sv::kPageSize];
  memset(page, 0xA5, sizeof page);
  for (int i = 0; i < 40; i++) flash.eraseSector(i % RING_SECTORS);
  for (int r = 0; r < 4; r++) {
    flash.eraseSector(0);
    for (uint32_t p = 0; p < sv::kSectorSize / sv::kPageSize; p++) flash.program(p * sv::kPageSize, page, 256);
    flash.eraseSector(0);
    for (uint32_t s = 0; s < sv::kSlotsPerSector; s++) flash.program(s * 32, page, 32);
  }
  for (uint32_t s = 0; s < RING_SECTORS; s++) flash.eraseSector(s);  // leave the region blank
  printStat("erase", flash.erase);
  printStat("program32", flash.prog32);
  printStat("program256", flash.prog256);
  benchResult("flash_ring_timing",
              "erase_n=%u erase_min_us=%u erase_avg_us=%u erase_max_us=%u prog32_min_us=%u prog32_avg_us=%u "
              "prog32_max_us=%u prog256_min_us=%u prog256_avg_us=%u prog256_max_us=%u flash_errors=%u",
              flash.erase.n, flash.erase.min, flash.erase.avg(), flash.erase.max, flash.prog32.min,
              flash.prog32.avg(), flash.prog32.max, flash.prog256.min, flash.prog256.avg(), flash.prog256.max,
              flash.errors);
}

// Restores from the ring and reports valid / torn / blank slots. `boots` goes in rec.tag.
void restore() {
  uint32_t valid = 0, torn = 0, blank = 0, maxBoot = 0, minVcc = 0xFFFFFFFF;
  for (uint32_t s = 0; s < RING_SECTORS * sv::kSlotsPerSector; s++) {
    sv::Rec r;
    flash.read(s * sizeof r, &r, sizeof r);
    if (sv::recValid(r)) {
      valid++;
      if (r.tag > maxBoot) maxBoot = r.tag;
      if (r.reserved && r.reserved < minVcc) minVcc = r.reserved;
    } else {
      const uint8_t* p = reinterpret_cast<const uint8_t*>(&r);
      bool allFF = true;
      for (size_t i = 0; i < sizeof r; i++) allFF &= p[i] == 0xFF;
      (allFF ? blank : torn)++;
    }
  }
  sv::Rec newest;
  bool found = ring.scan(newest);
  rec = found ? newest : sv::Rec{};
  rec.epoch = 1;
  rec.tag = maxBoot + 1;  // this boot's number
  benchResult("flash_ring_soak",
              "found=%d restored=%u seq=%u boot=%u valid=%u torn=%u blank=%u last_vcc_mv=%u min_vcc_mv=%u "
              "base=0x%x sectors=%u flash_errors=%u",
              found, found ? newest.total : 0, found ? newest.seq : 0, rec.tag, valid, torn, blank,
              found ? newest.reserved : 0, valid ? minVcc : 0, (unsigned)RING_BASE, (unsigned)RING_SECTORS,
              flash.errors);
}

}  // namespace

void setup() {
  benchBoot("flash_ring");
  Serial.printf("region base=0x%x sectors=%u slots_per_sector=%u use_vcc=%d\n", (unsigned)RING_BASE,
                (unsigned)RING_SECTORS, (unsigned)sv::kSlotsPerSector, USE_VCC);
  if (ESP.getFlashChipRealSize() < RING_BASE + RING_SECTORS * sv::kSectorSize) {
    benchResult("flash_ring_soak", "error=region_beyond_flash real_bytes=%u base=0x%x", ESP.getFlashChipRealSize(),
                (unsigned)RING_BASE);
    while (true) delay(1000);
  }
  char c = benchPrompt("t=timing w=wipe+soak, else soak", 2000);
  if (c == 't') {
    timing();
    Serial.println("timing done; reset the board to soak");
    while (true) delay(1000);
  }
  if (c == 'w')
    for (uint32_t s = 0; s < RING_SECTORS; s++) flash.eraseSector(s);
  restore();
  Serial.println("SOAK start: V <counter> seq=<seq> vcc=<mV>");
}

// One checkpoint per iteration: flash first, serial after.
void loop() {
  static uint32_t lastPrint = 0;
  rec.seq++;
  rec.total++;
  rec.reserved = vcc();
  sv::sealRec(rec);
  ring.append(rec);
  if (millis() - lastPrint >= 20 && Serial.availableForWrite() >= 40) {
    lastPrint = millis();
    Serial.printf("V %u seq=%u vcc=%u\n", rec.total, rec.seq, rec.reserved);
  }
  if (SOAK_DELAY_MS) delay(SOAK_DELAY_MS);
}
