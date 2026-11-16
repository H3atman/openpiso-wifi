# Subvendo simulation report

Seeds: 1..500 per scenario (500 seeds; the `wear` scenario uses one). Output is a pure function of the seeds. Every scenario runs under both checkpoint policies.

Core config: debounce 3 ms, quiet time 130 ms, window gap 300 ms, heartbeat 2000 ms, resend 1000 ms, reachable if acked within 6000 ms, open must echo one of the last 3 reports, ring 2 sectors.

## Checkpoint policy comparison

`per-burst` (default) saves once when a burst ends; `per-pulse` saves after every counted pulse, in the main loop. Years = worst sector reaching 100000 erase cycles (W25Q32JV figure, bench Q7) at 300 coins/day with a 2-sector ring. Short runs erase 0 or 1 sectors, so only the `wear` row is a usable projection (`-` = no erase happened).

| scenario | policy | lost F4 pulses | lost per losing cut | records per coin | erases per burst | worst sector erases | years @300 coins/day |
|---|---|---|---|---|---|---|---|
| baseline | per-burst | 0 | - | 1.03 | 0.0000 | 0 | - |
| baseline | per-pulse | 0 | - | 4.42 | 0.0254 | 1 | 36 |
| lossy | per-burst | 0 | - | 1.03 | 0.0000 | 0 | - |
| lossy | per-pulse | 0 | - | 4.46 | 0.0261 | 1 | 35 |
| ack_loss | per-burst | 0 | - | 1.03 | 0.0000 | 0 | - |
| ack_loss | per-pulse | 0 | - | 4.57 | 0.0264 | 1 | 35 |
| reorder | per-burst | 0 | - | 1.03 | 0.0000 | 0 | - |
| reorder | per-pulse | 0 | - | 4.20 | 0.0244 | 1 | 37 |
| dup | per-burst | 0 | - | 1.03 | 0.0000 | 0 | - |
| dup | per-pulse | 0 | - | 4.23 | 0.0241 | 1 | 38 |
| ctl_down | per-burst | 0 | - | 1.03 | 0.0000 | 0 | - |
| ctl_down | per-pulse | 0 | - | 4.45 | 0.0195 | 1 | 47 |
| power_cut | per-burst | 8548 | 4.42 | 1.01 | 0.0000 | 0 | - |
| power_cut | per-pulse | 1768 | 1.02 | 3.85 | 0.0223 | 2 | 49 |
| warm_rtc_kept | per-burst | 9148 | 4.38 | 1.01 | 0.0000 | 0 | - |
| warm_rtc_kept | per-pulse | 1817 | 1.02 | 3.94 | 0.0233 | 2 | 47 |
| warm_rtc_lost | per-burst | 8548 | 4.42 | 1.01 | 0.0000 | 0 | - |
| warm_rtc_lost | per-pulse | 1768 | 1.02 | 3.85 | 0.0223 | 2 | 49 |
| erase_cut | per-burst | 20354 | 4.58 | 0.98 | 0.0698 | 5 | 22 |
| erase_cut | per-pulse | 4757 | 1.02 | 2.98 | 0.0537 | 3 | 30 |
| restore_fail | per-burst | 678 | 3.96 | 1.12 | 0.1260 | 9 | 7 |
| restore_fail | per-pulse | 2 | 1.00 | 4.45 | 0.1264 | 8 | 7 |
| restore_rtc_only | per-burst | 2283 | 4.15 | 1.00 | 0.0000 | 0 | - |
| restore_rtc_only | per-pulse | 17 | 1.06 | 4.35 | 0.0260 | 2 | 36 |
| restore_fail_wipe | per-burst | 0 | - | 1.11 | 0.0000 | 0 | - |
| restore_fail_wipe | per-pulse | 0 | - | 4.47 | 0.0000 | 0 | - |
| expiry_inflight | per-burst | 0 | - | 1.03 | 0.0000 | 0 | - |
| expiry_inflight | per-pulse | 0 | - | 6.94 | 0.0403 | 1 | 23 |
| handover_inflight | per-burst | 0 | - | 0.96 | 0.0000 | 0 | - |
| handover_inflight | per-pulse | 0 | - | 7.81 | 0.0461 | 1 | 21 |
| glitch | per-burst | 0 | - | 1.67 | 0.0000 | 0 | - |
| glitch | per-pulse | 0 | - | 5.20 | 0.0162 | 1 | 35 |
| forged_replay | per-burst | 4266 | 4.48 | 1.01 | 0.0000 | 0 | - |
| forged_replay | per-pulse | 640 | 1.03 | 4.18 | 0.0223 | 2 | 44 |
| wrap | per-burst | 1375 | 3.92 | 0.98 | 0.0000 | 0 | - |
| wrap | per-pulse | 16 | 1.33 | 4.26 | 0.0246 | 2 | 38 |
| stale_open | per-burst | 0 | - | 1.16 | 0.0000 | 0 | - |
| stale_open | per-pulse | 0 | - | 4.65 | 0.0000 | 0 | - |
| chaos | per-burst | 5094 | 3.82 | 1.64 | 0.0000 | 0 | - |
| chaos | per-pulse | 1064 | 1.01 | 5.12 | 0.0124 | 2 | 49 |
| wear | per-burst | 0 | - | 1.00 | 0.0077 | 22 | 232 |
| wear | per-pulse | 0 | - | 4.36 | 0.0339 | 95 | 54 |

Wear in the `wear` scenario if every coin were 5 pulses (default coins are 1, 1, 5, 5, 10 pulses): per-burst 232 years; per-pulse 47 years;

### Residual F4 loss (coin counted, power cut before its checkpoint)

Summed over the scenarios that cut power or reset. Loss is bounded by the time from a pulse to the end of its flash record. It is always reported, never hidden.

| policy | runs with cuts | cuts | cuts that lost pulses | pulses lost | per losing cut | losing cuts inside a flash/RTC op | restore-failed pulses | epoch bumps | pulses of an accepted coin arriving while down |
|---|---|---|---|---|---|---|---|---|---|
| per-burst | 5000 | 43416 | 13757 | 60294 | 4.38 | 10792 | 2068 | 3833 | 10045 |
| per-pulse | 5000 | 45099 | 11647 | 11849 | 1.02 | 11647 | 1584 | 3845 | 67543 |

## Invariants: per-burst (default)

Counts are pulses summed over all seeds. `counted` = real pulses the board counted plus spurious edges. I2 requires counted = credited + audit + lost (F4) + lost (restore-failed).

| scenario | runs | coins taken | coins refused (blocked) | counted | credited | audit | lost F4 | lost down | lost restore-failed | spurious | unexplained | I1 | I2 | I3 | I4 | I5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 500 | 18670 | 1161 | 80387 | 80256 | 131 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| lossy | 500 | 18255 | 1943 | 84726 | 84604 | 122 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| ack_loss | 500 | 17082 | 2560 | 81808 | 81667 | 141 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| reorder | 500 | 17783 | 2212 | 77228 | 77089 | 139 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| dup | 500 | 18879 | 1170 | 78589 | 78507 | 82 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| ctl_down | 500 | 15023 | 4933 | 65781 | 65715 | 66 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| power_cut | 500 | 17248 | 2710 | 74024 | 65339 | 137 | 8548 | 909 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| warm_rtc_kept | 500 | 17202 | 2760 | 74234 | 64979 | 107 | 9148 | 922 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| warm_rtc_lost | 500 | 17248 | 2710 | 74024 | 65339 | 137 | 8548 | 909 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| erase_cut | 500 | 17165 | 2844 | 75353 | 54895 | 104 | 20354 | 754 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| restore_fail | 500 | 17134 | 2916 | 73785 | 71727 | 128 | 678 | 1373 | 1252 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| restore_rtc_only | 500 | 16687 | 3229 | 71466 | 69034 | 149 | 2283 | 1595 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| restore_fail_wipe | 500 | 17860 | 2021 | 76615 | 75686 | 113 | 0 | 697 | 816 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| expiry_inflight | 500 | 10197 | 1408 | 71156 | 57867 | 13289 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| handover_inflight | 500 | 11904 | 2290 | 92738 | 92339 | 399 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| glitch | 500 | 18973 | 1222 | 97716 | 96728 | 988 | 0 | 0 | 0 | 14848 | 0 | PASS | PASS | PASS | PASS | PASS |
| forged_replay | 500 | 16862 | 3190 | 72767 | 68287 | 214 | 4266 | 997 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| wrap | 500 | 17127 | 2861 | 73393 | 71881 | 137 | 1375 | 957 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| stale_open | 500 | 3155 | 16945 | 14237 | 13868 | 369 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| chaos | 500 | 12231 | 9339 | 67326 | 55528 | 6704 | 5094 | 932 | 0 | 9119 | 0 | PASS | PASS | PASS | PASS | PASS |
| wear | 1 | 5595 | 394 | 24308 | 24248 | 60 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |

Fault coverage (faults that actually landed, summed over seeds):

| scenario | cuts | torn flash programs | torn sector erases | torn RTC writes | stale RTC applied | storage damaged | attacker msgs | Controller rejected bad MAC | second window refused | regress reports |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 32 |
| lossy | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 438 |
| ack_loss | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 85 |
| reorder | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2941 |
| dup | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 65 |
| ctl_down | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 24 |
| power_cut | 5085 | 1653 | 0 | 889 | 0 | 0 | 0 | 0 | 0 | 25 |
| warm_rtc_kept | 5145 | 1807 | 0 | 802 | 1676 | 0 | 0 | 0 | 0 | 19 |
| warm_rtc_lost | 5085 | 1653 | 0 | 889 | 0 | 0 | 0 | 0 | 0 | 25 |
| erase_cut | 8186 | 4194 | 148 | 2230 | 0 | 0 | 0 | 0 | 0 | 18 |
| restore_fail | 3521 | 2 | 0 | 0 | 0 | 2096 | 0 | 0 | 0 | 36 |
| restore_rtc_only | 4538 | 2 | 0 | 4 | 0 | 3090 | 0 | 0 | 0 | 32 |
| restore_fail_wipe | 1737 | 2 | 0 | 1 | 0 | 1737 | 0 | 0 | 0 | 28 |
| expiry_inflight | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 22 |
| handover_inflight | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1449 |
| glitch | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 42 |
| forged_replay | 3369 | 666 | 0 | 323 | 0 | 0 | 199064 | 29545 | 25099 | 26816 |
| wrap | 2645 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 127 |
| stale_open | 0 | 0 | 0 | 0 | 0 | 0 | 204847 | 0 | 0 | 8 |
| chaos | 4105 | 1041 | 0 | 584 | 867 | 0 | 86392 | 9249 | 11382 | 9779 |
| wear | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9 |

## Invariants: per-pulse

Counts are pulses summed over all seeds. `counted` = real pulses the board counted plus spurious edges. I2 requires counted = credited + audit + lost (F4) + lost (restore-failed).

| scenario | runs | coins taken | coins refused (blocked) | counted | credited | audit | lost F4 | lost down | lost restore-failed | spurious | unexplained | I1 | I2 | I3 | I4 | I5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 500 | 18851 | 1194 | 83130 | 83038 | 92 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| lossy | 500 | 17836 | 2045 | 79414 | 79317 | 97 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| ack_loss | 500 | 17220 | 2615 | 78491 | 78355 | 136 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| reorder | 500 | 17804 | 2289 | 74626 | 74496 | 130 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| dup | 500 | 18939 | 1213 | 79997 | 79876 | 121 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| ctl_down | 500 | 15096 | 4890 | 66866 | 66690 | 176 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| power_cut | 500 | 17406 | 2597 | 66774 | 64148 | 858 | 1768 | 9470 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| warm_rtc_kept | 500 | 17290 | 2579 | 67986 | 65260 | 909 | 1817 | 9927 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| warm_rtc_lost | 500 | 17406 | 2597 | 66774 | 64148 | 858 | 1768 | 9470 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| erase_cut | 500 | 17499 | 2565 | 52545 | 45845 | 1943 | 4757 | 24835 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| restore_fail | 500 | 17223 | 2821 | 73980 | 72981 | 126 | 2 | 1309 | 871 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| restore_rtc_only | 500 | 16753 | 3225 | 72660 | 72437 | 206 | 17 | 1922 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| restore_fail_wipe | 500 | 18002 | 1978 | 78276 | 77455 | 108 | 0 | 616 | 713 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| expiry_inflight | 500 | 10277 | 1290 | 71165 | 57506 | 13659 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| handover_inflight | 500 | 11770 | 2479 | 91914 | 91390 | 524 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| glitch | 500 | 18870 | 1191 | 98103 | 97151 | 952 | 0 | 0 | 0 | 14581 | 0 | PASS | PASS | PASS | PASS | PASS |
| forged_replay | 500 | 16943 | 3143 | 70610 | 69525 | 445 | 640 | 3965 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| wrap | 500 | 17081 | 2903 | 73122 | 72962 | 144 | 16 | 977 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| stale_open | 500 | 3144 | 16944 | 14104 | 13701 | 403 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |
| chaos | 500 | 12331 | 9231 | 62779 | 54350 | 7365 | 1064 | 5052 | 0 | 9262 | 0 | PASS | PASS | PASS | PASS | PASS |
| wear | 1 | 5603 | 398 | 24531 | 24485 | 46 | 0 | 0 | 0 | 0 | 0 | PASS | PASS | PASS | PASS | PASS |

Fault coverage (faults that actually landed, summed over seeds):

| scenario | cuts | torn flash programs | torn sector erases | torn RTC writes | stale RTC applied | storage damaged | attacker msgs | Controller rejected bad MAC | second window refused | regress reports |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| lossy | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 124 |
| ack_loss | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| reorder | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4043 |
| dup | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8 |
| ctl_down | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| power_cut | 5270 | 1792 | 8 | 854 | 0 | 0 | 0 | 0 | 0 | 3 |
| warm_rtc_kept | 5314 | 1818 | 16 | 805 | 1625 | 0 | 0 | 0 | 0 | 3 |
| warm_rtc_lost | 5270 | 1792 | 8 | 854 | 0 | 0 | 0 | 0 | 0 | 3 |
| erase_cut | 9161 | 4806 | 30 | 2425 | 0 | 0 | 0 | 0 | 0 | 4 |
| restore_fail | 3512 | 12 | 0 | 3 | 0 | 2109 | 0 | 0 | 0 | 0 |
| restore_rtc_only | 4497 | 8 | 5 | 6 | 0 | 3113 | 0 | 0 | 0 | 8 |
| restore_fail_wipe | 1736 | 6 | 0 | 1 | 0 | 1736 | 0 | 0 | 0 | 0 |
| expiry_inflight | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 |
| handover_inflight | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2000 |
| glitch | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 5 |
| forged_replay | 3424 | 638 | 3 | 315 | 0 | 0 | 198295 | 29524 | 25137 | 27095 |
| wrap | 2669 | 7 | 5 | 4 | 0 | 0 | 0 | 0 | 0 | 4 |
| stale_open | 0 | 0 | 0 | 0 | 0 | 0 | 205402 | 0 | 0 | 0 |
| chaos | 4246 | 1069 | 7 | 546 | 862 | 0 | 86146 | 9149 | 11133 | 9793 |
| wear | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |

## Scenario descriptions

- `baseline`: clean network, no faults
- `lossy`: 25% loss each way, duplicates, delay up to 400 ms (F1, F2)
- `ack_loss`: 50% of Controller->board messages lost (F2, T26)
- `reorder`: delays up to 1.5 s with duplicates: older totals arrive late (T17)
- `dup`: 50% of messages duplicated (T19)
- `ctl_down`: Controller down 5-20 s at intervals (F1, T29)
- `power_cut`: power loss every ~9 s, half inside a flash/RTC op (F3, F4, T12-T14, T28)
- `warm_rtc_kept`: warm reset, RTC survives, 30% stale RTC (F3, T14, T15; bench Q4)
- `warm_rtc_lost`: warm reset, RTC lost (F3; bench Q4 negative case)
- `erase_cut`: ring nearly full so sector erases are frequent, cuts aimed at flash ops (T13)
- `restore_fail`: cuts that also give every record a bad CRC: epoch bump (T22)
- `restore_rtc_only`: newest flash record destroyed, RTC kept: restore from RTC (T16)
- `restore_fail_wipe`: flash and RTC wiped: nothing survives, the epoch is random (T22, F-A)
- `expiry_inflight`: coins land just before the window ends (F7, T18, T25)
- `handover_inflight`: back-to-back windows, slow network, 10-pulse coin in flight at the handover (T30)
- `glitch`: isolated glitch edges and contact bounce (F9)
- `forged_replay`: forged and replayed messages, with reboots in between (F8, T20, T21)
- `wrap`: millis() and total wrap during the run (F10, T23)
- `stale_open`: short windows, 80% of Controller->board lost: whole windows are never received, then replayed late (T31)
- `chaos`: loss, reorder, outages, cuts, glitches and attacks together
- `wear`: ~6000 coins, ~4 per Coin Window, clean network: flash erases per coin (I6)

## Failures

none

## Canonical message vectors (for the Go Controller)

```
key = 030a11181f262d343b424950575e656c737a81888f969da4abb2b9c0c7ced5dc
report{vendo=1 epoch=2 total=300 window=7 nonce=0xa1b2c3d4 rseq=9}
  msg = 0100000001000000020000012c00000007a1b2c3d400000009
  mac = 29120c1cab1314363b4b8f14a60c298c67a032ec8971c03ed7ab0effd6469aa4
open{vendo=1 window=7 expires_ms=45000 nonce=0xa1b2c3d4 rseq=9}
  msg = 0200000001000000070000afc8a1b2c3d400000009
  mac = 92b10e528311f2aa03989a9a257dcbd709d2dc7cc4f6cc23452697a2a7e3e411
ack{vendo=1 epoch=2 total=300 nonce=0xa1b2c3d4 rseq=9}
  msg = 0300000001000000020000012ca1b2c3d400000009
  mac = 9bf26a8c2e43dbf4ac2cc10b79938329c378fae534f0b230c99c1b1462a4eacf
```
