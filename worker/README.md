# Optional Cloudflare Worker

Serves cooked radar objects from the R2 bucket with CORS headers so a browser map (MapLibre / Leaflet) can load tiles from a custom domain.

## Bindings

| Binding | Purpose |
| --- | --- |
| `RADAR_BUCKET` | R2 bucket the EC2 cooker uploads to (`R2_BUCKET`) |

Keys match cooker output: `clean/latest/{z}/{x}/{y}.png` (composite), `rala/clean/latest/{z}/{x}/{y}.png`, `manifest.json`, `colorbar.png`. If `R2_PREFIX=radar`, either set the Worker to strip a prefix or upload with an empty prefix.

## Deploy

```bash
cd worker
npx wrangler login
npx wrangler r2 bucket create mpwg-radar   # once
npx wrangler deploy
```

Set `ALLOWED_ORIGIN` in `wrangler.toml` or the dashboard if you do not want `*`.

The cooker can also use a **public R2 custom domain** without this Worker. Use the Worker when you need CORS, auth-free GET, or empty-tile 200s.
