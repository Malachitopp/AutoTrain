"""The public operator list.

GET /operators is the one endpoint outside /auth with no session: the landing
page shows which operators AutoTrain can file with before anyone has signed
in, and the add-journey form uses the same list. Thin by rule (ARCHITECTURE
§3): ask the claims service, shape the response. "Supported" is the claims
module's word (OperatorFiling.is_supported); this file does not know the rule,
only the list.
"""

from __future__ import annotations

from fastapi import APIRouter

from autotrain.api.deps import ConnDep
from autotrain.api.schemas import OperatorOut
from autotrain.modules.claims import service

router = APIRouter(prefix="/operators", tags=["operators"])


@router.get("")
def list_operators(conn: ConnDep) -> list[OperatorOut]:
    # No UserIdDep, on purpose — see the module docstring.
    return [OperatorOut.model_validate(row) for row in service.supported_operators(conn)]
