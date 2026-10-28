const request = require('supertest');
const assert = require('assert');
const { makeApp } = require('./helpers');
const invoices = require('../routes/invoices');

describe('invoices', () => {
  it('serves the invoice pdf', async () => {
    const app = makeApp((a) => a.use('/invoices', invoices));
    const res = await request(app).get('/invoices/1/pdf').expect(200);
    assert.ok(res.body.toString().includes('sample invoice'));
  });
});
