package main

import (
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestParseFeed(t *testing.T) {
	for _, sample := range []struct {
		name, xml string
		want      []item
	}{
		{"RSS fixture", fixture, []item{
			{"https://example.invalid/restored", "Service restores access", "https://example.invalid/restored", "positive"},
			{"https://example.invalid/outage", "Payment outage frustrates customers", "https://example.invalid/outage", "negative"},
			{"https://example.invalid/earnings", "Company schedules earnings call", "https://example.invalid/earnings", "neutral"},
		}},
		{"Atom", `<feed xmlns="http://www.w3.org/2005/Atom">
			<entry><id>stable</id><title> Service   update </title><link rel="self" href="self"/>
			<link rel="alternate" href="https://example.test/one"/><summary>Delays reported</summary></entry>
			<entry><id>stable</id><title>Duplicate</title></entry><entry><id>blank</id></entry></feed>`,
			[]item{{"stable", "Service update", "https://example.test/one", "negative"}}},
		{"RDF", `<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><item><title>Sample</title></item></rdf:RDF>`,
			[]item{{"Sample", "Sample", "", "neutral"}}},
	} {
		t.Run(sample.name, func(t *testing.T) {
			got, err := parseFeed([]byte(sample.xml))
			if err != nil || !slices.Equal(got, sample.want) {
				t.Fatalf("parseFeed() = %+v, %v; want %+v", got, err, sample.want)
			}
		})
	}
	for _, invalid := range []string{"<rss>", "<html/>", "<rss><channel>&unknown;</channel></rss>"} {
		if _, err := parseFeed([]byte(invalid)); err == nil {
			t.Errorf("accepted %q", invalid)
		}
	}
}

func productionCommand(state string, args ...string) *exec.Cmd {
	command := append([]string{"-test.run=^TestProductionProcess$", "--", "--state", state}, args...)
	cmd := exec.Command(os.Args[0], command...)
	cmd.Env = append(os.Environ(), "GO_PRODUCTION_TEST_PROCESS=1")
	return cmd
}

func productionOutput(t *testing.T, state string, args ...string) []byte {
	t.Helper()
	output, err := productionCommand(state, args...).Output()
	if err != nil {
		var status *exec.ExitError
		if errors.As(err, &status) {
			t.Fatalf("%v: %v\n%s", args, err, status.Stderr)
		}
		t.Fatalf("%v: %v", args, err)
	}
	return output
}

func inbox(t *testing.T, state string) []map[string]any {
	t.Helper()
	var reports []map[string]any
	if err := json.Unmarshal(productionOutput(t, state, "inbox"), &reports); err != nil {
		t.Fatal(err)
	}
	return reports
}

func TestMonitorCrashRecovery(t *testing.T) {
	state := t.TempDir()
	const slot = "2030-01-02T03:00:00Z"
	crash, err := productionCommand(state, "monitor", "--slot", slot, "--crash-after-delivery").CombinedOutput()
	var status *exec.ExitError
	if !errors.As(err, &status) || status.ExitCode() != 75 || !strings.Contains(string(crash), "inserted=true") {
		t.Fatalf("expected crash after inbox commit: %v\n%s", err, crash)
	}
	if reports := inbox(t, state); len(reports) != 1 {
		t.Fatalf("expected committed report before replay, got %v", reports)
	}
	replay, err := productionCommand(state, "monitor", "--slot", "2030-01-01T19:00:00-08:00").CombinedOutput()
	if err != nil || !strings.Contains(string(replay), "inserted=false") {
		t.Fatalf("expected same delivery key on replay: %v\n%s", err, replay)
	}
	third, err := productionCommand(state, "monitor", "--slot", slot).CombinedOutput()
	if err != nil || strings.Contains(string(third), "inserted=") {
		t.Fatalf("expected acknowledged report to be skipped: %v\n%s", err, third)
	}
	if reports := inbox(t, state); len(reports) != 1 {
		t.Fatalf("replay duplicated the report: %v", reports)
	}
}

func TestMonitorConcurrentSlot(t *testing.T) {
	state := t.TempDir()
	productionOutput(t, state, "inbox")
	results := make(chan error, 4)
	for range cap(results) {
		go func() {
			output, err := productionCommand(state, "monitor", "--slot", "2030-01-02T04:00:00Z").CombinedOutput()
			if err != nil {
				err = errors.New(err.Error() + "\n" + string(output))
			}
			results <- err
		}()
	}
	for range cap(results) {
		if err := <-results; err != nil {
			t.Error(err)
		}
	}
	if reports := inbox(t, state); len(reports) != 1 {
		t.Fatalf("expected a single report: %v", reports)
	}
}

func TestReminderDelivery(t *testing.T) {
	state := t.TempDir()
	productionOutput(t, state, "remind", "--id", "due", "--at", "2000-01-01T09:00:00-08:00", "Check the outbox")
	productionOutput(t, state, "remind", "--id", "due", "--at", "2000-01-01T17:00:00Z", "Check the outbox")
	productionOutput(t, state, "remind", "--id", "future", "--at", "2099-01-01T00:00:00Z", "Wait")
	if output, err := productionCommand(state, "remind", "--id", "due", "--at", "2000-01-01T17:00:00Z", "Changed").CombinedOutput(); err == nil || !strings.Contains(string(output), "already has different details") {
		t.Fatalf("expected reminder ID conflict: %v\n%s", err, output)
	}
	if got := strings.TrimSpace(string(productionOutput(t, state, "reminders-tick"))); got != "New reminders: 1" {
		t.Fatal(got)
	}
	if got := strings.TrimSpace(string(productionOutput(t, state, "reminders-tick"))); got != "New reminders: 0" {
		t.Fatal(got)
	}
	if reports := inbox(t, state); len(reports) != 1 || reports[0]["id"] != "due" || reports[0]["text"] != "Check the outbox" {
		t.Fatalf("expected only the due reminder: %v", reports)
	}
}

func TestBriefing(t *testing.T) {
	state := t.TempDir()
	var result struct {
		Weather struct {
			City        string
			Temperature int `json:"temperature_c"`
		}
		Events []struct{ Title string }
		Outfit string
	}
	if err := json.Unmarshal(productionOutput(t, state, "briefing", "--city", "Seattle"), &result); err != nil {
		t.Fatal(err)
	}
	if result.Weather.City != "Seattle" || result.Weather.Temperature != 12 ||
		len(result.Events) != 1 || result.Events[0].Title == "" || result.Outfit != "Bring a jacket." {
		t.Fatalf("unexpected briefing: %+v", result)
	}
	if output, err := productionCommand(state, "briefing", "--city", "Rome").CombinedOutput(); err == nil || !strings.Contains(string(output), "no fixture weather") {
		t.Fatalf("expected unknown city error: %v\n%s", err, output)
	}
}

func TestProductionProcess(t *testing.T) {
	if os.Getenv("GO_PRODUCTION_TEST_PROCESS") != "1" {
		return
	}
	os.Args = append(os.Args[:1], os.Args[slices.Index(os.Args, "--")+1:]...)
	main()
	os.Exit(0)
}

func TestMonitorFeedFile(t *testing.T) {
	state := t.TempDir()
	path := filepath.Join(state, "feed.xml")
	if err := os.WriteFile(path, []byte(`<rss><channel><item><guid>sample</guid><title>Service improves</title></item></channel></rss>`), 0600); err != nil {
		t.Fatal(err)
	}
	var report struct {
		Job   string
		Items []item
	}
	if err := json.Unmarshal(productionOutput(t, state, "monitor", "--job", "custom", "--feed-file", path, "--minutes", "30"), &report); err != nil {
		t.Fatal(err)
	}
	want := []item{{"sample", "Service improves", "", "positive"}}
	if report.Job != "custom" || !slices.Equal(report.Items, want) {
		t.Fatalf("unexpected report: %+v", report)
	}
	for _, args := range [][]string{
		{"monitor", "--minutes", "0"},
		{"monitor", "--job", "invalid:job"},
		{"monitor", "--slot", "tomorrow"},
		{"monitor", "--job", "missing", "--feed-file", filepath.Join(state, "missing.xml")},
		{"remind", "--id", "invalid-date", "--at", "tomorrow", "Message"},
		{"remind", "--id", "", "--at", "2000-01-01T00:00:00Z", "Message"},
	} {
		if output, err := productionCommand(state, args...).CombinedOutput(); err == nil {
			t.Errorf("accepted %v:\n%s", args, output)
		}
	}
	if reports := inbox(t, state); len(reports) != 1 {
		t.Fatalf("invalid commands changed the inbox: %v", reports)
	}
}
