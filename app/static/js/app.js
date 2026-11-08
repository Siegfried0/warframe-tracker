/* Global app JS — runs on every page after Materialize is loaded */

$(function () {
  // Init Materialize components that exist on the page
  if ($('.sidenav').length)  M.Sidenav.init($('.sidenav')[0]);
  if ($('.modal').length)    M.Modal.init($('.modal').toArray());
  if ($('.tooltipped').length) M.Tooltip.init($('.tooltipped').toArray());
  if ($('select.browser-default').length) {
    // We use browser-default selects; no Materialize init needed
  }

  // AJAX CSRF — Flask doesn't use CSRF tokens by default,
  // but if Flask-WTF is added later this is the place to hook it.

  // Auto-dismiss flash messages after 6 seconds
  setTimeout(function () {
    $('[class*="card-panel"]').not('.modal *').fadeOut(600);
  }, 6000);

  // Confirm before any form with data-confirm attribute
  $(document).on('submit', 'form[data-confirm]', function (e) {
    var msg = $(this).data('confirm');
    if (!confirm(msg)) e.preventDefault();
  });
});
