/* Global app JS — runs on every page after Materialize is loaded */

// CSRF: the server refuses a request that is not GET/HEAD/OPTIONS without the session's
// token. The page's <meta name="csrf-token"> holds it; forms carry it as a hidden field.
window.csrfToken = function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
};

// Icons: the app's own SVG sprite (its URL in <meta name="icon-sprite">), one symbol i-<name>
// per icon. Page scripts draw an icon with wfIcon and swap one with wfSetIcon; they never
// write icon markup or text by hand. A name is a lower-case word with digits and underscores.
var WF_ICON_NAME = /^[a-z0-9_]+$/;

window.wfIconHref = function (name) {
  if (!WF_ICON_NAME.test(name)) throw new Error('bad icon name');
  var meta = document.querySelector('meta[name="icon-sprite"]');
  return (meta ? meta.getAttribute('content') : '') + '#i-' + name;
};

// The markup of one icon, as the template macro writes it. `classes` are extra class names
// (tiny, small, left, ...); `label`, when given, is the icon's accessible name.
window.wfIcon = function (name, classes, label) {
  var esc = function (s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  };
  var cls = 'wf-icon' + (classes ? ' ' + esc(classes) : '');
  var a11y = label ? ' role="img" aria-label="' + esc(label) + '"' : ' aria-hidden="true"';
  return '<svg class="' + cls + '" width="24" height="24" focusable="false"' + a11y + '>' +
         '<use href="' + esc(window.wfIconHref(name)) + '"></use></svg>';
};

// Swap the icon an element draws (the <svg> itself, or the first one inside it) to `name`.
// Only the reference changes: the icon keeps its classes and its accessible name.
window.wfSetIcon = function (el, name) {
  var svg = el && (el.matches && el.matches('svg.wf-icon') ? el : el.querySelector('svg.wf-icon'));
  if (!svg) return;
  var use = svg.querySelector('use');
  if (use) use.setAttribute('href', window.wfIconHref(name));
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
