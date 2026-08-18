"""Owner dossiers: what we know about the twelve people in the room.

`schema` is the question set and the record. `store` is `data/dossiers.json`.
`form` renders a fill-in questionnaire for doing the interview away from a
terminal.
"""

from ffa.dossier.schema import (
    QUESTIONS,
    DossierError,
    OwnerDossier,
    parse_answer,
    render_answer,
    seed,
)
from ffa.dossier.store import (
    DEFAULT_DOSSIER_PATH,
    DossierBook,
    load_dossiers,
    save_dossiers,
)

__all__ = [
    "QUESTIONS",
    "DEFAULT_DOSSIER_PATH",
    "DossierBook",
    "DossierError",
    "OwnerDossier",
    "load_dossiers",
    "parse_answer",
    "render_answer",
    "save_dossiers",
    "seed",
]
