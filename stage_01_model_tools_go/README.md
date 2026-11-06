# Stage 1 · Weather and math in Go

With Go 1.24 or later, run these commands from this directory.

```sh
go run . --trace --prompt 'Weather in San Jose and calculate (12 + 8) / 4'
go run .
```

The first command returns 19 °C and 5. In the interactive session, try
`calculate 18 / 2`, then `What did you calculate?`. Use `/reset` to clear
the session and `/quit` to exit. Try `calculate 1/0` to see a tool error.

Prompt patterns dispatch the tools. Weather comes from fixtures for San Jose,
Seattle, and Paris. The arithmetic tool evaluates only numeric expressions
using parentheses and the five basic operators `+ - * / %`.

## Tests

Run `go test ./...` from this directory.
