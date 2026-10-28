module.exports = function mountFallback(app) {
  app.get('*', (req, res) => {
    res.status(404).json({ error: 'no such route' });
  });
};
