package main

import (
	"database/sql"
	"encoding/json"
	"encoding/xml"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	_ "github.com/mattn/go-sqlite3"
)

const fixture = `<rss><channel>
<item><title>Service restores access</title><link>https://example.invalid/restored</link><description>Customers praise the recovery.</description></item>
<item><title>Payment outage frustrates customers</title><link>https://example.invalid/outage</link><description>Several regions reported delays.</description></item>
<item><title>Company schedules earnings call</title><link>https://example.invalid/earnings</link><description>The call is Thursday.</description></item>
</channel></rss>`

type feedEntry struct {
	Title       string `xml:"title"`
	GUID        string `xml:"guid"`
	ID          string `xml:"id"`
	Description string `xml:"description"`
	Summary     string `xml:"summary"`
	Content     string `xml:"content"`
	Links       []struct {
		URL  string `xml:"href,attr"`
		Rel  string `xml:"rel,attr"`
		Text string `xml:",chardata"`
	} `xml:"link"`
}

type item struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	Link      string `json:"link"`
	Sentiment string `json:"sentiment"`
}

func sentiment(text string) string {
	score := 0
	for _, word := range strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	}) {
		switch word {
		case "restores", "praise", "recovery", "improves":
			score++
		case "outage", "frustrates", "delays", "failure":
			score--
		}
	}
	switch {
	case score > 0:
		return "positive"
	case score < 0:
		return "negative"
	default:
		return "neutral"
	}
}

func parseFeed(data []byte) ([]item, error) {
	var feed struct {
		XMLName xml.Name
		Atom    []feedEntry `xml:"entry"`
		RSS     []feedEntry `xml:"channel>item"`
		RDF     []feedEntry `xml:"item"`
	}
	if err := xml.Unmarshal(data, &feed); err != nil {
		return nil, err
	}
	var entries []feedEntry
	switch feed.XMLName.Local {
	case "rss":
		entries = feed.RSS
	case "feed":
		entries = feed.Atom
	case "RDF":
		entries = feed.RDF
	default:
		return nil, errors.New("expected an RSS or Atom document")
	}
	items := make([]item, 0, len(entries))
	seen := make(map[string]bool)
	for _, entry := range entries {
		title := strings.Join(strings.Fields(entry.Title), " ")
		if title == "" {
			continue
		}
		link := ""
		for _, candidate := range entry.Links {
			if candidate.Rel == "" || candidate.Rel == "alternate" {
				link = strings.TrimSpace(candidate.URL)
				if link == "" {
					link = strings.TrimSpace(candidate.Text)
				}
				if link != "" {
					break
				}
			}
		}
		id := title
		for _, candidate := range []string{entry.GUID, entry.ID, link} {
			if value := strings.TrimSpace(candidate); value != "" {
				id = value
				break
			}
		}
		if !seen[id] {
			seen[id] = true
			text := strings.Join([]string{title, entry.Description, entry.Summary, entry.Content}, " ")
			items = append(items, item{id, title, link, sentiment(text)})
		}
	}
	return items, nil
}

type storage struct {
	worker, inbox *sql.DB
}

func openDB(path, schema string) (*sql.DB, error) {
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func openStorage(path string) (*storage, error) {
	if err := os.MkdirAll(path, 0700); err != nil {
		return nil, err
	}
	worker, err := openDB(filepath.Join(path, "worker.sqlite3"), `
		CREATE TABLE IF NOT EXISTS outbox (
			key TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
		);
		CREATE TABLE IF NOT EXISTS reminders (
			id TEXT PRIMARY KEY, due_ms INTEGER NOT NULL, text TEXT NOT NULL
		);`)
	if err != nil {
		return nil, err
	}
	inbox, err := openDB(filepath.Join(path, "inbox.sqlite3"), `
		CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, payload TEXT NOT NULL);`)
	if err != nil {
		worker.Close()
		return nil, err
	}
	return &storage{worker, inbox}, nil
}

func (s *storage) deliver(prefix string, crash bool) (int64, error) {
	rows, err := s.worker.Query(`SELECT key, payload FROM outbox
		WHERE delivered = 0 AND substr(key, 1, length(?1)) = ?1 ORDER BY rowid`, prefix)
	if err != nil {
		return 0, err
	}
	defer rows.Close()

	var pending []struct{ key, payload string }
	for rows.Next() {
		var message struct{ key, payload string }
		if err := rows.Scan(&message.key, &message.payload); err != nil {
			return 0, err
		}
		pending = append(pending, message)
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}
	var inserted int64
	for _, message := range pending {
		result, err := s.inbox.Exec(`INSERT INTO inbox VALUES (?, ?) ON CONFLICT(key) DO NOTHING`,
			message.key, message.payload)
		if err != nil {
			return inserted, err
		}
		added, err := result.RowsAffected()
		if err != nil {
			return inserted, err
		}
		fmt.Fprintf(os.Stderr, "%s: inserted=%t\n", message.key, added == 1)
		if crash {
			os.Exit(75)
		}
		if _, err := s.worker.Exec(`UPDATE outbox SET delivered = 1 WHERE key = ?`, message.key); err != nil {
			return inserted, err
		}
		inserted += added
	}
	return inserted, nil
}

func output(value any) error {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

var jobName = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)

func (s *storage) monitor(args []string) error {
	flags := flag.NewFlagSet("monitor", flag.ContinueOnError)
	job := flags.String("job", "sentiment-demo", "stable job name")
	file := flags.String("feed-file", "", "local RSS or Atom file")
	slotText := flags.String("slot", "", "explicit RFC 3339 slot")
	minutes := flags.Int("minutes", 60, "UTC slot length in minutes")
	crash := flags.Bool("crash-after-delivery", false, "exit 75 after receiver commits")
	if err := parseFlags(flags, args, 0); err != nil {
		return err
	}
	if !jobName.MatchString(*job) || *minutes < 1 || *minutes > int((1<<63-1)/time.Minute) {
		return errors.New("provide a valid job name and positive slot length")
	}
	duration := time.Duration(*minutes) * time.Minute
	now := time.Now().UTC()
	seconds := int64(duration / time.Second)
	slot := time.Unix(now.Unix()/seconds*seconds, 0).UTC()
	if *slotText != "" {
		parsed, err := time.Parse(time.RFC3339Nano, *slotText)
		if err != nil {
			return err
		}
		slot = parsed.UTC()
	}
	prefix := "report:" + *job + ":"
	key := prefix + slot.Format(time.RFC3339Nano)
	if _, err := s.deliver(prefix, *crash); err != nil {
		return err
	}
	var payload string
	query := `SELECT payload FROM outbox WHERE key = ?`
	err := s.worker.QueryRow(query, key).Scan(&payload)
	if errors.Is(err, sql.ErrNoRows) {
		data := []byte(fixture)
		if *file != "" {
			data, err = os.ReadFile(*file)
			if err != nil {
				return err
			}
		}
		items, err := parseFeed(data)
		if err != nil {
			return err
		}
		body, err := json.Marshal(map[string]any{"key": key, "job": *job, "slot": slot, "items": items})
		if err != nil {
			return err
		}
		if _, err := s.worker.Exec(`INSERT INTO outbox (key, payload) VALUES (?, ?) ON CONFLICT(key) DO NOTHING`,
			key, string(body)); err != nil {
			return err
		}
		if err := s.worker.QueryRow(query, key).Scan(&payload); err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	if _, err := s.deliver(prefix, *crash); err != nil {
		return err
	}
	return output(json.RawMessage(payload))
}

func (s *storage) remind(args []string) error {
	flags := flag.NewFlagSet("remind", flag.ContinueOnError)
	id := flags.String("id", "", "stable reminder ID")
	at := flags.String("at", "", "RFC 3339 timestamp")
	if err := parseFlags(flags, args, 1); err != nil {
		return err
	}
	due, err := time.Parse(time.RFC3339Nano, *at)
	if err != nil {
		return err
	}
	text := strings.TrimSpace(flags.Arg(0))
	if *id == "" || utf8.RuneCountInString(*id) > 100 || text == "" || utf8.RuneCountInString(text) > 500 {
		return errors.New("reminders require an ID of 1–100 characters and text of 1–500 characters")
	}
	if _, err := s.worker.Exec(`INSERT INTO reminders VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING`,
		*id, due.UnixMilli(), text); err != nil {
		return err
	}
	var savedDue int64
	var savedText string
	if err := s.worker.QueryRow(`SELECT due_ms, text FROM reminders WHERE id = ?`, *id).
		Scan(&savedDue, &savedText); err != nil {
		return err
	}
	if savedDue != due.UnixMilli() || savedText != text {
		return fmt.Errorf("reminder ID %q already has different details", *id)
	}
	fmt.Printf("Scheduled %s for %s\n", *id, due.UTC().Format(time.RFC3339Nano))
	return nil
}

func (s *storage) tick() error {
	_, err := s.worker.Exec(`INSERT INTO outbox (key, payload)
		SELECT 'reminder:' || id,
		       json_object('key', 'reminder:' || id, 'id', id, 'due_ms', due_ms, 'text', text)
		FROM reminders WHERE due_ms <= ? ON CONFLICT(key) DO NOTHING`, time.Now().UnixMilli())
	if err != nil {
		return err
	}
	inserted, err := s.deliver("reminder:", false)
	if err != nil {
		return err
	}
	fmt.Printf("New reminders: %d\n", inserted)
	return nil
}

func (s *storage) recent() error {
	rows, err := s.inbox.Query(`SELECT payload FROM inbox ORDER BY rowid DESC LIMIT 50`)
	if err != nil {
		return err
	}
	defer rows.Close()
	messages := make([]json.RawMessage, 0)
	for rows.Next() {
		var payload string
		if err := rows.Scan(&payload); err != nil {
			return err
		}
		messages = append(messages, json.RawMessage(payload))
	}
	if err := rows.Err(); err != nil {
		return err
	}
	return output(messages)
}

func briefing(args []string) error {
	flags := flag.NewFlagSet("briefing", flag.ContinueOnError)
	city := flags.String("city", "San Jose", "fixture city")
	if err := parseFlags(flags, args, 0); err != nil {
		return err
	}
	temperatures := map[string]int{"san jose": 19, "seattle": 12, "paris": 16}
	temperature, ok := temperatures[strings.ToLower(*city)]
	if !ok {
		return fmt.Errorf("no fixture weather for %s", *city)
	}
	outfit := "Wear light layers."
	if temperature < 15 {
		outfit = "Bring a jacket."
	}
	return output(map[string]any{
		"source":  "fixture",
		"weather": map[string]any{"city": *city, "temperature_c": temperature},
		"events":  []map[string]any{{"title": "Planning session", "starts_at": time.Now().UTC().Add(2 * time.Hour)}},
		"outfit":  outfit,
	})
}

func parseFlags(flags *flag.FlagSet, args []string, operands int) error {
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != operands {
		return fmt.Errorf("%s expects %d positional arguments", flags.Name(), operands)
	}
	return nil
}

func run(args []string) error {
	flags := flag.NewFlagSet("production", flag.ContinueOnError)
	state := flags.String("state", ".agent_lab/production-go", "local SQLite state directory")
	flags.Usage = func() {
		fmt.Fprintln(flags.Output(), "Usage: production [--state DIR] <monitor|inbox|briefing|remind|reminders-tick>")
		flags.PrintDefaults()
	}
	if err := flags.Parse(args); err != nil {
		return err
	}
	command := flags.Arg(0)
	switch command {
	case "briefing":
		return briefing(flags.Args()[1:])
	case "monitor", "remind":
	case "inbox", "reminders-tick":
		if err := parseFlags(flag.NewFlagSet(command, flag.ContinueOnError), flags.Args()[1:], 0); err != nil {
			return err
		}
	default:
		flags.Usage()
		return errors.New("choose a command")
	}
	s, err := openStorage(*state)
	if err != nil {
		return err
	}
	defer s.worker.Close()
	defer s.inbox.Close()
	switch command {
	case "monitor":
		return s.monitor(flags.Args()[1:])
	case "remind":
		return s.remind(flags.Args()[1:])
	case "inbox":
		return s.recent()
	default:
		return s.tick()
	}
}

func main() {
	if err := run(os.Args[1:]); err != nil && !errors.Is(err, flag.ErrHelp) {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
