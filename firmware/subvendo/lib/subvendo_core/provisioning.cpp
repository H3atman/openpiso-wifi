#include "provisioning.h"
#include "wire.h"
#include <string.h>

namespace sv {

ConfigStatus readProvisioning(ConfigStorage& storage, Provisioning& out) {
  out = {};
  Provisioning record{};
  if (!storage.read(&record, sizeof record)) return ConfigStatus::kReadError;
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&record);
  bool blank = true;
  for (size_t i = 0; i < sizeof record; i++) blank = blank && bytes[i] == 0xFF;
  if (blank) return ConfigStatus::kMissing;
  if (record.magic != kConfigMagic || record.crc != crc32(&record, sizeof record - 4))
    return ConfigStatus::kCorrupt;
  if (!record.vendoId || !record.controllerUrl[0] || !record.hostname[0] ||
      !memchr(record.controllerUrl, 0, sizeof record.controllerUrl) ||
      !memchr(record.hostname, 0, sizeof record.hostname)) return ConfigStatus::kCorrupt;
  out = record;
  return ConfigStatus::kValid;
}

void vendoHostname(const uint8_t mac[6], char out[10]) {
  static const char hex[] = "0123456789ABCDEF";
  memcpy(out, "opnw-", 5);
  for (size_t i = 0; i < 2; i++) {
    out[5 + i * 2] = hex[mac[4 + i] >> 4];
    out[6 + i * 2] = hex[mac[4 + i] & 15];
  }
  out[9] = 0;
}

}  // namespace sv
