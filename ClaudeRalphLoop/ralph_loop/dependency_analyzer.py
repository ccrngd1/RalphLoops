"""Pure dependency analyzer for Ralph Loop task lists.

The analyzer inspects a ``list[Task]`` for the two dependency-health
conditions that demand a task be marked ``"stuck"`` per Requirements
R2.9-R2.11:

1. **Missing dependency id** (R2.9): a task whose ``depends_on`` contains an
   identifier that does not exist in the task list is ineligible in any
   future iteration, so the loop marks it ``stuck`` and logs the offending
   dependency id.
2. **Cycle participation** (R2.10, R2.11): when the loop starts and again
   after every Task_Creation_Event, the loop sweeps ``depends_on`` for
   cycles. Every task participating in a cycle is marked ``stuck`` and the
   detected cycle path is emitted in the log in DFS order.

The module is side-effect-free: it never mutates the tasks passed in (new
``Task`` instances are produced via ``task.model_copy(update=...)``), it
performs no I/O, and it does not log. The Resumer and
TaskCreationProcessor call it and attach logging downstream. That purity
is what lets Property 3 (design.md) exercise the analyzer with Hypothesis
over arbitrary task lists.

The analyzer is the canonical implementation referenced by the Resumer
sequence diagram (design.md "Resumption on startup") and by the
Task-creation-event processing sequence (design.md "Task-creation-event
processing (post-iteration)").
"""

from __future__ import annotations

from ralph_loop.models import DependencyAnalysis, Task


# Node colours used by the cycle-detection DFS. Using a single dict of
# ids-to-colours keeps the bookkeeping compact; the three values mirror
# the classic white/gray/black scheme from Cormen et al.
_WHITE = 0  # unvisited
_GRAY = 1  # on the current DFS recursion stack
_BLACK = 2  # fully explored


def _find_missing_dep_ids(
    tasks: list[Task], known_ids: set[str]
) -> dict[str, list[str]]:
    """Return, for each task id, the ``depends_on`` entries that are unknown.

    The returned dict is keyed by ``task.id`` and maps to the
    first-occurrence-preserving list of dependency ids that do not appear in
    ``known_ids``. Tasks with no missing dependencies are omitted from the
    result so callers can iterate it directly.
    """

    missing: dict[str, list[str]] = {}
    for task in tasks:
        unknown: list[str] = []
        for dep_id in task.depends_on or []:
            if dep_id not in known_ids:
                unknown.append(dep_id)
        if unknown:
            missing[task.id] = unknown
    return missing


def _find_cycles(
    tasks: list[Task], known_ids: set[str]
) -> tuple[list[list[str]], set[str]]:
    """Run DFS cycle detection and return cycle paths + participant ids.

    The traversal uses an explicit iterative DFS rather than Python
    recursion so arbitrarily large task lists do not blow the recursion
    limit. Each node starts ``_WHITE``; when the DFS first visits it the
    node becomes ``_GRAY`` and is pushed onto ``path`` (the current DFS
    recursion stack). When all of a node's dependency edges have been
    explored the node becomes ``_BLACK`` and is popped.

    A back edge (an edge from a ``_GRAY`` node to another ``_GRAY`` node
    already on ``path``) identifies a cycle. The cycle path is the slice
    of ``path`` starting at the target of the back edge and ending just
    before the pushed copy, i.e. the ids traversed in DFS order from the
    cycle's entry point back to itself. Self-loops (``A -> A``) surface
    naturally as a single-element cycle ``[A]``.

    Only dependency edges whose target exists in ``known_ids`` are
    followed; unknown dep ids are handled separately by
    :func:`_find_missing_dep_ids` so cycle detection is not confused by
    ids that have no corresponding node.

    Returns:
        A tuple ``(cycles, participants)`` where ``cycles`` is the list
        of cycle paths in the order they were discovered and
        ``participants`` is the union of ids across all discovered
        cycles.
    """

    # Build an adjacency list once; tasks may appear only once in the
    # input so dict-of-lists is sufficient.
    adjacency: dict[str, list[str]] = {}
    for task in tasks:
        deps = [d for d in (task.depends_on or []) if d in known_ids]
        adjacency[task.id] = deps

    colour: dict[str, int] = {task.id: _WHITE for task in tasks}
    # ``path`` is the current DFS recursion stack (ordered ids); the
    # companion set lets us answer "is this id on the stack?" in O(1).
    path: list[str] = []
    on_path: set[str] = set()
    # Iterator stack lets the iterative DFS resume dependency iteration
    # after descending into a child.
    iter_stack: list[tuple[str, int]] = []

    cycles: list[list[str]] = []
    participants: set[str] = set()

    for start in adjacency:
        if colour[start] != _WHITE:
            continue
        # Prime the DFS at ``start``.
        colour[start] = _GRAY
        path.append(start)
        on_path.add(start)
        iter_stack.append((start, 0))

        while iter_stack:
            node, idx = iter_stack[-1]
            deps = adjacency[node]
            if idx < len(deps):
                # Advance the iterator for ``node`` before any branching so
                # sibling edges resume correctly after a child's DFS.
                iter_stack[-1] = (node, idx + 1)
                neighbour = deps[idx]
                n_colour = colour[neighbour]
                if n_colour == _WHITE:
                    colour[neighbour] = _GRAY
                    path.append(neighbour)
                    on_path.add(neighbour)
                    iter_stack.append((neighbour, 0))
                elif n_colour == _GRAY and neighbour in on_path:
                    # Back edge: the cycle is ``path[i:]`` where
                    # ``path[i] == neighbour``.
                    i = path.index(neighbour)
                    cycle = list(path[i:])
                    cycles.append(cycle)
                    participants.update(cycle)
                # _BLACK neighbours cannot be part of a cycle with
                # ``node`` because they have already been fully explored
                # and popped off the recursion stack.
            else:
                # All edges out of ``node`` processed; finalize it.
                colour[node] = _BLACK
                popped = path.pop()
                on_path.discard(popped)
                iter_stack.pop()

    return cycles, participants


def analyze_dependencies(tasks: list[Task]) -> DependencyAnalysis:
    """Analyze ``depends_on`` health across ``tasks``.

    Implements Requirements R2.9 (missing-dependency stuck marking),
    R2.10 (cycle detection runs on startup and after every
    Task_Creation_Event), and R2.11 (every cycle participant marked
    stuck, detected cycle path returned for logging). The function is
    pure: the input list is not mutated, and each updated task is a
    fresh ``Task`` instance produced via ``model_copy``. See Property 3
    in ``design.md``.

    Algorithm:

    1. Build the set of known task ids.
    2. Identify tasks whose ``depends_on`` references an id outside that
       set; record them as missing-dep candidates.
    3. Run the iterative DFS cycle detector
       (:func:`_find_cycles`). Only known dep ids participate; unknown
       ids were already captured in step 2.
    4. For every task that either has a missing dep or participates in a
       cycle, emit an updated copy with ``status="stuck"``. Tasks
       already stuck are still included in the relevant bucket (so
       callers can log them) but their ``updated_tasks`` entry is
       effectively a no-op copy.

    Tasks outside both buckets are passed through unchanged in
    ``updated_tasks`` (same instance - the Pydantic model is immutable
    from the analyzer's perspective so sharing is safe).
    """

    known_ids: set[str] = {task.id for task in tasks}
    missing_by_id = _find_missing_dep_ids(tasks, known_ids)
    cycles, cycle_participants = _find_cycles(tasks, known_ids)

    updated_tasks: list[Task] = []
    stuck_by_missing_dep: list[Task] = []
    stuck_by_cycle: list[Task] = []

    for task in tasks:
        is_missing = task.id in missing_by_id
        is_cycle = task.id in cycle_participants
        if is_missing or is_cycle:
            # Produce a stuck copy (even if the task was already stuck;
            # ``model_copy`` without an update would share the original).
            # Using ``update={"status": "stuck"}`` is a no-op when the
            # status is already stuck and is idempotent across repeated
            # analyzer runs.
            if task.status != "stuck":
                updated = task.model_copy(update={"status": "stuck"})
            else:
                updated = task
            updated_tasks.append(updated)
            if is_missing:
                stuck_by_missing_dep.append(updated)
            if is_cycle:
                stuck_by_cycle.append(updated)
        else:
            updated_tasks.append(task)

    return DependencyAnalysis(
        updated_tasks=updated_tasks,
        stuck_by_missing_dep=stuck_by_missing_dep,
        stuck_by_cycle=stuck_by_cycle,
        detected_cycles=cycles,
    )
