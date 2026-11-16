// Flash-time credentials are read through the same public seam as the board.
#include <cstdio>
#include <cstring>
#include "provisioning.h"

namespace sim {
namespace {
struct ConfigImage : sv::ConfigStorage {
  uint8_t bytes[312] = {};
  bool readable = true;
  bool read(void* dst, size_t len) override {
    if (!readable) return false;
    memcpy(dst, bytes, len);
    return true;
  }
};
}

bool provisioningScenarios() {
  // Arduino 3.1.2 OTA ends at 0x300000; FS updates end at 0x3FA000.
  // Credentials must remain below RF calibration at 0x3FC000.
  bool layout = sv::kConfigAddress >= 0x3FA000 && sv::kConfigAddress + 4096 <= 0x3FC000;
  printf("config_update_safe %s\n", layout ? "ok" : "FAIL");
  if (!layout) return false;
  ConfigImage image;
  // Independent image fixture: little-endian fields, IEEE CRC from Python zlib.
  const char* hex =
      "4f43503107000000000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f687474703a2f2f63"
      "6f6e74726f6c6c65723a3830383000000000000000000000000000000000000000000000000000000000000000000000"
      "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
      "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
      "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
      "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
      "00000000000000006f706e772d304330310000003057f922";
  for (size_t i = 0; i < sizeof image.bytes; i++) {
    unsigned v;
    if (sscanf(hex + 2 * i, "%2x", &v) != 1) return false;
    image.bytes[i] = uint8_t(v);
  }
  sv::Provisioning config{};
  bool ok = sv::readProvisioning(image, config) == sv::ConfigStatus::kValid &&
            config.vendoId == 7 && !strcmp(config.controllerUrl, "http://controller:8080") &&
            !strcmp(config.hostname, "opnw-0C01");
  for (unsigned i = 0; i < 32; i++) ok = ok && config.key[i] == i;
  printf("config_valid %s\n", ok ? "ok" : "FAIL");
  const uint8_t mac[6] = {0xA0, 0x20, 0xA6, 0x11, 0x0C, 0x01};
  char hostname[10];
  sv::vendoHostname(mac, hostname);
  bool named = !strcmp(hostname, "opnw-0C01");
  printf("hostname %s\n", named ? "ok" : "FAIL");
  ok = ok && named;
  image.bytes[8] ^= 1;
  bool corrupt = sv::readProvisioning(image, config) == sv::ConfigStatus::kCorrupt && !config.vendoId;
  printf("config_corrupt %s\n", corrupt ? "ok" : "FAIL");
  ok = ok && corrupt;
  memset(image.bytes, 0xFF, sizeof image.bytes);
  bool missing = sv::readProvisioning(image, config) == sv::ConfigStatus::kMissing && !config.vendoId;
  printf("config_missing %s\n", missing ? "ok" : "FAIL");
  ok = ok && missing;
  image.readable = false;
  bool unreadable = sv::readProvisioning(image, config) == sv::ConfigStatus::kReadError && !config.vendoId;
  printf("config_read_error %s\n", unreadable ? "ok" : "FAIL");
  return ok && unreadable;
}
}  // namespace sim
