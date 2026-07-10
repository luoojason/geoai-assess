"""Rule-based intervention engine for GeoAssess.

Applies threshold rules derived from real planning standards (APA, EPA, HUD)
to a metrics dict and returns a list of flagged interventions with severity,
finding, recommendation, and standard citation.
"""
from __future__ import annotations

from .references import get_references


def generate_interventions(metrics: dict) -> list[dict]:
    """Return a list of intervention dicts for any metrics outside acceptable ranges.

    Each dict has: category, finding, recommendation, standard, severity.
    Returns [] when all metrics are within acceptable ranges.
    """
    results: list[dict] = []

    green_ratio = metrics.get("greenspace_coverage_ratio", 0.0)
    green_area_m2 = metrics.get("greenspace_area_m2", 0.0)
    bldg_ratio = metrics.get("building_coverage_ratio", 0.0)
    road_density = metrics.get("road_density_km_per_km2", 0.0)
    far = metrics.get("far", 0.0)

    # --- Greenspace coverage ---
    if green_ratio < 0.05:
        results.append({
            "category": "Greenspace",
            "finding": f"Greenspace coverage is {green_ratio:.1%} — critically below recommended minimums.",
            "recommendation": "Prioritize park creation, green corridors, and pocket parks. Consider land acquisition or rooftop greening programs.",
            "standard": "APA recommends ≥10% greenspace coverage in urban neighborhoods.",
            "severity": "high",
            "references": get_references("Greenspace"),
        })
    elif green_ratio < 0.10:
        results.append({
            "category": "Greenspace",
            "finding": f"Greenspace coverage is {green_ratio:.1%} — below APA recommended threshold.",
            "recommendation": "Expand existing parks, add street trees, and integrate green infrastructure into public right-of-way.",
            "standard": "APA recommends ≥10% greenspace coverage in urban neighborhoods.",
            "severity": "medium",
            "references": get_references("Greenspace"),
        })

    # --- Greenspace area (absolute park deficit) ---
    if green_area_m2 < 10_000 and green_ratio < 0.10:
        results.append({
            "category": "Park Access",
            "finding": f"Total greenspace area is {green_area_m2/1e4:.2f} ha — less than 1 hectare in the study area.",
            "recommendation": "Establish at least one neighborhood park ≥1 ha within 400 m walking distance of all residents.",
            "standard": "EPA recommends minimum 1 ha accessible park space per neighborhood unit.",
            "severity": "high",
            "references": get_references("Park Access"),
        })

    # --- Building coverage ---
    if bldg_ratio > 0.80:
        results.append({
            "category": "Building Density",
            "finding": f"Building coverage is {bldg_ratio:.1%} — extreme density with minimal open space.",
            "recommendation": "Require setbacks, open courtyards, or green roofs in new development. Introduce floor area bonuses for ground-level public space.",
            "standard": "Most zoning codes cap lot coverage at 60–80% to ensure light, air, and stormwater management.",
            "severity": "high",
            "references": get_references("Building Density"),
        })
    elif bldg_ratio > 0.60:
        results.append({
            "category": "Building Density",
            "finding": f"Building coverage is {bldg_ratio:.1%} — above typical maximum lot coverage limits.",
            "recommendation": "Review zoning compliance and require open space contributions for new permits.",
            "standard": "Standard residential/mixed-use zoning caps lot coverage at 60%. Above this, urban heat island and stormwater risk increase.",
            "severity": "medium",
            "references": get_references("Building Density"),
        })

    # --- Road density (low connectivity) ---
    if road_density < 2.0 and road_density > 0.0:
        results.append({
            "category": "Road Connectivity",
            "finding": f"Road density is {road_density:.2f} km/km² — poor street network connectivity.",
            "recommendation": "Introduce additional street connections, mid-block pedestrian paths, or shared-use lanes to improve walkability and emergency access.",
            "standard": "EPA Smart Growth guidelines recommend ≥2 km/km² road density for walkable neighborhoods.",
            "severity": "medium",
            "references": get_references("Road Connectivity"),
        })

    # --- Road density (overdevelopment / traffic risk) ---
    if road_density > 20.0:
        results.append({
            "category": "Road Connectivity",
            "finding": f"Road density is {road_density:.2f} km/km² — very high, indicating potential traffic congestion risk.",
            "recommendation": "Evaluate traffic calming measures, pedestrian-priority zones, and converting low-use roads to greenways.",
            "standard": "Road density above 20 km/km² is associated with increased traffic noise, pedestrian risk, and impervious surface runoff.",
            "severity": "low",
            "references": get_references("Road Connectivity"),
        })

    # --- Parking coverage ---
    parking_ratio = metrics.get("parking_coverage_ratio", 0.0)
    if "parking_coverage_ratio" in metrics and parking_ratio > 0.15:
        results.append({
            "category": "Parking",
            "finding": f"Parking coverage is {parking_ratio:.1%} — over-reliance on surface parking reduces walkability and greenspace.",
            "recommendation": "Reduce surface parking through shared parking policies, structured parking, or conversion to mixed-use development.",
            "standard": "Urban Land Institute recommends surface parking below 15% of total land area to support walkable, transit-oriented development.",
            "severity": "medium",
            "references": get_references("Parking"),
        })

    # --- Pedestrian density ---
    ped_density = metrics.get("pedestrian_density_km_per_km2", 0.0)
    if "pedestrian_density_km_per_km2" in metrics and ped_density < 1.0:
        results.append({
            "category": "Pedestrian Access",
            "finding": f"Sidewalk density is {ped_density:.2f} km/km² — poor pedestrian infrastructure.",
            "recommendation": "Expand sidewalk network, fill gaps in pedestrian connectivity, and prioritize routes to schools, transit, and commercial areas.",
            "standard": "APA recommends ≥1 km/km² sidewalk density as a baseline for walkable neighborhoods.",
            "severity": "medium",
            "references": get_references("Pedestrian Access"),
        })

    # --- Transit access ---
    transit_density = metrics.get("transit_stop_density_per_km2", 0.0)
    if "transit_stop_density_per_km2" in metrics and transit_density < 2.0:
        results.append({
            "category": "Transit Access",
            "finding": f"Transit stop density is {transit_density:.2f} stops/km² — limited public transit access.",
            "recommendation": "Advocate for additional bus stops, transit route extensions, or micro-transit services to improve coverage.",
            "standard": "FTA guidelines suggest ≥2 stops/km² for adequate transit access in urban areas.",
            "severity": "medium" if transit_density > 0.0 else "high",
            "references": get_references("Transit Access"),
        })

    # --- FAR (floor area ratio) ---
    if far > 5.0:
        results.append({
            "category": "Floor Area Ratio",
            "finding": f"FAR is {far:.2f} — very high floor area ratio indicating extreme vertical density.",
            "recommendation": "Ensure infrastructure (transit, utilities, open space) can support high-density load. Require public amenities as part of development agreements.",
            "standard": "HUD and APA flag FAR >5.0 as requiring special infrastructure capacity review.",
            "severity": "high",
            "references": get_references("Floor Area Ratio"),
        })
    elif far > 3.0:
        results.append({
            "category": "Floor Area Ratio",
            "finding": f"FAR is {far:.2f} — above typical mid-rise residential threshold.",
            "recommendation": "Review transit capacity and public open space provision to support increased density.",
            "standard": "FAR >3.0 typically triggers infrastructure capacity review under HUD Community Development guidelines.",
            "severity": "medium",
            "references": get_references("Floor Area Ratio"),
        })

    return results
