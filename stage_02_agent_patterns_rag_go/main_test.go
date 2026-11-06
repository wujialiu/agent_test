package main

import (
	"flag"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/fstest"
)

func TestResearchFixtures(t *testing.T) {
	corpus, err := fs.Sub(fixtures, "corpus")
	if err != nil {
		t.Fatal(err)
	}
	index, err := load(corpus)
	if err != nil {
		t.Fatal(err)
	}
	for _, engine := range []string{"direct", "react", "graph", "roles"} {
		for _, sample := range cases {
			t.Run(engine+"/"+sample.question, func(t *testing.T) {
				result := research(index, engine, sample.question, 3)
				if len(result.hits) > 3 {
					t.Fatalf("top-k exceeded: %d hits", len(result.hits))
				}
				if sample.source == "" {
					if len(result.citations) != 0 || result.steps[len(result.steps)-1] != "abstain" {
						t.Fatalf("expected abstention, got %+v", result)
					}
					return
				}
				found := false
				for _, quote := range result.citations {
					found = found || quote.source == sample.source
					if !slices.ContainsFunc(result.hits, func(evidence hit) bool {
						return evidence.source == quote.source && evidence.text == quote.text
					}) {
						t.Errorf("citation has no matching retrieved passage: %+v", quote)
					}
				}
				if !found {
					t.Fatalf("missing expected citation %q", sample.source)
				}
			})
		}
	}
}

func TestCustomCorpus(t *testing.T) {
	index, err := load(fstest.MapFS{
		"a.md":        {Data: []byte("# Heading\n\nFirst paragraph about orchids.\nIt wraps.\n\nSecond paragraph about roses.")},
		"b.md":        {Data: []byte("Orchids require humidity.")},
		"ignored.txt": {Data: []byte("Orchids and humidity.")},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(index) != 3 || index[0].text != "First paragraph about orchids. It wraps." {
		t.Fatalf("unexpected chunks: %+v", index)
	}
	ranked := search(index, terms("orchids humidity"), 1)
	if len(ranked) != 1 || ranked[0].source != "b.md" || ranked[0].score != 2 {
		t.Fatalf("unexpected ranking: %+v", ranked)
	}
	result := research(index, "direct", "orchids caffeine", 3)
	if len(result.hits) == 0 || len(result.citations) != 0 {
		t.Fatalf("weak evidence should be retrieved without being cited: %+v", result)
	}
	if _, err := load(fstest.MapFS{}); err == nil {
		t.Error("empty corpus accepted")
	}
}

func TestResearchCLI(t *testing.T) {
	custom := t.TempDir()
	if err := os.WriteFile(filepath.Join(custom, "orchids.md"), []byte("Orchids require humidity."), 0600); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name  string
		args  []string
		want  string
		count int
		fail  bool
	}{
		{"default", nil, "[1] delivery.md", 1, false},
		{"comparison", []string{"--compare", "--details"}, "retrieval 4/4, citations 4/4, correct abstentions 1/1", 4, false},
		{"custom", []string{"--corpus", custom, "--top-k", "1", "orchids humidity"}, "[1] orchids.md", 1, false},
		{"bad engine", []string{"--engine", "unknown"}, "choose a valid engine", 1, true},
		{"bad top k", []string{"--top-k", "0"}, "positive top-k", 1, true},
		{"details alone", []string{"--details"}, "--details requires --compare", 1, true},
		{"compare custom corpus", []string{"--compare", "--corpus", custom}, "--compare requires the built-in corpus", 1, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			args := append([]string{"-test.run=^TestResearchProcess$", "--"}, test.args...)
			cmd := exec.Command(os.Args[0], args...)
			cmd.Env = append(os.Environ(), "GO_RESEARCH_TEST_PROCESS=1")
			result, err := cmd.CombinedOutput()
			if (err != nil) != test.fail || strings.Count(string(result), test.want) != test.count {
				t.Fatalf("unexpected CLI result: %v\n%s", err, result)
			}
		})
	}
}

func TestResearchProcess(t *testing.T) {
	if os.Getenv("GO_RESEARCH_TEST_PROCESS") != "1" {
		return
	}
	os.Args = append(os.Args[:1], os.Args[slices.Index(os.Args, "--")+1:]...)
	flag.CommandLine = flag.NewFlagSet(os.Args[0], flag.ExitOnError)
	main()
	os.Exit(0)
}
