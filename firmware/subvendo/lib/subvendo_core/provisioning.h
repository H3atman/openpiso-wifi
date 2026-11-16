// Flash-time credentials. This record is separate from the checkpoint ring.
#pragma once
#include "hal.h"

namespace sv {

// Dedicated EEPROM sector of eagle.flash.4m1m.ld, outside OTA and FS updates.
// Override only for build-time layout checks; flash.py provisions the real address.
#ifndef SV_CONFIG_ADDRESS
#define SV_CONFIG_ADDRESS 0x3FB000
#endif
constexpr uint32_t kConfigAddress = SV_CONFIG_ADDRESS;
// Physical boundaries from Arduino 3.1.2's 4m1m linker script. config.guard.ld
// checks the linked boundaries too, so changing the layout cannot bypass these.
static_assert(kConfigAddress % kSectorSize == 0, "config must be sector aligned");
static_assert(kConfigAddress >= 0x300000, "config overlaps sketch/OTA staging");
static_assert(kConfigAddress >= 0x3FA000, "config overlaps filesystem");
// #43 assigns the default EEPROM sector exclusively to config. No Arduino
// EEPROM instance may use it; rtc_retention journals at 0x310000 instead.
static_assert(kConfigAddress == 0x3FB000, "config must own the reserved EEPROM sector");
static_assert(uint64_t(kConfigAddress) + kSectorSize <= 0x3FC000,
              "config overlaps RF calibration/SDK reserved sectors");
constexpr uint32_t kConfigMagic = 0x3150434F;  // bytes "OCP1", little-endian
struct Provisioning {
  uint32_t magic, vendoId;
  uint8_t key[kKeyLen];
  char controllerUrl[256], hostname[12];
  uint32_t crc;
};
static_assert(sizeof(Provisioning) == 312, "provisioning layout");

struct ConfigStorage {
  virtual bool read(void* dst, size_t len) = 0;
};
enum class ConfigStatus { kValid, kMissing, kCorrupt, kReadError };
// Leaves out zeroed on failure. The caller must not report without kValid.
ConfigStatus readProvisioning(ConfigStorage& storage, Provisioning& out);

// Nine characters plus NUL, derived only from the last two MAC bytes.
void vendoHostname(const uint8_t mac[6], char out[10]);

}  // namespace sv
