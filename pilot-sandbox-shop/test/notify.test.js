const assert = require('assert');
const notify = require('../lib/notify');

describe('notify', () => {
  it('builds name=value pairs', () => {
    assert.strictEqual(notify.param('a', 1), 'a=1');
  });
});
