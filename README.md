# living-context

Layered, self-reorganising long-term memory for AI agents.

Status: experimental. Nothing here is proven yet; results will be recorded in
this repository as they are measured, including what does not work.

## Idea

- Memory is segmented into leaves bounded by a maximum context size, with a
  summary index over them. When an index grows too large it is segmented and
  indexed in turn, recursively.
- Recall is a parallel fan-out down the index tree: each index holder is asked
  which of its children may be relevant to a prompt, and parents return
  organised reference pointers with sizes, not content.
- Leaves are cross-linked in a graph, so distance in the tree is not isolation.
- The structure reorganises itself from evidence: what was offered, what was
  actually opened and used, what a deeper reflection pass finds afterwards,
  and a reverse audit at pruning time that asks which past searches a leaf
  should have answered and whether it was found. Every structure version is
  kept, and a reorganisation is adopted only if replay shows it helps.

## License

Business Source License 1.1. See [LICENSE](LICENSE).

Free for non-commercial use, research, education and evaluation. Commercial
production use requires a commercial license from the licensor. Each version
converts to the Apache License 2.0 on its change date.
