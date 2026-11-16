// Open question 4: does ESP8266 RTC user memory survive each kind of reset?
//
// Steps run in order. Each one writes a record (magic, step, nonce, CRC) to RTC user memory at
// block offset 32 (blocks 0..31 belong to eboot), journals the step in EEPROM (flash, survives
// everything), then causes the reset. On the next boot the record is checked against the journaled
// nonce: a match means RTC kept its data. Auto steps fire after 3 s; manual steps wait for you.
// Console commands at any time: n = skip this step, r = restart the whole sequence.
#include <EEPROM.h>
#include "bench.h"

namespace {

// Keep the default EEPROM sector reserved for provisioning credentials.
// This destructive bench borrows the ISR load scratch sector instead.
EEPROMClass journal(0x310000 / SPI_FLASH_SEC_SIZE);

constexpr uint32_t kRtcOffset = 32;  // block offset, must be >= 32
constexpr uint32_t kRtcMagic = 0x52544331, kJournalMagic = 0x4A524E31;

struct RtcRec {
  uint32_t magic, step, nonce, crc;
};

// Flash journal: survives every reset, so the sequence resumes even when RTC is lost.
struct Journal {
  uint32_t magic, nonce;
  uint8_t step, armed;
  uint8_t result[8];  // 0 pending, 1 KEPT, 2 LOST, 3 SKIPPED
  uint8_t reason[8];  // rst_info.reason seen on the boot after the step
};

enum Kind : uint8_t { kAuto, kManual };
struct Step {
  const char* name;
  Kind kind;
  int expectReason;  // -1 = not asserted
  const char* how;
};
const Step kSteps[] = {
    {"restart", kAuto, 4, "ESP.restart()"},
    {"reset", kAuto, -1, "ESP.reset()"},
    {"swwdt", kAuto, 3, "while(1){} until the soft WDT bites (~3 s)"},
    {"hwwdt", kAuto, 1, "interrupts off + spin until the hardware WDT bites (~6-8 s)"},
    {"ext_rst", kManual, 6, "PRESS the board's RST/EN button now"},
    {"power", kManual, 0, "UNPLUG power, wait 5 s, plug it back in"},
#ifdef RTC_TEST_DEEPSLEEP
    {"deepsleep", kAuto, 5, "ESP.deepSleep(2 s); needs GPIO16 wired to RST"},
#endif
};
constexpr uint8_t kSteps_n = sizeof kSteps / sizeof kSteps[0];
static_assert(kSteps_n <= 8, "Journal arrays hold 8 steps");
const char* kResultName[] = {"PENDING", "KEPT", "LOST", "SKIPPED"};

Journal j;

uint32_t recCrc(const RtcRec& r) {
  uint32_t h = 2166136261u;  // FNV-1a over the first three words
  const uint8_t* p = reinterpret_cast<const uint8_t*>(&r);
  for (size_t i = 0; i < 12; i++) h = (h ^ p[i]) * 16777619u;
  return h;
}

void saveJournal() {
  journal.put(0, j);
  journal.commit();
}

void resetJournal() {
  j = Journal{};
  j.magic = kJournalMagic;
  saveJournal();
}

// True if RTC holds exactly the record written before the last reset.
bool rtcKept() {
  RtcRec r;
  ESP.rtcUserMemoryRead(kRtcOffset, reinterpret_cast<uint32_t*>(&r), sizeof r);
  return r.magic == kRtcMagic && r.crc == recCrc(r) && r.step == j.step && r.nonce == j.nonce;
}

void finish() {
  char buf[200];
  int n = 0;
  for (uint8_t i = 0; i < kSteps_n; i++)
    n += snprintf(buf + n, sizeof buf - n, "%s=%s ", kSteps[i].name, kResultName[j.result[i]]);
  Serial.println("SEQUENCE DONE (r = run again)");
  benchResult("rtc_retention", "%s", buf);
}

// Writes the RTC record and journal, then returns; the caller triggers the reset.
void arm() {
  const Step& s = kSteps[j.step];
  j.nonce = micros() ^ (j.nonce * 2654435761u) ^ 0x5A5A0000;
  j.armed = 1;
  RtcRec r{kRtcMagic, j.step, j.nonce, 0};
  r.crc = recCrc(r);
  ESP.rtcUserMemoryWrite(kRtcOffset, reinterpret_cast<uint32_t*>(&r), sizeof r);
  saveJournal();
  Serial.printf("STEP %u/%u %s: %s\n", j.step + 1, kSteps_n, s.name, s.how);
}

void trigger() {
  switch (j.step) {
    case 0: ESP.restart(); break;
    case 1: ESP.reset(); break;
    case 2: while (1) {} break;
    case 3: ESP.wdtDisable(); noInterrupts(); while (1) {} break;
#ifdef RTC_TEST_DEEPSLEEP
    case 6: ESP.deepSleep(2000000); break;
#endif
    default: break;
  }
}

// Evaluates the step that was armed before this boot.
void evaluate() {
  const Step& s = kSteps[j.step];
  uint8_t reason = ESP.getResetInfoPtr()->reason;
  bool kept = rtcKept();
  j.result[j.step] = kept ? 1 : 2;
  j.reason[j.step] = reason;
  j.armed = 0;
  Serial.printf("STEPRESULT step=%s rtc=%s reason=%u reason_expected=%d %s\n", s.name, kResultName[j.result[j.step]],
                reason, s.expectReason, s.expectReason < 0 ? "n/a" : (reason == s.expectReason ? "ok" : "MISMATCH"));
  Serial.println(ESP.getResetInfo());
  j.step++;
  saveJournal();
}

}  // namespace

void setup() {
  benchBoot("rtc_retention");
  journal.begin(sizeof(Journal));
  journal.get(0, j);
  if (j.magic != kJournalMagic) {
    Serial.println("journal blank: starting a fresh sequence");
    resetJournal();
  } else if (j.armed) {
    evaluate();
  }
  if (j.step >= kSteps_n) return finish();
  arm();
}

void loop() {
  if (j.step >= kSteps_n) {
    if (Serial.available() && Serial.read() == 'r') {
      resetJournal();
      ESP.restart();
    }
    return;
  }
  uint32_t t0 = millis();
  while (kSteps[j.step].kind == kManual || millis() - t0 < 3000) {
    if (!Serial.available()) {
      delay(5);
      continue;
    }
    char c = Serial.read();
    if (c == 'n') {
      j.result[j.step++] = 3;
      j.armed = 0;
      saveJournal();
      if (j.step >= kSteps_n) return finish();
      return arm();
    }
    if (c == 'r') {
      resetJournal();
      ESP.restart();
    }
  }
  trigger();
}
