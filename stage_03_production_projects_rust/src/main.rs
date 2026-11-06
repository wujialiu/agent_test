use chrono::{DateTime, Utc};
use clap::{Parser, Subcommand};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{collections::HashSet, error::Error, fs, path::PathBuf};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const FEED: &str = r#"<rss><channel>
  <item><title>Service restores access</title><link>https://example.invalid/restored</link><description>Customers praise the recovery.</description></item>
  <item><title>Payment outage frustrates customers</title><link>https://example.invalid/outage</link><description>Several regions reported delays.</description></item>
  <item><title>Company schedules earnings call</title><link>https://example.invalid/earnings</link><description>The call is Thursday.</description></item>
</channel></rss>"#;

#[derive(Parser)]
#[command(about = "Local feed monitor and personal assistant")]
struct Args {
    #[arg(long, default_value = ".agent_lab/production_rust")]
    state: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Monitor {
        #[arg(long, default_value = "sentiment-demo")]
        job: String,
        #[arg(long)]
        feed_file: Option<PathBuf>,
        #[arg(long)]
        slot: Option<DateTime<Utc>>,
        #[arg(long, default_value_t = 60, value_parser = clap::value_parser!(u32).range(1..))]
        minutes: u32,
        #[arg(long)]
        crash_after_delivery: bool,
    },
    Inbox,
    Briefing {
        #[arg(long, default_value = "San Jose")]
        city: String,
    },
    Remind {
        #[arg(long)]
        id: String,
        #[arg(long)]
        at: DateTime<Utc>,
        text: String,
    },
    RemindersTick,
}

struct Storage {
    worker: Connection,
    inbox: Connection,
}

impl Storage {
    fn open(state: PathBuf) -> Result<Self> {
        fs::create_dir_all(&state)?;
        let worker = Connection::open(state.join("worker.sqlite3"))?;
        let inbox = Connection::open(state.join("inbox.sqlite3"))?;
        worker.execute_batch(
            "CREATE TABLE IF NOT EXISTS outbox (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
             );
             CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY, due_ms INTEGER NOT NULL, text TEXT NOT NULL
             );",
        )?;
        inbox.execute_batch(
            "CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, payload TEXT NOT NULL);",
        )?;
        Ok(Self { worker, inbox })
    }

    fn deliver_pending(&self, prefix: &str, crash: bool) -> Result<usize> {
        let mut query = self.worker.prepare(
            "SELECT key, payload FROM outbox WHERE delivered = 0 AND substr(key, 1, length(?1)) = ?1 ORDER BY rowid",
        )?;
        let pending = query
            .query_map([prefix], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(query);
        let mut inserted = 0;
        for (key, payload) in pending {
            let added = self.inbox.execute(
                "INSERT INTO inbox VALUES (?1, ?2) ON CONFLICT(key) DO NOTHING",
                params![key, payload],
            )?;
            eprintln!("{key}: inserted={}", added == 1);
            if crash {
                std::process::exit(75);
            }
            self.worker
                .execute("UPDATE outbox SET delivered = 1 WHERE key = ?1", [&key])?;
            inserted += added;
        }
        Ok(inserted)
    }

    fn report(&self, key: &str) -> Result<Option<Value>> {
        let mut query = self
            .worker
            .prepare("SELECT payload FROM outbox WHERE key = ?1")?;
        let mut rows = query.query([key])?;
        rows.next()?
            .map(|row| -> Result<Value> { Ok(serde_json::from_str(&row.get::<_, String>(0)?)?) })
            .transpose()
    }
}

fn classify(text: &str) -> &'static str {
    let score = text
        .split(|c: char| !c.is_alphanumeric())
        .map(|word| match word.to_ascii_lowercase().as_str() {
            "restores" | "praise" | "recovery" | "improves" => 1,
            "outage" | "frustrates" | "delays" | "failure" => -1,
            _ => 0,
        })
        .sum::<i32>();
    match score.cmp(&0) {
        std::cmp::Ordering::Greater => "positive",
        std::cmp::Ordering::Less => "negative",
        std::cmp::Ordering::Equal => "neutral",
    }
}

fn entries(feed: &str) -> Result<Vec<Value>> {
    let document = roxmltree::Document::parse(feed)?;
    if !matches!(
        document.root_element().tag_name().name(),
        "rss" | "feed" | "RDF"
    ) {
        return Err("expected an RSS or Atom document".into());
    }
    let mut seen = HashSet::new();
    let mut items = Vec::new();
    for entry in document
        .descendants()
        .filter(|node| node.has_tag_name("item") || node.has_tag_name("entry"))
    {
        let child = |name| entry.children().find(|node| node.has_tag_name(name));
        let field = |name| {
            child(name)
                .and_then(|node| node.text())
                .unwrap_or("")
                .trim()
        };
        let title = field("title")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if title.is_empty() {
            continue;
        }
        let link = entry
            .children()
            .filter(|node| node.has_tag_name("link"))
            .find(|node| matches!(node.attribute("rel"), None | Some("alternate")))
            .and_then(|node| node.attribute("href").or_else(|| node.text()))
            .unwrap_or("")
            .trim();
        let id = [field("guid"), field("id"), link, &title]
            .into_iter()
            .find(|value| !value.is_empty())
            .unwrap();
        if seen.insert(id.to_owned()) {
            let text = format!(
                "{title} {} {} {}",
                field("description"),
                field("summary"),
                field("content")
            );
            items.push(
                json!({"id": id, "title": title, "link": link, "sentiment": classify(&text)}),
            );
        }
    }
    Ok(items)
}

fn briefing(city: &str) -> Result<Value> {
    let temperature = match city.to_ascii_lowercase().as_str() {
        "san jose" => 19,
        "seattle" => 12,
        "paris" => 16,
        _ => return Err(format!("no fixture weather for {city}").into()),
    };
    let outfit = if temperature < 15 {
        "Bring a jacket."
    } else {
        "Wear light layers."
    };
    Ok(json!({
        "source": "fixture",
        "weather": {"city": city, "temperature_c": temperature},
        "events": [{"title": "Planning session", "starts_at": (Utc::now() + chrono::Duration::hours(2)).to_rfc3339()}],
        "outfit": outfit
    }))
}

fn main() -> Result<()> {
    let args = Args::parse();
    if let Command::Briefing { city } = args.command {
        println!("{:#}", briefing(&city)?);
        return Ok(());
    }
    let storage = Storage::open(args.state)?;
    match args.command {
        Command::Monitor {
            job,
            feed_file,
            slot,
            minutes,
            crash_after_delivery,
        } => {
            if job.is_empty()
                || !job
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
            {
                return Err(
                    "job names must use ASCII letters, numbers, hyphens, or underscores".into(),
                );
            }
            let now = Utc::now().timestamp();
            let seconds = i64::from(minutes) * 60;
            let slot = slot.unwrap_or_else(|| {
                DateTime::from_timestamp(now - now.rem_euclid(seconds), 0).unwrap()
            });
            let prefix = format!("report:{job}:");
            let key = format!("{prefix}{}", slot.to_rfc3339());
            storage.deliver_pending(&prefix, crash_after_delivery)?;
            if storage.report(&key)?.is_none() {
                let feed = match feed_file {
                    Some(path) => fs::read_to_string(path)?,
                    None => FEED.to_owned(),
                };
                let report = json!({"key": key, "job": job, "slot": slot.to_rfc3339(), "items": entries(&feed)?});
                storage.worker.execute(
                    "INSERT INTO outbox (key, payload) VALUES (?1, ?2) ON CONFLICT(key) DO NOTHING",
                    params![key, report.to_string()],
                )?;
            }
            storage.deliver_pending(&prefix, crash_after_delivery)?;
            println!("{:#}", storage.report(&key)?.ok_or("missing report")?);
        }
        Command::Inbox => {
            let mut query = storage
                .inbox
                .prepare("SELECT payload FROM inbox ORDER BY rowid DESC LIMIT 50")?;
            let payloads = query.query_map([], |row| row.get::<_, String>(0))?;
            let mut messages = Vec::new();
            for payload in payloads {
                messages.push(serde_json::from_str::<Value>(&payload?)?);
            }
            println!("{:#}", json!(messages));
        }
        Command::Remind { id, at, text } => {
            let text = text.trim();
            if !(1..=100).contains(&id.chars().count())
                || !(1..=500).contains(&text.chars().count())
            {
                return Err(
                    "reminders require an ID of 1–100 characters and text of 1–500 characters"
                        .into(),
                );
            }
            let inserted = storage.worker.execute(
                "INSERT INTO reminders VALUES (?1, ?2, ?3) ON CONFLICT(id) DO NOTHING",
                params![id, at.timestamp_millis(), text],
            )?;
            if inserted == 0 {
                let saved = storage.worker.query_row(
                    "SELECT due_ms, text FROM reminders WHERE id = ?1",
                    [&id],
                    |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
                )?;
                if saved != (at.timestamp_millis(), text.to_owned()) {
                    return Err(format!("reminder ID {id} already has different details").into());
                }
            }
            println!("Scheduled {id} for {at}");
        }
        Command::RemindersTick => {
            storage.worker.execute(
                "INSERT INTO outbox (key, payload)
                 SELECT 'reminder:' || id,
                        json_object('key', 'reminder:' || id, 'id', id, 'due_ms', due_ms, 'text', text)
                 FROM reminders WHERE due_ms <= ?1 ON CONFLICT(key) DO NOTHING",
                [Utc::now().timestamp_millis()],
            )?;
            println!(
                "New reminders: {}",
                storage.deliver_pending("reminder:", false)?
            );
        }
        Command::Briefing { .. } => unreachable!(),
    }
    Ok(())
}
