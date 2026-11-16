// #43: config -> Ethernet DHCP option 12 -> one signed first-contact Report.
// No coin ISR or flash writes. SET remains inhibited for this entire sketch.
#include "bench.h"
#include "provisioning.h"
#include "wire.h"
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <bearssl/bearssl.h>
#include <lwip/dhcp.h>
#if defined(ETH_W5500)
#include <W5500lwIP.h>
using EthernetBase = Wiznet5500lwIP;
#else
#include <W5100lwIP.h>
using EthernetBase = Wiznet5100lwIP;
#endif

namespace {

struct BoardConfig : sv::ConfigStorage {
  bool read(void* dst, size_t len) override {
    if (ESP.getFlashChipRealSize() != 4 * 1024 * 1024) return false;
    return ESP.flashRead(sv::kConfigAddress, static_cast<uint32_t*>(dst), len);
  }
};

struct Ethernet : EthernetBase {
  Ethernet() : EthernetBase(ETH_CS) {}
  bool beginNamed(const uint8_t mac[6], const char* hostname) {
    // 3.1.2's begin() starts DHCP immediately, with no per-interface hostname setter.
    // Suppress that automatic start using a temporary TEST-NET static address;
    // clear it and set option 12 before the FIRST DHCP allocation, not a renewal.
    if (!config(IPAddress(192, 0, 2, 1), IPAddress(0, 0, 0, 0), IPAddress(255, 255, 255, 0)) ||
        !EthernetBase::begin(mac)) return false;
    netif_set_addr(&_netif, IP4_ADDR_ANY4, IP4_ADDR_ANY4, IP4_ADDR_ANY4);
    _netif.hostname = hostname;
    return dhcp_start(&_netif) == ERR_OK;
  }
};

BoardConfig storage;
sv::Provisioning config;
Ethernet eth;

void firstReport() {
  sv::Report report{};
  report.vendoId = config.vendoId;
  report.epoch = os_random();
  report.bootNonce = os_random();
  report.rseq = 1;
  uint8_t frame[sv::kReportMsgLen + sv::kMacLen];
  sv::reportMsg(report, frame);
  br_hmac_key_context key;
  br_hmac_context hmac;
  br_hmac_key_init(&key, &br_sha256_vtable, config.key, sizeof config.key);
  br_hmac_init(&hmac, &key, sv::kMacLen);
  br_hmac_update(&hmac, frame, sv::kReportMsgLen);
  br_hmac_out(&hmac, frame + sv::kReportMsgLen);

  WiFiClient client;  // lwIP socket follows eth's default route; WiFi is off.
  HTTPClient http;
  String url(config.controllerUrl);
  while (url.endsWith("/")) url.remove(url.length() - 1);
  url += "/vendo/messages";
  if (!http.begin(client, url)) {
    benchResult("provisioning_report", "error=http_begin");
    return;
  }
  http.setTimeout(15000);
  http.addHeader("Content-Type", "application/octet-stream");
  int status = http.POST(frame, sizeof frame);
  // Never print the key, signed frame, or response body. Check adoption in admin:
  // first-contact Router cleanup can close the TCP connection before its reply.
  benchResult("provisioning_report", "vendo_id=%u http=%d bytes=%u", config.vendoId, status,
              unsigned(sizeof frame));
  http.end();
}

}  // namespace

void setup() {
  benchBoot("provisioning");
  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);
  sv::ConfigStatus status = sv::readProvisioning(storage, config);
  if (status != sv::ConfigStatus::kValid) {
    const char* reason = status == sv::ConfigStatus::kMissing ? "missing" :
                         status == sv::ConfigStatus::kCorrupt ? "corrupt" : "read_error_or_not_4MB";
    benchResult("provisioning_config", "error=%s no_reports=YES", reason);
    Serial.println("DONE");
    return;
  }
  uint8_t mac[6];
  wifi_get_macaddr(STATION_IF, mac);
  char expected[10];
  sv::vendoHostname(mac, expected);
  if (strcmp(expected, config.hostname) || strncmp(config.controllerUrl, "http://", 7)) {
    benchResult("provisioning_config", "error=hostname_or_url no_reports=YES");
    Serial.println("DONE");
    return;
  }
  Serial.printf("CONFIG vendo_id=%u mac=%02X:%02X:%02X:%02X:%02X:%02X hostname=%s\n", config.vendoId,
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], config.hostname);
  if (!eth.beginNamed(mac, config.hostname)) {
    benchResult("provisioning_dhcp", "error=ethernet_begin");
    Serial.println("DONE");
    return;
  }
  eth.setDefault();
  uint32_t start = millis();
  while (!eth.connected() && millis() - start < 60000) delay(10);
  if (!eth.connected()) {
    benchResult("provisioning_dhcp", "error=timeout");
  } else {
    benchResult("provisioning_dhcp", "ip=%s mask=%s gateway=%s hostname_requested=%s",
                eth.localIP().toString().c_str(), eth.subnetMask().toString().c_str(),
                eth.gatewayIP().toString().c_str(), config.hostname);
    firstReport();
  }
  Serial.println("DONE");
}

void loop() { delay(1000); }
