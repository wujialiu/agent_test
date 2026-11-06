package main

import (
	"flag"
	"os"
	"os/exec"
	"slices"
	"strings"
	"testing"
)

func TestCalculate(t *testing.T) {
	for _, sample := range []struct {
		expression string
		want       float64
	}{
		{"(12 + 8) / 4", 5},
		{"2 + 3 * 4", 14},
		{"-2 * +(3 + 1)", -8},
		{"5.5 % 2", 1.5},
		{"0.5 - 2", -1.5},
	} {
		t.Run(sample.expression, func(t *testing.T) {
			got, err := calculate(sample.expression)
			if err != nil || got != sample.want {
				t.Fatalf("calculate() = %g, %v; want %g", got, err, sample.want)
			}
		})
	}

	for _, input := range []string{"", "1/0", "0%0", "(1+2", "sqrt(4)", "1 << 3", "1/* 2 */+3", "1//2", "1/(1/0)", strings.Repeat("1", 161)} {
		if _, err := calculate(input); err == nil {
			t.Errorf("calculate(%q) accepted invalid input", input)
		}
	}
}

func TestToolsCLI(t *testing.T) {
	for _, test := range []struct {
		name  string
		args  []string
		input string
		want  []string
	}{
		{"combined", []string{"--trace", "--prompt", "Weather in San Jose AND calculate (12 + 8) / 4"}, "", []string{"San Jose: 19 °C", "weather (", "(12 + 8) / 4 = 5", "calculate ("}},
		{"unknown city", []string{"--prompt", "weather in Rome and calculate 2 + 2"}, "", []string{"weather error: no fixture weather for Rome", "2 + 2 = 4"}},
		{"session", nil, "calculate 18 / 2\ncalculate 1/0\nWhat did you calculate?\n/reset\nWhat did you calculate?\n/quit\ncalculate 100+1\n",
			[]string{"18 / 2 = 9", "calculate error:", "18 / 2 = 9", "No calculation in this session yet."}},
	} {
		t.Run(test.name, func(t *testing.T) {
			args := append([]string{"-test.run=^TestToolsProcess$", "--"}, test.args...)
			cmd := exec.Command(os.Args[0], args...)
			cmd.Env = append(os.Environ(), "GO_TOOLS_TEST_PROCESS=1")
			cmd.Stdin = strings.NewReader(test.input)
			result, err := cmd.CombinedOutput()
			if err != nil {
				t.Fatalf("CLI failed: %v\n%s", err, result)
			}
			remaining := string(result)
			for _, expected := range test.want {
				_, after, ok := strings.Cut(remaining, expected)
				if !ok {
					t.Fatalf("missing %q in order:\n%s", expected, result)
				}
				remaining = after
			}
			if strings.Contains(string(result), "100+1") {
				t.Fatalf("CLI processed input after /quit:\n%s", result)
			}
		})
	}
}

func TestToolsProcess(t *testing.T) {
	if os.Getenv("GO_TOOLS_TEST_PROCESS") != "1" {
		return
	}
	os.Args = append(os.Args[:1], os.Args[slices.Index(os.Args, "--")+1:]...)
	flag.CommandLine = flag.NewFlagSet(os.Args[0], flag.ExitOnError)
	main()
	os.Exit(0)
}
