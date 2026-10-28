const express = require('express');

// Each test builds only the routes it exercises, so a breaking change shows up as a failure
// in the test (and a stack frame) that belongs to the affected route.
function makeApp(...mounts) {
  const app = express();
  app.use(express.json());
  mounts.forEach((mount) => mount(app));
  app.use((err, req, res, next) => { // eslint-disable-line no-unused-vars
    console.error(err.stack);
    res.status(500).json({ error: err.message });
  });
  return app;
}

module.exports = { makeApp };
