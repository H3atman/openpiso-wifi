"""Shared E2E Device identities: the MACs the harness's Devices, Vendo boards and browsers use."""


DEVICE_MAC = "02:00:00:00:0C:01"
CHANGED_DEVICE_MAC = "02:00:00:00:0C:02"
UNKNOWN_VENDO_MAC = "02:00:00:00:0C:03"
COIN_BOARD_MAC = "02:00:00:00:0C:04"
BROWSER_MACS = tuple(f"02:00:00:00:0E:{number:02X}" for number in range(1, 5))
BOARD_MACS = tuple(f"02:00:00:41:00:{number:02X}" for number in range(1, 4))
