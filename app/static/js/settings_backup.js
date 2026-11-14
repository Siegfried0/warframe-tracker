/* Settings, the admin's backup section: show only the fields of the chosen strategy.
 *
 * Without this script both strategies' fields are shown and work. The fields of the strategy
 * not chosen are hidden, never disabled: a disabled field is not submitted, and every save
 * sends all six fields. */
(function () {
  function showChosen(form) {
    var checked = form.querySelector('input[name="strategy"]:checked');
    var chosen = checked ? checked.value : null;
    form.querySelectorAll('fieldset[data-strategy]').forEach(function (fields) {
      fields.hidden = chosen !== null && fields.getAttribute('data-strategy') !== chosen;
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('form.backup-retention').forEach(function (form) {
      showChosen(form);
      form.addEventListener('change', function (event) {
        if (event.target && event.target.name === 'strategy') showChosen(form);
      });
    });
  });
})();
