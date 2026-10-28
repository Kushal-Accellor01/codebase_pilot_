// Negative control for the impact analysis: no Express here, and it has its own param() helper
// that must never be reported as a use of req.param().
const sent = [];

function param(name, value) {
  return `${name}=${value}`;
}

function orderCreated(order) {
  sent.push(param('order', order.id));
}

module.exports = { orderCreated, param, sent };
