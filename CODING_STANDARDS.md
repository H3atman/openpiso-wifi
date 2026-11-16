# Coding Standards

Judgement calls for the Reviewer. Mechanical rules belong in the pre-commit hook or CI, not here.

## Vendo secrets

A Vendo secret exists only in Controller memory, the ledger and the board. It never goes into a file on the PC, a log, a report or a commit, including temporary files written by flash or bench tooling.
