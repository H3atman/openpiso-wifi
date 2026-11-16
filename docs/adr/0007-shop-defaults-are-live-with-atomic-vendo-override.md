# Shop defaults are live, with atomic per-Vendo override

One Controller serves one Shop, and the Shop holds the default Rate tiers and Points table. The default is **live**: a Vendo without its own set reads the Shop's at each Coin Window and each Points redemption, so editing the Shop reprices every such Vendo from its next Coin Window while an open window keeps the tiers it snapshotted. An override is **atomic**: Customize gives the Vendo a full copy of the current Shop set to edit, Use Shop tiers discards it whole, and no field falls back to the Shop on its own. There is one editor and one stored setting for the Shop default; a Vendo's Custom set is the only other place tiers live ([research](../research/shop-rates-default-and-per-vendo-override.md) §5; ADR 0006 for what a tier carries).

## Considered Options

- **Copy the Shop set into each Vendo at pairing.** Rejected: "change the Shop's prices" would then mean editing every Vendo, and identical sets drift apart.
- **Per-field inheritance** (Shop time with a Custom Validity, say). Rejected: it cannot be explained in the Vendo List or on the Portal, and the atomic model already covers the KiTifi-parity cases.
- **Named rate plans shared by several Vendos.** Not now: nothing in the field shows the need; many identical Custom sets would be the signal to add them.
