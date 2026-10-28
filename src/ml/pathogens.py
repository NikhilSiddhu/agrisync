"""What each cabbage disease needs, written down once with where it came from.

Every number the odds engine uses about a disease lives here. Lab and trial
values are the starting point; the field log is what checks them on this
farm (src/ml/odds.py, calibrate). Values marked ASSUMED have no direct source
and are the first thing to revise when the field log disagrees.

SOURCES
    AHDB   Brassica diseases guide, AHDB Horticulture (UK), 2019 edition
    PNW    Pacific Northwest Plant Disease Management Handbook, cabbage and
           cauliflower entries (Oregon State University)
    HONG   Hong, Fitt & Welham (1996), Plant Pathology 45: wetness period and
           temperature for Alternaria brassicae (via PNW)
    HJP    Humpherson-Jones & Phelps (1989), Annals of Applied Biology:
           sporulation of A. brassicae and A. brassicicola (via PNW)
    UMASS  UMass Extension vegetable fact sheet, black rot of brassicas
    NCSU   NC State Extension, downy mildew on brassica crops
    NHB    National Horticulture Board (India), cabbage diseases sheet
    SKL    SkleroPro infection criteria (Koch et al. 2007), as used by the
           AHDB sclerotinia risk alerts: 23 continuous hours, RH >= 80%,
           T >= 7 C
    MAG    Magarey, Sutton & Thayer (2005), Phytopathology 95:92-100, the
           generic infection model this module follows

HOW A WET SPELL BECOMES AN INFECTION (MAG)
    A spell of wet hours infects once its length W reaches the wetness the
    pathogen needs at the spell's mean temperature:
        W_needed(T) = W_min / f(T)    capped at W_max
    where f(T) is 1 at the optimum temperature and falls to 0 at the
    cardinal limits. A dry break shorter than `dry_break_h` does not end the
    spell (spores survive it); a longer one does. Beyond W_needed, longer
    spells give heavier infection, up to W_max.
"""
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Pathogen:
    key: str
    group: str                 # field-log group: alternaria, downy_mildew, ...
    name: str
    latin: str
    t_min: float
    t_opt: float
    t_max: float
    w_min: float               # wet hours needed at the optimum temperature
    w_max: float               # wet hours past which longer adds nothing
    dry_break_h: float         # a dry spell this long ends the wet spell
    wet_rule: str = "leaf"     # "leaf": leaf wetness; "humid": RH/T rule (sclerotinia)
    latent_d: tuple = (2, 5)   # days from infection to visible symptoms
    latent_dd: tuple = None    # or degree-days above 5 C, when temperature-driven
    rain_boost: float = 0.0    # extra weight for hours with splashing rain
    stage: dict = field(default_factory=dict)      # susceptibility by crop stage
    inoculum: dict = field(default_factory=dict)   # background spores by month
    sources: str = ""
    assumed: str = ""

    def f_temp(self, t):
        """Cardinal-temperature (beta) response, 0..1, vectorised."""
        t = np.asarray(t, dtype=float)
        a = (self.t_opt - self.t_min) / (self.t_max - self.t_opt)
        left = np.clip((t - self.t_min) / (self.t_opt - self.t_min), 0.0, None)
        right = np.clip((self.t_max - t) / (self.t_max - self.t_opt), 0.0, None)
        out = (left ** a) * right
        out = np.where((t <= self.t_min) | (t >= self.t_max), 0.0, out)
        return np.clip(out, 0.0, 1.0)

    def w_needed(self, t_mean, w_min=None, w_max=None):
        w_min = self.w_min if w_min is None else w_min
        w_max = self.w_max if w_max is None else w_max
        f = self.f_temp(t_mean)
        with np.errstate(divide="ignore"):
            need = np.where(f > 0, w_min / np.maximum(f, 1e-9), np.inf)
        return np.minimum(need, np.where(f > 0.05, w_max, np.inf))


# Crop stage by days after transplanting. Early cabbage in western UP is
# transplanted 25-35 days after sowing and cut 60-80 days later (TAMU guide).
STAGES = (("nursery", -999, 0), ("establishing", 0, 20), ("leafy growth", 20, 45),
          ("head forming", 45, 70), ("mature", 70, 9999))


def stage_of(days_after_transplant):
    if days_after_transplant is None:
        return None
    for name, lo, hi in STAGES:
        if lo <= days_after_transplant < hi:
            return name
    return "mature"


PATHOGENS = (
    Pathogen(
        key="alternaria_brassicae", group="alternaria",
        name="Alternaria leaf spot (cool)", latin="Alternaria brassicae",
        t_min=5.0, t_opt=19.0, t_max=30.0, w_min=4.0, w_max=12.0, dry_break_h=3.0,
        latent_d=(2, 5),
        stage={"nursery": 0.6, "establishing": 0.6, "leafy growth": 0.85,
               "head forming": 1.0, "mature": 1.0},
        inoculum={m: 1.0 for m in range(1, 13)},
        sources="HONG/PNW: 4 h wet at 18 C infects, severity rising to 12 h. AHDB: "
                "infects in 6 h at 20 C, needs 10 h for much disease; symptoms 2-5 days "
                "after infection whatever the temperature; none below 5 C. PNW: grows "
                "5-30 C. NHB: older leaves more susceptible. PNW: spores present "
                "through the whole crop cycle.",
        assumed="dry break 3 h; stage weights."),
    Pathogen(
        key="alternaria_brassicicola", group="alternaria",
        name="Alternaria leaf spot (warm)", latin="Alternaria brassicicola",
        t_min=10.0, t_opt=28.0, t_max=35.0, w_min=6.0, w_max=16.0, dry_break_h=3.0,
        latent_d=(2, 5),
        stage={"nursery": 0.6, "establishing": 0.6, "leafy growth": 0.85,
               "head forming": 1.0, "mature": 1.0},
        inoculum={m: 1.0 for m in range(1, 13)},
        sources="PNW: disease develops 28-31 C; slower to infect than A. brassicae. "
                "HJP: sporulates 20-30 C at RH 87% or more.",
        assumed="w_min 6 h and w_max 16 h (slower than the cool species, per PNW); "
                "t_min 10 C; dry break 3 h."),
    Pathogen(
        key="downy_mildew", group="downy_mildew",
        name="Downy mildew", latin="Hyaloperonospora brassicae",
        t_min=5.0, t_opt=13.0, t_max=24.0, w_min=4.0, w_max=10.0, dry_break_h=2.0,
        latent_d=(3, 4),
        stage={"nursery": 1.0, "establishing": 0.6, "leafy growth": 0.35,
               "head forming": 0.35, "mature": 0.4},
        inoculum={1: 1.0, 2: 1.0, 3: 0.8, 4: 0.4, 5: 0.2, 6: 0.2, 7: 0.2, 8: 0.3,
                  9: 0.4, 10: 0.6, 11: 1.0, 12: 1.0},
        sources="AHDB: germinates and penetrates 6-24 C, colonises 8-16 C; 4 h of leaf "
                "wetness lets airborne spores infect; symptoms in 3-4 days; cotyledons "
                "and first leaves most at risk, field crops gain adult-plant "
                "resistance. NCSU: favoured at 10-15 C. NHB: heavy dew and fog favour it.",
        assumed="w_max 10 h; dry break 2 h; stage weights; monthly spore levels "
                "(cool months high)."),
    Pathogen(
        key="black_rot", group="black_rot",
        name="Black rot", latin="Xanthomonas campestris pv. campestris",
        t_min=8.0, t_opt=27.0, t_max=36.0, w_min=6.0, w_max=24.0, dry_break_h=4.0,
        latent_d=(10, 14), latent_dd=(225.0, 315.0), rain_boost=2.0,
        stage={"nursery": 1.0, "establishing": 1.0, "leafy growth": 0.8,
               "head forming": 0.7, "mature": 0.6},
        inoculum={1: 0.4, 2: 0.4, 3: 0.5, 4: 0.6, 5: 0.6, 6: 0.7, 7: 0.8, 8: 0.8,
                  9: 0.8, 10: 0.7, 11: 0.5, 12: 0.4},
        sources="AHDB: warm wet weather, optimum about 25 C; symptoms 10-14 days after "
                "infection at the optimum, longer when cooler; spread by water splash "
                "and wind-driven rain; seedlings and transplants worst hit. UMASS: "
                "optimum 27-30 C; rain or dew needed. NHB: enters through leaf-edge "
                "pores, any stage, seed-borne.",
        assumed="t_min 8 C, t_max 36 C; wet hours 6-24; splash weight 2x; degree-day "
                "latency (10-14 days at 27 C above a 5 C base); monthly levels."),
    Pathogen(
        key="sclerotinia", group="sclerotinia",
        name="White mould", latin="Sclerotinia sclerotiorum",
        t_min=7.0, t_opt=16.0, t_max=27.0, w_min=23.0, w_max=36.0, dry_break_h=0.0,
        wet_rule="humid", latent_d=(4, 10),
        stage={"nursery": 0.2, "establishing": 0.2, "leafy growth": 0.4,
               "head forming": 0.8, "mature": 1.0},
        inoculum={1: 1.0, 2: 1.0, 3: 0.7, 4: 0.3, 5: 0.1, 6: 0.1, 7: 0.1, 8: 0.1,
                  9: 0.1, 10: 0.15, 11: 0.4, 12: 0.9},
        sources="SKL/AHDB: infection needs 23 continuous hours of RH 80% or more at "
                "7 C or more; growth 0-25 C, held back by hot dry weather; apothecia "
                "form when soils are moist and above 10 C; senescent leaves and mature "
                "heads at risk. NHB: damp weather, stems and leaves touching soil.",
        assumed="optimum 16 C and limit 27 C; latency 4-10 days; monthly levels "
                "(a December-February disease in the north Indian plains)."),
)

BY_KEY = {p.key: p for p in PATHOGENS}
GROUPS = {}
for _p in PATHOGENS:
    GROUPS.setdefault(_p.group, []).append(_p.key)
GROUP_NAMES = {"alternaria": "Alternaria leaf spot", "downy_mildew": "Downy mildew",
               "black_rot": "Black rot", "sclerotinia": "White mould"}


def latent_days(p, mean_temp_c=None):
    """(earliest, latest) days from infection to symptoms."""
    if p.latent_dd and mean_temp_c is not None:
        rate = max(float(mean_temp_c) - 5.0, 3.0)
        return p.latent_dd[0] / rate, p.latent_dd[1] / rate
    return float(p.latent_d[0]), float(p.latent_d[1])
