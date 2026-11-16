# A Voucher Batch carries its purchase terms

Printed Vouchers must not change price or benefits when the Operator edits a Rate tier or the Shop's Voucher settings. A Voucher Batch therefore snapshots price, time, Validity and Pause Limit when generated: a Shop Rate tier supplies all four, while Custom requires a price (including ₱0) and time and takes the Shop's Voucher Validity and Pause Limit. This amends [ADR 0006](0006-rate-tier-sets-validity-pause-limit-and-points.md): Voucher redemption reads the batch, not the current Shop settings, and still earns no Points.

## Considered Options

- **Read current tier or Shop settings at redemption.** Rejected: a printed card's benefits could change after sale, and deleting a tier would leave its Vouchers without terms.
- **Keep Voucher Validity and Pause Limit site-set.** Rejected: a Voucher sold at a tier's price should carry that tier's benefits, just as a coin purchase does.

## Consequences

- Editing or removing a Rate tier does not change an existing batch. The tier label is a snapshot, not a live reference.
- Existing batches are numbered in creation order and remain Custom with their original price, or no price if it was unknown. Their Validity and Pause Limit are filled from the Shop's Voucher settings at upgrade. The migration only adds columns; it does not rebuild or reprice old batches.
- Each batch receives a sequential number for people; its existing token remains the URL key. Equal creation timestamps are ordered by insertion order.
- Redemption remains a normal Top-up: Validity never shortens, the latest Top-up resets Pauses and sets the Pause Limit, and the Router remains the sole meter (ADR 0003).
