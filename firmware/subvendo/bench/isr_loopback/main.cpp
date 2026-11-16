// Open question 5: does anything the board does (Ethernet, flash writes, WiFi) make the coin ISR
// miss pulses or run late?
//
// Wiring: jumper GEN_PIN -> COIN_PIN (default GPIO5 -> GPIO2). Disconnect any real acceptor first.
// timer1 drives GEN_PIN with CH-926-like trains (idle high, low pulses, counted on the RISING edge
// like the KiTifi firmware). An IRAM ISR on COIN_PIN counts edges and stamps them with the CPU cycle
// counter. Latency = cycle count in the ISR minus the cycle count taken right after the generator's
// GPIO write (the timer ISR cannot be preempted by the coin ISR, so the stamp is always in place).
// Pulse width jitter does not matter: every edge is stamped when it actually happens.
//
// Loads, in this order: idle, flash (erase+program loop), wifi (needs credentials), eth (UDP sink;
// start bench/flood.py when the SINK line appears). Each load runs every width in kWidthsMs.
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <SPI.h>
#include <algorithm>
#include "bench.h"
#if defined(ETH_W5500)
#include <W5500lwIP.h>
Wiznet5500lwIP eth(ETH_CS);
#else
#include <W5100lwIP.h>
Wiznet5100lwIP eth(ETH_CS);
#endif

#ifndef CASE_COINS
#define CASE_COINS 24
#endif
#ifndef PULSES_PER_COIN
#define PULSES_PER_COIN 5
#endif
#ifndef COIN_GAP_MS
#define COIN_GAP_MS 400  // between coins, max 1600 (timer1 range)
#endif
#ifndef PULSE_GAP_MS
#define PULSE_GAP_MS 0  // between pulses of one coin; 0 = same as the pulse width
#endif
#ifndef SINK_PORT
#define SINK_PORT 9000
#endif
#ifndef FLOOD_WAIT_MS
#define FLOOD_WAIT_MS 15000
#endif
#ifndef LOAD_FLASH_BASE
#define LOAD_FLASH_BASE 0x310000  // one scratch sector for the flash load, away from flash_ring's 0x300000
#endif
static_assert(GEN_PIN <= 15, "the generator uses GPOS/GPOC, GPIO0..15 only");

namespace {

constexpr uint32_t kWidthsMs[] = {20, 50, 100};
constexpr uint32_t kTicksPerMs = 5000;  // timer1 at 80 MHz / 16
constexpr uint32_t kMaxSamples = 2048;
static_assert(CASE_COINS * PULSES_PER_COIN <= kMaxSamples, "raise kMaxSamples");

// Shared with the ISRs (DRAM only: nothing here may live in flash while a flash op runs).
volatile uint32_t genStamp, genEdges, isrCount;
volatile bool genDone, phaseLow;
uint32_t coinTicks, widthTicks, gapTicks, pulseInCoin, coinsDone;
uint32_t latCycles[kMaxSamples];

inline __attribute__((always_inline)) uint32_t ccount() {
  uint32_t v;
  asm volatile("rsr %0, ccount" : "=a"(v));
  return v;
}

void IRAM_ATTR coinIsr() {
  uint32_t now = ccount();
  if (isrCount < kMaxSamples) latCycles[isrCount] = now - genStamp;
  isrCount = isrCount + 1;
}

// One timer1 event per edge: fall (start a pulse), then rise (end it, stamped).
void IRAM_ATTR genIsr() {
  if (!phaseLow) {
    GPOC = 1 << GEN_PIN;
    phaseLow = true;
    timer1_write(widthTicks);
    return;
  }
  GPOS = 1 << GEN_PIN;
  genStamp = ccount();
  genEdges = genEdges + 1;
  phaseLow = false;
  if (++pulseInCoin < PULSES_PER_COIN) return timer1_write(gapTicks);
  pulseInCoin = 0;
  if (++coinsDone >= CASE_COINS) {
    genDone = true;
    return;
  }
  timer1_write(coinTicks);
}

void startGen(uint32_t widthMs) {
  widthTicks = widthMs * kTicksPerMs;
  gapTicks = (PULSE_GAP_MS ? PULSE_GAP_MS : widthMs) * kTicksPerMs;
  coinTicks = COIN_GAP_MS * kTicksPerMs;
  pulseInCoin = coinsDone = 0;
  genEdges = isrCount = 0;
  phaseLow = genDone = false;
  timer1_attachInterrupt(genIsr);
  timer1_enable(TIM_DIV16, TIM_EDGE, TIM_SINGLE);
  timer1_write(5 * kTicksPerMs);
}

// ---- loads ----
WiFiUDP udp;
uint32_t sinkPkts, sinkBytes, flashOps, flashStep;

void drainSink() {
  uint8_t buf[256];
  while (int n = udp.parsePacket()) {
    sinkPkts++;
    sinkBytes += n;
    udp.read(buf, sizeof buf);  // the rest of the datagram is dropped on the next parsePacket
  }
}

void flashWork() {
  alignas(4) static uint32_t page[64];
  if (flashStep % 17 == 0)
    spi_flash_erase_sector(LOAD_FLASH_BASE / 4096);
  else
    spi_flash_write(LOAD_FLASH_BASE + (flashStep % 17 - 1) * 256, page, sizeof page);
  flashStep++;
  flashOps++;
}

bool startWifi() {
#ifdef WIFI_SSID
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 200 && WiFi.status() != WL_CONNECTED; i++) delay(100);
  if (WiFi.status() != WL_CONNECTED) return false;
  udp.begin(SINK_PORT);
  Serial.printf("SINK load=wifi ip=%s port=%u\n", WiFi.localIP().toString().c_str(), SINK_PORT);
  return true;
#else
  return false;  // no credentials in the build
#endif
}
void stopWifi() {
  udp.stop();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
}

bool startEth() {
  eth.setDefault();
  if (!eth.begin()) return false;  // chip not found
  udp.begin(SINK_PORT);
  return true;
}
bool ethUp() {
  if (!eth.connected() || !eth.localIP().isSet()) return false;
  Serial.printf("SINK load=eth ip=%s port=%u\n", eth.localIP().toString().c_str(), SINK_PORT);
  return true;
}

struct Load {
  const char* name;
  bool (*start)();   // false = skip this load
  bool (*ready)();   // polled from loop() until true (DHCP), up to 30 s; nullptr = ready
  void (*work)();    // one short slice per loop()
  void (*stop)();
  bool flood;        // wait FLOOD_WAIT_MS for the host flood before the pulses
};
const Load kLoads[] = {
    {"idle", [] { return true; }, nullptr, [] {}, [] {}, false},
    {"flash", [] { return ESP.getFlashChipRealSize() >= LOAD_FLASH_BASE + 4096; }, nullptr, flashWork, [] {}, false},
    {"wifi", startWifi, nullptr, drainSink, stopWifi, true},
    {"eth", startEth, ethUp, drainSink, [] { udp.stop(); }, true},
};
constexpr size_t kLoadsN = sizeof kLoads / sizeof kLoads[0];

// ---- case runner ----
enum State { kStartLoad, kReady, kFlood, kStartCase, kRun, kSettle, kDone } state = kStartLoad;
size_t loadIdx, widthIdx;
uint32_t stateSince, totalMissed, totalExtra, worstUs, worstP99, cases;

void to(State s) {
  state = s;
  stateSince = millis();
}

void nextLoad() {
  if (kLoads[loadIdx].stop) kLoads[loadIdx].stop();
  widthIdx = 0;
  to(++loadIdx < kLoadsN ? kStartLoad : kDone);
}

void report() {
  const Load& l = kLoads[loadIdx];
  uint32_t mhz = ESP.getCpuFreqMHz(), sent = genEdges, got = isrCount, n = std::min(got, kMaxSamples);
  std::sort(latCycles, latCycles + n);
  uint32_t us10[3] = {0, 0, 0};  // min, max, p99 in tenths of a microsecond
  uint64_t sum = 0;
  for (uint32_t i = 0; i < n; i++) sum += latCycles[i];
  if (n) {
    us10[0] = latCycles[0] * 10 / mhz;
    us10[1] = latCycles[n - 1] * 10 / mhz;
    us10[2] = latCycles[std::min(n - 1, n * 99 / 100)] * 10 / mhz;
  }
  uint32_t avg10 = n ? (uint32_t)(sum * 10 / n / mhz) : 0;
  uint32_t missed = sent > got ? sent - got : 0, extra = got > sent ? got - sent : 0;
  totalMissed += missed;
  totalExtra += extra;
  worstUs = std::max(worstUs, us10[1]);
  worstP99 = std::max(worstP99, us10[2]);
  cases++;
  benchResult("isr_loopback",
              "load=%s width_ms=%u ppc=%d sent=%u got=%u missed=%u extra=%u lat_min_us=%u.%u lat_avg_us=%u.%u "
              "lat_max_us=%u.%u lat_p99_us=%u.%u load_ops=%u",
              l.name, kWidthsMs[widthIdx], PULSES_PER_COIN, sent, got, missed, extra, us10[0] / 10, us10[0] % 10,
              avg10 / 10, avg10 % 10, us10[1] / 10, us10[1] % 10, us10[2] / 10, us10[2] % 10,
              loadIdx == 1 ? flashOps : sinkPkts);
}

}  // namespace

void setup() {
  benchBoot("isr_loopback");
  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);
  pinMode(GEN_PIN, OUTPUT);
  digitalWrite(GEN_PIN, HIGH);
  pinMode(COIN_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(COIN_PIN), coinIsr, RISING);
  Serial.printf("wire GPIO%d -> GPIO%d; %d coins x %d pulses per case, coin gap %d ms\n", GEN_PIN, COIN_PIN,
                CASE_COINS, PULSES_PER_COIN, COIN_GAP_MS);
}

void loop() {
  const Load& l = kLoads[loadIdx < kLoadsN ? loadIdx : 0];
  switch (state) {
    case kStartLoad:
      sinkPkts = sinkBytes = flashOps = 0;
      if (l.start()) return to(kReady);
      Serial.printf("SKIP load=%s (not available: no credentials, or no Ethernet chip)\n", l.name);
      return nextLoad();
    case kReady:
      if (!l.ready || l.ready()) return to(l.flood ? kFlood : kStartCase);
      if (millis() - stateSince > 30000) {
        Serial.printf("SKIP load=%s (no link or DHCP within 30 s)\n", l.name);
        return nextLoad();
      }
      return;
    case kFlood:  // keep draining while the host starts the flood
      l.work();
      if (millis() - stateSince > FLOOD_WAIT_MS) to(kStartCase);
      return;
    case kStartCase:
      startGen(kWidthsMs[widthIdx]);
      return to(kRun);
    case kRun:
      l.work();
      if (genDone) to(kSettle);
      return;
    case kSettle:  // let the last ISR and any queued work finish
      l.work();
      if (millis() - stateSince < 300) return;
      timer1_disable();
      report();
      if (++widthIdx < sizeof kWidthsMs / sizeof kWidthsMs[0]) return to(kStartCase);
      return nextLoad();
    case kDone:
      benchResult("isr_loopback", "load=ALL cases=%u missed_total=%u extra_total=%u lat_worst_max_us=%u.%u "
                  "lat_worst_p99_us=%u.%u", cases, totalMissed, totalExtra, worstUs / 10, worstUs % 10,
                  worstP99 / 10, worstP99 % 10);
      Serial.println("DONE (reset to run again)");
      while (true) delay(1000);
  }
}
