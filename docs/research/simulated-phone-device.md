# Simulated phone Device behind the CHR HotSpot

Read this when you want to iterate on the captive portal UI/UX without a physical phone, or before building `lab/dev/`.

Status: research, 2026-10-27. Scope: which simulated phone OS (or browser stand-in) can act as a customer Device on `hotspot-lan` (bridge `virbr-hs`), how faithful each one is to the real-phone results of 2026-10-18 ([`hotspot-identity-lab.md` Q3](hotspot-identity-lab.md#q3-results-2026-10-18-plain-run)), and what attaching it takes. Nothing was installed, no VM was started, and no network config was changed. Terms follow `GLOSSARY.md`.

Every claim carries one label:

- **[D]** documented. The inline link is the primary source (AOSP/Linux/Firefox source, vendor docs).
- **[V]** verified on this host on 2026-10-27 with read-only checks (`ls`, `virsh net-list`, `ip link`, `pacman -Qi`, `debugfs` on the installed Waydroid image), or read in this repo.
- **[I]** inferred by me from the cited sources. Not tested. Items that need a hands-on run are collected in [5.3](#53-open-questions-for-a-hands-on-spike).

## TL;DR

- **Daily UI/UX loop: a browser in netns `hsA` on `virbr-hs`.** The netns client gets the real HotSpot redirect and Router enforcement. Playwright device profiles supply mobile viewport, UA and touch. It is not a phone OS, and it opens no captive sheet. **[I]**
- **Highest Android fidelity: the Android Emulator with `-wifi-tap <tap on virbr-hs>`.** It is the only option here where AOSP NetworkMonitor runs on a **Wi-Fi** network and the WifiTrackerLib auto-open path can fire. Both need Wi-Fi to reproduce the phone sheet behaviour. It needs the SDK installed and a spike: no release note mentions TAP, and a new networking stack shipped in 36.5. **[D]** flags and code; **[I]** that the bridge works.
- **Waydroid is already installed and has the real sheet app** (Android 13, CaptivePortalLogin, Google WebView) **[V]**. Its link is Ethernet, so expect a "Sign in to network" notification, not an auto-opened sheet **[I]**. It is a cheap second try if the emulator spike fails.
- **Cuttlefish, redroid and Android-x86/BlissOS add nothing we need.** Cuttlefish puts Wi-Fi behind an OpenWRT VM, so the CHR would see the AP's MAC, not the phone's **[D/I]**. The others are Ethernet-only or stale **[D]**.
- **iOS cannot be simulated here.** The Simulator is macOS-only and does not replicate device features **[D]**; Corellium is a paid cloud product reachable through a TAP VPN **[D]**. iOS sheet cookie loss (Q3 E2/E3) still needs a real iPhone **[I]**.
- **No option reproduces the phones' MAC randomization** **[I]**. Rebind tests must change the MAC by hand, as `lab/chr` already does **[V]**.

## Questions

| ID | Question | Answer | Basis |
|---|---|---|---|
| S1 | Can a simulator reproduce Q3 E1 (sheet opens in about 2s, closes itself after login)? | **Android Emulator on Wi-Fi only, probably.** The auto-open comes from WifiTrackerLib after a *manual* Wi-Fi connect. Auto-close is in CaptivePortalLogin itself. Ethernet-only Android (Waydroid, redroid, BlissOS) gets a notification. | [D] [mechanics](#1-captive-mechanics-the-parts-a-simulator-must-reproduce), [I] |
| S2 | Can a simulator reproduce the Android sheet keeping the cookie (Q3 E4/E5)? | **Plausibly.** The emulator and Waydroid images run the same CaptivePortalLogin app, which clears the cache but not cookies. Survival across reboot is untested. | [D] code, [V] image, [I] |
| S3 | Can a simulator reproduce iOS behaviour (E2/E3, private MAC, "Off" gives no popup)? | **No** on this Linux host. Approximate it with a fresh browser context per "sheet". | [D] Apple docs, [I] |
| S4 | Does any simulator randomize its MAC like the phones? | **No.** The emulator's Wi-Fi overlay leaves connected MAC randomization at the AOSP default `false`; Ethernet has none. MAC changes are manual. | [D] overlay, [I] |
| S5 | What is the cheapest faithful loop for UI/UX work? | A browser in netns `hsA` against the real CHR and Controller, with Playwright device descriptors and screenshots as artifacts. | [D] Playwright, [V] repo, [I] |
| S6 | What can run on this host without installing anything? | Firefox, Waydroid (with the GAPPS Android 13 image), Docker, QEMU/libvirt. Not installed: Android Emulator/SDK, Chromium/Chrome, Playwright, Cuttlefish. | [V] |
| S7 | What does attaching to `virbr-hs` cost? | One root step for every option: a TAP enslaved to the bridge, an LXC link change, or a veth/netns. The existing lab wizards already use `sudo`. | [D] per option, [V] repo |

## 1. Captive mechanics: the parts a simulator must reproduce

### 1.1 Android probes and the portal verdict

- AOSP's default probe URLs [D] ([NetworkStack `config.xml`](https://android.googlesource.com/platform/packages/modules/NetworkStack/+/refs/heads/main/res/values/config.xml)):
  - HTTP: `http://connectivitycheck.gstatic.com/generate_204`
  - HTTPS: `https://www.google.com/generate_204`
  - Fallbacks: `http://www.google.com/gen_204` and `http://play.googleapis.com/generate_204`
- The real Xiaomi (Android 14) used a different HTTPS set: `connectivitycheck.gstatic.com` and `www.google.cn` [V] ([adb logcat](results/2026-10-18-phones-adb/steps/android-f-mac/logcat.txt)). So OEM probe URLs differ from AOSP [I]. That does not matter behind the HotSpot, which answers every HTTP probe with a 302 [V, same logcat].
- What counts as a portal [D] ([`CaptivePortalProbeResult.java`](https://android.googlesource.com/platform/packages/modules/NetworkStack/+/refs/heads/main/common/captiveportal/src/android/net/captiveportal/CaptivePortalProbeResult.java)):
  - 204 means success.
  - Any other code from 200 to 399 means portal.
  - So the HotSpot's 302 on `generate_204` reads as a portal, which matches the phone log `isCaptivePortal: isSuccessful()=false isPortal()=true` [V].
- When detection runs [D] ([`NetworkMonitor.java#2390`](https://android.googlesource.com/platform/packages/modules/NetworkStack/+/refs/heads/main/src/com/android/server/connectivity/NetworkMonitor.java#2390)):
  - Detection is off only when `captive_portal_mode` is IGNORE.
  - HTTPS probing is on by default (`CAPTIVE_PORTAL_USE_HTTPS`, default 1, [#2411](https://android.googlesource.com/platform/packages/modules/NetworkStack/+/refs/heads/main/src/com/android/server/connectivity/NetworkMonitor.java#2411)).
- NetworkStack also has capport API support (`CapportApiProbeResult`) [D] ([NetworkStack tree](https://android.googlesource.com/platform/packages/modules/NetworkStack/+/refs/heads/main/src/android/net/captiveportal/)). The CHR HotSpot does not need it [I].

### 1.2 Which networks get probed: Wi-Fi and Ethernet differ here

- **Which networks get validated** [D] ([`NetworkMonitorUtils.java`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/staticlibs/device/com/android/net/module/util/NetworkMonitorUtils.java)):
  - A network is validated, and so probed, when it has `INTERNET` plus `NOT_RESTRICTED` plus `TRUSTED`.
  - This does not depend on the transport.
- **Plain Ethernet qualifies by default** [D]:
  - Default Ethernet capabilities include `INTERNET` and `NOT_RESTRICTED` ([`EthernetTracker.java`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service-t/src/com/android/server/ethernet/EthernetTracker.java)).
  - `TRUSTED` comes from the `NetworkCapabilities` defaults ([`NetworkCapabilities.java`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/framework/src/android/net/NetworkCapabilities.java)).
  - Only interfaces matching `eth\d+` are tracked by default ([`EthernetTracker.java`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service-t/src/com/android/server/ethernet/EthernetTracker.java)).
  - So unconfigured Ethernet *is* probed [I].
- **A device can restrict Ethernet** [D]:
  - Mechanism: `config_ethernet_interfaces` sets per-interface capabilities ([`ServiceConnectivityResources config.xml`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service/ServiceConnectivityResources/res/values/config.xml)).
  - The emulator (goldfish) uses it: `eth0;11,12,14;;` gives NOT_METERED, INTERNET and TRUSTED but no NOT_RESTRICTED, under the comment "Restricting eth0" ([goldfish `ConnectivityOverlay`](https://android.googlesource.com/device/generic/goldfish/+/refs/heads/main/rro_overlays/ConnectivityOverlay/res/values/config.xml)).
  - So emulator eth0 is never probed, and only the emulator's Wi-Fi path can show a portal [I].
- **Waydroid's image has no Connectivity overlay** [V]:
  - Its product overlays include only one network overlay, `NetworkStackOverlay`, and that sets only `config_dhcp_client_hostname`.
  - So Waydroid eth0 keeps the default capabilities and should be probed [I].

### 1.3 Sheet vs notification

- **The notification** [D] ([`NetworkNotificationManager.java#258`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service/src/com/android/server/connectivity/NetworkNotificationManager.java#258)):
  - On a portal, ConnectivityService posts a SIGN_IN notification.
  - The title is `wifi_available_sign_in` on Wi-Fi and the generic `network_available_sign_in` on other transports.
  - The priority is high when the user explicitly selected the network ([`ConnectivityService.java#5004`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service/src/com/android/server/ConnectivityService.java#5004)).
- **The auto-open** [D] ([`StandardWifiEntry.java`](https://android.googlesource.com/platform/frameworks/opt/net/wifi/+/refs/heads/main/libs/WifiTrackerLib/src/com/android/wifitrackerlib/StandardWifiEntry.java), `connect()` and `onNetworkCapabilitiesChanged()`):
  - WifiTrackerLib (used by Settings and the Wi-Fi picker) sets `mShouldAutoOpenCaptivePortal = true` on a manual connect, "(i.e. not auto-join)".
  - When the network then shows `CAPTIVE_PORTAL`, it calls `signIn()`, which starts the sheet.
  - Ethernet has no such path, so Ethernet-only Android shows a notification the user must tap [I].
- **Who may launch the sheet:** `startCaptivePortalApp` requires NetworkStack or Settings permission [D] ([`ConnectivityService.java#6474`](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service/src/com/android/server/ConnectivityService.java#6474)). The real phone's sheet was launched from uid 1000 via `startCaptivePortalAppInternal` [V, adb logcat].

### 1.4 Cookies and auto-close in the Android sheet

- **Cookies** [D] ([`CaptivePortalLoginActivity.java#565`](https://android.googlesource.com/platform/packages/modules/CaptivePortalLogin/+/refs/heads/main/src/com/android/captiveportallogin/CaptivePortalLoginActivity.java#565)):
  - The sheet is a WebView.
  - On setup it calls `clearCache(true)` and `setAcceptThirdPartyCookies(webview, true)`.
  - It does not remove cookies. That is consistent with Q3 E4/E5, where the cookie survived [I].
- **Auto-close** [D] ([#1141](https://android.googlesource.com/platform/packages/modules/CaptivePortalLogin/+/refs/heads/main/src/com/android/captiveportallogin/CaptivePortalLoginActivity.java#1141)):
  - After each page load the sheet calls `reevaluateNetwork()`.
  - It closes when the network validates. That matches the Android 14 sheet closing in under 1s after login [V, results].
- **Custom Tabs mode** [D] ([#657](https://android.googlesource.com/platform/packages/modules/CaptivePortalLogin/+/refs/heads/main/src/com/android/captiveportallogin/CaptivePortalLoginActivity.java#657), [#489](https://android.googlesource.com/platform/packages/modules/CaptivePortalLogin/+/refs/heads/main/src/com/android/captiveportallogin/CaptivePortalLoginActivity.java#489)):
  - Gated by the DeviceConfig flag `captive_portal_custom_tabs`.
  - When on, the sheet uses the default browser's Custom Tab, but only if that browser supports multi-network Custom Tabs; otherwise it falls back to the WebView.
  - If a newer image enables this flag, the sheet may share the browser's cookie jar, unlike Q3 E6 [I]. Spike item.

### 1.5 MAC randomization

- **Behaviour** [D] ([AOSP MAC randomization behaviour](https://source.android.com/docs/core/connect/wifi-mac-randomization-behavior)):
  - Android randomizes per network profile (persistent).
  - Some networks, including ones that saw a captive portal, stay on the persistent MAC rather than re-randomizing.
- **Device support** [D] ([AOSP MAC randomization](https://source.android.com/docs/core/connect/wifi-mac-randomization)):
  - Needs HAL `setMacAddress` and the overlay `config_wifi_connected_mac_randomization_supported`.
  - That overlay defaults to `false` ([`ServiceWifiResources config.xml`](https://android.googlesource.com/platform/packages/modules/Wifi/+/refs/heads/main/service/ServiceWifiResources/res/values/config.xml)).
- **The emulator does not opt in.** No goldfish overlay sets the randomization overlay [D] ([goldfish tree](https://android.googlesource.com/device/generic/goldfish/+/refs/heads/main)), so emulator Wi-Fi uses a fixed MAC [I].

### 1.6 Apple

- **iOS captive behaviour** [D] (["How to modernize your captive network"](https://developer.apple.com/news/?id=q78sq5rv)):
  - iOS 14 and macOS Big Sur support the captive-portal API: DHCP option 114, RA option 37, `application/captive+json`.
  - iOS "automatically" sends a probe when joining a network and displays the login page.
- **The legacy probe still answers today** [V, `curl` on 2026-10-27]:
  - `http://captive.apple.com/hotspot-detect.html` returns 200 with a `Success` body.
  - `connectivitycheck.gstatic.com/generate_204` returns 204.

### 1.7 Desktop Firefox

- **Captive detection is on by default** in Firefox [D]:
  - Enabled by `network.captive-portal-service.enabled=true` ([`firefox.js`](https://raw.githubusercontent.com/mozilla-firefox/firefox/main/browser/app/profile/firefox.js)).
  - It probes `http://firefox-portal-detection.com/generate_204` and expects an empty body ([`all.js`](https://raw.githubusercontent.com/mozilla-firefox/firefox/main/modules/libpref/init/all.js)).
- Behind the HotSpot that probe gets the 302, so Firefox should show its own captive-portal notice [I]. Mozilla's support page for this was unreachable during research.

## 2. Host baseline

All [V] on 2026-10-27:

- **CPU, RAM, GPU:**
  - AMD Ryzen 5 PRO 2400G, 8 threads, 15 GiB RAM (about 6.3 GiB free at check time).
  - Radeon RX 470/480/570/580; `kvm_amd` with nested=1.
- **Devices and kernel:**
  - `/dev/kvm`, `/dev/vhost-net` and `/dev/net/tun` are all present, mode `crw-rw-rw-`.
  - Kernel 7.2.11-cachyos with `ANDROID_BINDER_IPC=y`, `ANDROID_BINDERFS=y`, `VIRT_WIFI=m`, `MAC80211_HWSIM=m`.
  - KDE on Wayland.
- **Installed:** `adb`, `waydroid` 1.6.3 (GPL-3.0-or-later), `docker`, `firefox` 158.0, `qemu-system-x86_64`, `virsh`.
- **Not installed:** `emulator`, `sdkmanager`, `avdmanager`, Chrome/Chromium, Playwright, podman, `cvd`. There is no `~/Android/Sdk`, but `~/.android/adbkey` exists.
- **libvirt:**
  - Networks `default`, `hotspot-lan` and `pisonet` are active.
  - VMs `chr-lab`, `chr-client`, `piso-server` and `piso-client` are shut off.
  - `virbr-hs` is DOWN (NO-CARRIER), with no ports.
- **CHR LAN** (from `lab/chr/wizard.sh`):
  - `10.5.50.1/24` on `ether2`.
  - HotSpot DHCP pool `10.5.50.10-10.5.50.200`, DNS `10.5.50.1`.
  - netns clients `hsA`/`hsB` use static `.201`/`.202` with MACs `02:00:00:00:0a:01`/`0b:01`.

## 3. Options

### 3.1 Android Emulator (goldfish/ranchu)

- **Networking:**
  - By default each instance sits behind its own virtual router on `10.0.2/24`, with Ethernet at `10.0.2.15` and Wi-Fi at `10.0.2.16` [D] ([emulator networking address space](https://developer.android.com/studio/run/emulator-networking-address)).
  - The Wi-Fi is a "Simulated Wi-Fi access point (AndroidWifi)" [D] ([emulator networking](https://developer.android.com/studio/run/emulator-networking)).
  - Version 36.5.10 (2026-04-02) shipped "a new networking stack" for multi-device testing [D] ([emulator release notes](https://developer.android.com/studio/releases/emulator)).
  - `-netsim-args="--wifi <SSID> <PASSWORD>"` configures that stack's AP [D] ([advanced networking](https://developer.android.com/studio/run/emulator-networking-advanced)).
  - Latest stable is 37.2.12 (2026-10-01). A new engine for API 37+ AVDs is in Studio Canary, and existing AVDs stay on the classic engine "for now" [D] ([release notes](https://developer.android.com/studio/releases/emulator)).
- **TAP flags exist in the source:**
  - `-net-tap <interface>` ("use this TAP interface for networking") and `-wifi-tap <interface>` ("use this TAP interface for Virtio Wi-Fi") [D] ([`cmdline-options.h`](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/emu/cmdline/include/android/cmdline-options.h)).
  - `main.cpp` turns `-wifi-tap` into a QEMU `tap` netdev for `virtio-wifi-pci`; otherwise it uses `user,…dhcpstart=10.0.2.16`. It also starts the built-in hostapd when VirtioWifi is on [D] ([`main.cpp`](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android-qemu2-glue/main.cpp)).
  - The forwarder converts 802.11 frames to and from Ethernet (`toEthernet()`, `buildFromEthernet()`) [D] ([`VirtioWifiForwarder.cpp`](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-35-3-release/android-qemu2-glue/emulation/VirtioWifiForwarder.cpp)).
  - The guest builds `wlan0` on `mac80211_hwsim` (`mac80211_create_radios`) [D] ([goldfish `init.ranchu-net.sh`](https://android.googlesource.com/device/generic/goldfish/+/refs/heads/main/init.ranchu-net.sh)).
  - The public `emu-master-dev` branch was last committed on 2025-03-27, and the release notes never mention TAP [D] (gitiles log, release notes). So whether the flags still work on 36.5+ is unknown [I].
- **Attaching to `virbr-hs`:**
  - Create one user-owned TAP with root (`ip tuntap add mode tap user $USER` + `ip link set … master virbr-hs`), then run `emulator -wifi-tap <tap>` [I].
  - Expected result: Android associates with the emulator's own hostapd, and its Ethernet-converted frames reach the CHR. `wlan0` should then get a CHR lease from `10.5.50.10-200` and hit the HotSpot [I].
  - Use `-wifi-tap`, not `-net-tap`: eth0 is restricted (see 1.2), so it would never show a portal [I].
- **Fidelity:**
  - Same NetworkMonitor and CaptivePortalLogin as phones [D]. Wi-Fi transport, so the WifiTrackerLib auto-open applies when you tap the network in Settings [I].
  - Real Android WebView engine [I].
  - No MAC randomization (1.5). `-wifi-mac-address` "only applies" when the AVD loads from a snapshot in read-only mode [D] ([`cmdline-options.h`](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/emu/cmdline/include/android/cmdline-options.h)). Rebind tests need that, or `adb root` plus `ip link set wlan0 address` [I].
- **Automation and artifacts:**
  - adb reaches the instance on host loopback ports (console 5554, adb 5555) [D] ([emulator command line](https://developer.android.com/studio/run/emulator-commandline)). So adb does not need a route into `10.5.50.0/24` [I].
  - The `lab/phones/wizard.sh` capture lines work as they are [V/I]: `screencap`, `logcat` grep for `NetworkMonitor|CaptivePortal`, `ip link show wlan0`.
  - `-no-window` runs headless [D] ([emulator command line](https://developer.android.com/studio/run/emulator-commandline)).
- **Images and root:**
  - Play Store images are release-key signed with no root; AOSP images allow `adb root` [D] ([managing AVDs](https://developer.android.com/studio/run/managing-avds)).
  - Use a Google APIs or AOSP image for root [I].
- **Host needs:** KVM; the recommended 16 GB RAM and 16 GB disk [D] ([emulator requirements](https://developer.android.com/studio/run/emulator)). This host has 15 GiB, so running it with the CHR and the Controller will be tight [V/I].
- **Licence and cost:** no cost [I]. The SDK installs under Google's SDK terms, which were not re-checked.
- **Setup effort:** medium. SDK download plus a one-time TAP, then the spike in 5.3 [I].

### 3.2 Cuttlefish

- **Requirements and launch** [D] ([Cuttlefish get started](https://source.android.com/docs/devices/cuttlefish/get-started)):
  - KVM and Debian host packages.
  - `launch_cvd --daemon`, a WebRTC UI at `https://localhost:8443`, and adb.
- **Attaching to an existing bridge** [D]:
  - Host config `bridge_interface=` "suppresses bridge management, and instead adds the cuttlefish wifi and ethernet tap devices to the specified bridge" ([`cuttlefish-host-resources.default`](https://github.com/google/android-cuttlefish/blob/main/base/debian/cuttlefish-base.cuttlefish-host-resources.default)).
  - The default is managed bridges `cvd-wbr`/`cvd-ebr` with dnsmasq and NAT ([`.init`](https://github.com/google/android-cuttlefish/blob/main/base/debian/cuttlefish-base.cuttlefish-host-resources.init)).
- **The Wi-Fi path goes through an OpenWRT AP VM** whose WAN has a static IP (`192.168.94+.x`/`96.x`) [D] ([`openwrt_args.cpp`](https://github.com/google/android-cuttlefish/blob/main/base/cvd/cuttlefish/host/libs/config/openwrt_args.cpp)).
- **What that means for us** [I]:
  - On `virbr-hs` the CHR would see OpenWRT's WAN MAC and IP, not the phone's.
  - That defeats MAC-based identity and the per-Device HotSpot host entry.
  - The Ethernet tap may get a CHR lease, but Cuttlefish's Ethernet capabilities were not checked.
- **Fit for this host:** CachyOS is not a documented host [I]. Setup effort is high. Licence: the GitHub repo reports `NOASSERTION` [D, GitHub API].

### 3.3 Waydroid (installed)

- **Image** [V]:
  - LineageOS 20, Android 13, x86_64, `userdebug`, GAPPS.
  - Contains `CaptivePortalLogin`, `NetworkStack`/`NetworkStackNext`, `WebViewGoogle`, `TrichromeLibrary`, `GmsCore` and `Jelly`. No Chrome.
  - Graphics through Mesa on `/dev/dri/renderD128`.
- **Default networking** [V] (`/var/lib/waydroid/lxc/waydroid/config`, `waydroid-net.sh`):
  - An LXC veth `eth0` on host bridge `waydroid0`, with `hwaddr 00:16:3e:f9:d3:03`.
  - `waydroid-net.sh` provides dnsmasq on `192.168.240.0/24` and NAT.
- **Attaching to `virbr-hs`** [V/I]:
  - Set `lxc.net.0.link = virbr-hs`. `waydroid-net.sh` then prints "vnic is …, bailing out" and stops managing NAT and DHCP [V], so Android's DHCP client should lease from the CHR [I].
  - `set_lxc_config()` rewrites this config on `waydroid init` and `upgrade` [V], so the edit must be re-applied by the lab script [I].
  - `waydroid adb connect` reads only `dnsmasq.waydroid0.leases` [V], so it stops working. Use `waydroid shell` (lxc-attach, needs no network) for `screencap`, `input`, `am` and `logcat` [V/I].
- **Fidelity:**
  - Ethernet transport. Probing should run (1.2); a "Sign in to network" notification should appear; there is no auto-open [I].
  - Tapping the notification should open the real CaptivePortalLogin WebView [I].
  - The MAC is whatever `hwaddr` says, with no randomization [I]. A Rebind test means editing `hwaddr` and restarting the container [I].
  - `persist.waydroid.fake_wifi` only makes listed apps believe they are on Wi-Fi [D] ([Waydroid prop options](https://docs.waydro.id/usage/waydroid-prop-options)). It does not create a Wi-Fi network for NetworkMonitor [I].
- **Host needs:** Wayland session (present), binder (built in), root for the container service [V/I]. Licence: GPL-3.0-or-later; the GAPPS apps are under Google's terms [V/I].
- **Setup effort:** low. One config edit plus a re-apply hook [I].

### 3.4 redroid

- **What it is** [D] ([redroid-doc README](https://github.com/remote-android/redroid-doc/blob/master/README.md)):
  - Android 8.1 to 16 images in Docker: `docker run --privileged … -p 5555:5555 redroid/redroid:16.0.0-latest`.
  - Needs binder devices.
  - DNS and proxy come from `androidboot.redroid_net_*` boot params.
  - GMS via OpenGApps, MicroG or MindTheGapps.
  - Apache-2.0, with kernel modules under GPLv2.
- **Networking** [I]:
  - The `device_redroid` branches show no network config in `init.redroid.rc`, the overlays or `redroid.prop` [D, repo read], so Android's address comes from Docker networking.
  - A macvlan or bridge on `virbr-hs` is possible, but whether Android does DHCP against the CHR, and whether eth0 is probed, is unknown.
- **Verdict:** no advantage over Waydroid, and more unknowns [I].

### 3.5 Android-x86 / BlissOS

- **Android-x86 is stale.** Its last release is 9.0-r2 (2020-03-25) [D] ([release note](https://www.android-x86.org/releases/releasenote-9-0-r2.html)).
- **BlissOS QEMU docs are thin** [D] ([install in QEMU](https://docs.blissos.org/installation/install-in-a-virtual-machine/install-in-qemu/), [advanced QEMU config](https://docs.blissos.org/installation/install-in-a-virtual-machine/advanced-qemu-config/)):
  - They show `-enable-kvm`, `virtio-vga-gl` and `-net nic,model=virtio-net-pci`.
  - They say "Bliss 14 at the current moment does not work", and virt-manager has "no guide".
- **Attaching** [I]: a libvirt NIC on `hotspot-lan` with a chosen MAC is trivial. It is Ethernet only, so this is Waydroid-level fidelity with older Android and more breakage.

### 3.6 iOS: Xcode Simulator, Corellium

- **Xcode Simulator** [D]:
  - Simulators "don't replicate the performance or features of a physical device" ([Simulator vs hardware](https://developer.apple.com/documentation/xcode/testing-in-simulator-versus-testing-on-hardware-devices)).
  - Xcode 27.2 RC requires macOS Tahoe 26.6 or later ([Xcode system requirements](https://developer.apple.com/xcode/system-requirements/)).
  - Not runnable on this Linux host [V]. A Simulator shares the Mac's network and never joins Wi-Fi, so it gets no captive sheet [I].
- **Corellium** (now Cellebrite) [D]:
  - Virtual iPhones; trials are offered and prices are not public ([Corellium trial](https://cellebrite.com/en/products/corellium/trial)).
  - The LAN path is an OpenVPN that makes an "outbound UDP connection to AWS servers in the us-east-2 region" and "must support TAP mode" ([Corellium VPN](https://support.corellium.com/features/connect/vpn)).
  - Bridging that TAP into `virbr-hs` might put a virtual iPhone on the CHR LAN, but it is paid, cloud-hosted and adds latency. Whether its captive sheet matches a real iPhone is unknown [I].
- **Verdict:** keep real iPhones for iOS behaviour (Q3 E2/E3, the "Private Wi-Fi Address Off" case) [I].

### 3.7 Desktop browser in netns `hsA` (baseline)

- **Attaching** [V]: `lab/chr/wizard.sh` already creates `hsA`/`hsB` as veth clients on `virbr-hs`, with static IPs and settable MACs. The Q2 lab drove the real HotSpot through them.
- **Mobile emulation:**
  - Playwright device descriptors set `userAgent`, `viewport`, `deviceScaleFactor`, `isMobile` and `hasTouch` [D] ([Playwright emulation](https://playwright.dev/docs/emulation)).
  - Playwright's Linux WebKit is "derived from the latest WebKit main branch", not Safari [D] ([Playwright browsers](https://playwright.dev/docs/browsers)).
  - Chrome DevTools device mode is a "first-order approximation" ([device mode](https://developer.chrome.com/docs/devtools/device-mode)) [D].
- **Fidelity:**
  - Real HotSpot redirect, real `login.html` hop, real Controller and Router enforcement [I].
  - No OS captive detection except Firefox's notice (1.7). No sheet, no MAC randomization.
  - Sheet cookie behaviour can be modelled: a fresh browser context per "sheet" (iOS-like), a persistent context (Android-like) [I].
- **Automation and artifacts:** run headless Playwright in the netns as your user: `sudo ip netns exec hsA sudo -u $USER …` [I]. It produces per-step screenshots and traces, and the netns needs a `resolv.conf` pointing at `10.5.50.1` [I].
- **Host needs:** root for the netns (as today) [V]; Playwright plus browsers (not installed) [V]. Firefox works now [V]. No cost.
- **Setup effort:** lowest [I].

## 4. Comparison

| Option | Captive detection | Sheet (CNA) | Cookie kept in sheet | MAC randomization | Real mobile engine | Attach to `virbr-hs` | Host / cost | Effort | On this host |
|---|---|---|---|---|---|---|---|---|---|
| Browser in `hsA` | Firefox notice only [D/I] | none | modelled per context [I] | none; manual MAC [V] | Blink/Gecko/WebKit-on-Linux, not mobile [D] | existing veth [V] | root for netns; free | low | Firefox yes; Playwright no [V] |
| **Android Emulator** `-wifi-tap` | NetworkMonitor on Wi-Fi [D/I] | auto-open on manual connect [D/I] | same app as phones; untested [D/I] | none (overlay false) [D/I] | Android WebView [I] | root TAP into bridge [I] | KVM, 16 GB RAM rec. [D]; free [I] | medium + spike | not installed [V] |
| Waydroid | NetworkMonitor on Ethernet [D/I] | notification, tap to open [D/I] | same app; untested [V/I] | none; `hwaddr` [V] | Google WebView [V] | `lxc.net.0.link` [V] | Wayland, binder [V]; GPL-3.0 [V] | low | installed [V] |
| Cuttlefish | Wi-Fi behind OpenWRT NAT [D/I] | likely, but hidden MAC [I] | same app [I] | AP hides it [I] | Android WebView [I] | `bridge_interface=` [D] | KVM, Debian pkgs [D] | high | not installed [V] |
| redroid | unknown [I] | unknown [I] | unknown | none [I] | Android | Docker macvlan [I] | privileged Docker, binder [D] | medium | Docker yes [V] |
| Android-x86 / BlissOS | Ethernet [I] | notification [I] | old app versions [I] | none | old WebView [I] | libvirt NIC [I] | KVM [D] | medium | no image [V] |
| iOS Simulator | no Wi-Fi join [I] | none [I] | n/a | none | iOS WebKit [I] | n/a | macOS only [D] | n/a | impossible [V] |
| Corellium | unknown [I] | unknown [I] | unknown | unknown | real iOS [D] | TAP VPN to AWS [D] | paid [D] | high | n/a |

Gaps against the 2026-10-18 phones, best case:

- **E1 (auto-open, auto-close):** emulator only, and only for a manual connect from Settings. Waydroid and BlissOS need a tap on the notification [I].
- **E2/E3 (iOS sheet drops cookies; stable private MAC):** nothing reproduces this; the desktop models it [I].
- **E4/E5 (Android sheet keeps the cookie across idle-out, MAC change, reboot):** emulator and Waydroid run the same code; this needs a run [I].
- **E6 (no cookie sharing with the browser):** holds for the WebView path. It may break if Custom Tabs is enabled on newer images [D/I].
- **Recheck "Private Wi-Fi Address Off" (hardware MAC, no popup, old session keeps charging):** iOS only. The Router side can be imitated by switching MACs on `hsA` [I].

## 5. What this means for our lab

### 5.1 Ranked recommendation

1. **Browser in netns `hsA`, driven by Playwright (Chromium and WebKit device profiles), plus Firefox by hand.** Use it for everyday portal UI/UX: layout, copy, the Voucher, Coin Window and Resume Code flows. It exercises the real CHR and Controller, it is cheap, and every step leaves a screenshot. [I]
2. **Android Emulator with `-wifi-tap`.** Use it for anything that depends on the Android sheet: auto-open, auto-close, cookie survival, and logcat parity with `results/2026-10-18-phones-adb`. Gate it on spike items 1 to 4 below. Pin a known-good emulator version once it works. [I]
3. **Waydroid on `virbr-hs`.** It is already installed, so it is the fastest way to see the real Android 13 sheet render the portal. It is the fallback if `-wifi-tap` fails, accepting notification-instead-of-auto-open. [I]
4. **Cuttlefish, redroid, Android-x86/BlissOS: skip.** [I]
5. **iOS: keep the real iPhone.** Re-run the `lab/phones` wizard only when a change touches iOS cookie or MAC behaviour. [I]

### 5.2 How it plugs into `lab/dev/`

All [I] unless marked:

- **Router:** reuse `hotspot-lan`/`virbr-hs` and `chr-lab`, with the `lab/chr/wizard.sh` LAN (`10.5.50.1`, pool `.10-.200`) [V], and the `lab/phones` templated `login.html` plus the walled-garden rule for the Controller [V].
- **Controller** in dev mode on the host, as `lab/phones` does with `PORTAL_ADDR` [V].
- **Coin simulator:** `controller/e2e/simulated_vendo.py` [V]. A Coin Window run is then fully virtual: Device, Router, Controller and Vendo.
- **Device slots** that `lab/dev` can bring up on demand:
  - `browser`: the `hsA` netns plus Playwright.
  - `emu`: TAP `tap-emu0` into `virbr-hs` plus `emulator -wifi-tap tap-emu0 -no-window`.
  - `waydroid`: re-apply `lxc.net.0.link=virbr-hs` before each session.
  - Each slot gets a fixed MAC so `/ip/hotspot/host` stays readable. A "change MAC" step covers Rebind.
- **Capture:**
  - Reuse the `lab/phones/wizard.sh` per-step capture: `screencap`, `logcat` grep for `NetworkMonitor|CaptivePortal|captive|portal`, the `wlan0` MAC, and the `/ip/hotspot/{host,active,user}` JSON snapshots [V].
  - Desktop slots add Playwright screenshots and traces.
  - Write results to `docs/research/results/<date>-simphone-<slot>/results.md`, matching the existing layout.
- **Memory budget:** the CHR, the Controller and one emulator fit in 15 GiB only if `chr-client` and the `piso-*` VMs stay off [V/I].

### 5.3 Open questions for a hands-on spike

| # | Question | How to answer it |
|---|---|---|
| 1 | Does `-wifi-tap` still work on 36.5+ (new networking stack) and on 37.x? If not, which last version works? | Boot an API 34–36 Google APIs AVD with `-wifi-tap`; look for the lease in `/ip/dhcp-server/lease` and the host entry in `/ip/hotspot/host`. |
| 2 | Does the emulator's built-in hostapd still handle association when the backend is a TAP, so `wlan0` reaches `COMPLETED`? | `adb shell cmd wifi status`; `logcat` for `wpa_supplicant`. |
| 3 | Does a manual connect to `AndroidWifi` in Settings auto-open the sheet, while a cold boot only notifies? Does it close itself after login, like E1? | Run both paths; capture `logcat` lines matching the phone run (`isPortal()=true`, `CAPTIVE_PORTAL`). |
| 4 | Does the sheet's cookie survive idle-out, a MAC change and a cold boot in the emulator (E4/E5)? | Repeat the `lab/phones` Android steps with `adb root` plus a `wlan0` MAC change. |
| 5 | Is `captive_portal_custom_tabs` enabled on current Google APIs images, and does it change cookie sharing (E6)? | `adb shell device_config get captive_portal_login captive_portal_custom_tabs`; observe which app opens. |
| 6 | Does emulator eth0 (slirp, restricted) let any system traffic bypass the HotSpot? | `-tcpdump` on the emulator, compared with the CHR's `/ip/hotspot/host` counters. |
| 7 | On Waydroid with `lxc.net.0.link=virbr-hs`: does eth0 get a CHR lease, and does NetworkMonitor flag the portal and post "Sign in to network"? | `sudo waydroid shell` then `dumpsys connectivity`; `logcat`. |
| 8 | Can `virt_wifi` (built as a module here [V]) make a Waydroid link look like Wi-Fi to Android? | Low priority. It needs the Android Wi-Fi HAL and supplicant inside LXC, which is probably not workable [I]. |
| 9 | How far do Playwright WebKit and Chromium mobile screenshots drift from the 2026-10-18 phone screenshots? | Render the same portal pages and compare them by eye with the `results/2026-10-18-phones-*` captures. |
