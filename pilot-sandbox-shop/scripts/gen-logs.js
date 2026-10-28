// Deterministic generator for logs/shop-api.jsonl (about 300 lines over 7 days).
// Run: node scripts/gen-logs.js > logs/shop-api.jsonl
let seed = 42;
const rand = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };
const pick = (items) => items[Math.floor(rand() * items.length)];

const end = Date.UTC(2026, 9, 28, 10, 30, 0);
const start = end - 7 * 24 * 3600 * 1000;
const lines = [];

const normal = [
  ['GET', '/orders/1', 200], ['POST', '/orders', 201], ['GET', '/invoices/1/pdf', 200],
  ['GET', '/greeting', 200], ['GET', '/admin/cache', 200],
];
const warnings = [
  { route: '/orders/:id', msg: 'express deprecated req.param(name): Use req.params, req.body, or req.query instead at routes/orders.js:8:25' },
  { route: '/orders', msg: 'express deprecated res.json(obj, status): Use res.status(status).json(obj) instead at routes/orders.js:18:14' },
  { route: '/invoices/:id/pdf', msg: 'express deprecated res.sendfile: Use res.sendFile instead at routes/invoices.js:11:7' },
  { route: '/greeting', msg: 'express deprecated req.acceptsLanguage: Use acceptsLanguages instead at routes/i18n.js:3:22' },
];

for (let i = 0; i < 300; i++) {
  const ts = new Date(start + Math.floor(rand() * (end - start))).toISOString();
  if (rand() < 0.38) {
    const w = pick(warnings);
    lines.push({ ts, service: 'shop-api', level: 'warn', msg: w.msg, route: w.route });
  } else {
    const [method, route, status] = pick(normal);
    lines.push({ ts, service: 'shop-api', level: 'info', msg: `${method} ${route} ${status} ${Math.floor(5 + rand() * 80)}ms`, route });
  }
}
lines.sort((a, b) => a.ts.localeCompare(b.ts));
process.stdout.write(lines.map((l) => JSON.stringify(l)).join('\n') + '\n');
