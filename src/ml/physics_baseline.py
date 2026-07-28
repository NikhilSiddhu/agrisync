"""Physics-floor disease pressure for cabbage.

WHAT THIS IS
    A temperature-and-wetness response curve per disease. Given the leaf
    wetness hours accumulated and the temperature during those hours, it
    returns a 0-1 "infection pressure".

WHAT THIS IS NOT
    A calibrated probability of infection. Every response from the risk API
    carries calibrated: false for this reason. Turning pressure into a real
    probability needs a season of field_log.csv observations to fit against.
    Do not present these numbers as predicted infection rates.

PARAMETER SOURCES
    Alternaria brassicae values below come from published Brassica work: a
    minimum wetness period of about 4 hours is needed for infection at
    around 18 C, with severity increasing up to about 12 hours.
    A. brassicicola germinates and develops in a distinctly warmer band,
    roughly 28-31 C.

    The downy mildew and black rot entries are marked PLACEHOLDER. They are
    plausible shapes, not sourced parameters. Replace them with values from
    literature for your region before you make any spray decision from them.
"""
from dataclasses import dataclass, field


@dataclass
class DiseaseModel:
    key: str
    name: str
    pathogen: str
    t_min_c: float          # no infection below this
    t_opt_c: float          # fastest infection here
    t_max_c: float          # no infection above this
    wet_hours_min: float    # nothing happens below this many wet hours
    wet_hours_sat: float    # response saturates here
    sourced: bool = False   # False means the numbers still need a citation
    notes: str = ""

    def temperature_response(self, temp_c: float) -> float:
        """Cardinal-temperature beta response, 0 to 1."""
        t = float(temp_c)
        if t <= self.t_min_c or t >= self.t_max_c:
            return 0.0
        exponent = (self.t_opt_c - self.t_min_c) / (self.t_max_c - self.t_opt_c)
        left = (t - self.t_min_c) / (self.t_opt_c - self.t_min_c)
        right = (self.t_max_c - t) / (self.t_max_c - self.t_opt_c)
        return max(0.0, min(1.0, (left ** exponent) * right))

    def wetness_response(self, wet_hours: float) -> float:
        """Ramp from 0 at wet_hours_min to 1 at wet_hours_sat."""
        w = float(wet_hours)
        if w <= self.wet_hours_min:
            return 0.0
        if w >= self.wet_hours_sat:
            return 1.0
        span = self.wet_hours_sat - self.wet_hours_min
        return (w - self.wet_hours_min) / span

    def pressure(self, temp_c: float, wet_hours: float) -> float:
        """Both conditions must be met. A warm dry day scores zero."""
        return self.temperature_response(temp_c) * self.wetness_response(wet_hours)


DISEASES = {
    "alternaria_brassicae": DiseaseModel(
        key="alternaria_brassicae",
        name="Alternaria leaf spot (cool strain)",
        pathogen="Alternaria brassicae",
        t_min_c=5.0, t_opt_c=18.0, t_max_c=30.0,
        wet_hours_min=4.0, wet_hours_sat=12.0,
        sourced=True,
        notes="4 h minimum wetness at ~18 C, severity rising to ~12 h.",
    ),
    "alternaria_brassicicola": DiseaseModel(
        key="alternaria_brassicicola",
        name="Alternaria leaf spot (warm strain)",
        pathogen="Alternaria brassicicola",
        t_min_c=12.0, t_opt_c=29.0, t_max_c=36.0,
        wet_hours_min=6.0, wet_hours_sat=14.0,
        sourced=True,
        notes="Germination and disease development roughly 28-31 C.",
    ),
    "downy_mildew": DiseaseModel(
        key="downy_mildew",
        name="Downy mildew",
        pathogen="Hyaloperonospora brassicae",
        t_min_c=4.0, t_opt_c=12.0, t_max_c=24.0,
        wet_hours_min=4.0, wet_hours_sat=10.0,
        sourced=False,
        notes="PLACEHOLDER SHAPE. Cool and wet favoured. Needs real parameters.",
    ),
    "black_rot": DiseaseModel(
        key="black_rot",
        name="Black rot",
        pathogen="Xanthomonas campestris pv. campestris",
        t_min_c=15.0, t_opt_c=28.0, t_max_c=36.0,
        wet_hours_min=6.0, wet_hours_sat=16.0,
        sourced=False,
        notes="PLACEHOLDER SHAPE. Warm and wet favoured. Needs real parameters.",
    ),
}


def wet_hours_from_readings(readings, threshold=50.0):
    """Count wet hours from a list of reading dicts.

    Assumes readings are roughly evenly spaced. Each wet reading contributes
    its share of the interval it represents.
    """
    if not readings:
        return 0.0, None

    wet = [r for r in readings if float(r.get("leaf_wetness", 0)) >= threshold]
    if not wet:
        return 0.0, None

    # Span of the window in hours, divided across the samples.
    stamps = sorted(r["ts"] for r in readings if r.get("ts"))
    if len(stamps) < 2:
        return 0.0, None
    span_h = (stamps[-1] - stamps[0]).total_seconds() / 3600.0
    per_sample_h = span_h / max(len(readings) - 1, 1)

    wet_hours = len(wet) * per_sample_h
    temps = [float(r["air_temp_c"]) for r in wet if r.get("air_temp_c") is not None]
    mean_wet_temp = sum(temps) / len(temps) if temps else None
    return wet_hours, mean_wet_temp


def evaluate_all(temp_c: float, wet_hours: float) -> dict:
    return {
        key: {
            "name": model.name,
            "pathogen": model.pathogen,
            "pressure": round(model.pressure(temp_c, wet_hours), 4),
            "parameters_sourced": model.sourced,
        }
        for key, model in DISEASES.items()
    }


def _readout():
    """Printed when you run: python -m src.ml.physics_baseline"""
    print("AgriSync physics baseline - infection pressure readout")
    print("This is a physics floor, NOT a calibrated probability.\n")

    header = f"{'temp C':>7} {'wet h':>6} " + " ".join(
        f"{k.split('_')[0][:9]:>10}" for k in DISEASES
    )
    print(header)
    print("-" * len(header))

    for temp in (10, 15, 18, 22, 26, 30):
        for wet in (2, 6, 10, 14):
            row = f"{temp:>7} {wet:>6} "
            row += " ".join(
                f"{DISEASES[k].pressure(temp, wet):>10.3f}" for k in DISEASES
            )
            print(row)

    print("\nColumns:", ", ".join(f"{k} = {v.name}" for k, v in DISEASES.items()))
    unsourced = [k for k, v in DISEASES.items() if not v.sourced]
    if unsourced:
        print("\nWARNING - unsourced placeholder parameters still in use:")
        for k in unsourced:
            print(f"  - {k}: {DISEASES[k].notes}")


if __name__ == "__main__":
    _readout()
