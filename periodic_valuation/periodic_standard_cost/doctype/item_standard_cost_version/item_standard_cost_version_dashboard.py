from frappe import _


def get_data():
	return {
		"fieldname": "cost_version",
		"transactions": [{"label": _("Priced Events"), "items": ["Inventory Valuation Event"]}],
	}
