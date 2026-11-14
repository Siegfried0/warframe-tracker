"""The S3 target's settings (bucket, prefix, keys, region, CA bundle) and the one seam that makes
its client: configured by the ``BACKUP_S3_*`` settings alone, TLS verified against the configured
bundle or botocore's own certificates, and no TLS key log."""
import os
import stat
from pathlib import Path

S3_KEYS_REASON = 'BACKUP_S3_ACCESS_KEY and BACKUP_S3_SECRET_KEY must both be set'
# The sentences name the setting, never the path.
CA_BUNDLE_ABSOLUTE_REASON = 'BACKUP_S3_CA_BUNDLE must be an absolute path'
CA_BUNDLE_NOT_A_FILE_REASON = 'BACKUP_S3_CA_BUNDLE is not a file'


def s3_bucket(config):
    return (config.get('BACKUP_S3_BUCKET') or '').strip() or None


def s3_prefix(config) -> str:
    """Blanks and leading ``/`` stripped; empty is the bucket root, else ends in one ``/``."""
    text = (config.get('BACKUP_S3_PREFIX') or '').strip().lstrip('/').rstrip('/')
    return text + '/' if text else ''


def s3_keys_missing(config):
    """The reason the S3 target is not configured although a bucket is (an access key or the
    secret key missing or blank), else None. Credentials are never taken from anywhere else
    (``AWS_*`` variables, ``~/.aws``, the instance-metadata endpoint)."""
    if not (config.get('BACKUP_S3_ACCESS_KEY') or '').strip() \
            or not (config.get('BACKUP_S3_SECRET_KEY') or '').strip():
        return S3_KEYS_REASON
    return None


def s3_region(config):
    """The configured region (blanks stripped), or None when unset or blank: botocore's S3
    default. Not validated: a wrong region fails the backup with the store's error."""
    return (config.get('BACKUP_S3_REGION') or '').strip() or None


def s3_ca_bundle(config):
    """The configured CA bundle (blanks stripped): None when unset or blank (botocore's own
    certificates), else the path of a regular file. A relative path is refused, never resolved
    against the working directory; so is an absolute path that is not a regular file (missing,
    a folder, a link that leads nowhere). Verification is never switched off: ``false`` or
    ``0`` is a relative path. The file's contents are not checked here (the first TLS call
    fails on a file that is not PEM)."""
    text = (config.get('BACKUP_S3_CA_BUNDLE') or '').strip()
    if not text:
        return None
    if not Path(text).is_absolute():
        raise ValueError(CA_BUNDLE_ABSOLUTE_REASON)
    try:
        regular = stat.S_ISREG(os.stat(text).st_mode)       # follows a link
    except (OSError, ValueError):
        regular = False
    if not regular:
        raise ValueError(CA_BUNDLE_NOT_A_FILE_REASON)
    return text


def _tls_context():
    """A TLS context with botocore's settings (``botocore.httpsession.create_urllib3_context``:
    no SSLv2, SSLv3, compression or session tickets, post-handshake auth, ``CERT_REQUIRED``,
    hostname checked by urllib3) that never reads ``SSLKEYLOGFILE``: botocore's own factory
    sets every context's key log from it, so a variable set for another tool's debugging
    would write each upload's session keys to a file. A test compares the two attribute by
    attribute."""
    import ssl

    import botocore.httpsession
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ciphers = getattr(botocore.httpsession, 'DEFAULT_CIPHERS', None)
    if ciphers:
        context.set_ciphers(ciphers)
    context.options |= (ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_COMPRESSION
                        | ssl.OP_NO_TICKET)
    if getattr(context, 'post_handshake_auth', None) is not None:
        context.post_handshake_auth = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False
    return context


def _ignored_session_vars(table):
    """Botocore's session variable table with every source but the default removed: no
    variable is read from the environment and no setting from a config file."""
    return {name: (None, None, default, converter)
            for name, (_config_name, _env_name, default, converter) in table.items()}


def _s3_client(config):
    """The S3 client of the configured endpoint, region and keys (the tests replace this seam).
    Its timeouts and retries bound an unreachable endpoint to about a minute. Without both keys
    it raises before boto3 is imported, so no other credentials are ever looked up.

    The target is configured by these settings alone: the session reads no variable of
    botocore's session variable table from the environment (the profile, the region, the CA
    bundle, the data path, the retry settings, FIPS and dual-stack endpoints, ...; the table is
    taken from the installed botocore, so a variable a later version adds is ignored too) or of
    its S3 settings table (``AWS_S3_*``), no AWS config or credentials file, no endpoint from the
    environment (``AWS_ENDPOINT_URL``, ``AWS_ENDPOINT_URL_S3``) and no data file under
    ``~/.aws/models`` (models and endpoints come from botocore's own data folder), so
    settings meant for another tool neither send the database elsewhere nor stop the backup.
    TLS is verified against the certificates botocore ships with, or against
    ``BACKUP_S3_CA_BUNDLE`` when set (never ``AWS_CA_BUNDLE`` or
    ``REQUESTS_CA_BUNDLE``; a bundle that is not an absolute regular file raises before boto3
    is imported); the proxy variables (``HTTPS_PROXY``, ``HTTP_PROXY``, ``NO_PROXY``) still
    apply, and the link to an HTTPS proxy is verified against the same bundle.

    No TLS context of the client writes a key log, whatever ``SSLKEYLOGFILE`` holds and
    whenever it was set: botocore's context factory is replaced by ``_tls_context`` (contexts
    are also built at request time, for a proxy, so the replacement stays for the process;
    ``os.environ`` is never changed)."""
    reason = s3_keys_missing(config)
    if reason:
        raise ValueError(reason)
    bundle = s3_ca_bundle(config)
    import boto3
    import botocore.configprovider
    import botocore.httpsession
    import botocore.loaders
    import botocore.session
    from botocore.config import Config
    # (name in the config file, environment variable, default, converter): no file, no variable.
    session_vars = _ignored_session_vars(botocore.configprovider.BOTOCORE_DEFAUT_SESSION_VARIABLES)
    session_vars.update({
        'profile': (None, None, None, None),
        'config_file': (None, None, os.devnull, None),
        'credentials_file': (None, None, os.devnull, None),
    })
    core = botocore.session.Session(session_vars=session_vars)
    # Looked up by name at call time by ``URLLib3Session._get_ssl_context``, its one caller.
    botocore.httpsession.create_urllib3_context = _tls_context
    # botocore's data (service models, the endpoints table) is read from botocore's own folder
    # only: its default loader searches ``~/.aws/models`` first, where a file another tool left
    # would send the requests to another host or break every client.
    core.register_component('data_loader', botocore.loaders.Loader(
        extra_search_paths=[botocore.loaders.Loader.BUILTIN_DATA_PATH],
        include_default_search_paths=False))
    # The S3 settings (``AWS_S3_USE_ARN_REGION``, ``AWS_S3_US_EAST_1_REGIONAL_ENDPOINT``, ...)
    # are botocore's second table: each keeps its default too.
    s3_defaults = {name: entry[2]
                   for name, entry in botocore.configprovider.DEFAULT_S3_CONFIG_VARS.items()
                   if entry[2] is not None}
    core.get_component('config_store').set_config_provider(
        's3', botocore.configprovider.ConstantProvider(s3_defaults or None))
    return boto3.session.Session(botocore_session=core).client(
        's3',
        region_name=s3_region(config),
        endpoint_url=(config.get('BACKUP_S3_ENDPOINT') or '').strip() or None,
        verify=bundle or True,
        aws_access_key_id=config['BACKUP_S3_ACCESS_KEY'].strip(),
        aws_secret_access_key=config['BACKUP_S3_SECRET_KEY'].strip(),
        config=Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 2},
                      ignore_configured_endpoint_urls=True,
                      # The link to an HTTPS proxy gets its context from _tls_context too
                      # (urllib3 would build its own, which logs keys), against the bundle the
                      # store's connections use.
                      proxies_config={'proxy_ca_bundle':
                                      bundle or botocore.httpsession.get_cert_path(True)}),
    )
