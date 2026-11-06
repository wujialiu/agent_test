# Stage 3 · Feed monitor and personal assistant in Rust

Run from this directory with Rust, Cargo, and a C compiler installed. Cargo
builds bundled SQLite.

```sh
cargo run --locked -- monitor
cargo run --locked -- inbox
cargo run --locked -- briefing --city Seattle
cargo run --locked -- remind --id practice-1 --at 2000-01-01T09:00:00-08:00 'Practice explaining the report outbox'
cargo run --locked -- reminders-tick
```

The monitor parses a built-in RSS feed or a local RSS or Atom file supplied
with `--feed-file`. It deduplicates entries by ID and labels sentiment using
keywords. Use a separate `--job` for each feed configuration. Each run replays
pending reports for the job, then processes its current UTC slot. Use
`--minutes` to change the default 60-minute slot duration or `--slot` to
supply an explicit RFC 3339 timestamp. The briefing uses fixture weather and
a sample calendar event. Monitor and reminder ticks are one-shot commands
suitable for calling from a local scheduler.

## Crash recovery

```sh
state=$(mktemp -d)
slot=2030-01-02T03:00:00Z
cargo run --locked -- --state "$state" monitor --slot "$slot" --crash-after-delivery
cargo run --locked -- --state "$state" monitor --slot "$slot"
cargo run --locked -- --state "$state" inbox
```

The first run exits with status 75 after the receiver commits. The replay
prints `inserted=false` and acknowledges the same delivery key. The inbox
contains one report. The worker and inbox use separate SQLite files in the
state directory. The default state path is `.agent_lab/production_rust`,
relative to the working directory.

Live feed fetching, model classification, weather, and Google OAuth are
outside this Rust lab.
