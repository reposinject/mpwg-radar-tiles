# Final reconstruction harness — NOT PRODUCTION

Offline comparison for the 2026-09-29 James Final RALA Renderer Push. Do not merge. Do not deploy to the production cooker. `MPWG_RALA_DBZ_INTERP` stays default off. C stays harness-only. D is the review flag `monotone_pchip` and uses this same monotone cubic sampler with no retune. See `docs/p3n-dbz-interp/D_REVIEW_COOK.md`.

Frame `20260929T004243Z`. Same crop windows as the earlier judge set.

| Column | Method |
| --- | --- |
| RAW | nearest native cell |
| A | `bilinear_peak_hold` |
| B | `tight_peak_hold` |
| C | bounded cubic, kept only inside the 2×2 range |
| D | monotone cubic Hermite (Fritsch–Carlson) |

Each method directory holds a uint16 numerical field, an 8-bit preview, the p3k colored field, a sample-center overlay, a source-vs-reconstructed CSV, and an error image. Contact sheets at this level are labeled NOT PRODUCTION. Numbers are in `metrics.json`. The write-up and the recommendation (D, James picks) are in `docs/p3n-dbz-interp/P3N_BOUNDED_DBZ_INTERP_2026-09-29.md`.

These pictures are the dense reconstruction (8 samples per 0.01° cell, 16 on the intense core). They are not Mapbox screenshots. Cooker tiles remain 512×512 PNG at native zoom 9. Mapbox linearly filters that PNG when the map zooms past 9, which widens the ramp already in the tile.

The intense ≥65 core plants one Dickinson cell (46.945°N, 102.855°W) from the observed 58.0 dBZ to 68.0 so a magenta cell exists on this frame. That 68 is not observed dBZ.

```bash
PYTHONPATH=src python3 scripts/p3n_final_harness.py \
  --grib MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz \
  --out docs/p3n-dbz-interp/judge/final-harness
```
