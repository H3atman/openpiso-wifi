# A Rate tier sets Validity, Pause Limit and Points

Amended by [ADR 0008](0008-voucher-batch-carries-purchase-terms.md): Vouchers take Validity and Pause Limit from their Voucher Batch snapshot.

Validity and Pause Limit were site-set: one value for every Top-up. Operators moving from KiTifi price these per Vendo and per rate, so a ₱30 purchase can carry a one-day Validity and 15 Pauses while ₱1 carries one Pause, and coins earn Points that buy time later ([research](../research/vendo-list-management.md) §4). We now let each Top-up carry its own values: a coin Top-up takes them from the Rate tier that priced its Coin Window, a Points redemption from the Points row redeemed, and Vouchers and admin grants from the site values. A Vendo uses the site's Rate tiers and Points table unless the Operator gives it its own.

**Rules.**

1. **Validity never shortens.** Every Top-up sets the deadline to the later of the current one and now + its Validity. "No limit" is the longest Validity the ledger allows.
2. **The latest Top-up sets the Pause Limit**, even when it lowers it. The Pause count already resets on each Top-up, so the limit only ever governs the stretch until the next one.
3. **Points live on the Time Credit and outlive its Validity.** When a coin or Voucher Top-up arrives after Validity ended, the Controller still creates a replacement Time Credit instead of reviving the expired one, and the Points move to that replacement in the same transaction. Points follow Rebind. A customer who enters an expired Resume Code that still holds Points gets it Rebound, so their next purchase carries them. Redeeming Points on an expired Time Credit takes the same replacement path before deducting.
4. **Points are earned only by coins** (coins × tier Points ÷ tier pesos, in thousandths) and **redeeming them is never Sales**. Earned Points pool across Vendos. Redemption uses the Points table of the Vendo serving the Device's HotSpot server, or the site table when none does.

## Considered Options

- **Keep Validity and Pause Limit site-set and skip Points.** Rejected: it blocks KiTifi parity for existing sites, which is the point of the Vendo List.
- **Latest Top-up also sets Validity, even if shorter.** Rejected: a ₱1 coin after a ₱30 day pass would cut the customer's day short.
- **Points forfeit with Validity.** Rejected by Rafael: Points are a reason to come back, and losing them with the time defeats that.
- **Revive the expired Time Credit instead of carrying Points to a replacement.** Rejected: forfeited time must stay forfeited, and the replacement path already exists.

## Consequences

- [ADR 0003](0003-router-is-sole-meter.md) holds. Redemption is a Top-up of bought time through the normal path. The Controller never counts Points down as time.
- The Rate tier snapshot a Coin Window takes when it opens now carries Validity, Pause Limit and Points, so editing rates never changes a live window.
- [ADR 0004](0004-portal-is-operator-editable-static-files-over-json-api.md): the Portal's `rates` become the serving Vendo's rates, and Points add a balance, a table and a redeem action. `docs/portal-contract.md` records the change.
