# Sign-in and accounts

The app has three ways to sign in: local accounts with a username and password (always, unless
Azure EasyAuth is on), an OpenID Connect provider beside them (optional), and Azure App Service
EasyAuth instead of both. Every account has its own progress records and one of two roles,
`admin` or `user`. Admins manage the accounts in the admin panel
(see [Local accounts and roles](#local-accounts-and-roles)).

## Local accounts and roles

- **First-run setup.** On an empty database the app sends you to the setup page, which creates
  the first account as an `admin`. The setup page creates the admin only while the database has
  no account at all. If another setup or a first OpenID Connect sign-in made the first account in
  the meantime, the form is refused and you are sent to the login page.
- **Registration.** Anyone who reaches the login page can register an account of their own
  (`user` role); a password has at least 8 characters.
- **Admins** can also create local accounts, change roles, deactivate, delete and reset the
  password of accounts in the admin panel.
- A local account changes its own password in **Settings → Change Password**.

## Sign-in with an OpenID Connect provider

Keycloak, Authentik, Google, … Beside the local login, the app can sign users in through any
OpenID Connect provider:

1. Register the app at your provider as a *confidential* client using the authorization code
   flow, with the redirect URI `https://<your host>/auth/oidc/callback` (the exact address users
   reach the app at).
2. Set all four variables: `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ISSUER_URL` (the
   provider's issuer, for example `https://id.example.com/realms/home`; the app reads
   `<issuer>/.well-known/openid-configuration`) and `OIDC_REDIRECT_URI` (the same redirect URI).
   They are read from the environment only, so the secret never ends up in the database, a
   backup or an export.
3. Restart the app. The login page (and, on an empty database, the setup page) shows **Sign in
   with OpenID Connect**.

Notes:
- With only some of the four set, OIDC sign-in stays off and the startup log names the missing
  variables. With `EASYAUTH_ENABLED=true`, OIDC sign-in is off.
- The provider must be reached over `https`; plain `http` is accepted only for a provider on the
  same machine (`localhost`, `127.0.0.1`, `::1`). The app trusts the ID token because it comes
  straight from the provider's token endpoint over TLS, so `OIDC_ISSUER_URL` must name a provider
  you trust.
- **Who can sign up:** without the two `OIDC_ALLOWED_*` variables, anyone your provider signs
  in gets a `user` account on their first sign-in, as with self-registration. With a public
  provider (Google, GitHub) that is anyone with an account there. To restrict it, set one or
  both (with both, a user must pass both):
  - `OIDC_ALLOWED_EMAIL_DOMAINS=example.com, example.org`: the email the provider gives must end
    in `@` and one of these domains exactly (case does not matter; `sub.example.com` is not
    `example.com`, list it on its own). The provider must vouch for the email: one it marks as
    not verified is refused; use a provider that only gives addresses it has checked.
  - `OIDC_ALLOWED_GROUPS=tracker-users`: the user must be in one of these groups, spelled
    exactly as the provider sends them (case matters; some send ids or paths such as
    `/tracker-users`). The provider must be set up to put a `groups` claim into the ID token.

  The check runs at every sign-in: tightening a list, or removing someone from the group at the
  provider, refuses their existing account at its next sign-in (the account and its data are
  kept; a session already signed in runs on until it ends). A refused user sees "Your account
  at the identity provider is not allowed to sign in to this app." A variable that is set but
  empty (or only commas and blanks) turns OIDC sign-in off and the startup log names it. An
  admin can also deactivate or delete accounts.
- On an empty database the first account, local or OIDC, becomes the admin.
- An OIDC account is never linked to another account, not even one with the same name or
  email. On the first sign-in the username is the provider's `preferred_username` (else the
  email); when another account already has that name, the new account gets the first free of
  `name-2`, `name-3`, … (at most 64 characters). When another account already uses the email,
  the new account is created without an email. When an Azure EasyAuth account of an earlier
  setup holds the same provider id, the sign-in is refused. Passwords cannot be reset for OIDC
  accounts.
- Logging out ends the session in this app only; you stay signed in at your provider.
- Pointing `OIDC_ISSUER_URL` at another provider later gives its users new accounts; the old
  ones remain until an admin removes them.

## Azure EasyAuth

On Azure App Service, `EASYAUTH_ENABLED=true` makes the platform's sign-in the only one: the
app reads the signed-in user from the headers EasyAuth adds. EasyAuth must also be enabled in
the Azure portal. How it behaves is on the [Azure page](azure.md#easyauth).

## The return page after login

After a login (local or OpenID Connect) the app returns to the page that asked for it only when
that is a page of this app; any other address (another site, `//host`, a backslash, a
`javascript:` link, or an encoded form of these) goes to the dashboard. So does an address
longer than 200 characters (counted as written, before any `%` encoding is undone): it is never
shortened, and the sign-in in progress always fits in the browser's session cookie.

## Forms and cross-site requests

Every form and every change the pages send carries a per-session security token, and the server
refuses a change without it (protection against cross-site request forgery). Logging out is a
button in the menu that sends it too. A form left open across a login, a logout or an expired
session is refused once with a page saying "Reload the page and try again"; after a reload it
works, in every tab that reloads. A script or tool outside the browser that posts to the app
must first read the token from a page (`<meta name="csrf-token">`) and send it in the
`X-CSRF-Token` header with the same session cookie.

The session cookie's `SameSite` attribute (`SESSION_COOKIE_SAMESITE`, default `Lax`) keeps
browsers from sending it on cross-site posts as well; see [Configuration](configuration.md).
