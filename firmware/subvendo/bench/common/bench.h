// Shared boot banner and output helpers. Output is line oriented so bench/capture.py can parse it:
//   BOOT <test> ...            once per boot, with the reset cause
//   <anything else>            free text
//   RESULT <test> k=v k=v ...  one machine-parsable line per conclusion
#pragma once
#include <Arduino.h>
#include <stdarg.h>
#include <user_interface.h>  // rst_info, and the uint32 types spi_flash.h needs
extern "C" {
#include <spi_flash.h>
}
#include "bench_pins.h"

// Min/avg/max over unsigned samples (microseconds).
struct Stat {
  uint32_t n = 0, min = 0xFFFFFFFF, max = 0;
  uint64_t sum = 0;
  void add(uint32_t v) {
    n++;
    sum += v;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  uint32_t avg() const { return n ? (uint32_t)(sum / n) : 0; }
};

// Starts the console, holds SET low (acceptor inhibited), prints the BOOT line.
inline void benchBoot(const char* test) {
  Serial.begin(115200);
  pinMode(SET_PIN, OUTPUT);
  digitalWrite(SET_PIN, LOW);
  delay(200);
  Serial.printf("\nBOOT %s board=%s coin=%d set=%d gen=%d eth_cs=%d cpu_mhz=%u reset=\"%s\" reason=%u\n", test,
                BOARD_NAME, COIN_PIN, SET_PIN, GEN_PIN, ETH_CS, ESP.getCpuFreqMHz(),
                ESP.getResetReason().c_str(), (unsigned)ESP.getResetInfoPtr()->reason);
}

// printf-style RESULT line, e.g. benchResult("flash_id", "id=0x%06x", id).
inline void benchResult(const char* test, const char* fmt, ...) {
  char buf[256];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  Serial.printf("RESULT %s %s\n", test, buf);
}

// Waits up to `ms` for one command character on the console, 0 if none.
// Prints "CMD? <help>" first so capture.py --send can answer it.
inline char benchPrompt(const char* help, uint32_t ms) {
  while (Serial.available()) Serial.read();
  Serial.printf("CMD? %s (waiting %u ms)\n", help, (unsigned)ms);
  for (uint32_t t0 = millis(); millis() - t0 < ms;) {
    if (Serial.available()) return (char)Serial.read();
    delay(5);
  }
  return 0;
}
