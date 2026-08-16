"""STD conformance — bench --site <site> execute periodic_valuation.tests.smoke_std.run

Replays the audited 28-event MTD walkthrough (std_mtd_walkthrough.xlsx) through
the Frappe StdEngine and asserts every Settlement Table anchor to the penny,
plus GL invariants. Rolled back unless commit=True.
"""

import frappe
from frappe.utils import flt

from periodic_valuation.tests.smoke_kernel import ensure_masters, get_company

CHECKS = []
ITEM = "_STD-LIMESTONE"


def _fy_covered(fy):
	"""A differently-named Fiscal Year may already span this year on a shared
	site (e.g. `_Test Fiscal Year 2025` from framework fixtures). Creating our
	own would overlap and be rejected, so reuse whatever is already there."""
	return bool(frappe.db.exists("Fiscal Year", {
		"year_start_date": ("<=", f"{fy}-01-01"),
		"year_end_date": (">=", f"{fy}-12-31"),
	}))

def check(label, ok, detail=""):
	CHECKS.append((label, bool(ok)))
	print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail and not ok else ""))


def ensure_std_masters(company):
	for fy in ("2025", "2026"):
		if not frappe.db.exists("Fiscal Year", fy) and not _fy_covered(fy):
			frappe.get_doc({"doctype": "Fiscal Year", "year": fy,
				"year_start_date": f"{fy}-01-01", "year_end_date": f"{fy}-12-31"}).insert(
				ignore_permissions=True)
			frappe.get_doc({"doctype": "Fiscal Year Company", "parent": fy,
				"parenttype": "Fiscal Year", "parentfield": "companies",
				"company": company}).insert(ignore_permissions=True)

	if not frappe.db.exists("Periodic Standard Cost Settings", {"company": company}):
		def acc(hint, root="Expense"):
			rows = frappe.get_all("Account", filters={"company": company, "is_group": 0,
				"account_name": ("like", f"%{hint}%")}, limit=1, pluck="name")
			return rows[0] if rows else frappe.get_all("Account",
				filters={"company": company, "is_group": 0, "root_type": root},
				limit=1, pluck="name")[0]
		frappe.get_doc({
			"doctype": "Periodic Standard Cost Settings", "company": company,
			"ppv_account": acc("Cost of Goods"),
			"std_reval_reserve_account": acc("Stock Adjustment", "Liability"),
			"cogs_adjustment_account": acc("Miscellaneous"),
			"customer_cogs_account": acc("Cost of Goods"),
			"fy_carry_forward_account": acc("Temporary", "Liability"),
			"default_settlement_view": "MTD",
		}).insert(ignore_permissions=True)

	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({
			"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD",
		}).insert(ignore_permissions=True)


def make_scv(company, year, month, sc, release=True):
	scv = frappe.get_doc({
		"doctype": "Item Standard Cost Version", "company": company, "item_code": ITEM,
		"valid_from_year": year, "valid_from_month": month, "standard_cost": sc,
		"source_type": "MANUAL_OVERRIDE",
	})
	scv.insert(ignore_permissions=True)
	return scv


def run(commit=False):
	global ITEM
	from periodic_valuation.periodic_standard_cost.engine import SettReverseRuleViolation, StdEngine

	ensure_masters()
	company = get_company()
	ensure_std_masters(company)

	scv10 = make_scv(company, 2025, 12, 10)
	scv10.flags.via_release_flow = True
	scv10.status = "RELEASED"
	scv10.save(ignore_permissions=True)

	eng = StdEngine(company, ITEM)
	check("engine view MTD", eng.view == "MTD", eng.view)
	src = ("Item Standard Cost Version", scv10.name)

	# ---- Dec 2025 (SC $10)
	eng.post(trans="Rec", qty=1000, sc=10, ac=12, posting_date="2025-12-13", source=src)
	eng.post(trans="Iss", qty=300, sc=10, posting_date="2025-12-20", source=src)

	# ---- early Jan 2026 (SC $10)
	eng.post(trans="Rec", qty=500, sc=10, ac=10, posting_date="2026-01-03", source=src)
	eng.post(trans="Iss", qty=100, sc=10, posting_date="2026-01-08", source=src)

	# ---- boundary reval Jan 9 ($10 -> $11): release SCV11 via the real flow?
	# For date-exact conformance we post the triplet directly (the SCV release
	# flow is exercised separately below).
	scv11 = make_scv(company, 2026, 1, 11)
	scv11.flags.via_release_flow = True
	scv11.status = "RELEASED"
	scv11.supersedes_version = scv10.name
	scv11.save(ignore_permissions=True)
	frappe.db.set_value("Item Standard Cost Version", scv10.name, "status", "SUPERSEDED")
	src11 = ("Item Standard Cost Version", scv11.name)
	eng.post(trans="Rev Beg", posting_date="2026-01-09", source=src11, ref="R1",
		sc=11, ac=10, t_sc_override=700, entry_date="2026-01-09")
	eng.post(trans="REV In", posting_date="2026-01-09", source=src11, ref="R1",
		sc=11, ac=10, t_sc_override=500, entry_date="2026-01-09")
	eng.post(trans="REV out", posting_date="2026-01-09", source=src11, ref="R1",
		sc=11, ac=10, t_sc_override=-100, entry_date="2026-01-09")

	# ---- Dec close (run Jan 10)
	s_dec = eng.close_period(year=2025, month=12, sc=10, source=src,
		entry_date="2026-01-10", ref="8")
	check("Dec close 1400/600", flt(s_dec.es_var, 2) == 1400.00 and flt(s_dec.out_var, 2) == 600.00,
		f"{s_dec.es_var}/{s_dec.out_var}")

	# ---- Jan activity
	eng.post(trans="SC+", qty=30, sc=11, posting_date="2026-01-17", source=src11)
	eng.post(trans="PR", qty=80, sc=11, ac=12, posting_date="2026-01-20", source=src11)
	eng.post(trans="SR", qty=40, sc=11, posting_date="2026-01-22", source=src11)
	eng.post(trans="LC", posting_date="2026-01-24", source=src11,
		t_sc_override=0, t_ac_override=600)
	# E14: same-month backdate -> plain Iss per the label rule (flags identical)
	eng.post(trans="Iss", qty=20, sc=11, posting_date="2026-01-18", source=src11,
		entry_date="2026-01-28")
	eng.post(trans="Iss", qty=15, sc=11, posting_date="2026-01-30", source=src11)

	# ---- Feb activity
	eng.post(trans="SC-", qty=10, sc=11, posting_date="2026-02-03", source=src11)
	eng.post(trans="PR", qty=50, sc=11, ac=13, posting_date="2026-02-05", source=src11)
	eng.post(trans="Rec", qty=80, sc=11, ac=14, posting_date="2026-02-07", source=src11)

	# ---- Jan close (run Feb 15)
	s_jan = eng.close_period(year=2026, month=1, sc=11, source=src11,
		entry_date="2026-02-15", ref="19")
	check("Jan close 678.21/41.79",
		flt(s_jan.es_var, 2) == 678.21 and flt(s_jan.out_var, 2) == 41.79,
		f"{s_jan.es_var}/{s_jan.out_var}")

	# ---- period lock: plain posting into settled Jan must be refused
	try:
		eng.post(trans="Iss", qty=1, sc=11, posting_date="2026-01-25", source=src11,
			entry_date="2026-02-16")
		check("settled period locked", False, "posted")
	except frappe.ValidationError:
		check("settled period locked", True)

	# ---- Sett-Reverse Jan + backdated issue + re-close
	eng.sett_reverse(s_jan.name, source=src11, entry_date="2026-02-18")
	eng.post(trans="Issue (BD)", qty=25, sc=11, posting_date="2026-01-20", source=src11,
		entry_date="2026-02-18")
	s_jan2 = eng.close_period(year=2026, month=1, sc=11, source=src11,
		entry_date="2026-02-20", ref="24")
	check("Jan re-close 662.14/57.86",
		flt(s_jan2.es_var, 2) == 662.14 and flt(s_jan2.out_var, 2) == 57.86,
		f"{s_jan2.es_var}/{s_jan2.out_var}")

	# ---- Feb late receipt (negative PPV) + Feb close
	eng.post(trans="Rec", qty=40, sc=11, ac=9, posting_date="2026-02-22", source=src11)
	s_feb = eng.close_period(year=2026, month=2, sc=11, source=src11,
		entry_date="2026-03-02", ref="27")
	check("Feb close 715.58/6.56",
		flt(s_feb.es_var, 2) == 715.58 and flt(s_feb.out_var, 2) == 6.56,
		f"{s_feb.es_var}/{s_feb.out_var}")

	# ---- Sett-Reverse rule: Dec from a March date must raise
	try:
		eng.sett_reverse(s_dec.name, source=src, entry_date="2026-03-02")
		check("Dec reverse from Mar blocked", False, "reversed")
	except (SettReverseRuleViolation, frappe.ValidationError):
		check("Dec reverse from Mar blocked", True)

	# ---- rollups
	check("Dec end 700", flt(eng.end_qty_mtd(2025, 12)) == 700, eng.end_qty_mtd(2025, 12))
	check("Jan end 1030", flt(eng.end_qty_mtd(2026, 1)) == 1030, eng.end_qty_mtd(2026, 1))
	check("Feb end 1090", flt(eng.end_qty_mtd(2026, 2)) == 1090, eng.end_qty_mtd(2026, 2))

	# ---- GL invariants over this item's events
	gl = frappe.db.sql(
		"""SELECT g.account, SUM(g.debit) d, SUM(g.credit) c
		FROM `tabGL Entry` g JOIN `tabInventory Valuation Event` ive ON ive.name = g.valuation_event_id
		WHERE ive.item_code = %s AND g.is_cancelled = 0 GROUP BY g.account""",
		(ITEM,), as_dict=True,
	)
	net = flt(sum(flt(x.d) - flt(x.c) for x in gl), 2)
	check("trial balance nets to zero", net == 0, str(net))
	neg = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabGL Entry` g
		JOIN `tabInventory Valuation Event` ive ON ive.name = g.valuation_event_id
		WHERE ive.item_code = %s AND (g.debit < 0 OR g.credit < 0)""", (ITEM,))[0][0]
	check("no negative GL cells", neg == 0, str(neg))

	from periodic_valuation.shared.accounts import get_inventory_account
	stock_acct = get_inventory_account(company, ITEM, None)
	stock_net = flt(next((flt(x.d) - flt(x.c) for x in gl if x.account == stock_acct), 0), 2)
	check("Stock In Hand = 1090 x 11", stock_net == 11990.00, str(stock_net))

	# ---- SCV release flow posts the reval triplet automatically
	item2 = "_STD-RELEASE"
	if not frappe.db.exists("Item", item2):
		frappe.get_doc({"doctype": "Item", "item_code": item2, "item_name": item2,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "MTD"}).insert(ignore_permissions=True)
	orig_item = ITEM
	ITEM = item2
	try:
		# The triplet posts at nowdate() (DR-23), so the scenario must live in
		# the current month — hardcoded months go stale when the calendar rolls.
		from frappe.utils import getdate, nowdate
		today = getdate(nowdate())
		prev_y, prev_m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
		v1 = make_scv(company, prev_y, prev_m, 10)
		v1.release()
		e2 = StdEngine(company, item2)
		s2 = ("Item Standard Cost Version", v1.name)
		e2.post(trans="Rec", qty=100, sc=10, ac=10, posting_date=str(today), source=s2)
		e2.post(trans="Iss", qty=30, sc=10, posting_date=str(today), source=s2)
		v2 = make_scv(company, today.year, today.month, 12)
		v2.release()
		trips = frappe.get_all("Inventory Valuation Event",
			filters={"item_code": item2, "std_trans": ("in", ["Rev Beg", "REV In", "REV out"])},
			fields=["std_trans", "total_sc"])
		by = {t.std_trans: flt(t.total_sc, 2) for t in trips}
		check("SCV release triplet (In +200, out -60)",
			by.get("REV In") == 200.00 and by.get("REV out") == -60.00 and "Rev Beg" not in by,
			str(by))
	finally:
		ITEM = orig_item

	run_ytd(company)

	failed = [x for x in CHECKS if not x[1]]
	print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
	if commit and not failed:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	if failed:
		raise Exception("STD conformance failures: " + "; ".join(x[0] for x in failed))


YTD_ITEM = "_STD-YTD-LIME"

# (ref, ent, pst, trans, qty, sc, ac, t_sc_override, t_ac_override) — verbatim
# from badia_docs walkthrough_scenario.py (kernel-driven v4)
E = None
YTD_EVENTS = [
	("", "2025-12-13", "2025-12-13", "Rec", 1000, 10, 12, E, E),
	("", "2025-12-20", "2025-12-20", "Iss", 300, 10, E, E, E),
	("", "2026-01-03", "2026-01-03", "Rec", 500, 10, 10, E, E),
	("", "2026-01-08", "2026-01-08", "Iss", 100, 10, E, E, E),
	("R1", "2026-01-09", "2026-01-09", "Rev Beg", E, 11, 10, 700, E),
	("R1", "2026-01-09", "2026-01-09", "REV In", E, 11, 10, 500, E),
	("R1", "2026-01-09", "2026-01-09", "REV out", E, 11, 10, -100, E),
	("", "2026-01-09", "2025-12-29", "Issue (BY)", 20, 10, E, E, E),
	("", "2026-01-09", "2026-01-09", "Issue (BY) - Rev", 20, 10, 11, -20, E),
	("", "2026-01-09", "2025-12-30", "REC (BY)", 40, 10, 11, E, E),
	("", "2026-01-09", "2026-01-09", "REC (BY) - Rev", 40, 10, 11, 40, E),
	("", "2026-01-17", "2026-01-17", "SC+", 30, 11, E, E, E),
	("", "2026-01-20", "2026-01-20", "PR", 80, 11, 12, E, E),
	("", "2026-01-22", "2026-01-22", "SR", 40, 11, E, E, E),
	("", "2026-01-24", "2026-01-24", "LC", E, E, 600, 0, 600),
	("", "2026-01-30", "2026-01-30", "Iss", 15, 11, E, E, E),
	("", "2026-02-03", "2026-02-03", "SC-", 10, 11, E, E, E),
	("", "2026-02-05", "2026-02-05", "PR", 50, 11, 13, E, E),
	("", "2026-02-07", "2026-02-07", "Rec", 80, 11, 14, E, E),
	("", "2026-02-12", "2026-01-05", "Issue (BD)", 15, 11, E, E, E),
	("", "2026-02-12", "2026-02-12", "Issue (BD) - Rev", 15, 11, 11, 0, E),
	("", "2026-02-13", "2026-01-06", "REC (BD)", 20, 11, 12, E, E),
	("", "2026-02-13", "2026-02-13", "REC (BD) - Rev", 20, 11, 11, 0, E),
	("", "2026-02-18", "2026-01-20", "Iss", 25, 11, E, E, E),
	("", "2026-02-22", "2026-02-22", "Rec", 40, 11, 9, E, E),
	("R2", "2026-02-24", "2026-02-24", "Rev Beg", E, 13, 11, 1440, E),
	("R2", "2026-02-24", "2026-02-24", "REV In", E, 13, 11, 1020, E),
	("R2", "2026-02-24", "2026-02-24", "REV out", E, 13, 11, -190, E),
	("", "2026-02-26", "2026-02-08", "Iss", 10, 13, E, E, E),
	("", "2026-02-27", "2026-02-10", "Rec", 30, 13, 15, E, E),
]

# settlement actions interleaved by after-event index (0-based into YTD_EVENTS)
YTD_SETTLEMENTS = [
	{"after": 10, "action": "close", "year": 2025, "month": 12, "sc": 10,
	 "ent": "2026-01-10", "ref": "DEC25"},
	{"after": 22, "action": "close", "year": 2026, "month": 1, "sc": 11,
	 "ent": "2026-02-15", "ref": "JAN26_orig"},
	{"after": 22, "action": "reverse", "target": "JAN26_orig", "ent": "2026-02-18"},
	{"after": 23, "action": "close", "year": 2026, "month": 1, "sc": 11,
	 "ent": "2026-02-20", "ref": "JAN26_re"},
	{"after": 29, "action": "close", "year": 2026, "month": 2, "sc": 13,
	 "ent": "2026-03-02", "ref": "FEB26"},
]


def run_ytd(company):
	"""Replay the 40-event kernel-driven YTD walkthrough; simulator anchors."""
	from periodic_valuation.periodic_standard_cost.engine import StdEngine

	if not frappe.db.exists("Item", YTD_ITEM):
		frappe.get_doc({"doctype": "Item", "item_code": YTD_ITEM, "item_name": YTD_ITEM,
			"item_group": frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0],
			"stock_uom": frappe.get_all("UOM", limit=1, pluck="name")[0],
			"is_stock_item": 1, "valuation_method": "Periodic Standard Cost",
			"settlement_view": "YTD"}).insert(ignore_permissions=True)
	global ITEM
	orig = ITEM
	ITEM = YTD_ITEM
	try:
		scv = make_scv(company, 2025, 12, 10)
		scv.flags.via_release_flow = True
		scv.status = "RELEASED"
		scv.save(ignore_permissions=True)
	finally:
		ITEM = orig
	src = ("Item Standard Cost Version", scv.name)

	eng = StdEngine(company, YTD_ITEM)
	check("YTD engine view", eng.view == "YTD", eng.view)

	setts = {}
	by_idx = {}
	for a in YTD_SETTLEMENTS:
		by_idx.setdefault(a["after"], []).append(a)

	for idx, (ref, ent, pst, trans, qty, sc, ac, t_sc, t_ac) in enumerate(YTD_EVENTS):
		kwargs = dict(trans=trans, posting_date=pst, entry_date=ent, ref=ref, source=src)
		if qty is not None:
			kwargs["qty"] = qty
		if sc is not None:
			kwargs["sc"] = sc
		if ac is not None:
			kwargs["ac"] = ac
		if t_sc is not None:
			kwargs["t_sc_override"] = t_sc
		if t_ac is not None:
			kwargs["t_ac_override"] = t_ac
		eng.post(**kwargs)
		for a in by_idx.get(idx, []):
			if a["action"] == "close":
				setts[a["ref"]] = eng.close_period(year=a["year"], month=a["month"],
					sc=a["sc"], source=src, entry_date=a["ent"], ref=a["ref"])
			else:
				eng.sett_reverse(setts[a["target"]].name, source=src, entry_date=a["ent"])

	anchors = [
		("DEC25", 1412.31, 627.69),
		("JAN26_orig", 694.43, 37.88),
		("JAN26_re", 678.65, 53.66),
		("FEB26", -1473.72, -133.97),
	]
	for ref, es, out in anchors:
		s_ = frappe.get_doc("Inventory Period Settlement", setts[ref].name)
		check(f"YTD {ref} {es}/{out}",
			abs(flt(s_.es_var) - es) <= 0.01 and abs(flt(s_.out_var) - out) <= 0.01,
			f"{s_.es_var}/{s_.out_var}")
	cancelled = frappe.db.get_value("Inventory Period Settlement", setts["JAN26_orig"].name, "cancelled")
	check("YTD JAN26_orig cancelled", cancelled == 1)

	check("YTD Feb end qty 1155", flt(eng.end_qty_ytd(2026, 2)) == 1155,
		eng.end_qty_ytd(2026, 2))

	gl = frappe.db.sql(
		"""SELECT SUM(g.debit) d, SUM(g.credit) c,
			SUM(CASE WHEN g.debit < 0 OR g.credit < 0 THEN 1 ELSE 0 END) neg
		FROM `tabGL Entry` g JOIN `tabInventory Valuation Event` ive ON ive.name = g.valuation_event_id
		WHERE ive.item_code = %s AND g.is_cancelled = 0""", (YTD_ITEM,), as_dict=True)[0]
	check("YTD trial balance nets to zero", abs(flt(gl.d) - flt(gl.c)) <= 0.01,
		f"{gl.d} vs {gl.c}")
	check("YTD no negative GL cells", not gl.neg, str(gl.neg))
