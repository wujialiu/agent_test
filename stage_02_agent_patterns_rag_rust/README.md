# Stage 2 · Grounded research in Rust

Run from this directory with Rust and Cargo installed.

```sh
cargo run --locked -- --engine react 'How does an outbox use a stable delivery key?'
cargo run --locked -- --compare --details
cargo run --locked -- --corpus /path/to/markdown --top-k 3 'Your question'
```

The built-in corpus is compiled from the four Markdown documents in the
Python lab. A custom corpus reads UTF-8 Markdown files from the specified
directory. The index splits documents into paragraphs and ranks them by
shared terms with the query. Reports quote retrieved passages and give their
source filenames. When the evidence threshold is missed, the report abstains.

The direct, ReAct, graph, and role walkthroughs show different step sequences
over the same deterministic retrieval and extraction logic. The comparison
reports retrieval hits, expected citations, and correct abstentions over
five fixture questions. LangGraph, AutoGen, Chroma, and live model or embedding
providers are outside this Rust lab.
