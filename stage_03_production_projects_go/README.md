# Stage 3 · Feed monitor and personal assistant in Go

Run from this directory with Go 1.24 or later and a C compiler. The SQLite
driver uses CGO and builds SQLite from bundled source.

```sh
go run . monitor
go run . inbox
go run . briefing --city Seattle
go run . remind --id practice-1 --at 2000-01-01T09:00:00-08:00 'Check the outbox'
go run . reminders-tick
```

The monitor uses a built-in RSS fixture or reads a local RSS or Atom file with
`--feed-file`. It deduplicates entries by ID and classifies sentiment with
keywords. Use a separate `--job` for each feed configuration. The worker
replays pending reports and then processes the current UTC slot. Slots default
to 60 minutes. Override the interval with `--minutes` or select a specific
slot with `--slot`. The briefing uses fixture weather and a sample event.

State defaults to `.agent_lab/production-go` relative to the working directory.
Use `--state` before the subcommand to choose another directory. Monitor and
reminder ticks are one-shot commands that can be invoked by a local scheduler.

## Crash recovery

```sh
state=$(mktemp -d)
go build -o "$state/monitor" .
slot=2030-01-02T03:00:00Z
"$state/monitor" --state "$state" monitor --slot "$slot" --crash-after-delivery
"$state/monitor" --state "$state" monitor --slot "$slot"
"$state/monitor" --state "$state" inbox
```

The first command that runs the monitor exits with status 75 after the inbox
database commits the report. Replay prints `inserted=false` and acknowledges
the original delivery key in the worker database. The inbox shows one report.

## Tests

Run `go test ./...` from this directory.
