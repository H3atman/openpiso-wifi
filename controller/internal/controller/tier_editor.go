package controller

// tierEditor is the dot of the "tier_editor" partial (web/templates/tier_editor.html):
// the structured Rate tier or Points row editor. The Vendo dialog renders it per
// Vendo; a Shop page renders it with its own Action, Hint and SaveLabel and no VendoID.
type tierEditor struct {
	Kind      string          // "rates" or "points": data-tiers, the t-<kind> class and the Add label
	Action    string          // the form's POST target
	VendoID   uint32          // posted as id when nonzero
	Rates     []rateTierRow   // seed rows when Kind is "rates"
	Points    []pointsTierRow // seed rows when Kind is "points"
	Hint      string          // the line under Add
	SaveLabel string
}

// RatesEditor is the Vendo dialog's Rates tab editor, seeded with the Vendo's own tiers.
func (row vendoRow) RatesEditor() tierEditor {
	return tierEditor{Kind: "rates", Action: "/admin/vendos/rates", VendoID: row.ID, Rates: row.Rates,
		Hint: "Customers get the highest tier their coins reach. A change applies from the next Coin Window.", SaveLabel: "Save tiers"}
}

// PointsEditor is the Vendo dialog's Points tab editor, seeded with the Vendo's own table.
func (row vendoRow) PointsEditor() tierEditor {
	return tierEditor{Kind: "points", Action: "/admin/vendos/points", VendoID: row.ID, Points: row.Points,
		Hint: "An empty table means customers cannot redeem Points here.", SaveLabel: "Save Points table"}
}
