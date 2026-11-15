# Accounts and administration

The app is multi-user: each account keeps independent progress records (collection, goals,
watchlist, preferences). Accounts sign in locally, through an OpenID Connect provider or through
Azure EasyAuth (see [Sign-in and accounts](../sign-in.md)), and have one of two roles:

- **user** — their own data only.
- **admin** — also the admin panel, the [wiki sync](wiki-sync.md) page and the backup section
  of the [Settings](settings.md) page.

The first account on an empty database becomes the admin; later accounts are users.

## The admin panel

Admins can create, deactivate, and delete accounts, assign roles, and reset passwords for
local-auth accounts directly from the admin panel.

- **Create Local User** — a username, a password and a role. Hidden with Azure EasyAuth, where
  accounts are created automatically on their first access.
- **Role** — change another account's role; you cannot change your own.
- **Activate / deactivate** — a deactivated account cannot sign in; its data is kept. You cannot
  deactivate yourself.
- **Reset password** — for local accounts only (at least 8 characters); OpenID Connect and
  EasyAuth accounts have no password here.
- **Delete** — removes the account and its data (type `DELETE` to confirm). Your own account is
  deleted from the Danger Zone of the [Settings](settings.md) page instead.
