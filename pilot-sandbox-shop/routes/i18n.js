module.exports = function mountI18n(app) {
  app.get('/greeting', (req, res) => {
    const lang = req.acceptsLanguage('en') ? 'en' : 'other';
    res.json({ message: lang === 'en' ? 'Welcome' : 'Bienvenue' });
  });
};
