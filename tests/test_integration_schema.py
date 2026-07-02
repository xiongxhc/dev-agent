import pytest
from pydantic import ValidationError
from devagent.schema import IntegrationCheck, ServiceNode, SystemDesign


def _svc(id):
    return ServiceNode(id=id, name=id, kind="backend", stack="node-express", prd_slice="x")


def test_integration_check_defaults():
    c = IntegrationCheck(service="api", route="/api/todos")
    assert c.method == "GET" and c.expected_status == 200 and c.body is None


def test_system_design_accepts_integration_checks():
    d = SystemDesign(title="t", services=[_svc("api")],
                     integration_checks=[IntegrationCheck(service="api", route="/api/todos")])
    assert d.integration_checks[0].service == "api"


def test_system_design_defaults_empty_integration_checks():
    assert SystemDesign(title="t", services=[_svc("api")]).integration_checks == []


def test_rejects_integration_check_for_unknown_service():
    with pytest.raises(ValidationError):
        SystemDesign(title="t", services=[_svc("api")],
                     integration_checks=[IntegrationCheck(service="ghost", route="/x")])
