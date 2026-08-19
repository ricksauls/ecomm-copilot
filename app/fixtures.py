"""Fixture data for the ecomm-copilot screens.

Everything here is realistic stand-in content for the pet-water-fountain demo
scenario described in the design handoff. It exists so the templates can render
against a real data shape without a database or external API wired up yet.

Replace these functions with real data access (a service layer over the DB /
Walmart API) when the backend exists. The *shape* of what they return is the
contract the templates depend on, so keep the keys stable when you swap the
source.
"""


def get_dashboard():
    """Return the agency dashboard view model.

    Mirrors the "Agency Dashboard" data described in the handoff: portfolio
    header, four KPI cards, and the "products losing ground" table. The table
    order is an editorial "needs attention" ranking, not a simple sort on any
    one column (see the handoff note).
    """
    return {
        "agency": {
            "name": "Meridian Commerce Group",
            "subtitle": "15 brands \u00b7 148 products \u00b7 Walmart \u00b7 week of Aug 10",
        },
        "kpis": [
            {"label": "Products managed", "value": "148", "footnote": "+6 this month"},
            {
                "label": "Avg competitive score",
                "value": "71",
                "delta": "+3",
                # Sparkline bar heights (px) and tones, per the handoff spec.
                "sparkline": [8, 9, 8, 11, 12, 13, 16],
            },
            {"label": "Analyses run", "value": "24", "footnote": "Aug \u00b7 8 pending review"},
            {
                "label": "Creative sets delivered",
                "value": "11",
                "footnote": "88 images \u00b7 3 in production",
            },
        ],
        # Minus signs are typographic U+2212, per the voice rules.
        "losing_ground": [
            {
                "name": "Cascade 84 oz Pet Water Fountain",
                "brand": "Northlane Home",
                "item": "#WM-4471902",
                "score": "72",
                "gap": "\u221217",
                "recommended": "PDP image set",
                "worst": True,  # Only the worst row's gap is rendered in signal red.
            },
            {
                "name": "Harbor 6-Quart Enameled Dutch Oven",
                "brand": "Fieldhouse Kitchen",
                "item": "#WM-2298140",
                "score": "64",
                "gap": "\u221222",
                "recommended": "Run analysis",
                "worst": False,
            },
            {
                "name": "Trailmark Insulated 32 oz Tumbler",
                "brand": "Trailmark Outdoors",
                "item": "#WM-8830271",
                "score": "69",
                "gap": "\u221215",
                "recommended": "Tailgating set",
                "worst": False,
            },
            {
                "name": "Northlane Ceramic Slow Cooker, 7 qt",
                "brand": "Northlane Home",
                "item": "#WM-5512088",
                "score": "58",
                "gap": "\u221228",
                "recommended": "Content rewrite",
                "worst": False,
            },
            {
                "name": "Kestrel 20V Cordless Leaf Blower",
                "brand": "Kestrel Tools",
                "item": "#WM-1094552",
                "score": "75",
                "gap": "\u22129",
                "recommended": "Run analysis",
                "worst": False,
            },
        ],
        "recent_activity": [
            {"title": "Cascade Pet Fountain \u2014 core set delivered", "meta": "Creative \u00b7 Aug 15"},
            {"title": "Competitive analysis ran on 3 Northlane products", "meta": "Analysis \u00b7 Aug 14"},
            {"title": "Content rewrite approved \u2014 Fieldhouse Dutch Oven", "meta": "Content \u00b7 Aug 13"},
            {"title": "Back to School window opened", "meta": "Seasonal \u00b7 Aug 12"},
        ],
    }
