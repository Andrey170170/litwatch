"""Grading for Watch topics: two labs grade each candidate, and the two are reconciled.

`grade` writes nothing. `record` stores a verdict in production; evaluation stores the same
result in its own tables.
"""

from litwatch.watch.grade import Graded, grade, grade_many, grading_request
from litwatch.watch.record import record

__all__ = ["Graded", "grade", "grade_many", "grading_request", "record"]
