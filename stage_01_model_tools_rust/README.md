# Stage 1 · Weather and math assistant in Rust

Run from this directory with Rust and Cargo installed.

```sh
cargo run --locked -- --trace --prompt 'Weather in San Jose and calculate (12 + 8) / 4'
cargo run --locked
```

The first command returns 19 °C and 5. In the interactive session, enter
`calculate 18 / 2`, then `What did you calculate?`. Try `/reset` to clear
the last successful calculation, `calculate 1/0` to see a tool error, and
`/quit` to exit.

The agent routes requests by matching prompt patterns. It can run both tools
in one turn. Weather readings for San Jose, Seattle, and Paris come from
fixtures. Arithmetic uses floating-point evaluation with a restricted input
alphabet. Session state lives in memory. Live model calls, live weather, and
LCEL are outside this Rust lab.
