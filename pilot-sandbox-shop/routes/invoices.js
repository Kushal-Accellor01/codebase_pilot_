const path = require('path');
const express = require('express');

const router = express.Router();

function pdfPath(id) {
  return path.join(__dirname, '..', 'data', `invoice-${id}.pdf`);
}

router.get('/:id/pdf', (req, res) => {
  res.sendfile(pdfPath(req.params.id));
});

module.exports = router;
