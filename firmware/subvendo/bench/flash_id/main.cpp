// Open question 7: which SPI flash part is on the module? Prints the JEDEC ID, the real size and a
// vendor/part decode. Cross-check from the host: .venv/bin/esptool.py flash_id (see bench/README.md).
#include "bench.h"

namespace {

struct Vendor {
  uint8_t id;
  const char* name;
};
const Vendor kVendors[] = {
    {0xEF, "Winbond"}, {0xC8, "GigaDevice"}, {0x20, "XMC"},   {0x68, "Boya"},       {0x85, "Puya"},
    {0x0B, "XTX"},     {0xC2, "Macronix"},   {0x1C, "EON"},   {0x5E, "Zbit"},       {0xA1, "Fudan"},
    {0x9D, "ISSI"},    {0xBF, "SST"},        {0x25, "Dosilicon"}, {0x70, "Elite/ESMT-class (unverified)"},
};

// Known parts by 24-bit ID (manufacturer | type << 8 | capacity << 16). Not exhaustive.
struct Part {
  uint32_t id;
  const char* name;
};
const Part kParts[] = {
    {0x1640EF, "W25Q32 (FV or JV-IQ/JQ)"}, {0x1670EF, "W25Q32JV-IM/JM"}, {0x1540EF, "W25Q16"},
    {0x1440EF, "W25Q80"},                  {0x1740EF, "W25Q64"},          {0x1640C8, "GD25Q32"},
    {0x1540C8, "GD25Q16"},                 {0x1440C8, "GD25Q80"},         {0x165020, "XM25QH32"},
    {0x155020, "XM25QH16"},                {0x166085, "P25Q32H"},         {0x156085, "P25Q16H"},
    {0x164068, "BY25Q32"},                 {0x1540A1, "FM25Q16"},
};

const char* vendorName(uint8_t id) {
  for (const Vendor& v : kVendors)
    if (v.id == id) return v.name;
  return "unknown";
}

const char* partName(uint32_t id) {
  for (const Part& p : kParts)
    if (p.id == id) return p.name;
  return "unknown";
}

}  // namespace

void setup() {
  benchBoot("flash_id");
  uint32_t id = ESP.getFlashChipId();  // 0x00CCTTMM: capacity, type, manufacturer
  uint8_t mfr = id & 0xFF, type = (id >> 8) & 0xFF, cap = (id >> 16) & 0xFF;
  uint32_t real = ESP.getFlashChipRealSize();
  Serial.printf("jedec=0x%06x manufacturer=0x%02x (%s) type=0x%02x capacity=0x%02x\n", id, mfr, vendorName(mfr),
                type, cap);
  Serial.printf("part=%s\n", partName(id));
  Serial.printf("real_size=%u configured_size=%u mode=%u speed_hz=%u\n", real, ESP.getFlashChipSize(),
                (unsigned)ESP.getFlashChipMode(), ESP.getFlashChipSpeed());
  // The ID cannot separate the W25Q32FV from the W25Q32JV (same 0xEF4016). Both are specified for
  // 100K erase cycles per sector, which is what the wear projection assumes.
  bool w25q32 = (id & 0xFFFFFF) == 0x1640EF || (id & 0xFFFFFF) == 0x1670EF;
  benchResult("flash_id", "jedec=0x%06x vendor=%s part=%s real_bytes=%u w25q32_family=%s", id, vendorName(mfr),
              partName(id), real, w25q32 ? "YES" : "NO");
}

void loop() {}
