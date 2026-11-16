// Pin and Ethernet-chip configuration for the bench sketches. Every value is overridable with -D.
//
// Pins the Ethernet chip and the console take, so tests must not use them:
//   GPIO14 SCK, GPIO12 MISO, GPIO13 MOSI (hardware SPI), ETH_CS (KiTifi: GPIO0),
//   GPIO1 / GPIO3 UART0 (serial console), GPIO6..11 (SPI flash).
// GPIO2 (COIN) and GPIO15 (SET) are boot-strapping pins: leave them at their boot levels.
#pragma once

#if defined(BENCH_BOARD_JUANFI)
// JuanFi LAN UI defaults: coin 2, set 15, button 3, LEDs disabled. W5500.
// The Ethernet CS pin is not documented for JuanFi: 0 is a guess, check the board.
#define BOARD_NAME "juanfi"
#ifndef COIN_PIN
#define COIN_PIN 2
#endif
#ifndef SET_PIN
#define SET_PIN 15
#endif
#ifndef ETH_CS
#define ETH_CS 0
#endif
#if !defined(ETH_W5100) && !defined(ETH_W5500)
#define ETH_W5500
#endif
#else
// KiTifi ESP8266 LAN (board-behavior-esp8266-lan.md): coin GPIO2, SET GPIO15, W5x00 CS GPIO0.
#define BOARD_NAME "kitifi"
#ifndef COIN_PIN
#define COIN_PIN 2
#endif
#ifndef SET_PIN
#define SET_PIN 15
#endif
#ifndef ETH_CS
#define ETH_CS 0
#endif
#if !defined(ETH_W5100) && !defined(ETH_W5500)
#define ETH_W5100
#endif
#endif

// The loopback generator output. GPIO5 is free on the known layouts; GPIO4 and GPIO16 are the
// alternatives. (GPIO16 cannot be used by the deep sleep step of rtc_retention.)
#ifndef GEN_PIN
#define GEN_PIN 5
#endif

constexpr bool benchPinFree(int p) {
  return p >= 0 && p <= 16 && !(p >= 6 && p <= 11) && p != 1 && p != 3 && p != 12 && p != 13 &&
         p != 14 && p != 15 && p != COIN_PIN && p != SET_PIN && p != ETH_CS;
}
static_assert(benchPinFree(GEN_PIN), "GEN_PIN collides with SPI, UART, flash, COIN, SET or ETH_CS");
static_assert(COIN_PIN != ETH_CS && COIN_PIN != SET_PIN, "COIN, SET and ETH_CS must differ");
