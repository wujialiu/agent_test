# Stage 2 · Grounded research in Go

With Go 1.24 or later, run these commands from this directory.

```sh
go run . --engine react 'How does an outbox use a stable delivery key?'
go run . --compare --details
go run . --corpus /path/to/markdown --top-k 3 'Your question'
```

The bundled Markdown corpus is embedded in the binary. With `--corpus`, the
program loads Markdown files from the supplied directory. It ranks paragraphs
by shared terms, quotes the selected evidence with source filenames, and
abstains when no passage meets the evidence threshold.

The direct, ReAct, graph, and roles walkthroughs display different step
sequences over the same local retrieval and extraction logic. The comparison
measures retrieval, citations, and abstention across five fixture questions.

## Tests

Run `go test ./...` from this directory.
