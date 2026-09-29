# P3N — bounded numerical dBZ interpolation before the p3k LUT

**Date:** 2026-09-29  
**Frame:** `20260929T004243Z` (the Dickinson shot, MRMS `ReflectivityAtLowestAltitude`)  
**Status:** draft. James Final RALA Renderer Push is measured offline. **James picks.** Nothing here is deployed. Default flag **off** = production p3l. Stamps stay **spatial p3l** and **palette `2026-09-rala-p3k`**. Do not merge. Do not deploy. Do not set a review value on the production cooker. C and D are harness-only.

Crops and `metrics.json` live in this directory. Reproduce with:

```bash
PYTHONPATH=src python3 scripts/p3n_dbz_interp_ab.py \
  --grib MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz \
  --out docs/p3n-dbz-interp
```

Source object: `s3://noaa-mrms-pds/CONUS/ReflectivityAtLowestAltitude_00.50/20260929/MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz`

## James Final RALA Renderer Push — 2026-09-29

**NOT PRODUCTION.** This section supersedes seam-only width tuning. Target: smooth without softness. Blocky means internal structure (large flat regions). Exterior grid character is acceptable. Colored squares are not RGB-blurred. Order: native dBZ → constrained numerical reconstruction → dense sampling (≥8× per axis; 16× on one small crop) → p3k → RGBA. Sample centers stay exact. A peak rises to its genuine high. There is no flat ≥65 plateau stamp and no overshoot. Magenta / p3k / loop / native max zoom 9 / float64 axes stay locked. NO-ECHO is never a numeric sample. No sharpen, no fake contours, no z10 meteorological invent, no Phase 4, no production deploy.

Package: `docs/p3n-dbz-interp/judge/final-harness/` (`metrics.json`, contact sheets, per-method fields). Same frame and the same crop windows as the earlier judge set.

| Crop | Window | Samples / cell | Image |
| --- | --- | --- | --- |
| Dickinson tight | −103.15, 46.70, −102.40, 47.25 | 8 | 440×600 |
| Belle Fourche tight | −103.98, 44.50, −103.45, 44.80 | 8 | 240×424 |
| Intense ≥65 core | −102.98, 46.86, −102.72, 47.06 | 8 | 160×208 |
| Same core, 16× | same window | 16 | 320×416 |

This CONUS frame’s regional maximum is 58.0 dBZ. The intense crop plants the Dickinson cell at 46.945°N, 102.855°W from 58.0 to 68.0 so a ≥65 core exists to measure. That 68 is not observed dBZ.

Regenerate:

```bash
PYTHONPATH=src python3 scripts/p3n_final_harness.py \
  --grib MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz \
  --out docs/p3n-dbz-interp/judge/final-harness
```

### Methods

| Id | Name | In the cooker flag? |
| --- | --- | --- |
| RAW | nearest native cell | control only |
| A | `bilinear_peak_hold` | yes, default off |
| B | `tight_peak_hold` | yes, default off |
| C | bounded cubic (`bounded-cubic`) | harness only |
| D | monotone cubic Hermite (`monotone-pchip`) | harness only |

**RAW.** Nearest native cell. Interiors are constant because the source cell is constant. This is the block control.

**A.** Masked bilinear on ordinary cells. A local maximum, including a tied plateau, keeps the p3l seam, so the source dBZ is held out to 0.28 cell. A plain ramp’s 10–90% width is 0.80 cell. Centers match. NO-ECHO is not a numeric sample.

**B.** The same peak-hold as A. The bilinear fraction is remapped with power 2, so a plain ramp’s 10–90% width is 0.50 cell. The sample stays a convex combination of the valid corners. Peaks are still the flat p3l core. Centers match.

**C.** Catmull-Rom on the 4×4 valid taps, written only when that value is already inside the min/max of the surrounding 2×2 cell centers. Otherwise the masked bilinear sample is left in place, so a hard clip cannot stamp a shelf at the clip value. Cell centers match because both kernels are interpolating. NO-ECHO is not a tap. On a uniform staircase the cubic reproduces the straight line (10–90% width 0.80 cell). On a flat-to-flat 20|30 edge the width is 0.713 cell.

**D.** Successive one-dimensional monotone cubic Hermite (Fritsch–Carlson). Longitude runs first on the four bracketing rows, then latitude through those four results. Slopes are the harmonic mean when adjacent secants share a sign, zero at a sign change or a flat neighbor, then limited so each segment stays between its endpoints. A local maximum is the source value at its own cell center, with a flat tangent there, and is not held across a 0.28-cell core. Where an endpoint is NO-ECHO or missing, the sample falls back to masked bilinear. On a uniform staircase the segment is the straight line (width 0.80 cell). On a flat-to-flat 20|30 edge the width is 0.608 cell.

### Transition width (synthetic, 10–90% of the step, in cells)

One cell per level, 20 then 30 then 40 then 50 then 60 then 70. Min and max of every reconstruction stay on the segment endpoints (overshoot 0).

| Segment | RAW | A | B | C | D |
| --- | --- | --- | --- | --- | --- |
| 20→30 | 0.00 | 0.80 | 0.50 | 0.80 | 0.80 |
| 30→40 | 0.00 | 0.80 | 0.50 | 0.80 | 0.80 |
| 40→50 | 0.00 | 0.80 | 0.50 | 0.80 | 0.80 |
| 50→60 | 0.00 | 0.80 | 0.50 | 0.80 | 0.80 |
| 60→≥65 (the 70 cell) | 0.00 | 0.557 | 0.407 | 0.80 | 0.80 |

A and B shorten only the last segment because 70 is a local maximum and the peak-hold fires. C and D keep the line, which is the shape of a real gradient.

Flat-to-flat 20,20,30,30 is the hard block edge. The upper plateau cells are local maxima (ties count), so A and B apply the hold.

| Edge | RAW | A | B | C | D |
| --- | --- | --- | --- | --- | --- |
| 20\|30 flat-to-flat | 0.00 | 0.557 | 0.407 | 0.713 | 0.608 |

### Real-frame bounds, centers, ≥65, NO-ECHO, weak area

Every method, every crop: center MAE 0.0 dBZ, center max abs error 0.0, overshoot high 0.0, overshoot low 0.0, NO-ECHO numeric leaks 0. Source and reconstructed min/max below are the dense field. The dense grid is offset by half a subcell, so it does not land on cell centers. C and D therefore report a dense maximum a few hundredths under the planted 68 while the center CSV at that cell is 68. A and B report dense max 68 because the hold is a plateau.

| Crop | Source min/max | A recon min/max | B | C | D |
| --- | --- | --- | --- | --- | --- |
| Dickinson tight | 5.5 / 58.0 | 5.625 / 58.0 | 5.509 / 58.0 | 5.625 / 57.963 | 5.625 / 57.951 |
| Belle Fourche tight | 4.0 / 58.0 | 4.295 / 58.0 | 4.020 / 58.0 | 4.295 / 57.962 | 4.064 / 57.934 |
| Intense core, 8× | 7.0 / 68.0 | 7.062 / 68.0 | 7.004 / 68.0 | 7.062 / 67.960 | 7.005 / 67.755 |
| Intense core, 16× | 7.0 / 68.0 | 7.031 / 68.0 | 7.001 / 68.0 | 7.031 / 67.996 | 7.001 / 67.937 |

≥65 on the planted cell: source cells ≥65 = 1, and every method keeps that center ≥65. Dense area fraction ≥65 (source 0.0035): A and B 0.0020 at 8× (0.0019 at 16×); C 0.0012 (0.0011 at 16×); D 0.0010 (0.0011 at 16×). Dickinson and Belle have no observed ≥65 cell, so both fractions stay 0.

Weak-return area is the fraction of dense samples ≤10 dBZ. Change is reconstructed minus source. Ramps pull some fringe samples above 10, so the fraction falls.

| Crop | A | B | C | D |
| --- | --- | --- | --- | --- |
| Dickinson tight | −0.0185 | −0.0149 | −0.0178 | −0.0171 |
| Belle Fourche tight | −0.0156 | −0.0119 | −0.0138 | −0.0131 |
| Intense core, 8× | −0.0063 | −0.0042 | −0.0061 | −0.0053 |
| Intense core, 16× | −0.0064 | −0.0038 | −0.0062 | −0.0054 |

Inner-flat fraction: on steps of ≥8 dBZ, the share of offsets 0.05 / 0.10 / 0.15 / 0.20 cell that are still within 0.5 dBZ of the source cell. Higher means a larger flat interior. Peak-flat is the same statistic on local maxima ≥50 dBZ.

| Crop | RAW | A | B | C | D | A/B/C/D peak ≥50 |
| --- | --- | --- | --- | --- | --- | --- |
| Dickinson (n=260) | 1.00 | 0.50 | 1.00 | 0.50 | 0.75 | 1.00 / 1.00 / 0.875 / 1.00 |
| Belle Fourche (n=340) | 1.00 | 0.50 | 1.00 | 0.50 | 0.75 | 1.00 / 1.00 / 0.625 / 1.00 |
| Intense 8× (n=110) | 1.00 | 0.50 | 1.00 | 0.50 | 0.625 | 1.00 / 1.00 / 0.75 / 0.75 |
| Intense 16× (n=110) | 1.00 | 0.50 | 1.00 | 0.50 | 0.625 | 1.00 / 1.00 / 0.75 / 0.75 |

B matches the native cell on that interior statistic. A and C leave the center on a plain ramp. D stays nearer the endpoint on a hard edge (flat tangent) and still follows the straight line on a real gradient.

### What each crop contains

Under `judge/final-harness/<site>/` for RAW, A, B, C, D:

- `*_numerical_u16.png` — grayscale numerical field, linear map of −20..80 dBZ into uint16 (1..65535), missing = 0
- `*_numerical_preview.png` — 8-bit preview of the same field, 0..70 dBZ, for viewing
- `*_p3k.png` — that field through the p3k LUT only (no p3l edge inset, no RGB blur)
- `*_centers.png` — native sample-center overlay
- `*_centers.csv` — lat, lon, category, source dBZ, reconstructed dBZ, error
- `*_error.png` — reconstructed minus nearest source, ±15 dBZ

Contact sheets at the harness root, banner **NOT PRODUCTION**:

- `dickinson_tight_p3k_compare.png`, `dickinson_tight_numerical_compare.png`
- `belle_fourche_tight_p3k_compare.png`, `belle_fourche_tight_numerical_compare.png`
- `intense65_core_p3k_compare.png`, `intense65_core_numerical_compare.png`
- `intense65_core_16x_p3k_compare.png`, `intense65_core_16x_numerical_compare.png`

### Render resolution and the Mapbox second filter

The harness pictures are the dense reconstruction, 8 samples per native 0.01° cell (16 on the intense core). They are not Mapbox screenshots and they are not cooker tiles.

The cooker still writes one 512×512 PNG per z9 XYZ tile. That PNG covers the same ground as a 256 CSS slippy tile. At Dickinson, map zoom 9 is about 3.65 CSS px per cell east–west. Mapbox linearly filters those 512px pixels when the map zooms past 9. That second filter widens whatever ramp is already in the tile. A narrower reconstruction on the tile still gets that display filter on top. These crops stop before that filter.

### Recommendation for James

**D (monotone cubic Hermite) is the engineering fit. James picks. Do not deploy. Do not merge. The flag stays off, and C and D stay out of `MPWG_RALA_DBZ_INTERP`.**

D meets the constraints this push added. Centers are exact (MAE 0). Overshoot against the in-window source range is 0.0 dBZ on every crop. NO-ECHO never becomes a number. The planted ≥65 cell stays ≥65 at its center, and the value is that cell’s 68 only at the center: the dense field around it falls under 68 (67.76 at 8×, 67.94 at 16×) because the sample grid misses the center, which is the opposite of a flat 65 plateau. On a hard block edge the 10–90% width is 0.61 cell, tighter than C (0.71) and tighter than a bilinear ramp (0.80), while a real staircase stays the straight line at 0.80 cell, so a true gradient is not given extra blur. Weak-area change sits with A and C (Dickinson −0.017).

C is the alternative if the D crops still read as interior facets. C has the same center error, the same zero overshoot, the same NO-ECHO behavior, and A’s interior-flat fraction (0.50). Its hard-edge ramp is 0.71 cell, which is the softness side of this target.

A and B keep the p3l core on every local maximum, including tied plateaus. That is the flat stamp this push rules out. B’s inner-flat fraction is 1.00, the same as the native cell, so the interior blocks remain.

## James review — direction keep, acceptance not yet

Medium zoom looks much better (the Lego is largely gone). Tight overzoom looks too soft. Generic sharpening and unsharp mask are rejected. Peak-hold stays. p3k stays frozen. The loop stays locked. Magenta stays ≥65. NO-ECHO stays hard. No z10. Do not revert the review path to p3l. Do not merge. Do not deploy. Production remains flag-off = p3l.

Candidate A stays `bilinear_peak_hold`. Candidate B is `tight_peak_hold`: the same peak-hold, a shorter numerical ramp before `palette.colorize`. This A/B pass added no third width. The final harness above is the later measurement of C and D, and it is the one that applies to the smooth-without-softness target.

### Transition width on this frame

10–90% of the dBZ step from one cell center to the next orthogonal center. Native nearest-cell is a step at the shared face, so its blend width is **0.00 cell**. The cell itself is still 1.00 source cell of constant color.

Ordinary pairs: both cells valid, |Δ| ≥ 8 dBZ, neither cell a local maximum (so the hold does not fire). Dickinson n=145, Belle Fourche n=214. Every method’s p10 and p90 sit within 0.01 cell of the median.

| Method | 10–90% width | Dickinson ordinary | Belle Fourche ordinary | Peak-to-shoulder (steepest 12) |
| --- | --- | --- | --- | --- |
| Native cell | 0.00 cell | 0.00 | 0.00 | 0.00 |
| p3l seam | 0.31 cell | 0.312 | 0.312 | 0.312 |
| A bilinear + peak hold | 0.80 cell | 0.800 | 0.800 | 0.555 |
| B tight + peak hold | 0.50 cell | 0.500 | 0.500 | 0.405 |

Peak-to-shoulder is shorter than a pure ramp because the hot cell keeps the p3l core. B is still shorter than A there (0.405 vs 0.555), and the shoulder stays on its own real dBZ instead of being washed toward the peak.

Screen pixels at Dickinson (3.64 CSS px per cell E–W and 5.33 N–S at map z9; the published 3.65 / 5.32 pitch). The tight crops are native z9 tiles scaled 8×, which is map zoom 12. One image pixel in those PNGs is 2 CSS px (the 512px tile in a 256px CSS slot, DPR 2).

| Method | z9 E–W CSS | z10.45 E–W CSS | z12 E–W CSS | z12 N–S CSS | z12 E–W image px | z13 E–W CSS |
| --- | --- | --- | --- | --- | --- | --- |
| Native step | 0 | 0 | 0 | 0 | 0 | 0 |
| p3l | 1.1 | 3.1 | 9.1 | 13.4 | 18 | 18.3 |
| A | 2.9 | 8.0 | 23.3 | 34.1 | 47 | 46.6 |
| B | 1.8 | 5.0 | 14.6 | 21.3 | 29 | 29.1 |

The hypothesis holds. A’s ramp is the full center-to-center span (0.80 cell of the 10–90% rise). Once a cell is 29 CSS px wide at map z12, that ramp is 23 CSS px E–W and 34 CSS px N–S. That is the softness. B cuts the same rise to 0.50 cell: 15 CSS px E–W and 21 N–S at z12.

Examples (ordinary, neither cell held):

| Site | From → to | Where | A | B |
| --- | --- | --- | --- | --- |
| Dickinson | 14.0 → 47.5 | 46.985°N, 102.875°W, N–S | 0.797 cell | 0.497 cell |
| Belle Fourche | 42.0 → 9.0 | 44.515°N, 103.745°W, N–S | 0.800 cell | 0.500 cell |

Full pairs: `docs/p3n-dbz-interp/judge/transition_widths.json`.

### What B does

B uses the same valid corners as masked bilinear and the same peak-hold as A. The bilinear fraction is remapped with power 2 (`t² / (t² + (1−t)²)`). It is 0 at a cell center, 0.5 on the shared face, and 1 at the next center. Neighbor weight grows more slowly inside the cell, so the 10–90% band is 0.50 cell instead of 0.80. The sample is still a convex combination of those corners: no overshoot, no new dBZ, no RGB blend, no unsharp mask. Category, clear-air, NO-ECHO, and alpha stay on the p3l splat.

On a 68-in-30 peak the hot cell is held (worst-case z9 half-pixel stays 68, and a 65 stays 65). The ordinary neighbor 0.20 cell in from its own center is about 32 dBZ under B and about 38 dBZ under A. The shared face stays between 30 and 68, so the peak is the top of the real gradient, not a spike sitting in a washed field.

### Scale-aware footprint

Not implemented. The screen width of a cell-fraction ramp is `fraction × px_per_cell(z9) × 2^(map_zoom − 9)`. It grows with overzoom because Mapbox magnifies one z9 raster. Holding the medium-zoom bilinear width (8.0 CSS px E–W at z10.45) when the user is at map z12 would require a **0.27-cell** ramp. That is the p3l shelf, which reads as blocks at the zoom where A already looks right. A per-zoom kernel would also mean extra native zooms. No z10. B is the fixed 0.625× width (0.50/0.80) at every zoom, which is the knob these crops are for.

### Acceptance checklist

| Item | State |
| --- | --- |
| Direction is bilinear + peak hold, not a return to p3l | kept |
| A remains available | `bilinear_peak_hold` |
| B is narrower at tight zoom and still hides the hard square | in these crops; **James has not signed it** |
| No unsharp mask, no RGB blur, no Gaussian, no fake sharpen | held |
| Peak-hold, magenta ≥65 on the harsh half-pixel, NO-ECHO hard | held |
| p3k frozen, loop locked, native max zoom 9 | held |
| Production cooker | flag omitted = p3l. Do not merge. Do not deploy. |

### Review flags

| | |
| --- | --- |
| Variable | `MPWG_RALA_DBZ_INTERP` |
| Production | omit it, or `off` / `p3l` / empty |
| A | `bilinear_peak_hold` |
| B | `tight_peak_hold` |
| What changes | RALA `masked-splat` numerical dBZ only, before p3k `palette.colorize`. Local maxima keep the p3l seam. |
| What stays | Category, NO-ECHO, clear-air alpha, `mode_spec.spatial` = `p3l`, palette `2026-09-rala-p3k`, zoom, loop. Composite ignores the variable. |

One-off review cooks, no upload:

```bash
MPWG_RALA_DBZ_INTERP=bilinear_peak_hold MPWG_UPLOAD=false \
  mpwg-radar cook --product rala

MPWG_RALA_DBZ_INTERP=tight_peak_hold MPWG_UPLOAD=false \
  mpwg-radar cook --product rala
```

Do not put either value in `/etc/mpwg-radar.env` or `mpwg-radar-cooker-rala.service`. A flagged cook still stamps `p3l`, so those tiles must not be published.

Tight A vs B crops (same frame, map-zoom-12 overzoom of the z9 tiles, labeled **NOT PRODUCTION**):

- `judge/dickinson_tight_z12_A_vs_B.png`
- `judge/belle_fourche_tight_z12_A_vs_B.png`
- `judge/dickinson_core_transition_tight_z12_A_vs_B.png`

Regenerate the measurement and these three crops with:

```bash
PYTHONPATH=src python3 scripts/p3n_dbz_interp_ab.py \
  --grib MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz \
  --tight-review --out docs/p3n-dbz-interp/judge
```

## Recommendation

The final-harness recommendation above is the current one: **D**, with **C** as the alternative if James reads D as still faceted. A and B stay available behind the default-off flag and are not the fit for this push. `SPATIAL_REVISION` stays `p3l`. Do not enable any review value on a cooker that publishes tiles.

## Review flag

The table above is the current switch. The notes below are the first A/B against p3l.

| | |
| --- | --- |
| Variable | `MPWG_RALA_DBZ_INTERP` |
| Production value | omit it, or `off` / `p3l` / empty |
| A | `bilinear_peak_hold` |
| B | `tight_peak_hold` |
| What it changes | RALA `masked-splat` tiles only. Numerical dBZ is replaced before the p3k LUT. Local-maximum cells keep the p3l seam. Category and the clear-air alpha stay on the p3l splat. |
| What it does not change | `mode_spec.spatial` stays `p3l`. Palette version stays `2026-09-rala-p3k`. Composite nearest sampling ignores the variable. Loop, zoom, and the cooker timer are untouched. |
| How you can see a review cook | `frame.json` → `mode_spec.dbz_interp` is the flag token only while the flag is on. A normal cook does not write that key. |

One-off review cook, no upload:

```bash
MPWG_RALA_DBZ_INTERP=tight_peak_hold MPWG_UPLOAD=false \
  mpwg-radar cook --product rala
```

Do not put the variable in `/etc/mpwg-radar.env` or `mpwg-radar-cooker-rala.service`. A cook with the flag on still stamps `p3l`, so those tiles must not be published as production. Unset the variable before any cooker that uploads.

Unknown values fail config load. A typo does not silently stay on p3l and does not enable another sampler.

Judge crops for Slack or email (current p3l beside bilinear + peak hold, labeled **NOT PRODUCTION**):

- `dickinson_overzoom_p3l_vs_peakhold.png`
- `belle_fourche_overzoom_p3l_vs_peakhold.png`
- `dickinson_core_p3l_vs_peakhold.png`
- `magenta_probe_p3l_vs_peakhold.png`

Copies sit in `docs/p3n-dbz-interp/judge/`. Regenerate with `--judge-only` (no full-CONUS benchmark):

```bash
PYTHONPATH=src python3 scripts/p3n_dbz_interp_ab.py \
  --grib MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz \
  --judge-only --out docs/p3n-dbz-interp/judge
```

James still needs to sign those crops before anyone enables the flag on a cooker that publishes tiles.

That candidate is the one that does both jobs the gate asked for:

- The flat interior of ordinary cells goes away (inner-cell plateau 100% → about 16%, and the once-per-cell gradient spike drops by about 4×), on the Dickinson core and on Belle Fourche.
- A local maximum keeps the current p3l footprint, so the worst-case z9 pixel of a 65 dBZ cell in 30 dBZ rain stays 65. Plain bilinear returns 57.4 there. Clipped bicubic returns 62.8.

The open question for that signature is the weak fringe: cell centers stay exact, and clear air stays empty, but the area-mean of ≤10 dBZ cells rises by about an extra 0.5–1.0 dBZ because the ramp starts at the center. On the Belle Fourche window, 17% of those weak cells rise more than 3 dBZ.

## Where an interpolator would insert

RALA clean does not touch the grid. On this frame, `apply_mode(clean)` with the RALA switches (`apply_dbz_floor`, despeckle, and grid smooth all off) changed dBZ by **0.00**. Axes stayed **float64**.

```
decode_grib2
  classify_dbz          float32 dBZ + category (valid / no-echo / missing)
  lat, lon              float64
cook
  apply_mode(clean)     RALA: grid unchanged
  write_tiles → render_tile(sample_mode="masked-splat")
      Web Mercator pixel centers (_query_lonlat)
      sample_masked_splat
          _splat_echo   ← numerical dBZ. p3l seam. THIS is the slot.
      palette.colorize  ← p3k 0.1 dBZ LUT. RGBA starts here.
      alpha *= edge     ← clear-air inset inside the echo cell
```

p3l already blends dBZ, and only dBZ. Inside `_DETAIL_CORE` (0.28 cell) the sample is the source cell exactly. Past that, a smoothstep reaches a 50/50 face with a valid neighbor. No-echo and missing are not samples. The LUT never sees a blended RGB.

The Dickinson measurement (visible block / native cell ≈ 1) is that 0.28-cell plateau, drawn large by Mapbox overzoom past `max_zoom` 9. Linear filtering softens the seam. It does not fill in the plateau.

A bounded interpolator **replaces the dBZ array** that `sample_masked_splat` hands to `palette.colorize`. Category and `edge_scale` stay on the p3l path, so the A/B below differs only in the numbers that enter the LUT. Upsampling the source grid and then nearest-sampling would invent a finer lattice and the same squares would come back at the next overzoom. The sample sites are the tile pixels, so the blend belongs at query time.

## Candidates

These live in `src/mpwg_radar/dbz_interp_offline.py`. The cooker calls that module only for `MPWG_RALA_DBZ_INTERP=bilinear_peak_hold` (A) or `tight_peak_hold` (B). With the flag omitted, `render_tile` does not call it.

| Candidate | What it does to dBZ | Grid | Maxima / magenta | Cost vs p3l splat on one z9 tile |
| --- | --- | --- | --- | --- |
| **bilinear-masked** | Existing `sample_masked_bilinear`. Exact at the cell center. Invalid corners are left out of the average. | Inner plateau breaks. Value ramps across the cell. | Worst-case z9 pixel (0.137 cell E–W and 0.094 cell N–S off the center, Dickinson pixel size) of a 68-in-30 peak returns **59.7**. A 65-in-30 peak returns **57.4**. | 17 ms, **1.8×** the 9.3 ms splat |
| **bicubic-clipped** | Catmull-Rom on the 4×4 when every tap is valid echo; otherwise bilinear. Clipped to the min/max of the 2×2 cell centers, so the cubic cannot invent a hotter peak. Exact at the cell center. | Inner plateau breaks. The 1-cell gradient spike matches bilinear. | Same worst-case pixel: 68-in-30 returns **65.6** (still magenta, thin margin). 65-in-30 returns **62.8** (under the lock). | 77 ms, **8.3×** |
| **bilinear-peak-hold** (A) | Bilinear on ordinary cells. Where the nearest cell is a plateau-aware local maximum (dBZ ≥ every 8-neighbor), keep the p3l dBZ, core and seam included. 10–90% width **0.80 cell**. | Inner plateau breaks on the field that is not a local max. Real Dickinson/Belle cores keep today's footprint. | Worst-case pixel stays **68** and **65**. On this frame, visible-peak z9 loss is **0**. | 29 ms unfused (**3.1×**), because this offline build runs splat and bilinear and picks. A fused pass would share one neighborhood gather. Paint path: 45 ms vs 15 ms for p3l paint (**2.9×**). |
| **tight-peak-hold** (B) | Same corners and the same peak-hold. The bilinear fraction uses power 2, so the 10–90% width is **0.50 cell**. Still a convex combination. | Shorter ramp than A. The interior is not the p3l shelf (that shelf is 0.31 cell of the rise, with a hard core). | Same hold as A: worst-case pixel stays **68** and **65**. The neighbor interior stays closer to its own dBZ than A does. | Same unfused gather as A, plus one power on the fraction. |

A limited-radius Gaussian is not in the set. It is not interpolating, so the cell center becomes an average of its neighbors. That is the retired p3h result: a 68 dBZ cell in 30 dBZ rain came back at 62.6.

Benchmark tile: z9 `109/180`, the Dickinson tile from the p3n measurement, sampled against the full 3500×7000 CONUS grid (262144 pixels). Times are this VM, so the ratio is the useful number. At 2000 echo tiles the unfused peak-hold paint is on the order of a minute more than today's paint. Bicubic sampling alone is on the order of two and a half minutes before PNG encode.

## How the crops were made

Same frame as the iPad shot. RALA clean, float64 axes, palette `2026-09-rala-p3k`.

- **Mapbox-like strips** render real z9 tiles, then bilinear-upscale by `2^(10.45−9) ≈ 2.73`, which is the overzoom in the p3n measurement. Alpha uses the p3l clear-air inset for every method.
- **Core field** samples 8 times per native cell in lat/lon, then colorizes. The first panel is nearest-cell, so the native squares are visible next to p3l.
- **Magenta probe** is synthetic. This CONUS frame has **no cell ≥ 65** (Dickinson and Belle Fourche both peak at 58). The probe writes 68 onto the Dickinson 58 cell at 46.945°N, 102.855°W and 66 onto its south neighbor (was 55.5). Those two numbers are not observed dBZ.

Windows:

| Site | West | South | East | North | Valid cells | Max dBZ |
| --- | --- | --- | --- | --- | --- | --- |
| Dickinson | −103.15 | 46.70 | −102.40 | 47.25 | 1223 | 58 |
| Dickinson core | −102.98 | 46.86 | −102.72 | 47.06 | 288 | 58 |
| Belle Fourche | −103.98 | 44.50 | −103.45 | 44.80 | 912 | 58 |

## Metrics

**Plateau fraction.** On every valid cell with an orthogonal neighbor at least 3 dBZ away, sample a 5×5 grid inside ±0.20 cell of the center. Fraction of those samples within 0.05 dBZ of the source cell. ±0.20 sits inside the p3l core, so production scores 1.00 and sub-cell RMS 0. That flat interior is the block.

**Grid pitch.** Median normalized autocorrelation of the horizontal |ΔdBZ| at a lag of one native cell, on echo rows of an 8-sample-per-cell field. p3l spikes there (the seam repeats once per cell). The candidates do not.

**Center RMSE.** Sampled dBZ minus source dBZ at the cell's own lat/lon. Zero means the float64 center-lock still holds.

**Visible-peak z9 loss.** For local maxima at least 2 dBZ above their strongest neighbor, dBZ at the z9 pixel nearest the cell center, subtracted from the source. Positive means that pixel cooled. This is the pixel Mapbox will magnify. It depends on where the pixel grid falls. The harsh column is the guaranteed worst offset, independent of this frame's alignment.

**Weak lift.** Mean of (area-mean inside ±0.40 cell − source) for valid cells ≤ 10 dBZ. The ±0.40 window includes p3l's existing seam, so production is not zero. `>3 dBZ` is the fraction of those cells whose area-mean rises more than 3 dBZ. Centers of the same cells are inside the RMSE above. Clear-air leaks are samples at no-echo centers that came back finite. All four methods scored **0** leaks.

### Dickinson core

| Method | Plateau | Sub-cell RMS | Grid ac at 1 cell | Center RMSE | Visible-peak z9 loss | Weak mean lift |
| --- | --- | --- | --- | --- | --- | --- |
| p3l | 1.00 | 0.00 | 0.137 | 0 | 0.00 (1 peak) | 0.70 |
| bilinear | 0.13 | 1.20 | 0.036 | 0 | 0.27 | 1.48 |
| bicubic clipped | 0.15 | 1.14 | 0.036 | 0 | 0.06 | 1.45 |
| bilinear + peak hold | 0.16 | 1.18 | 0.036 | 0 | 0.00 | 1.48 |

Wide Dickinson window (1223 cells, 6 visible peaks, 71 weak cells): plateau 1.00 / 0.13 / 0.17 / 0.19. Bilinear's worst visible-peak z9 loss is 0.53 dBZ. Peak-hold's is 0. Weak cells with lift > 3 dBZ: p3l 0%, the three candidates about 6%.

### Belle Fourche

| Method | Plateau | Sub-cell RMS | Grid ac at 1 cell | Center RMSE | Visible-peak z9 loss | Weak mean lift | Weak lift > 3 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| p3l | 1.00 | 0.00 | 0.180 | 0 | 0.00 (8 peaks) | 0.88 | 2% |
| bilinear | 0.10 | 1.21 | 0.045 | 0 | 0.98 | 1.84 | 17% |
| bicubic clipped | 0.14 | 1.09 | 0.047 | 0 | 0.03 | 1.67 | 15% |
| bilinear + peak hold | 0.16 | 1.17 | 0.049 | 0 | 0.00 | 1.84 | 17% |

### Magenta, two ways

Worst-case z9 half-pixel, one hot cell in a field of 30 dBZ:

| Source | p3l | bilinear | bicubic | peak hold |
| --- | --- | --- | --- | --- |
| 68 | 68.0 | 59.7 | 65.6 | 68.0 |
| 65 | 65.0 | 57.4 | 62.8 | 65.0 |

Planted into the real Dickinson neighborhood (neighbors are the observed ~46–55 dBZ field, and this frame's z9 pixel happens to sit close to the center):

| Cell | p3l | bilinear | bicubic | peak hold | peak hold, also pin ≥65 |
| --- | --- | --- | --- | --- | --- |
| 68 local max | 68.0 | 67.3 | 67.9 | 68.0 | 68.0 |
| 66 shoulder (not a local max) | 66.0 | 65.2 | 65.8 | 65.2 | 66.0 |

The planted pixels stay magenta for every method **on this alignment**. That is why the harsh column is the gate. A 65 dBZ cell with cold neighbors loses magenta under bilinear and under bicubic as soon as the pixel center is half a pixel off the source cell. Peak-hold does not, because that offset (0.14 cell) is inside the 0.28 core p3l already treats as exact.

The ≥65 pin is a sensitivity, not a fourth candidate. It matters for a magenta shoulder that is cooler than a neighbor. On this probe the shoulder stayed at 65.2 without it.

## Crops

Dickinson, z9 tiles upscaled the way Mapbox did on the iPad shot:

![Dickinson overzoom](dickinson_mapbox_overzoom.png)

Dickinson core at 8 samples per native cell. Nearest cell is the source grid. p3l is flat inside each cell. The three candidates carry the neighbor-to-neighbor steps as ramps:

![Dickinson core field](dickinson_core_field.png)

Belle Fourche, same z9 overzoom:

![Belle Fourche overzoom](belle_fourche_mapbox_overzoom.png)

Planted 68 / 66 on the Dickinson core (not observed). Labels are the z9 pixel nearest each planted cell:

![Magenta probe](magenta_probe_mapbox_overzoom.png)

## What stays locked

float64 lat/lon axes, native max zoom 9, palette p3k, spatial p3l on the default path, no seam retune, weak/no-echo category mask, magenta handled by keeping source dBZ on local maxima, loop architecture untouched. No blur, no sharpen, no artificial contour bands, no Phase 4, no production stamp change.
