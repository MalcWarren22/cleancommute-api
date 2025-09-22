# emissions.py
from typing import Literal

# Very rough per-passenger emission factors (kg CO2e per km)
_FACTORS = {
    "car": 0.192,          # average gasoline car
    "car_gas": 0.192,
    "car_hybrid": 0.120,
    "rideshare": 0.212,    # extra for detours/idle
    "bus": 0.082,
    "train": 0.041,
    "subway": 0.045,
    "bike": 0.0,
    "walk": 0.0,
}

_Mode = Literal[
    "car","car_gas","car_hybrid","rideshare","bus","train","subway","bike","walk"
]

def _factor_for(mode: str) -> float:
    return _FACTORS.get((mode or "").lower(), _FACTORS["car"])

def estimate_emissions(distance_km: float, mode: str, *, passengers: int = 1) -> dict:
    """
    Return a simple per-trip emissions estimate.

    Args:
        distance_km: trip distance in kilometers (> 0)
        mode: one of _Mode (case-insensitive); defaults to 'car' if unknown
        passengers: for car-like modes, divide per-vehicle emissions by passengers (min 1)

    Returns:
        dict: {
          "kgCO2e": float,
          "factor_kg_per_km": float,
          "mode": str,
          "passengers": int,
          "source": "emissions.py"
        }
    """
    if distance_km <= 0:
        raise ValueError("distance_km must be > 0")
    f = _factor_for(mode)
    pax = max(1, int(passengers))

    # For private car / rideshare, treat factor as vehicle-level and divide by passengers.
    per_vehicle_modes = {"car", "car_gas", "car_hybrid", "rideshare"}
    divisor = pax if (mode or "").lower() in per_vehicle_modes else 1

    kg = round((distance_km * f) / divisor, 4)
    return {
        "kgCO2e": kg,
        "factor_kg_per_km": f,
        "mode": mode,
        "passengers": divisor,
        "source": "emissions.py",
    }
