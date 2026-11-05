"""WSO2 AUDIT_LOG events: the action, result, actor, target, user and application become fields."""

import time
from pathlib import Path

from app.ingestion.fields import resolve
from app.ingestion.fields.audit import audit_fields
from app.ingestion.sources import FileSource

TEXT = FileSource(id="wso2", path=Path("wso2.log"))

PIPE_LOGIN = (
    'Initiator : [REDACTED:email] | Action : Login | Target : ApplicationAuthenticationFramework | Data : '
    '{ "ContextIdentifier" : "1d340c53","AuthenticatedUser" : "[REDACTED:email]","ServiceProviderName" : "Console",'
    '"RequestType" : "oidc","UserStoreDomain" : "PRIMARY" } | Result : Success'
)
PIPE_FAILED = (
    'Initiator : Goutam003 | Action : Login | Target : ApplicationAuthenticationFramework | Data : '
    '{ "ServiceProviderName" : "portalApplication","StepNo" : "1" } | Result : Failed'
)
EQUALS_CLAIMS = (
    'Initiator=wso2.system.user Action=Get-User-Claim-Values Target=admin Data={"ServiceProviderName":"Console",'
    '"Claims":{"http://wso2.org/claims/username":"admin","http://wso2.org/claims/roles":"Internal/admin"}} '
    'Outcome=Success'
)
UPDATE_SESSION = (
    'Initiator : PRIMARY/admin | Action : UpdateSession | Data : { {"traceId":"17deb80d",'
    '"AuthenticatedUser":"PRIMARY/admin"} } | Result : Success'
)


def wso2_line(message: str, logger: str = "AUDIT_LOG") -> str:
    return f"TID: [-1234] [] [2026-11-05 16:03:26,686] [f39cb36f]  INFO {{{logger}}} - {message}"


def test_pipe_style_login():
    assert audit_fields(PIPE_LOGIN) == {
        "audit_action": "Login", "audit_result": "Success", "audit_actor": "[REDACTED:email]",
        "audit_target": "ApplicationAuthenticationFramework", "audit_user": "[REDACTED:email]",
        "audit_application": "Console",
    }


def test_failed_login_has_failed_result_and_no_user():
    fields = audit_fields(PIPE_FAILED)
    assert (fields["audit_result"], fields["audit_actor"], fields["audit_application"]) == (
        "Failed", "Goutam003", "portalApplication")
    assert "audit_user" not in fields


def test_equals_style_takes_outcome_and_ignores_claims():
    assert audit_fields(EQUALS_CLAIMS) == {
        "audit_action": "Get-User-Claim-Values", "audit_result": "Success", "audit_actor": "wso2.system.user",
        "audit_target": "admin", "audit_application": "Console",
    }


def test_user_store_prefix_is_removed():
    assert audit_fields(UPDATE_SESSION) == {
        "audit_action": "UpdateSession", "audit_result": "Success", "audit_actor": "admin", "audit_user": "admin",
    }


def test_a_message_without_audit_fields_gives_an_empty_dict():
    assert audit_fields("Server started") == {}


def test_values_are_capped_at_256_characters():
    fields = audit_fields("Initiator : " + "a" * 5000 + " | Action : Login | Result : Success")
    assert len(fields.get("audit_actor", "")) <= 256


def test_the_last_result_wins():
    message = 'Initiator : x | Action : Login | Data : { "Result" : "inner" } | Result : Success'
    assert audit_fields(message)["audit_result"] == "Success"


def test_hostile_input_is_fast_and_bounded():
    for hostile in ("Action=" * 200_000, '"ServiceProviderName" : "' + "a" * 10**6, "Initiator : [REDACTED:" * 50_000):
        start = time.perf_counter()
        fields = audit_fields(hostile)
        assert time.perf_counter() - start < 1
        assert all(len(value) <= 256 for value in fields.values())


def test_layout_adds_audit_fields_to_an_audit_log_event():
    fields = resolve(wso2_line(PIPE_LOGIN), TEXT).fields
    assert fields["audit_application"] == "Console" and fields["audit_action"] == "Login"
    assert fields["tenant"] == "-1234"


def test_layout_adds_nothing_for_another_logger():
    fields = resolve(wso2_line(PIPE_LOGIN, logger="OtherLogger"), TEXT).fields
    assert not [key for key in fields if key.startswith("audit_")]


def test_a_spring_boot_line_that_mentions_an_action_gets_no_audit_fields():
    line = "2026-11-05 10:00:00.000  INFO 123 --- [main] c.a.Service : Action=Login Outcome=Success"
    assert not [key for key in resolve(line, TEXT).fields if key.startswith("audit_")]


# --- fixes from the final review ---


def long_pipe(padding: int) -> str:
    return (
        'Initiator : Goutam003 | Action : Login | Target : T | Data : { "ServiceProviderName" : "portalApplication",'
        f'"Padding" : "{"x" * padding}" }} | Result : Failed'
    )


def test_a_verdict_after_a_long_data_block_is_kept():
    for padding in (9_000, 30_000):  # beyond the 8 KiB message cap, and beyond the 16 KiB scan
        fields = resolve(wso2_line(long_pipe(padding)), TEXT).fields
        assert fields["audit_result"] == "Failed", padding
        assert fields["audit_application"] == "portalApplication", padding
        assert audit_fields(long_pipe(padding))["audit_result"] == "Failed", padding


def test_a_claims_heavy_equals_line_keeps_its_outcome():
    claims = ",".join(f'"http://wso2.org/claims/c{i}":"{"v" * 40}"' for i in range(400))
    message = f'Initiator=wso2.system.user Action=Get-User-Claim-Values Target=admin Data={{"ServiceProviderName":"Console","Claims":{{{claims}}}}} Outcome=Success'
    fields = resolve(wso2_line(message), TEXT).fields
    assert (fields["audit_result"], fields["audit_application"]) == ("Success", "Console")


def test_only_a_user_store_domain_is_removed_from_a_user():
    def actor(value: str) -> str:
        return audit_fields(f"Initiator : {value} | Action : Login | Result : Success")["audit_actor"]

    assert actor("PRIMARY/admin") == "admin"
    assert actor("Internal/admin") == "Internal/admin"
    assert actor("Application/portal") == "Application/portal"
    assert actor("foo/bar/baz") == "foo/bar/baz"


def test_an_action_name_may_contain_digits():
    assert audit_fields("Initiator : x | Action : Get-OAuth2-Token | Result : Success")["audit_action"] == "Get-OAuth2-Token"
