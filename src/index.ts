// conversordepdf.pro conversion API — Cloudflare Worker + Containers.
// Route: conversordepdf.pro/api/* (same origin as the site → no CORS needed).
// POST /api/convert/<tool>  (multipart: file, optional level)
import { DurableObject } from 'cloudflare:workers';

export interface Env {
  CONVERTER: DurableObjectNamespace<Converter>;
  RATE_LIMITER: RateLimit;
  POOL_SIZE: string;
  SITE_ORIGIN: string;
}

const PORT = 8080;
const MAX_BYTES = 50 * 1024 * 1024;
const IDLE_MS = 5 * 60 * 1000; // container sleeps after 5 min without requests (no cost while asleep)
const TOOLS = new Set([
  'word-para-pdf', 'excel-para-pdf', 'powerpoint-para-pdf', 'pdf-para-pdfa', 'comprimir-pdf',
  'pdf-para-excel', 'pdf-para-powerpoint', 'pdf-para-epub', 'pdf-para-mobi', 'epub-para-mobi', 'pdf-para-xml',
  'pdf-para-word',
]);

const json = (status: number, error: string) =>
  new Response(JSON.stringify({ error }), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
  });

/** One Durable Object per container instance. Starts the container on demand and proxies to it. */
export class Converter extends DurableObject<Env> {
  private starting: Promise<void> | null = null;

  private async ensureRunning() {
    const c = this.ctx.container!;
    if (c.running) return;
    this.starting ??= (async () => {
      c.start({ image: c.images.base, instance: 'standard-2', enableInternet: false });
      c.monitor().catch((e) => console.error('container exited', e)).finally(() => { this.starting = null; });
      for (let i = 0; i < 150; i++) {
        try {
          const r = await c.getTcpPort(PORT).fetch('http://container/health');
          if (r.ok) return;
        } catch { /* still booting */ }
        await new Promise((r) => setTimeout(r, 200));
      }
      throw new Error('container did not become healthy');
    })();
    try { await this.starting; } catch (e) { this.starting = null; throw e; }
  }

  async fetch(req: Request): Promise<Response> {
    await this.ensureRunning();
    await this.ctx.container!.setInactivityTimeout(IDLE_MS);
    const url = new URL(req.url);
    // /api/convert/<tool>/ → /convert/<tool> (no trailing slash, avoids a redirect inside the container)
    const path = url.pathname.replace(/^\/api/, '').replace(/\/+$/, '');
    return this.ctx.container!.getTcpPort(PORT).fetch(`http://container${path}`, req);
  }
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const m = url.pathname.match(/^\/api\/convert\/([a-z0-9-]+)\/?$/);
    if (!m) return json(404, 'Não encontrado.');
    if (req.method !== 'POST') return json(405, 'Método não permitido.');
    if (!TOOLS.has(m[1])) return json(404, 'Ferramenta indisponível.');

    // Same-origin only: block other sites from using our converter.
    const origin = req.headers.get('Origin');
    if (origin && origin !== env.SITE_ORIGIN) return json(403, 'Origem não permitida.');

    const len = Number(req.headers.get('Content-Length') || 0);
    if (!len) return json(411, 'Envie um arquivo.');
    if (len > MAX_BYTES + 64 * 1024) return json(413, 'Arquivo muito grande (máximo de 50 MB).');

    const ip = req.headers.get('CF-Connecting-IP') ?? 'unknown';
    const { success } = await env.RATE_LIMITER.limit({ key: ip });
    if (!success) return json(429, 'Muitas conversões seguidas. Aguarde um minuto e tente de novo.');

    const pool = Math.max(1, Number(env.POOL_SIZE) || 1);
    const stub = env.CONVERTER.getByName(`pool-${Math.floor(Math.random() * pool)}`);
    try {
      return await stub.fetch(req);
    } catch (e) {
      console.error('converter error', e);
      return json(503, 'O conversor está iniciando. Tente de novo em alguns segundos.');
    }
  },
} satisfies ExportedHandler<Env>;
