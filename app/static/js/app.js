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

// Text made safe to put into markup, an attribute value included.
window.wfEscape = function (s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
};

// The markup of one icon, as the template macro writes it. `classes` are extra class names
// (tiny, small, left, ...); `label`, when given, is the icon's accessible name.
window.wfIcon = function (name, classes, label) {
  var esc = window.wfEscape;
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

// Market prices. Every display of an item's price is a .wf-price[data-item=<id>] (the template
// macro in _prices.html, or wfPriceMarkup below for a page script): its value, an "old" mark
// with the fetch time, and a refresh button (.wf-price-refresh) beside an old or missing price.
// A click asks GET /market/price/<id> (the URL for item 0 is in <meta name="market-price-url">)
// for that one item and updates every display of it on the page. A click while that item's
// request runs is ignored. A refresh that fails (ok false: 502 with the kept price, a 404, any
// error) keeps the shown price, marked old with the kept fetch time, and says so beside it and
// in a toast. Values from an answer are inserted as text, never as markup. After an ok refresh
// the handler triggers 'wf-price-refreshed' on document with the item id (a string of digits),
// so a page can follow the new price (the Farming Planner redraws its buy-or-farm answers);
// a failed refresh triggers nothing.
var WF_ITEM_ID = /^[1-9][0-9]*$/;
var wfPriceBusy = {};

window.wfPriceText = function (platinum) {
  return (typeof platinum === 'number' && isFinite(platinum)) ? platinum + 'p' : '—';
};

window.wfPriceUrl = function (itemId) {
  var meta = document.querySelector('meta[name="market-price-url"]');
  return (meta ? meta.getAttribute('content') : '').replace(/\/0$/, '/') + itemId;
};

// The markup of one item's price, as the template macro writes it, from a price view
// (`platinum`, `stale`, `fetched_label`, `failed`, `error`, `refresh`, `market_url`); `name`
// names the button, `link` adds the warframe.market link.
window.wfPriceMarkup = function (itemId, p, name, link) {
  var esc = window.wfEscape, id = String(itemId);
  if (!WF_ITEM_ID.test(id) || !p) return '';
  var html = '<span class="wf-price" data-item="' + id + '"><span class="wf-price-value">' +
             esc(window.wfPriceText(p.platinum)) + '</span>';
  if (p.stale) {
    html += ' <span class="wf-price-old orange-text" title="Fetched ' + esc(p.fetched_label) + '">' +
            'old (' + esc(p.fetched_label) + ')</span>';
  }
  if (p.failed) {
    html += ' <span class="wf-price-failed red-text" title="' + esc(p.error || '') + '">not refreshed</span>';
  }
  if (p.refresh) {
    html += ' <button type="button" class="wf-price-refresh" data-item="' + id + '"' +
            ' title="Ask warframe.market for this item&#39;s current price"' +
            ' aria-label="Refresh the price' + (name ? ' of ' + esc(name) : '') + ' from warframe.market">' +
            wfIcon('sync', 'tiny') + '</button>';
  }
  if (link && p.market_url) {
    html += ' <a href="' + esc(p.market_url) + '" target="_blank" rel="noopener" class="wf-price-link grey-text"' +
            ' title="Open on warframe.market">' + wfIcon('open_in_new', 'tiny', 'Open on warframe.market') + '</a>';
  }
  return html + '</span>';
};

// Show one refresh answer in every display of the item: `ok` the fresh price (mark and button
// gone), otherwise the kept price and fetch time when the answer has them, marked old, and
// a "not refreshed" note whose title is the error.
// wfPriceShowIn does it for any set of price displays (a component's, in the Farming Planner).
function wfPriceShow(itemId, answer, ok, error) {
  var label = window.wfPriceShowIn($('.wf-price[data-item="' + itemId + '"]'), answer, ok, error);
  if (label) $('.wf-price-fetched[data-item="' + itemId + '"]').text(label);
}

// Show one refresh answer in each of `$prices` (elements holding a .wf-price-value, and a
// .wf-price-old and a refresh button, .wf-price-refresh or a component's
// .wf-comp-price-refresh, when shown); returns the answer's fetch time or null.
window.wfPriceShowIn = function ($prices, answer, ok, error) {
  var hasPrice = !!answer && Object.prototype.hasOwnProperty.call(answer, 'platinum');
  var label = answer && typeof answer.fetched_label === 'string' ? answer.fetched_label : null;
  $prices.each(function () {
    var $p = $(this), $value = $p.find('.wf-price-value');
    if (ok || hasPrice) $value.text(window.wfPriceText(answer.platinum));
    $p.find('.wf-price-failed').remove();
    if (ok) {
      var $button = $p.find('.wf-price-refresh, .wf-comp-price-refresh');
      var focused = $button.is(':focus');
      $p.find('.wf-price-old').add($button).remove();
      if (focused) $value.attr('tabindex', '-1').trigger('focus');
      return;
    }
    var $old = $p.find('.wf-price-old');
    if (label) {
      if (!$old.length) {
        $old = $('<span class="wf-price-old orange-text"></span>');
        $value.after($old).after(' ');
      }
      $old.text('old (' + label + ')').attr('title', 'Fetched ' + label);
    }
    var $note = $('<span class="wf-price-failed red-text">not refreshed</span>').attr('title', error);
    ($old.length ? $old : $value).after($note).after(' ');
  });
  return label;
};

$(document).on('click', '.wf-price-refresh', function (e) {
  e.preventDefault();
  var itemId = String($(this).attr('data-item') || '');
  if (!WF_ITEM_ID.test(itemId) || wfPriceBusy[itemId]) return;
  wfPriceBusy[itemId] = true;
  var $prices = $('.wf-price[data-item="' + itemId + '"]').attr('aria-busy', 'true');
  var failed = function (answer, fallback) {
    var error = (answer && typeof answer.error === 'string' && answer.error) || fallback;
    wfPriceShow(itemId, answer, false, error);
    var note = document.createElement('span');
    note.textContent = 'The price was not refreshed: ' + error;
    M.toast({html: note});
  };
  $.ajax({
    url: window.wfPriceUrl(itemId),
    method: 'GET',
    dataType: 'json',
    success: function (r) {
      if (r && r.ok === true) {
        wfPriceShow(itemId, r, true, null);
        $(document).trigger('wf-price-refreshed', [itemId]);
      } else failed(r, 'no answer.');
    },
    error: function (xhr) { failed(xhr.responseJSON, 'the request failed.'); },
    complete: function () {
      delete wfPriceBusy[itemId];
      $prices.removeAttr('aria-busy');
    }
  });
});

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
