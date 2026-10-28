const request = require('supertest');
const assert = require('assert');
const { makeApp } = require('./helpers');
const orders = require('../routes/orders');

const build = () => makeApp((app) => app.use('/orders', orders));

describe('orders', () => {
  it('returns an order by id', async () => {
    const res = await request(build()).get('/orders/1').expect(200);
    assert.strictEqual(res.body.item, 'book');
  });

  it('returns 404 for an unknown order', async () => {
    await request(build()).get('/orders/99').expect(404);
  });

  it('creates an order and answers 201', async () => {
    const res = await request(build()).post('/orders').send({ item: 'pen', qty: 3 }).expect(201);
    assert.strictEqual(res.body.item, 'pen');
  });
});
