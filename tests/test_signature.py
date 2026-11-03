"""Event signatures: the same kind of event gets the same id however its variable parts differ (tool redesign §A)."""

import re

from app.ingestion.signature import Signature, event_signature

SCIM = "org.wso2.carbon.identity.scim2.common.impl.SCIMUserManager"
LDAP = "org.wso2.carbon.user.core.ldap.LDAPConnectionContext"
STARTED = "org.wso2.carbon.core.internal.StartupFinalizerServiceComponent"


def sig(message: str, level: str | None = "ERROR", logger: str | None = SCIM) -> Signature:
    return event_signature(level=level, logger=logger, message=message, raw_text=message)


def test_numbers_and_times_do_not_split_a_kind():
    a = sig("WSO2 Carbon started in 178 sec", "INFO", STARTED)
    b = sig("WSO2 Carbon started in 218 sec", "INFO", STARTED)

    assert a.sig == b.sig
    assert a.template == "INFO {StartupFinalizerServiceComponent} - WSO2 Carbon started in <*> sec"


def test_masked_user_names_of_any_length_are_one_kind():
    # WSO2 masks user names itself; 21 vs 22 stars split one kind in two in the prototype
    a = sig("Error while updating attributes of user: C*********************k "
            "org.wso2.carbon.user.core.UserStoreException: Profile information could not be updated")
    b = sig("Error while updating attributes of user: C**********************k "
            "org.wso2.carbon.user.core.UserStoreException: Profile information could not be updated")

    assert a.sig == b.sig
    assert "user: <*> UserStoreException: Profile information could not be updated" in a.template


def test_different_causes_stay_different_kinds():
    privilege = sig("Error while updating attributes of user: C***k org.wso2.carbon.user.core.UserStoreException: "
                    "Error occurred while accessing Java Security Manager Privilege Block")
    profile = sig("Error while updating attributes of user: C***k org.wso2.carbon.user.core.UserStoreException: "
                  "Profile information could not be updated in LDAP user store")

    assert privilege.sig != profile.sig


def test_the_root_exception_tail_does_not_split_a_kind():
    a = sig("Error obtaining connection. keycloak-window.cnxy.in:636 javax.naming.CommunicationException: "
            "keycloak-window.cnxy.in:636 [Root exception is java.net.SocketTimeoutException: Connect timed out]",
            logger=LDAP)
    b = sig("Error obtaining connection. keycloak-window.cnxy.in:636 javax.naming.CommunicationException: "
            "keycloak-window.cnxy.in:636 [Root exception is java.net.ConnectException: Connection refused]",
            logger=LDAP)

    assert a.sig == b.sig
    assert a.template.endswith("CommunicationException: keycloak-window.cnxy.in:<*>")


def test_a_second_exception_class_ends_the_template():
    a = sig("Claim handling failed! org.wso2.FrameworkException: lookup failed org.wso2.UserStoreException: x 1")
    b = sig("Claim handling failed! org.wso2.FrameworkException: lookup failed org.wso2.UserStoreException: y 2")

    assert a.sig == b.sig and "UserStoreException" not in a.template


def test_the_logger_class_separates_identical_messages():
    assert sig("Trying again", logger=LDAP).sig != sig("Trying again", logger=SCIM).sig


def test_the_level_separates_identical_messages():
    assert sig("Retry", level="WARN").sig != sig("Retry", level="ERROR").sig


def test_ids_addresses_emails_and_quoted_values_become_placeholders():
    a = sig("Request be9da973-8922-4ec3-93e0-cf8ab7819f22 from 10.2.4.17:443 for a@b.com attr 'sn' id 8f3a9c1e5b7d2f40")
    b = sig("Request 2b4fe388-1abd-4811-95fe-6fea6e4275c7 from 192.168.0.1 for x.y@corp.in attr 'mail' id 0x1f")

    assert a.sig == b.sig
    assert a.template == "ERROR {SCIMUserManager} - Request <*> from <*> for <*> attr <*> id <*>"


def test_our_own_redaction_masks_are_kept_as_constant_text():
    a = sig("login failed password=[REDACTED:secret] for [REDACTED:email]")
    assert "[REDACTED:secret]" in a.template and "[REDACTED:email]" in a.template


def test_an_unparsed_event_falls_back_to_its_first_line():
    a = event_signature(level=None, logger=None, message=None, raw_text="boom 42 happened\n\tat a.b(C.java:1)")
    b = event_signature(level=None, logger=None, message=None, raw_text="boom 7 happened\n\tat x.y(Z.java:9)")

    assert a.sig == b.sig and a.template == "- boom <*> happened"


def test_a_sig_is_eight_hex_characters_and_stable():
    one = sig("WSO2 Carbon started in 178 sec", "INFO", STARTED)

    assert re.fullmatch(r"[0-9a-f]{8}", one.sig)
    assert one == sig("WSO2 Carbon started in 178 sec", "INFO", STARTED)  # same input, same sig, every run


def test_templates_are_capped():
    assert len(sig("word " * 200).template) <= 200
