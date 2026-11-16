#include "ring.h"

namespace sv {

bool Ring::blank(uint32_t sector, uint32_t slot) {
  uint8_t b[sizeof(Rec)];
  flash_.read(sector * kSectorSize + slot * sizeof(Rec), b, sizeof b);
  for (uint8_t v : b)
    if (v != 0xFF) return false;
  return true;
}

bool Ring::scan(Rec& newest) {
  bool found = false;
  uint32_t bs = 0, bi = 0;
  bool anyData = false;
  for (uint32_t s = 0; s < sectors_; s++) {
    for (uint32_t i = 0; i < kSlotsPerSector; i++) {
      Rec r;
      flash_.read(s * kSectorSize + i * sizeof(Rec), &r, sizeof r);
      if (recValid(r)) {
        if (!found || (int32_t)(r.seq - newest.seq) > 0) {
          newest = r;
          bs = s;
          bi = i;
          found = true;
        }
        anyData = true;
      } else if (!blank(s, i)) {
        anyData = true;
      }
    }
  }
  needErase_ = false;
  if (!found) {
    // Blank flash starts at slot 0; junk with no valid record is erased first.
    sector_ = 0;
    slot_ = 0;
    needErase_ = anyData;
    return false;
  }
  sector_ = bs;
  slot_ = bi + 1;
  while (slot_ < kSlotsPerSector && !blank(sector_, slot_)) slot_++;
  if (slot_ >= kSlotsPerSector) {
    sector_ = (sector_ + 1) % sectors_;
    slot_ = 0;
    needErase_ = true;
  }
  return true;
}

void Ring::append(const Rec& r) {
  if (needErase_) {
    flash_.eraseSector(sector_);
    erases_++;
    needErase_ = false;
  }
  flash_.program(sector_ * kSectorSize + slot_ * sizeof(Rec), &r, sizeof r);
  if (++slot_ >= kSlotsPerSector) {
    sector_ = (sector_ + 1) % sectors_;
    slot_ = 0;
    needErase_ = true;
  }
}

}  // namespace sv
