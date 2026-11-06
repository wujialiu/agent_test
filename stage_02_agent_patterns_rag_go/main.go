package main

import (
	"embed"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"sort"
	"strings"
	"time"
	"unicode"
)

//go:embed corpus/*.md
var fixtures embed.FS

var cases = []struct{ question, source string }{
	{"How do we retry a rate limit with Retry-After?", "retries.md"},
	{"How does an outbox use a stable delivery key?", "delivery.md"},
	{"How should we measure retrieval recall and citation correctness?", "retrieval.md"},
	{"How should we compare p95 latency across tasks?", "observability.md"},
	{"What is the chemical formula for caffeine?", ""},
}

type passage struct {
	source, text string
	terms        map[string]struct{}
}

func terms(text string) map[string]struct{} {
	result := make(map[string]struct{})
	for _, word := range strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	}) {
		if len(word) < 3 {
			continue
		}
		switch word {
		case "the", "how", "what", "does", "should", "can", "with", "for", "and", "are", "from", "that", "when", "use":
			continue
		}
		result[word] = struct{}{}
	}
	return result
}

func load(corpus fs.FS) ([]passage, error) {
	files, err := fs.Glob(corpus, "*.md")
	if err != nil {
		return nil, err
	}
	if len(files) == 0 {
		return nil, fmt.Errorf("no Markdown files in corpus")
	}
	var index []passage
	for _, name := range files {
		data, err := fs.ReadFile(corpus, name)
		if err != nil {
			return nil, err
		}
		var paragraph []string
		for _, line := range strings.Split(string(data)+"\n", "\n") {
			line = strings.TrimSpace(line)
			if line != "" && !strings.HasPrefix(line, "#") {
				paragraph = append(paragraph, line)
				continue
			}
			if len(paragraph) != 0 {
				text := strings.Join(paragraph, " ")
				index = append(index, passage{name, text, terms(text)})
				paragraph = paragraph[:0]
			}
		}
	}
	return index, nil
}

type hit struct {
	passage
	score int
}

func search(index []passage, query map[string]struct{}, topK int) []hit {
	var results []hit
	for _, candidate := range index {
		score := 0
		for term := range query {
			if _, ok := candidate.terms[term]; ok {
				score++
			}
		}
		if score > 0 {
			results = append(results, hit{candidate, score})
		}
	}
	sort.SliceStable(results, func(i, j int) bool { return results[i].score > results[j].score })
	return results[:min(topK, len(results))]
}

type report struct {
	hits      []hit
	citations []passage
	steps     []string
}

var walkthroughs = map[string][]string{
	"direct": {"retrieve"},
	"react":  {"select search tool", "observe search results"},
	"graph":  {"prepare query", "retrieve", "route on evidence"},
	"roles":  {"researcher retrieves", "researcher hands evidence to writer"},
}

func research(index []passage, engine, question string, topK int) report {
	query := terms(question)
	result := report{hits: search(index, query, topK), steps: append([]string(nil), walkthroughs[engine]...)}
	for _, item := range result.hits {
		if item.score >= min(2, len(query)) {
			result.citations = append(result.citations, item.passage)
		}
	}
	if len(result.citations) == 0 {
		result.steps = append(result.steps, "abstain")
	} else {
		result.steps = append(result.steps, "quote evidence")
	}
	return result
}

func run() error {
	engine := flag.String("engine", "react", "walkthrough: direct, react, graph, or roles")
	compare := flag.Bool("compare", false, "evaluate the built-in questions")
	details := flag.Bool("details", false, "show individual evaluation results")
	directory := flag.String("corpus", "", "directory of Markdown files")
	topK := flag.Int("top-k", 3, "maximum retrieved passages")
	flag.Parse()

	if _, ok := walkthroughs[*engine]; !ok || *topK < 1 {
		return fmt.Errorf("choose a valid engine and a positive top-k")
	}
	if (*compare && *directory != "") || (*details && !*compare) {
		return fmt.Errorf("--compare requires the built-in corpus and --details requires --compare")
	}
	corpus, err := fs.Sub(fixtures, "corpus")
	if err != nil {
		return err
	}
	if *directory != "" {
		corpus = os.DirFS(*directory)
	}
	index, err := load(corpus)
	if err != nil {
		return err
	}

	if *compare {
		for _, name := range []string{"direct", "react", "graph", "roles"} {
			start := time.Now()
			hits, citations, abstentions := 0, 0, 0
			for _, sample := range cases {
				result := research(index, name, sample.question, *topK)
				retrieved, cited := false, false
				for _, item := range result.hits {
					retrieved = retrieved || item.source == sample.source
				}
				for _, item := range result.citations {
					cited = cited || item.source == sample.source
				}
				if retrieved {
					hits++
				}
				if cited {
					citations++
				}
				abstained := len(result.citations) == 0
				if sample.source == "" && abstained {
					abstentions++
				}
				if *details {
					fmt.Printf("%s | %s | retrieved=%t cited=%t abstained=%t\n", name, sample.question, retrieved, cited, abstained)
				}
			}
			fmt.Printf("%s: retrieval %d/4, citations %d/4, correct abstentions %d/1 (%s)\n", name, hits, citations, abstentions, time.Since(start))
		}
		return nil
	}
	question := strings.Join(flag.Args(), " ")
	if question == "" {
		question = cases[1].question
	}
	result := research(index, *engine, question, *topK)
	if len(result.citations) == 0 {
		fmt.Println("The corpus did not provide enough evidence to answer.")
	}
	for i, item := range result.citations {
		fmt.Printf("[%d] %s\n    %s\n", i+1, item.source, item.text)
	}
	fmt.Println("Steps:", strings.Join(result.steps, " → "))
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
