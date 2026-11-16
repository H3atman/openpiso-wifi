package controller

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"mime"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/H3atman/openpiso-wifi/controller/internal/ledger"
)

const salesDateLayout = "2006-01-02"

// localMidnight is 00:00 of t's calendar day in the Controller's system timezone.
// All Sales period boundaries start here, so "today" matches the owner's day.
func localMidnight(t time.Time) time.Time {
	t = t.In(time.Local)
	return time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.Local)
}

// monthStart is 00:00 on the 1st of t's month, local time.
func monthStart(t time.Time) time.Time {
	return time.Date(t.Year(), t.Month(), 1, 0, 0, 0, 0, time.Local)
}

type salesPeriod struct {
	Label    string
	From, To time.Time // inclusive local calendar days
}

// salesPeriods supplies both the Dashboard tiles and Sales shortcuts.
func salesPeriods(now time.Time) []salesPeriod {
	today := localMidnight(now)
	return []salesPeriod{
		{"Today", today, today},
		{"This week", today.AddDate(0, 0, -((int(today.Weekday()) + 6) % 7)), today},
		{"This month", monthStart(today), today},
		{"This year", time.Date(today.Year(), time.January, 1, 0, 0, 0, 0, time.Local), today},
	}
}

// salesView is the Sales page data and, flattened, the ?format=json body.
// From and To are the inclusive local days the owner picked.
type salesView struct {
	View string `json:"-"`
	From string `json:"from"`
	To   string `json:"to"`
	ledger.SalesPage
	TotalPesos int64           `json:"total_pesos"`
	VendoID    uint32          `json:"vendo_id"`
	Seller     string          `json:"seller"`
	Sellers    []string        `json:"-"`
	Search     string          `json:"q"`
	Vendos     []salesVendo    `json:"-"`
	Shortcuts  []salesShortcut `json:"-"`
}

type salesShortcut struct {
	Label string
	URL   string
}

type salesVendo struct {
	ID    uint32
	Label string
}

// salesPage serves GET /admin/sales. From and To are YYYY-MM-DD local days, both
// inclusive: the queried period is [From 00:00, To+1 00:00) in the Controller's
// timezone. It defaults to the 1st of this month through today.
func (s *Server) salesPage(response http.ResponseWriter, request *http.Request) {
	today := localMidnight(time.Now())
	from := monthStart(today)
	to := today
	var fromErr, toErr error
	if value := request.URL.Query().Get("from"); value != "" {
		from, fromErr = time.ParseInLocation(salesDateLayout, value, time.Local)
	}
	if value := request.URL.Query().Get("to"); value != "" {
		to, toErr = time.ParseInLocation(salesDateLayout, value, time.Local)
	}
	if fromErr != nil || toErr != nil || from.After(to) {
		http.Error(response, "choose valid From and To dates (YYYY-MM-DD) with From not after To", http.StatusBadRequest)
		return
	}
	page, size := 1, 25
	var err error
	if value := request.URL.Query().Get("page"); value != "" {
		page, err = strconv.Atoi(value)
		if err != nil || page < 1 {
			http.Error(response, "choose a positive page", http.StatusBadRequest)
			return
		}
	}
	if value := request.URL.Query().Get("page_size"); value != "" {
		size, err = strconv.Atoi(value)
		if err != nil || (size != 25 && size != 50 && size != 100) {
			http.Error(response, "choose page size 25, 50 or 100", http.StatusBadRequest)
			return
		}
	}
	vendos, err := s.ledger.Vendos()
	if err != nil {
		http.Error(response, "read Vendos", http.StatusInternalServerError)
		return
	}
	sort.SliceStable(vendos, func(i, j int) bool {
		return vendos[i].Status != "unpaired" && vendos[j].Status == "unpaired"
	})
	var vendoID uint32
	if value := request.URL.Query().Get("vendo"); value != "" && value != "all" {
		id, err := strconv.ParseUint(value, 10, 32)
		found := false
		for _, vendo := range vendos {
			if uint64(vendo.ID) == id {
				found = true
			}
		}
		if err != nil || !found {
			http.Error(response, "choose a known Vendo or All", http.StatusBadRequest)
			return
		}
		vendoID = uint32(id)
	}
	mode := request.URL.Query().Get("view")
	if mode == "" {
		mode = "table"
	}
	if mode != "table" && mode != "feed" {
		http.Error(response, "choose table or feed view", http.StatusBadRequest)
		return
	}
	seller := request.URL.Query().Get("seller")
	search := request.URL.Query().Get("q")
	filter := ledger.SalesFilter{From: from, To: to.AddDate(0, 0, 1), VendoID: vendoID, Seller: seller, Search: search, Page: page, PageSize: size}
	if request.URL.Path == "/admin/sales/export" {
		s.salesExport(response, request, filter, from.Format(salesDateLayout), to.Format(salesDateLayout))
		return
	}
	s.voucherMu.Lock()
	report, err := s.ledger.QuerySales(filter)
	s.voucherMu.Unlock()
	if err != nil {
		http.Error(response, "read sales report", http.StatusInternalServerError)
		return
	}
	view := salesView{View: mode, From: from.Format(salesDateLayout), To: to.Format(salesDateLayout), SalesPage: report,
		TotalPesos: report.Total(), VendoID: vendoID, Seller: seller, Search: search}
	view.Sellers, err = s.ledger.VoucherSellers()
	if err != nil {
		http.Error(response, "read Sellers", http.StatusInternalServerError)
		return
	}
	// Preserve an explicitly requested label even if its last batch was deleted.
	if seller != "" && seller != "Shop" {
		found := false
		for _, label := range view.Sellers {
			if label == seller {
				found = true
			}
		}
		if !found {
			view.Sellers = append(view.Sellers, seller)
		}
	}
	for _, vendo := range vendos {
		view.Vendos = append(view.Vendos, salesVendo{ID: vendo.ID, Label: vendo.DisplayName()})
	}
	for _, period := range salesPeriods(today) {
		query := view.filterQuery()
		query.Set("from", period.From.Format(salesDateLayout))
		query.Set("to", period.To.Format(salesDateLayout))
		view.Shortcuts = append(view.Shortcuts, salesShortcut{Label: period.Label, URL: "/admin/sales?" + query.Encode()})
	}
	if request.URL.Query().Get("format") == "json" {
		response.Header().Set("Content-Type", "application/json")
		json.NewEncoder(response).Encode(view)
		return
	}
	if isFragmentRequest(request) {
		renderFragment(response, "sales", "sales_report", view)
		return
	}
	s.renderPage(response, request, "sales", Page{Title: "Sales Inventory", Section: "Main", Active: "/admin/sales", Data: view})
}

func (v salesView) First() int64 {
	if v.Count == 0 {
		return 0
	}
	return (int64(v.Page)-1)*int64(v.PageSize) + 1
}

func (v salesView) Last() int64 {
	if v.Count == 0 {
		return 0
	}
	return v.First() + int64(len(v.Rows)) - 1
}

type salesTableRow struct {
	ledger.Sale
	Number int64
}

func (v salesView) TableRows() []salesTableRow {
	rows := make([]salesTableRow, 0, len(v.Rows))
	for index, row := range v.Rows {
		rows = append(rows, salesTableRow{Sale: row, Number: v.First() + int64(index)})
	}
	return rows
}

func (r salesTableRow) ShortUser() string {
	if r.Type == "coin" && len(r.User) > 8 {
		return r.User[:8] + "…"
	}
	return r.User
}

type salesPageLink struct {
	Number  int
	URL     string
	Current bool
}

// Keep navigation bounded even for a large historical ledger.
func (v salesView) Pages() []salesPageLink {
	last := max(int((v.Count+int64(v.PageSize)-1)/int64(v.PageSize)), 1)
	links := []salesPageLink{}
	for page := 1; page <= last; page++ {
		if page > 1 && page < v.Page-2 {
			page = v.Page - 2
		}
		if page > v.Page+2 && page < last {
			page = last
		}
		query := v.filterQuery()
		query.Set("page", strconv.Itoa(page))
		links = append(links, salesPageLink{Number: page, URL: "/admin/sales?" + query.Encode(), Current: page == v.Page})
	}
	return links
}

func (v salesView) filterQuery() url.Values {
	query := url.Values{"from": {v.From}, "to": {v.To}, "view": {v.View}, "page_size": {strconv.Itoa(v.PageSize)}}
	if v.VendoID != 0 {
		query.Set("vendo", strconv.FormatUint(uint64(v.VendoID), 10))
	}
	if v.Seller != "" {
		query.Set("seller", v.Seller)
	}
	if v.Search != "" {
		query.Set("q", v.Search)
	}
	return query
}

func (v salesView) ExportURL() string {
	query := v.filterQuery()
	query.Del("view")
	query.Del("page_size")
	return "/admin/sales/export?" + query.Encode()
}

func (s *Server) salesExport(response http.ResponseWriter, request *http.Request, filter ledger.SalesFilter, from, to string) {
	// Finish the read and close its connection before a slow download can block.
	var output bytes.Buffer
	output.WriteString("\xef\xbb\xbf")
	writer := csv.NewWriter(&output)
	if err := writer.Write([]string{"#", "Date", "Time", "IP", "MAC", "User", "Validity", "Type", "Vendo", "seller", "Credited time (seconds)", "Amount (PHP)"}); err != nil {
		http.Error(response, "build sales export", http.StatusInternalServerError)
		return
	}
	number := 0
	err := s.ledger.ExportSales(request.Context(), filter, func(row ledger.Sale) error {
		number++
		kind := "Coin"
		if row.Type == "voucher" {
			kind = "Voucher"
		}
		amount := ""
		if row.Amount != nil {
			amount = strconv.FormatInt(*row.Amount, 10)
		}
		record := []string{strconv.Itoa(number), row.At.Format(salesDateLayout), row.At.Format("15:04:05"), row.IP, row.MAC, row.User, row.Validity, kind, row.VendoName, row.Seller}
		for index, cell := range record {
			// Quoting CSV is not enough: spreadsheets still evaluate formulas.
			trimmed := strings.TrimLeft(cell, " \t\r\n")
			if strings.HasPrefix(cell, "\t") || strings.HasPrefix(cell, "\r") || strings.HasPrefix(cell, "\n") || (trimmed != "" && strings.ContainsAny(trimmed[:1], "=+-@")) {
				record[index] = "'" + cell
			}
		}
		return writer.Write(append(record, strconv.FormatInt(row.CreditedSeconds, 10), amount))
	})
	writer.Flush()
	if err != nil || writer.Error() != nil {
		http.Error(response, "build sales export", http.StatusInternalServerError)
		return
	}
	response.Header().Set("Content-Type", "text/csv; charset=utf-8")
	response.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": "sales-" + from + "-to-" + to + ".csv"}))
	response.Header().Set("Content-Length", strconv.Itoa(output.Len()))
	response.Write(output.Bytes())
}

// ViewURL changes presentation without changing the selected report or page.
func (v salesView) ViewURL(mode string) string {
	query := v.filterQuery()
	query.Set("view", mode)
	query.Set("page", strconv.Itoa(v.Page))
	return "/admin/sales?" + query.Encode()
}

type salesDay struct {
	Date string
	Rows []salesTableRow
}

func (v salesView) Days() []salesDay {
	var days []salesDay
	for _, row := range v.TableRows() {
		date := row.At.Format("Monday, 02 January 2006")
		if len(days) == 0 || days[len(days)-1].Date != date {
			days = append(days, salesDay{Date: date})
		}
		index := len(days) - 1
		days[index].Rows = append(days[index].Rows, row)
	}
	return days
}
