"""The contracts litwatch shares with the deep service (`core.contracts`, generated from
`deep/src/contracts.ts`): each of a read's names that litwatch words for the owner is worded."""

from typing import get_args

from litwatch.api import mcp_deep
from litwatch.core import deepread
from litwatch.core.contracts import deep as contract


def test_every_name_a_read_uses_is_worded():
    assert set(mcp_deep.KINDS) == set(get_args(contract.SectionKind.__value__))
    assert set(deepread.LIMITATION_BY) == set(get_args(contract.LimitationBy.__value__))
