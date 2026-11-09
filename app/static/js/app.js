/* Global app JS — runs on every page after Materialize is loaded */

// CSRF: the server refuses a request that is not GET/HEAD/OPTIONS without the session's
// token. The page's <meta name="csrf-token"> holds it; forms carry it as a hidden field.
window.csrfToken = function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
};

// Every unsafe jQuery request sends the token in the X-CSRF-Token header (registered before
// any page script runs; a fetch() call adds the header itself with window.csrfToken()).
$.ajaxPrefilter(function (options, originalOptions, jqXHR) {
  if (!options.crossDomain && !/^(GET|HEAD|OPTIONS)$/i.test(options.type)) {
    jqXHR.setRequestHeader('X-CSRF-Token', window.csrfToken());
  }
});

$(function () {
  // Init Materialize components that exist on the page
  if ($('.sidenav').length)  M.Sidenav.init($('.sidenav')[0]);
  if ($('.modal').length)    M.Modal.init($('.modal').toArray());
  if ($('.tooltipped').length) M.Tooltip.init($('.tooltipped').toArray());
  if ($('select.browser-default').length) {
    // We use browser-default selects; no Materialize init needed
  }

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
