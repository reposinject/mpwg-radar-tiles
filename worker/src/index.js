/**
 * Optional CORS tile front for MPWG radar objects in R2.
 *
 * Missing PNG keys return a 512×512 transparent tile so map clients do not 404
 * when the cooker skips empty tiles. Frame JSON / manifest stay 404 if absent.
 */

const TRANSPARENT_PNG_1X1 = Uint8Array.from(
  atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="),
  (c) => c.charCodeAt(0)
);

function corsHeaders(origin, extra = {}) {
  const allow = origin || "*";
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Range",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
    ...extra,
  };
}

function allowedOrigin(request, env) {
  const configured = (env.ALLOWED_ORIGIN || "*").trim();
  const origin = request.headers.get("Origin");
  if (configured === "*") return "*";
  const ok = configured.split(",").map((s) => s.trim());
  if (origin && ok.includes(origin)) return origin;
  return ok[0] || "*";
}

function contentType(key) {
  if (key.endsWith(".png")) return "image/png";
  if (key.endsWith(".json")) return "application/json; charset=utf-8";
  return "application/octet-stream";
}

function cacheControl(key) {
  if (key.endsWith("manifest.json")) return "public, max-age=15";
  if (key.includes("/latest/")) return "public, max-age=30";
  return "public, max-age=120";
}

export default {
  async fetch(request, env) {
    const origin = allowedOrigin(request, env);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", {
        status: 405,
        headers: corsHeaders(origin),
      });
    }

    const url = new URL(request.url);
    let key = url.pathname.replace(/^\/+/, "");
    if (!key || key === "health") {
      return new Response(JSON.stringify({ ok: true, service: "mpwg-radar-tiles" }), {
        headers: {
          ...corsHeaders(origin),
          "Content-Type": "application/json",
        },
      });
    }

    if (!env.RADAR_BUCKET) {
      return new Response("R2 bucket binding RADAR_BUCKET is not configured", {
        status: 500,
        headers: corsHeaders(origin),
      });
    }

    const object = await env.RADAR_BUCKET.get(key);
    if (!object) {
      if (key.endsWith(".png")) {
        // Cooker omits fully transparent tiles; serve a blank 512-capable PNG.
        return new Response(TRANSPARENT_PNG_1X1, {
          status: 200,
          headers: {
            ...corsHeaders(origin),
            "Content-Type": "image/png",
            "Cache-Control": "public, max-age=30",
            "X-MPWG-Empty-Tile": "1",
          },
        });
      }
      return new Response("Not found", { status: 404, headers: corsHeaders(origin) });
    }

    const headers = corsHeaders(origin, {
      "Content-Type": object.httpMetadata?.contentType || contentType(key),
      "Cache-Control": object.httpMetadata?.cacheControl || cacheControl(key),
      ETag: object.httpEtag,
    });
    if (request.method === "HEAD") {
      return new Response(null, { headers });
    }
    return new Response(object.body, { headers });
  },
};
