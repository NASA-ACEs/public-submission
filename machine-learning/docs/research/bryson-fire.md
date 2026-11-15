# The Bryson fire: the one real fire in the GCOV record

2026-11-15. The demo shows a simulated fire in the Woolsey scar of 2018 that is 3.8 times smaller with NISAR's
fuel state than from the fuel map alone. Can that be checked against something that happened? One fire burned in
the Santa Monica Mountains AOI after the first GCOV scene: **Bryson, 2026-10-15, 62.6 ha** (WFIGS perimeter;
ignition −118.8377, 34.0909, about 3.5 km south of the demo point). Bundle for that day:
`smm-20261015-nisar-v3.3.3-bryson` (weather of the day, GCOV scenes to 2026-10-13, so before the fire).
Script and numbers: `~/spaceapps/ml-work/research/bryson/check.py`, `docs/versions/v3.3.3/bryson_fire_20261015.json`.

## What the map said that morning

| At the ignition (7 × 7 cells around it) | |
|---|---|
| Risk class | 5 |
| `p_large_fire` | 11 % (median of the AOI that day 6.5 %; on 2026-10-20: 0.6 %) |
| Weather factor | × 16 |
| Years since fire | 7.9 (Woolsey) |
| NISAR fuel state | 0.74 at the ignition; median 0.51 inside what then burned, 88 % of it below 0.7 |

The risk model flagged the day and the place. The fire stayed at 63 ha, so it is not a large fire by the model's
definition (400 ha).

## The simulation against the perimeter

From the ignition with the wind of the map day (gridMET daily mean: 3.1 m/s from 14°), surface fire, no
suppression:

| Hours | With NISAR fuel state | Fuel map alone | IoU with the real perimeter (both) |
|---|---|---|---|
| 1 | 1.2 ha | 1.6 ha | 0.00 |
| 3 | 9.4 ha | 12.3 ha | 0.01 |
| 6 | 32.7 ha | 46.7 ha | 0.03 |

Both run south (head towards 194°); the real fire ran west of its ignition. After six hours either estimate
covers 4 to 6 % of what burned.

## What it shows

- **It does not validate the fuel-state scaling, and it does not refute it.** Neither estimate reproduces the
  fire: the direction is wrong in both, because the wind the simulator is given is a daily mean on a 4 km grid.
  The two estimates differ by a factor of 1.4 here, far less than they differ from the truth.
- **Regrowth burned.** The fire crossed 63 ha of land that the radar had as regrowth (fuel state 0.51) on a day
  whose weather multiplied the odds sixteen times. Eight-year-old regrowth carries fire in such weather. This
  agrees with the perimeter record (`time-since-fire.md`: cells of scars 6 to 9 years old burned 0.82 times as
  often as neighbouring land, shrub about 0.8 after two years), and it is a reason to read the demo's factor of
  3.8 as the size of an assumption, not as an expected effect.
- One fire. No conclusion about accuracy can rest on it.

## What can and cannot be said in the demo

Measured: the radar separates the 2018 scar from unburned land (AUC 0.89; optical greenness 0.65; the fuel map
0.58), and the truth for that is the mapped perimeter. Assumed: that woody fuel, and with it the rate of spread,
scales with the L-band echo. The fire-growth comparison shows how much the assumption would matter; it does not
show that the estimate with NISAR is the better one.
