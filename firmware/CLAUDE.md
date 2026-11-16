# Firmware pointers

- Physical-board guardrail: [never flash or touch a physical board without Rafael; back up the vendor firmware first](subvendo/bench/README.md#backup-first-before-any-flashing).
- For toolchain setup: root [`make venv`](../Makefile) delegates to [`firmware/subvendo/Makefile`'s `venv` target](subvendo/Makefile).
- Before firmware changes: [test plan](subvendo/TEST-PLAN.md).
- For bench work: [bench README](subvendo/bench/README.md).
- Before flash-layout changes: [config layout and flash reservation](subvendo/bench/README.md#config-layout-and-flash-reservation) and [vendor flash layout notes](../docs/research/kitifi-firmware/flash-layout.md).
