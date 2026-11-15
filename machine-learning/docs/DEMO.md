# Demo scenarios

Two prepared views of the app (frontend branch `feat/demo-scenario`, `?demo=<name>`). What each shows, the numbers
behind it and what must be said with it.

## `nisar`: the Woolsey scar, eight years on

Bundle `smm-20261020-nisar-v3.3.3` (Santa Monica Mountains, 30 m, 2026-10-20). Layer: `fuel_state`, NISAR GCOV
L-band HV power against unburned cells of the same fuel model and terrain.

- The Woolsey Fire burned here in November 2018, 7.9 years before the map day. The fuel-age modifier has faded
  (spread permeability 0.97 at the demo point), the LANDFIRE fuel map shows mature fuel again and optical
  greenness no longer separates the scar (detection AUC 0.58 and 0.65). L-band still does (0.89): median fuel
  state 0.61 in cells of that age against 1.03 where no fire is on record.
- Fire growth from −118.8455, 34.1231 (fuel state 0.42) with a wind of 12 m/s from 45°, after 1 / 3 / 6 hours:
  3.5 / 12 / 102 ha with NISAR's fuel state, 6.8 / 35 / 389 ha from the fuel map alone. Other points of the scar:
  548 against 952 ha, 658 against 941 ha (`docs/versions/v3.3.3/demo_point_candidates.json`: 70 points, wind and
  hours the same).
- The bundle carries the fire-growth components twice, with the fuel state and without (`*_plain`); the backend's
  `/spread?fuel_state=false` uses the second set.
- In the app the scenario opens by itself with a switch. "NISAR ON" shows the fuel-state layer and the fire with
  it; "NISAR OFF" hides the layer, leaving the satellite image on which the scar cannot be seen, and shows the
  fire from the fuel map alone. The other estimate stays as a thin dashed outline. Key N switches.
- The ignition is at the edge of Westlake Village. The regrowth NISAR sees is at the ignition itself; further
  south-west the fire would run into fuel NISAR shows as mature. With NISAR the fire spends its first hours in
  the regrowth, which is why it stays small within six hours.

The only real fire of the GCOV record in this area (Bryson, 15 October 2026, 63 ha, in the same scar) does not
settle which estimate is right: neither reproduces it, and it burned through land the radar had as regrowth
(`docs/research/bryson-fire.md`). So the comparison is never to be called "ours is more accurate".

Say with it: the simulation is surface-fire potential under one wind, without suppression; scaling woody fuel by
L-band power is a model assumption, and no fire has yet crossed this scar during the GCOV record to test it. What
is measured is that the radar sees the scar and the other sources do not.

## `la2025`: the morning of 7 January 2025

Bundle `ca-20250107-smap-v3.3.2` (California, 200 m). Layer: `weather_factor`, the odds of a large fire that day
divided by the odds on an ordinary ignition day.

- The model was trained on 2016–2024. Weather factor: median 0.25 over the state, 5.6 around Los Angeles, 4 to 15
  inside the perimeters of the Palisades, Eaton, Kenneth and Hughes fires; 97–100 % of that land in the top risk
  class against 13.5 % of the state.
- No NISAR: the satellite was not delivering yet.

Say with it: the map uses the weather analysed for that day, not a forecast, and 84 % of the Los Angeles area was
in the top class, so it shows that the day was dangerous there, not where the fires would start. The danger was
forecast by the weather service at the time.
