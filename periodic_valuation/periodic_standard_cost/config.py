# Copyright (c) 2026, Quark Cyber Systems
# License: GNU General Public License v3. See license.txt

"""STD posting-intent flag matrix - verbatim port of the client-conformant
reference simulator for the MTD workbook (V2.03-verified).

Flags: (mvt, in_flag, out_flag, ppv_with_sett, ppv_without_sett, rev_flag)
- in/out feed the quantity pools (PR nets against In; SR and SC+ against Out)
- ppv_without_sett feeds the settle-able PPV pool; ppv_with_sett additionally
  includes the Sett family for the running account-balance view
- rev_flag feeds the revaluation pool (REV out deliberately excluded)
"""

from collections import namedtuple

Flags = namedtuple("Flags", "mvt in_flag out_flag ppvw ppvwo rev")

TRANS_FLAGS = {
	"Beg":                Flags("In",  0, 0, 1, 1, 0),
	"Rec":                Flags("In",  1, 0, 1, 1, 0),
	"Iss":                Flags("Out", 0, 1, 0, 0, 0),
	"PR":                 Flags("Out", 1, 0, 1, 1, 0),
	"SR":                 Flags("In",  0, 1, 0, 0, 0),
	"LC":                 Flags("",    0, 0, 1, 1, 0),
	"SC+":                Flags("In",  0, 1, 0, 0, 0),
	"SC-":                Flags("Out", 0, 1, 0, 0, 0),
	"REC (BD)":           Flags("In",  1, 0, 1, 1, 0),
	"REC (BD) - Rev":     Flags("In",  1, 0, 0, 0, 1),
	"Issue (BD)":         Flags("Out", 0, 1, 0, 0, 0),
	# MTD: companion feeds the Rev pool; YTD: it does not (single flag-matrix
	# difference between the two kernels - resolved at post time by view)
	"Issue (BD) - Rev":   Flags("Out", 0, 1, 0, 0, 1),
	"REC (BY)":           Flags("In",  1, 0, 1, 1, 0),
	"REC (BY) - Rev":     Flags("In",  1, 0, 0, 0, 1),
	"Issue (BY)":         Flags("Out", 0, 1, 0, 0, 0),
	"Issue (BY) - Rev":   Flags("Out", 0, 1, 0, 0, 1),
	"Rev Beg":            Flags("In",  1, 0, 0, 0, 1),
	"REV In":             Flags("In",  1, 0, 0, 0, 1),
	"REV out":            Flags("Out", 0, 1, 0, 0, 0),
	# DR-50 before its 05/10/2026 amendment: revaluation of the closing stock
	# on the period's last day - the settlement gives it wholly to ending
	# stock. Kept for the events already posted; nothing new posts it.
	"Rev End":            Flags("In",  1, 0, 0, 0, 1),
	# DR-50 as amended: revaluation of the stock on hand when a cost change is
	# released (and the bridges of entries dated before it, posted later) -
	# shared at settlement between ending stock and consumption after the
	# switch (StdEngine._switch_revaluations)
	"Rev Rel":            Flags("In",  1, 0, 0, 0, 1),
	# DR-54: day-1 reversal, in the current period, of the stock effect of a
	# backdated change's revaluation of its (still open) valid-from month;
	# the current period then revalues again (Rev Beg / Rev Rel)
	"Rev Reverse":        Flags("In",  1, 0, 0, 0, 1),
	# YTD only: the earlier month's REV out, reversed with the rest of its
	# triplet (the current YTD triplet re-measures that consumption)
	"REV out Reverse":    Flags("Out", 0, 1, 0, 0, 0),
	"Sett":               Flags("",    0, 0, 1, 0, 0),
	"Sett - Rev":         Flags("",    0, 0, 1, 0, 0),
	"Sett - Reverse":     Flags("",    0, 0, 1, 0, 0),
	"Sett - Rev - Reverse": Flags("",  0, 0, 1, 0, 0),
	# post-close allocation correction (delta engine, exact-reversal path)
	"Sett - Delta":         Flags("",  0, 0, 1, 0, 0),
}

SETT_FAMILY = frozenset({"Sett", "Sett - Rev", "Sett - Reverse", "Sett - Rev - Reverse", "Sett - Delta"})
BD_BY_PRIMARIES = frozenset({"REC (BD)", "Issue (BD)", "REC (BY)", "Issue (BY)"})

# YTD flag override (the one matrix difference)
YTD_REV_FLAG_OVERRIDES = {"Issue (BD) - Rev": 0}


def flags_for(trans, view):
	flags = TRANS_FLAGS[trans]
	if view == "YTD" and trans in YTD_REV_FLAG_OVERRIDES:
		flags = flags._replace(rev=YTD_REV_FLAG_OVERRIDES[trans])
	return flags
