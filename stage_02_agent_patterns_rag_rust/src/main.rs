use clap::{Parser, ValueEnum};
use std::{collections::HashSet, fs, io, path::PathBuf, time::Instant};

const CORPUS: [(&str, &str); 4] = [
    (
        "delivery.md",
        include_str!("../../stage_02_agent_patterns_rag_py/corpus/delivery.md"),
    ),
    (
        "observability.md",
        include_str!("../../stage_02_agent_patterns_rag_py/corpus/observability.md"),
    ),
    (
        "retrieval.md",
        include_str!("../../stage_02_agent_patterns_rag_py/corpus/retrieval.md"),
    ),
    (
        "retries.md",
        include_str!("../../stage_02_agent_patterns_rag_py/corpus/retries.md"),
    ),
];

const CASES: [(&str, Option<&str>); 5] = [
    (
        "How do we retry a rate limit with Retry-After?",
        Some("retries.md"),
    ),
    (
        "How does an outbox use a stable delivery key?",
        Some("delivery.md"),
    ),
    (
        "How should we measure retrieval recall and citation correctness?",
        Some("retrieval.md"),
    ),
    (
        "How should we compare p95 latency across tasks?",
        Some("observability.md"),
    ),
    ("What is the chemical formula for caffeine?", None),
];

#[derive(Clone, Copy, Debug, ValueEnum)]
enum Engine {
    Direct,
    React,
    Graph,
    Roles,
}

#[derive(Parser)]
#[command(about = "Retrieve and cite evidence from a local Markdown corpus")]
struct Args {
    #[arg(default_value = "How does an outbox use a stable delivery key?")]
    question: String,
    #[arg(long, value_enum, default_value = "react")]
    engine: Engine,
    #[arg(long, conflicts_with = "corpus")]
    compare: bool,
    #[arg(long, requires = "compare")]
    details: bool,
    #[arg(long)]
    corpus: Option<PathBuf>,
    #[arg(long, default_value_t = 3, value_parser = clap::value_parser!(u32).range(1..))]
    top_k: u32,
}

fn terms(text: &str) -> HashSet<String> {
    text.split(|character: char| !character.is_alphanumeric())
        .map(str::to_lowercase)
        .filter(|word| {
            word.len() >= 3
                && ![
                    "the", "how", "what", "does", "should", "can", "with", "for", "and", "are",
                    "from", "that", "when", "use",
                ]
                .contains(&word.as_str())
        })
        .collect()
}

struct Passage {
    source: String,
    text: String,
    terms: HashSet<String>,
}

fn load(corpus: Option<PathBuf>) -> io::Result<Vec<Passage>> {
    let documents = if let Some(path) = corpus {
        let mut documents = Vec::new();
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            if entry.path().extension().is_some_and(|ext| ext == "md")
                && entry.file_type()?.is_file()
            {
                documents.push((
                    entry.file_name().to_string_lossy().into_owned(),
                    fs::read_to_string(entry.path())?,
                ));
            }
        }
        documents.sort();
        documents
    } else {
        CORPUS
            .iter()
            .map(|(name, text)| (name.to_string(), text.to_string()))
            .collect()
    };
    let mut passages = Vec::new();
    for (source, text) in documents {
        let mut paragraph = Vec::new();
        for line in text.lines().map(str::trim).chain([""]) {
            if line.is_empty() || line.starts_with('#') {
                if !paragraph.is_empty() {
                    let text = paragraph.join(" ");
                    passages.push(Passage {
                        source: source.clone(),
                        terms: terms(&text),
                        text,
                    });
                    paragraph.clear();
                }
            } else {
                paragraph.push(line);
            }
        }
    }
    Ok(passages)
}

fn search<'a>(index: &'a [Passage], query: &HashSet<String>, limit: usize) -> Vec<&'a Passage> {
    let mut ranked: Vec<_> = index
        .iter()
        .map(|passage| (query.intersection(&passage.terms).count(), passage))
        .filter(|(score, _)| *score > 0)
        .collect();
    ranked.sort_by_key(|(score, _)| std::cmp::Reverse(*score));
    ranked
        .into_iter()
        .take(limit)
        .map(|(_, passage)| passage)
        .collect()
}

struct Report<'a> {
    evidence: Vec<&'a Passage>,
    citations: Vec<&'a Passage>,
    steps: Vec<&'static str>,
}

fn run<'a>(engine: Engine, question: &str, index: &'a [Passage], limit: usize) -> Report<'a> {
    let query = terms(question);
    let mut steps = Vec::new();
    let evidence = match engine {
        Engine::Direct => {
            steps.push("retrieve");
            search(index, &query, limit)
        }
        Engine::React => {
            steps.extend(["select search tool", "observe search results"]);
            search(index, &query, limit)
        }
        Engine::Graph => {
            steps.extend(["prepare query", "retrieve", "route on evidence"]);
            search(index, &query, limit)
        }
        Engine::Roles => {
            steps.extend([
                "researcher retrieves",
                "researcher hands evidence to writer",
            ]);
            search(index, &query, limit)
        }
    };
    // Draft extracts from retrieved passages so the cited text has a known source.
    let citations: Vec<_> = evidence
        .iter()
        .copied()
        .filter(|passage| query.intersection(&passage.terms).count() >= 2.min(query.len()))
        .collect();
    steps.push(if citations.is_empty() {
        "abstain"
    } else {
        "quote evidence"
    });
    if matches!(engine, Engine::Roles) {
        steps.push("reviewer checks citations");
    }
    Report {
        evidence,
        citations,
        steps,
    }
}

fn main() -> io::Result<()> {
    let args = Args::parse();
    let index = load(args.corpus)?;
    let limit = args.top_k as usize;
    if args.compare {
        for engine in [Engine::Direct, Engine::React, Engine::Graph, Engine::Roles] {
            let start = Instant::now();
            let (mut hits, mut cites, mut abstains) = (0, 0, 0);
            for (question, expected) in CASES {
                let report = run(engine, question, &index, limit);
                let retrieved = report
                    .evidence
                    .iter()
                    .any(|p| Some(p.source.as_str()) == expected);
                let cited = report
                    .citations
                    .iter()
                    .any(|p| Some(p.source.as_str()) == expected);
                let abstained = report.citations.is_empty();
                hits += usize::from(retrieved);
                cites += usize::from(cited);
                abstains += usize::from(expected.is_none() && abstained);
                if args.details {
                    println!(
                        "{engine:?} | {question} | retrieved={retrieved} cited={cited} abstained={abstained}"
                    );
                }
            }
            println!(
                "{engine:?}: retrieval {hits}/4, citations {cites}/4, correct abstentions {abstains}/1 ({:.2?})",
                start.elapsed()
            );
        }
    } else {
        let report = run(args.engine, &args.question, &index, limit);
        if report.citations.is_empty() {
            println!("The corpus did not provide enough evidence to answer.");
        }
        for (number, passage) in report.citations.iter().enumerate() {
            println!("[{}] {}\n    {}", number + 1, passage.source, passage.text);
        }
        println!("Steps: {}", report.steps.join(" → "));
    }
    Ok(())
}
