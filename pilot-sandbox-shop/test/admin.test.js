const request = require('supertest');
const assert = require('assert');
const { makeApp } = require('./helpers');
const mountAdmin = require('../routes/admin');

describe('admin', () => {
  it('clears the cache with 204', async () => {
    const app = makeApp(mountAdmin);
    await request(app).delete('/admin/cache').expect(204);
    const res = await request(app).get('/admin/cache').expect(200);
    assert.strictEqual(res.body.entries, 0);
  });
});
