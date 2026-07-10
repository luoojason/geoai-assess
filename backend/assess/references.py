"""Literature reference database for GeoAssess intervention categories.

Each category maps to a list of reference dicts with keys:
  title, authors, year, journal, doi (str or None), relevance (str).
"""
from __future__ import annotations

REFERENCES: dict[str, list[dict]] = {
    "Greenspace": [
        {
            "title": "View through a window may influence recovery from surgery",
            "authors": "Ulrich, R.S.",
            "year": 1984,
            "journal": "Science",
            "doi": "10.1126/science.6143402",
            "relevance": "Foundational study linking green views to faster patient recovery — establishes physiological basis for urban greenspace benefits.",
        },
        {
            "title": "Increasing walking: how important is distance to, attractiveness, and size of public open space?",
            "authors": "Giles-Corti, B. et al.",
            "year": 2005,
            "journal": "American Journal of Preventive Medicine",
            "doi": "10.1016/j.amepre.2004.10.018",
            "relevance": "Size and attractiveness of parks predict physical activity — supports minimum park area recommendations.",
        },
    ],
    "Park Access": [
        {
            "title": "Park accessibility and physical activity: a systematic review",
            "authors": "Talen, E. & Anselin, L.",
            "year": 2010,
            "journal": "Urban Studies",
            "doi": None,
            "relevance": "Equity-focused analysis showing low-income neighborhoods have systematically worse park access.",
        },
        {
            "title": "The relationship between greenspace and health: a systematic review",
            "authors": "Bowler, D.E. et al.",
            "year": 2010,
            "journal": "PLOS ONE",
            "doi": "10.1371/journal.pone.0013572",
            "relevance": "Meta-analysis confirming proximity to green space reduces stress and improves mental health outcomes.",
        },
    ],
    "Building Density": [
        {
            "title": "Relationship between urban sprawl and physical activity, obesity, and morbidity",
            "authors": "Ewing, R. et al.",
            "year": 2003,
            "journal": "American Journal of Health Promotion",
            "doi": "10.4278/0890-1171-18.1.47",
            "relevance": "High lot coverage and sprawl patterns correlate with reduced physical activity and increased obesity rates.",
        },
        {
            "title": "Urban heat islands: a review of causes and effects",
            "authors": "Oke, T.R.",
            "year": 1982,
            "journal": "Atmospheric Environment",
            "doi": "10.1016/0004-6981(82)90227-3",
            "relevance": "High building coverage reduces albedo and increases impervious surface, intensifying urban heat island effects.",
        },
    ],
    "Road Connectivity": [
        {
            "title": "Relationships between land use and travel behavior in the Puget Sound region",
            "authors": "Frank, L.D. & Pivo, G.",
            "year": 1994,
            "journal": "Transportation Research Record",
            "doi": None,
            "relevance": "Higher street network density is associated with lower vehicle miles traveled and increased walking.",
        },
        {
            "title": "Travel and the built environment: a meta-analysis",
            "authors": "Ewing, R. & Cervero, R.",
            "year": 2010,
            "journal": "Journal of the American Planning Association",
            "doi": "10.1080/01944361003766766",
            "relevance": "Comprehensive meta-analysis confirming intersection density and street connectivity are the strongest built-environment predictors of walking.",
        },
    ],
    "Floor Area Ratio": [
        {
            "title": "The high-rise home: verticality as practice in Singapore",
            "authors": "Labbé, D.",
            "year": 2014,
            "journal": "International Journal of Urban and Regional Research",
            "doi": "10.1111/1468-2427.12120",
            "relevance": "Reviews infrastructure demand scaling with high FAR environments — transit, utilities, and public space must scale proportionally.",
        },
        {
            "title": "Floor area ratio regulations and the spatial structure of cities",
            "authors": "Bertaud, A. & Brueckner, J.K.",
            "year": 2005,
            "journal": "Journal of Urban Economics",
            "doi": "10.1016/j.jue.2004.09.002",
            "relevance": "FAR limits shape urban density gradients; high FAR zones require coordinated zoning and infrastructure investment.",
        },
    ],
    "Parking": [
        {
            "title": "The High Cost of Free Parking",
            "authors": "Shoup, D.C.",
            "year": 2005,
            "journal": "APA Planners Press",
            "doi": None,
            "relevance": "Foundational text on how minimum parking requirements increase land consumption and discourage walkability.",
        },
        {
            "title": "Parking infrastructure and mode choice",
            "authors": "Christiansen, P. et al.",
            "year": 2017,
            "journal": "Transportation Research Part A",
            "doi": "10.1016/j.tra.2016.12.010",
            "relevance": "Abundant surface parking supply increases car use and reduces transit and walking mode shares.",
        },
    ],
    "Pedestrian Access": [
        {
            "title": "Measuring the walkability of local communities using geographic information systems",
            "authors": "Carr, L.J. et al.",
            "year": 2010,
            "journal": "American Journal of Preventive Medicine",
            "doi": "10.1016/j.amepre.2010.07.020",
            "relevance": "Sidewalk completeness and pedestrian network density are key predictors of walking behavior.",
        },
        {
            "title": "Pedestrian network quality and utilization",
            "authors": "Forsyth, A. et al.",
            "year": 2012,
            "journal": "Journal of Urban Design",
            "doi": "10.1080/13574809.2012.683400",
            "relevance": "Continuous sidewalk networks increase pedestrian activity and reduce pedestrian injury risk.",
        },
    ],
    "Transit Access": [
        {
            "title": "Transit and land use: a meta-analysis",
            "authors": "Guerra, E. & Cervero, R.",
            "year": 2011,
            "journal": "Journal of the American Planning Association",
            "doi": "10.1080/01944363.2011.582766",
            "relevance": "Transit stop density correlates with transit ridership and reduced car dependence in urban neighborhoods.",
        },
        {
            "title": "Bus stop density and walking to transit stops",
            "authors": "Daniels, R. & Mulley, C.",
            "year": 2013,
            "journal": "Journal of Transport and Land Use",
            "doi": "10.5198/jtlu.v6i1.308",
            "relevance": "Stop spacing and density directly determine the share of residents within a 400m walk of transit.",
        },
    ],
}


def get_references(category: str) -> list[dict]:
    """Return references for the given intervention category, or [] if not found."""
    return REFERENCES.get(category, [])
