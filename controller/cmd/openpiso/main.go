package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/controller"
	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
	"github.com/H3atman/openpiso-wifi/controller/internal/routeros"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "update" {
		if err := runUpdate(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, "openpiso update:", err)
			os.Exit(1)
		}
		return
	}
	listen := flag.String("listen", "127.0.0.1:8080", "HTTP listen address")
	dataDir := flag.String("data", ".", "Controller data directory")
	portalURL := flag.String("portal-url", "http://127.0.0.1:8080", "URL Devices use to reach the portal")
	adminCIDR := flag.String("admin-network", "127.0.0.0/8", "management network allowed to access admin routes")
	initOnly := flag.Bool("init-only", false, "initialize the ledger and admin password without starting HTTP")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}
	adminNetwork, err := netip.ParsePrefix(*adminCIDR)
	if err != nil {
		log.Fatal(fmt.Errorf("parse admin network: %w", err))
	}

	if err := os.MkdirAll(*dataDir, 0o700); err != nil {
		log.Fatal(err)
	}
	store, err := ledger.Open(filepath.Join(*dataDir, "openpiso.db"))
	if err != nil {
		log.Fatal(err)
	}
	defer store.Close()
	if err := store.InitializeAdmin(os.Getenv("OPENPISO_ADMIN_PASSWORD")); err != nil {
		log.Fatal(err)
	}
	os.Unsetenv("OPENPISO_ADMIN_PASSWORD")
	if *initOnly {
		return
	}

	routerStatus, routerError := "", ""
	config, configured, err := store.RouterConfig()
	if err != nil {
		log.Fatal(err)
	}
	if configured {
		client, err := routeros.NewClient(config.Address, config.Username, config.APISecret, routeros.Certificate{
			DER:         config.Certificate,
			Fingerprint: config.Fingerprint,
		})
		if err == nil {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			err = adoptAllIfPending(ctx, store, client)
			var adopted []string
			if err == nil {
				adopted, err = store.AdoptedHotspotServers()
			}
			var settings ledger.PauseSettings
			if err == nil {
				settings, err = store.PauseSettings()
			}
			if err == nil {
				err = client.EnsureBaseline(ctx, *portalURL, settings.IdleSeconds, adopted)
			}
			var use ledger.ProfileUse
			if err == nil && len(adopted) > 0 {
				use, err = store.HotspotProfileUse()
				if err == nil {
					err = client.EnsurePauseSettings(ctx, settings.IdleEnabled, settings.IdleSeconds, use.TimeCredits, use.Profiles)
				}
			}
			client.Close()
			cancel()
		}
		if err != nil {
			routerStatus = "error"
			routerError = err.Error()
			log.Printf("Router baseline verification failed: %s", err)
		} else {
			routerStatus = "ready"
		}
	}

	log.Printf("Controller listening on %s (portal %s)", *listen, *portalURL)
	server := &http.Server{
		Addr:              *listen,
		Handler:           controller.Handler(context.Background(), store, *portalURL, routerStatus, routerError, *dataDir, version, adminNetwork),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	if err := server.ListenAndServe(); err != nil {
		log.Fatal(fmt.Errorf("serve Controller: %w", err))
	}
}

// adoptAllIfPending Adopts every HotSpot server on the Router when the ledger
// predates Router setup, since that Controller managed them all (ADR 0011).
func adoptAllIfPending(ctx context.Context, store *ledger.Ledger, client *routeros.Client) error {
	pending, err := store.AdoptAllPending()
	if err != nil || !pending {
		return err
	}
	servers, err := client.HotspotServers(ctx)
	if err != nil {
		return fmt.Errorf("list HotSpot servers: %w", err)
	}
	return store.AdoptHotspotServers(servers)
}
