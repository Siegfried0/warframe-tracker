"""CSRF protection: one secret token per session, checked for every unsafe request.

The token is created on first use and kept in the signed session cookie; every form of a
session and every tab share it. It is replaced when a user signs in or out, so a token seen
before a sign-in is not the signed-in session's token. Every request whose method is not
GET, HEAD or OPTIONS must carry it, as the form field ``csrf_token`` or the header
``X-CSRF-Token``; otherwise it is refused with 400 before any route runs and before
anything is written. No route is exempt: the check reads the request, not a list of routes.
"""
import hmac
import secrets

from flask import jsonify, make_response, render_template, request, session
from markupsafe import Markup, escape
from werkzeug.exceptions import RequestEntityTooLarge

from services.import_reader import MAX_REQUEST_BYTES

SESSION_KEY = '_csrf_token'
FIELD_NAME = 'csrf_token'
HEADER_NAME = 'X-CSRF-Token'
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
FORM_MIMETYPES = frozenset({'application/x-www-form-urlencoded', 'multipart/form-data'})

# The largest body the check parses to find the form field: the largest request any route
# accepts (the import's upload). A larger form is refused unread.
FORM_PARSE_LIMIT = MAX_REQUEST_BYTES

REFUSED = ('Refused, nothing changed: this form has expired or was not sent from this '
           'site. Reload the page and try again.')
TOO_LARGE = ('Refused, nothing changed: the request is larger than this site accepts '
             f'({MAX_REQUEST_BYTES // (1024 * 1024)} MiB). Go back and choose a smaller file.')


class _TooLarge(Exception):
    pass


def get_token() -> str:
    """The session's token, created on first use."""
    token = session.get(SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def rotate_token() -> None:
    """Forget the session's token (at sign-in and sign-out): the old one is refused from
    now on, and the next page the session renders gets a new one."""
    session.pop(SESSION_KEY, None)


def hidden_field() -> Markup:
    """The hidden field every ``<form method="post">`` carries."""
    return Markup(f'<input type="hidden" name="{FIELD_NAME}" value="{escape(get_token())}">')


def _sent_token() -> str:
    """The token the request sent: the header when present, else the form field of a form
    body. Never the query string: a token in a URL leaks into logs and history."""
    header = request.headers.get(HEADER_NAME)
    if header is not None:
        return header
    if request.mimetype not in FORM_MIMETYPES:
        return ''
    if request.max_content_length is None or request.max_content_length > FORM_PARSE_LIMIT:
        request.max_content_length = FORM_PARSE_LIMIT
    try:
        return request.form.get(FIELD_NAME, '')
    except RequestEntityTooLarge as exc:
        raise _TooLarge() from exc


def token_is_valid() -> bool:
    """True when the request sent exactly the session's token (constant-time compare)."""
    expected = session.get(SESSION_KEY)
    if not isinstance(expected, str) or not expected:
        return False
    sent = _sent_token()
    if not sent:
        return False
    # compare_digest takes str only when it is ASCII: compare UTF-8 bytes instead.
    return hmac.compare_digest(sent.encode('utf-8', 'replace'), expected.encode('utf-8'))


def wants_json() -> bool:
    """A JSON body, an AJAX request, or an Accept header preferring JSON to HTML."""
    if request.is_json:
        return True
    if request.headers.get('X-Requested-With', '').lower() == 'xmlhttprequest':
        return True
    accept = request.accept_mimetypes
    return accept['application/json'] > accept['text/html']


def _refusal(message: str, status: int):
    if wants_json():
        response = jsonify(error=message)
    else:
        response = make_response(render_template('csrf_error.html', message=message))
    response.status_code = status
    return response


def check_request():
    """The ``before_request`` hook: None to go on, else the refusal (400; 413 for a form
    too large to read the token from)."""
    if request.method in SAFE_METHODS:
        return None
    try:
        if token_is_valid():
            return None
    except _TooLarge:
        return _refusal(TOO_LARGE, 413)
    return _refusal(REFUSED, 400)


def init_app(app) -> None:
    """Run the check before every other request hook (EasyAuth's sign-in among them) and
    give every template ``csrf_token()`` and ``csrf_field()``."""
    app.before_request_funcs.setdefault(None, []).insert(0, check_request)

    @app.context_processor
    def _inject_csrf():
        return {'csrf_token': get_token, 'csrf_field': hidden_field}
