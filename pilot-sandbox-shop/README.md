# pilot-sandbox-shop

A tiny Express **4.21.2** shop API that Codebase Pilot uses as its demo target. It deliberately uses
APIs that Express 5 removed, so an upgrade to 5.x breaks it in known places.

| Planted usage | Where | Express 4.21.2 at runtime | Express 5 |
| --- | --- | --- | --- |
| `req.param('id')` | `routes/orders.js:8` | deprecation warning | removed |
| `res.json(order, 201)` | `routes/orders.js:18` | deprecation warning | signature removed |
| `res.sendfile(path)` | `routes/invoices.js:11` | deprecation warning | removed (`res.sendFile`) |
| `app.del(...)` | `routes/admin.js:9` | deprecation warning | removed (`app.delete`) |
| `res.send(204)` | `routes/admin.js:5` | deprecation warning | number body is no longer a status |
| `req.acceptsLanguage('en')` | `routes/i18n.js:3` | deprecation warning | removed (`acceptsLanguages`) |
| `app.get('*', ...)` | `routes/fallback.js:2` | works | throws at startup (`/*splat`) |

`lib/notify.js` is a negative control: it has its own `param()` helper and never touches Express.

```bash
npm ci
npm test                          # green on 4.21.2; stderr shows the deprecation warnings
npm i express@5 --no-save && npm test   # fails in the places above
npm ci                            # restore
```

Seeded logs for the telemetry step: `logs/shop-api.jsonl` (regenerate with `node scripts/gen-logs.js`).
