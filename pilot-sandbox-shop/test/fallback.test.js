const request = require('supertest');
const { makeApp } = require('./helpers');
const mountFallback = require('../routes/fallback');

describe('fallback', () => {
  it('answers 404 json for unknown routes', async () => {
    const app = makeApp(mountFallback);
    await request(app).get('/definitely/not/here').expect(404);
  });
});
