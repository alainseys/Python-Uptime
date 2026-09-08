# functions/ldap.py

import os
import ldap3
from flask import current_app
from ldap3.utils.conv import escape_filter_chars

# === ACTIVE DIRECTORY CONFIGURATION ===
AD_CONFIG = {
    'servers': [
        'ldap://ad-dc-01.domain.be:389',
        'ldap://ad-dc-02.domain.be:389',
        'ldap://ad-dc-03.domain.be:389'
    ],
    'base_dn': 'dc=vm,dc=be',
    'admin_group': 'AdminStatusDashboard',  # Group that grants admin access
}

# Service account for read-only lookups (used when we just want user info)
SERVICE_USER = os.getenv('AD_SERVICE_USER', 'svc_automate@domain.be')
SERVICE_PASS = os.getenv('AD_SERVICE_PASS', 'yourpassword')  # MUST be set in .env!

if not SERVICE_PASS:
    raise RuntimeError("AD_SERVICE_PASS environment variable is required for LDAP lookups")


def connect_ldap(username=None, password=None):
    """
    Connect to Active Directory.

    - If username + password → authenticate as that user (used for login)
    - If only username → bind as service account, then search for user info
    - If neither → just bind as service account (for future use)

    Returns: (connection, user_info_dict) or (None, None)
    """
    for server_url in AD_CONFIG['servers']:
        try:
            server = ldap3.Server(server_url, get_info=ldap3.ALL)

            # CASE 1: Full user authentication (login)
            if username and password is not None:
                conn = ldap3.Connection(
                    server,
                    user=username,
                    password=password,
                    auto_bind=True
                )
                if conn.bound:
                    current_app.logger.debug(f"LDAP bind successful as {username} on {server_url}")
                    return conn, _get_user_info(conn, username.split('@')[0])
                else:
                    current_app.logger.info(f"LDAP bind failed for {username} on {server_url}")
                    continue

            # CASE 2: Lookup mode — bind as service account first
            else:
                conn = ldap3.Connection(
                    server,
                    user=SERVICE_USER,
                    password=SERVICE_PASS
                )
                if not conn.bind():
                    current_app.logger.warning(f"LDAP service account bind failed on {server_url}")
                    continue

                current_app.logger.debug(f"LDAP service bind successful on {server_url}")

                if not username:
                    return conn, None  # Just return bound connection

                # Search for the user
                sam = username.split('@')[0]
                search_filter = f"(sAMAccountName={escape_filter_chars(sam)})"
                if conn.search(
                    search_base=AD_CONFIG['base_dn'],
                    search_filter=search_filter,
                    attributes=['givenName', 'sn', 'displayName', 'sAMAccountName', 'mail']
                ):
                    if conn.entries:
                        entry = conn.entries[0]
                        return conn, {
                            'first_name': _safe_attr(entry.givenName),
                            'last_name': _safe_attr(entry.sn) or _fallback_last_name(entry.displayName),
                            'username': entry.sAMAccountName.value,
                            'email': entry.mail.value if entry.mail else None
                        }
                return conn, None

        except ldap3.core.exceptions.LDAPException as e:
            current_app.logger.error(f"LDAP error on {server_url}: {e}")
            continue

    # If we get here → all servers failed
    current_app.logger.error("Failed to connect to any LDAP server")
    return None, None


def _safe_attr(attr):
    """Safely extract attribute value"""
    return attr.value if attr else ''


def _fallback_last_name(display_name_attr):
    """Try to extract last name from displayName if sn is missing"""
    if display_name_attr and display_name_attr.value:
        parts = display_name_attr.value.split()
        return parts[-1] if len(parts) > 1 else parts[0]
    return ''


def _get_user_info(conn, samaccountname):
    """Internal helper to fetch user details after successful bind"""
    search_filter = f"(sAMAccountName={escape_filter_chars(samaccountname)})"
    if conn.search(
        search_base=AD_CONFIG['base_dn'],
        search_filter=search_filter,
        attributes=['givenName', 'sn', 'displayName', 'sAMAccountName', 'mail']
    ):
        if conn.entries:
            e = conn.entries[0]
            return {
                'first_name': _safe_attr(e.givenName),
                'last_name': _safe_attr(e.sn) or _fallback_last_name(e.displayName),
                'username': e.sAMAccountName.value,
                'email': e.mail.value if e.mail else None
            }
    return None


def authenticate_user(username, password):
    """
    Authenticate user + check if they're in the AdminStatusDashboard group
    """
    if not username or not password:
        current_app.logger.warning("LDAP auth attempted without username/password")
        return False, None

    domain = 'vm.be'
    upn = f"{username}@{domain}"

    # Step 1: Try to bind as the user
    conn, user_info = connect_ldap(upn, password)
    if not conn or not conn.bound:
        current_app.logger.info(f"LDAP auth failed for {username} - invalid credentials")
        return False, None

    try:
        sam = username.split('@')[0]
        search_filter = f"(sAMAccountName={escape_filter_chars(sam)})"
        conn.search(
            search_base=AD_CONFIG['base_dn'],
            search_filter=search_filter,
            attributes=['memberOf']
        )

        if not conn.entries:
            return False, None

        member_of = [str(g).lower() for g in conn.entries[0].memberOf.values] if conn.entries[0].memberOf else []

        group_cn = AD_CONFIG['admin_group'].lower()
        if any(group_cn in group.lower() for group in member_of):
            current_app.logger.info(f"LDAP auth SUCCESS for {username} - in admin group")
            return True, user_info

        # Fallback: exact group membership check
        group_filter = f"(&(objectClass=group)(cn={AD_CONFIG['admin_group']})(member={conn.entries[0].entry_dn}))"
        conn.search(
            search_base=AD_CONFIG['base_dn'],
            search_filter=group_filter,
            attributes=['cn']
        )
        is_member = bool(conn.entries)
        if is_member:
            current_app.logger.info(f"LDAP auth SUCCESS for {username} - confirmed group membership")
            return True, user_info

        current_app.logger.info(f"LDAP auth FAILED for {username} - not in {AD_CONFIG['admin_group']}")
        return False, None

    except Exception as e:
        current_app.logger.error(f"LDAP group check error for {username}: {e}")
        return False, None
    finally:
        if conn:
            conn.unbind()
