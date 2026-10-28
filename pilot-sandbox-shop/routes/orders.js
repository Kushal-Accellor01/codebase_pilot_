const express = require('express');
const notify = require('../lib/notify');

const router = express.Router();
const orders = new Map([[1, { id: 1, item: 'book', qty: 2 }]]);

router.get('/:id', (req, res) => {
  const id = Number(req.param('id'));
  const order = orders.get(id);
  if (!order) return res.status(404).json({ error: 'not found' });
  res.json(order);
});

router.post('/', (req, res) => {
  const order = { id: orders.size + 1, item: req.body.item, qty: req.body.qty || 1 };
  orders.set(order.id, order);
  notify.orderCreated(order);
  return res.json(order, 201);
});

module.exports = router;
