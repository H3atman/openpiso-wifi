/* Keep provisioning.h's physical boundaries tied to the actual linked layout.
 * Arduino maps flash at 0x40200000. Config exclusively owns _EEPROM_start;
 * the next sector is RF calibration, followed by three SDK sectors.
 */
INCLUDE "eagle.flash.4m1m.ld"
ASSERT(_FS_start == 0x40500000, "config guard: sketch/OTA boundary changed")
ASSERT(_FS_end == 0x405FA000, "config guard: filesystem boundary changed")
ASSERT(_EEPROM_start == 0x405FB000, "config guard: EEPROM reservation changed")
