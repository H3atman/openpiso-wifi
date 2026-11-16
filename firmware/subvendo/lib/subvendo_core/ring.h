// CRC-checked record ring over >= 2 flash sectors. Records are appended; when
// the active sector is full the NEXT sector is erased (it never holds the
// newest record), so an erase can never destroy the only valid record.
#pragma once
#include "wire.h"

namespace sv {

constexpr uint32_t kSlotsPerSector = kSectorSize / sizeof(Rec);

class Ring {
 public:
  Ring(Flash& f, uint32_t sectors) : flash_(f), sectors_(sectors < 2 ? 2 : sectors) {}

  // Scans every slot. Returns true and fills `newest` if any record is valid.
  // Also positions the write head after the newest record, skipping slots that
  // are not blank (torn writes).
  bool scan(Rec& newest);

  // Writes the next record; erases the next sector first when needed.
  void append(const Rec& r);

  uint32_t erases() const { return erases_; }

 private:
  bool blank(uint32_t sector, uint32_t slot);
  Flash& flash_;
  uint32_t sectors_;
  uint32_t sector_ = 0, slot_ = 0;
  bool needErase_ = false;  // head sector must be erased before its next write
  uint32_t erases_ = 0;
};

}  // namespace sv
