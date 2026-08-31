from frappe import _


def get_data():
	return {
		"fieldname": "settlement_ref",
		"transactions": [{"label": _("Events"), "items": ["Inventory Valuation Event"]}],
	}
