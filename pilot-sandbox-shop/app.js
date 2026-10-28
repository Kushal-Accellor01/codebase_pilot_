const express = require('express');
const orders = require('./routes/orders');
const invoices = require('./routes/invoices');
const mountAdmin = require('./routes/admin');
const mountI18n = require('./routes/i18n');
const mountFallback = require('./routes/fallback');

const app = express();
app.use(express.json());

app.use('/orders', orders);
app.use('/invoices', invoices);
mountAdmin(app);
mountI18n(app);
mountFallback(app);

// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  res.status(500).json({ error: err.message });
});

module.exports = app;
