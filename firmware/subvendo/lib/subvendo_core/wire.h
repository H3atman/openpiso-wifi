// Canonical byte layouts: the checkpoint record and the HMAC input of each
// message (TEST-PLAN.md D3). A Go Controller must produce the same bytes.
#pragma once
#include "hal.h"

namespace sv {

constexpr uint8_t kTagReport = 0x01, kTagOpen = 0x02, kTagAck = 0x03;
constexpr size_t kReportMsgLen = 25, kOpenMsgLen = 21, kAckMsgLen = 21;

// Big-endian HMAC inputs. Each returns the length written to `out`.
size_t reportMsg(const Report& r, uint8_t* out);
size_t openMsg(const Open& o, uint8_t* out);
size_t ackMsg(const Ack& a, uint8_t* out);

// Checkpoint record, 32 bytes, stored in a flash slot and in RTC (8 blocks).
// `seq` is a global write counter; `tag` is the window tag of the latest
// burst. `spare` is unused (zero) and `reserved` is free for the bench to
// annotate; neither is covered by any logic, both are covered by the CRC.
struct Rec {
  uint32_t magic, seq, epoch, total, tag, spare, reserved, crc;
};
static_assert(sizeof(Rec) == 32, "record layout");
constexpr uint32_t kRecMagic = 0x53564431;  // "SVD1"

uint32_t crc32(const void* data, size_t len);
void sealRec(Rec& r);          // sets magic and crc
bool recValid(const Rec& r);   // magic and crc match

}  // namespace sv
