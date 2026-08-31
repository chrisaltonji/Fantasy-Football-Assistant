"""The inference layer — the half of the tool that can be wrong.

`advice/` is arithmetic over ground truth: every number it produces is derived
from events that happened, and it cannot be mistaken, only fed bad input. This
package is the opposite. It reads what `advice/` computed and adds judgement —
what a rival will actually pay, what a pick did to a plan, why a sale mattered.

The name difference is load-bearing, and so is the import direction:
**nothing in `advice/`, `domain/` or `state/` may ever import this package.**
`tests/unit/test_assist_boundary.py` walks the imports and fails if that stops
being true. Inference is allowed to depend on arithmetic; the reverse would put
a language model inside a bid ceiling.
"""

from ffa.assist.errors import AssistError

__all__ = ["AssistError"]
