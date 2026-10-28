const request = require('supertest');
const assert = require('assert');
const { makeApp } = require('./helpers');
const mountI18n = require('../routes/i18n');

describe('i18n', () => {
  it('greets in English when accepted', async () => {
    const app = makeApp(mountI18n);
    const res = await request(app).get('/greeting').set('Accept-Language', 'en').expect(200);
    assert.strictEqual(res.body.message, 'Welcome');
  });
});
