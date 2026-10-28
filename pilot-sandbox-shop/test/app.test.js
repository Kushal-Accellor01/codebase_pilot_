const request = require('supertest');

describe('app', () => {
  it('boots and answers the order route', async () => {
    const app = require('../app');
    await request(app).get('/orders/1').expect(200);
  });
});
