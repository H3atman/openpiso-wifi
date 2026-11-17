//go:build dev

// Command adminshots serves the admin panel from a throwaway ledger seeded with fixture data,
// for the screenshot harness `python3 -I lab/dev/dev.py admin-shots --out <dir>`.
//
//	go build -tags dev -o lab/dev/.run/adminshots ./cmd/adminshots
//	adminshots [-empty]
//
// It listens on 127.0.0.1:<random>, prints one JSON line {base, cookie, vendos} once the
// fixture is ready, and serves until killed. -empty seeds only the admin and its session.
package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/controller"
	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/vendoprotocol"
)

const day = 24 * 60 * 60

// Shop Rate tiers and Points table; the custom-rates Vendo gets its own variants below.
var (
	rateTiers = []ledger.RateTier{
		{Pesos: 1, Seconds: 10 * 60, ValiditySeconds: day, PauseLimit: 0, PointsMilli: 1000},
		{Pesos: 5, Seconds: 60 * 60, ValiditySeconds: 3 * day, PauseLimit: 2, PointsMilli: 5000},
		{Pesos: 10, Seconds: 150 * 60, ValiditySeconds: 7 * day, PauseLimit: 5, PointsMilli: 12000},
	}
	pointsTiers = []ledger.PointsTier{
		{PointsMilli: 5000, Seconds: 30 * 60, ValiditySeconds: day, PauseLimit: 1},
		{PointsMilli: 10000, Seconds: 75 * 60, ValiditySeconds: 2 * day, PauseLimit: 2},
	}
	customRates = []ledger.RateTier{
		{Pesos: 1, Seconds: 12 * 60, ValiditySeconds: day, PauseLimit: 0, PointsMilli: 1000},
		{Pesos: 5, Seconds: 70 * 60, ValiditySeconds: 3 * day, PauseLimit: 2, PointsMilli: 5000},
		{Pesos: 20, Seconds: 6 * 60 * 60, ValiditySeconds: 14 * day, PauseLimit: 10, PointsMilli: 25000},
	}
	customPoints = []ledger.PointsTier{
		{PointsMilli: 3000, Seconds: 20 * 60, ValiditySeconds: day, PauseLimit: 1},
	}
)

// fixtureVendo is one Vendo List row; state picks how it is seeded and its status label.
type fixtureVendo struct {
	name, location, server, state string
	custom                        bool
	coins                         []uint32 // one settled Coin Window per entry, in pesos
}

var vendos = []fixtureVendo{
	{"Aling Nena Sari-Sari", "Purok 3, Brgy. San Isidro, Antipolo", "hs-lan", "online", true, []uint32{5, 1, 10, 5, 20, 1, 5, 10, 5, 1, 5}},
	{"Kuya Jun's Store", "Rizal St. cor. Mabini, Brgy. Poblacion", "hs-lan", "online", false, []uint32{1, 5, 5, 10, 1, 1, 5, 10, 5}},
	{"Tindahan ni Aling Rosa", "Brgy. Malanday, Marikina", "hs-lan", "offline", false, []uint32{5, 10, 1, 5, 5, 1, 10}},
	{"Ate Joy Computer Shop", "Gen. Luna St., Brgy. Sto. Niño, Pasig", "hs-shop", "offline", false, []uint32{10, 5, 5, 1}},
	{"Mang Ben Bakery", "Brgy. Bagong Silang, Caloocan", "hs-lan", "deactivated", false, []uint32{5, 1, 5, 10, 1}},
	{"Lola Carmen Variety", "Brgy. San Roque, Cainta", "hs-lan", "waiting", false, nil},
}

// voucherBatches are minted in order; redeem says how many of each are sold.
var voucherBatches = []struct {
	batch  ledger.VoucherBatch
	pesos  int64
	count  int
	redeem int
}{
	{ledger.VoucherBatch{TierLabel: "₱5 tier", Seconds: 60 * 60, ValiditySeconds: 3 * day, PauseLimit: 2}, 5, 20, 6},
	{ledger.VoucherBatch{Seller: "Aling Nena", TierLabel: "₱10 tier", Seconds: 150 * 60, ValiditySeconds: 7 * day, PauseLimit: 5}, 10, 10, 4},
	{ledger.VoucherBatch{Seller: "Kuya Jun", CodePrefix: "PISO", CodeLength: 6, Seconds: 3 * 60 * 60}, 20, 15, 2},
	{ledger.VoucherBatch{TierLabel: "₱1 tier", CodePreset: "digits", CodeLength: 6, Seconds: 10 * 60, ValiditySeconds: day}, 1, 30, 0},
}

// board is a paired fixture Vendo's report state: each report advances RSeq, Totals are cumulative.
type board struct {
	id               uint32
	nonce, rseq, sum uint32
}

func main() {
	empty := flag.Bool("empty", false, "seed only the admin and its session")
	flag.Parse()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, *empty); err != nil {
		log.Fatal(err)
	}
}

func run(ctx context.Context, empty bool) error {
	dir, err := os.MkdirTemp("", "openpiso-adminshots-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	store, err := ledger.Open(filepath.Join(dir, "openpiso.db"))
	if err != nil {
		return err
	}
	defer store.Close()
	if err := store.InitializeAdmin("admin-shots"); err != nil {
		return err
	}
	tokenBytes := make([]byte, 32)
	rand.Read(tokenBytes)
	token := base64.RawURLEncoding.EncodeToString(tokenBytes)
	if err := store.CreateAdminSession(token, time.Now(), time.Now().Add(24*time.Hour)); err != nil {
		return err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	server := &http.Server{
		Handler:           controller.Handler(ctx, store, "http://127.0.0.1", "ready", "", dir, "dev", netip.MustParsePrefix("127.0.0.0/8")),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go server.Serve(listener)
	defer server.Close()

	ids := []uint32{}
	if !empty {
		var online []*board
		var offlineAt time.Time
		ids, online, offlineAt, err = seed(ctx, store)
		if err != nil {
			return fmt.Errorf("seed fixture: %w", err)
		}
		go heartbeat(ctx, store, online)
		// Offline Vendos read Offline only once their last report is older than vendoOnline (10 s).
		select {
		case <-time.After(time.Until(offlineAt.Add(11 * time.Second))):
		case <-ctx.Done():
			return nil
		}
	}
	line, _ := json.Marshal(map[string]any{"base": "http://" + listener.Addr().String(), "cookie": token, "vendos": ids})
	fmt.Println(string(line))
	<-ctx.Done()
	return nil
}

// seed writes the populated fixture through ledger methods only. Every Sale is stamped now:
// the ledger has no way to record a coin or a redemption at an earlier time.
func seed(ctx context.Context, store *ledger.Ledger) (ids []uint32, online []*board, offlineAt time.Time, err error) {
	if err = errors.Join(
		store.SaveValiditySeconds(3*day),
		store.SavePauseSettings(ledger.PauseSettings{PauseLimit: 3, IdleEnabled: true, IdleSeconds: 300}),
		store.SaveRateTiers(rateTiers),
		store.SavePointsTiers(pointsTiers),
	); err != nil {
		return
	}
	device := 0
	nextDevice := func() (string, string) {
		device++
		return fmt.Sprintf("02:5e:00:00:%02x:%02x", device/256, device%256), fmt.Sprintf("10.5.50.%d", 10+device%190)
	}
	report := func(b *board, tag byte, window, coins uint32) error {
		b.rseq++
		b.sum += coins
		return store.RecordVendoReport(ctx, vendoprotocol.Message{Tag: tag, VendoID: b.id, Epoch: 1, Total: b.sum, WindowID: window, BootNonce: b.nonce, RSeq: b.rseq})
	}
	// Offline Vendos first, so their last report is the oldest.
	order := []int{}
	for _, state := range []string{"offline", "deactivated", "online", "waiting"} {
		for i, v := range vendos {
			if v.state == state {
				order = append(order, i)
			}
		}
	}
	ids = make([]uint32, len(vendos))
	boards := map[int]*board{}
	for _, i := range order {
		v := vendos[i]
		var vendo ledger.Vendo
		if vendo, err = store.PrepareVendo(fmt.Sprintf("C8:C9:A3:10:00:%02X", i+1)); err != nil {
			return
		}
		ids[i] = vendo.ID
		if err = store.EditVendo(vendo.ID, v.name, v.location, v.server, ""); err != nil {
			return
		}
		if v.state == "waiting" {
			continue
		}
		if err = errors.Join(store.StageVendoPairing(vendo.ID, fmt.Sprintf("10.5.50.%d", 201+i), v.server), store.SetVendoStatus(vendo.ID, "paired")); err != nil {
			return
		}
		if v.custom {
			if err = errors.Join(store.CustomizeVendoRates(vendo.ID), store.SaveVendoRateTiers(vendo.ID, customRates),
				store.CustomizeVendoPoints(vendo.ID), store.SaveVendoPointsTiers(vendo.ID, customPoints)); err != nil {
				return
			}
		}
		b := &board{id: vendo.ID, nonce: 0x5eed0000 + uint32(i)}
		boards[i] = b
		if err = report(b, vendoprotocol.Report, 0, 0); err != nil {
			return
		}
		for _, coins := range v.coins {
			mac, ip := nextDevice()
			if _, err = store.OpenCoinWindow(vendo.ID, mac, ip, ""); err != nil {
				return
			}
			var windows []ledger.CoinWindow
			if windows, err = store.CoinWindows(); err != nil {
				return
			}
			window := windows[len(windows)-1]
			if err = report(b, vendoprotocol.Closed, window.ID, coins); err != nil {
				return
			}
		}
		switch v.state {
		case "offline":
			offlineAt = time.Now()
		case "deactivated":
			err = store.SetVendoActive(vendo.ID, false)
		case "online":
			online = append(online, b)
		}
		if err != nil {
			return
		}
	}
	// Coins with no open Coin Window: one pending audit item for review.
	if err = report(online[len(online)-1], vendoprotocol.Report, 0xdead, 5); err != nil {
		return
	}
	for _, spec := range voucherBatches {
		batch := spec.batch
		batch.Pesos = &spec.pesos
		var vouchers []ledger.Voucher
		if _, vouchers, err = store.CreateVoucherBatch(batch, spec.count); err != nil {
			return
		}
		for _, voucher := range vouchers[:spec.redeem] {
			mac, ip := nextDevice()
			if _, err = store.RedeemVoucher(voucher.Code, mac, ip, ""); err != nil {
				return
			}
		}
	}
	return
}

// heartbeat keeps the online Vendos within vendoOnline, as their 2 s board heartbeats would.
func heartbeat(ctx context.Context, store *ledger.Ledger, boards []*board) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			for _, b := range boards {
				b.rseq++
				if err := store.RecordVendoReport(ctx, vendoprotocol.Message{Tag: vendoprotocol.Report, VendoID: b.id, Epoch: 1, Total: b.sum, BootNonce: b.nonce, RSeq: b.rseq}); err != nil {
					log.Printf("heartbeat Vendo %d: %s", b.id, err)
				}
			}
		}
	}
}
