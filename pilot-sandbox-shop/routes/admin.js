let cacheEntries = 3;

function clearCache(req, res) {
  cacheEntries = 0;
  res.send(204);
}

module.exports = function mountAdmin(app) {
  app.del('/admin/cache', clearCache);
  app.get('/admin/cache', (req, res) => res.json({ entries: cacheEntries }));
};
