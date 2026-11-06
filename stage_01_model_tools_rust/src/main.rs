use clap::Parser;
use regex::Regex;
use std::io::{self, BufRead, Write};
use std::time::Instant;

#[derive(Parser)]
#[command(about = "Local weather and arithmetic agent")]
struct Args {
    #[arg(long)]
    prompt: Option<String>,
    #[arg(long)]
    trace: bool,
}

#[derive(Default)]
struct Session {
    last_calculation: Option<String>,
}

fn calculate(expression: &str) -> Result<f64, String> {
    if expression.is_empty()
        || expression.len() > 160
        || !expression
            .bytes()
            .all(|b| b.is_ascii_digit() || b" .()+-*/%".contains(&b))
    {
        return Err("enter arithmetic using numbers, parentheses, and + - * / %".into());
    }
    let value = meval::eval_str(expression).map_err(|error| error.to_string())?;
    if !value.is_finite() {
        return Err("the result is undefined or out of range".into());
    }
    Ok(value)
}

fn weather(city: &str) -> Result<String, String> {
    let temperature = match city.to_ascii_lowercase().as_str() {
        "san jose" => 19,
        "seattle" => 12,
        "paris" => 16,
        _ => return Err(format!("no fixture weather for {city}")),
    };
    Ok(format!("{city}: {temperature} °C (fixture)"))
}

struct Agent {
    weather_request: Regex,
    math_request: Regex,
}

impl Agent {
    fn new() -> Self {
        Self {
            weather_request: Regex::new(r"(?i)\bweather\s+(?:in|for)\s+(.+?)(?:\s+and\s+|[?,;]|$)")
                .unwrap(),
            math_request: Regex::new(r"(?i)\b(?:calculate|compute|evaluate)\s+([^?;]+)").unwrap(),
        }
    }

    fn answer(&self, prompt: &str, session: &mut Session, trace: bool) {
        let city = self.weather_request.captures(prompt);
        let expression = self.math_request.captures(prompt);
        let mut calls: Vec<(&str, Result<String, String>)> = Vec::new();
        let start = Instant::now();
        if let Some(city) = city {
            calls.push(("weather", weather(city[1].trim())));
        }
        if let Some(expression) = expression {
            let expression = expression[1].split(" and ").next().unwrap().trim();
            let result = calculate(expression).map(|value| format!("{expression} = {value}"));
            if let Ok(answer) = &result {
                session.last_calculation = Some(answer.clone());
            }
            calls.push(("calculate", result));
        }
        if calls.is_empty() {
            if prompt
                .to_ascii_lowercase()
                .contains("what did you calculate")
            {
                println!(
                    "{}",
                    session
                        .last_calculation
                        .as_deref()
                        .unwrap_or("No calculation in this session yet.")
                );
            } else {
                println!("Ask for weather in a city, or try 'calculate (12 + 8) / 4'.");
            }
        }
        for (index, (name, result)) in calls.into_iter().enumerate() {
            match result {
                Ok(answer) => println!("{answer}"),
                Err(error) => println!("{name} error: {error}"),
            }
            if trace {
                eprintln!(
                    "call {}: {name} ({:.2?} elapsed)",
                    index + 1,
                    start.elapsed()
                );
            }
        }
    }
}

fn main() -> io::Result<()> {
    let args = Args::parse();
    let agent = Agent::new();
    let mut session = Session::default();
    if let Some(prompt) = args.prompt {
        agent.answer(&prompt, &mut session, args.trace);
        return Ok(());
    }
    println!("Weather and math assistant. /reset clears the session, /quit exits.");
    let mut lines = io::stdin().lock().lines();
    loop {
        print!("you> ");
        io::stdout().flush()?;
        let Some(line) = lines.next() else { break };
        match line?.trim() {
            "/quit" => break,
            "/reset" => session = Session::default(),
            "" => {}
            prompt => agent.answer(prompt, &mut session, args.trace),
        }
    }
    Ok(())
}
