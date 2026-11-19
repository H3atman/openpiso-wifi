package ledger

import "fmt"

// NextSteps is which of the Shop's remaining setup tasks are done, as Router
// setup lists them once the Router is ready. None of them is required.
type NextSteps struct {
	ShopSettings bool // the Pause settings have been saved
	RateTiers    bool // the Operator has saved the Rate tiers, even at the shipped ₱1 = 60 s tier
	VendoPaired  bool // a Vendo is paired
	VoucherBatch bool // a Voucher Batch exists
}

// NextSteps reads each task from the ledger.
func (l *Ledger) NextSteps() (NextSteps, error) {
	var steps NextSteps
	if err := l.db.QueryRow(`SELECT
		EXISTS (SELECT 1 FROM settings WHERE key = 'pause_settings'),
		EXISTS (SELECT 1 FROM settings WHERE key = 'rate_tiers_saved'),
		EXISTS (SELECT 1 FROM vendos WHERE status = 'paired'),
		EXISTS (SELECT 1 FROM voucher_batches)`).Scan(&steps.ShopSettings, &steps.RateTiers, &steps.VendoPaired, &steps.VoucherBatch); err != nil {
		return steps, fmt.Errorf("read Next steps: %w", err)
	}
	if steps.RateTiers {
		return steps, nil
	}
	// A ledger saved before the marker existed shows a save as tiers that differ from the seed.
	tiers, err := l.RateTiers()
	if err != nil {
		return steps, err
	}
	steps.RateTiers = len(tiers) != 1 || tiers[0].Pesos != 1 || tiers[0].Seconds != 60
	return steps, nil
}
