package main

import (
	"bufio"
	"errors"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"math"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var (
	weatherRequest = regexp.MustCompile(`(?i)\bweather\s+(?:in|for)\s+(.+?)(?:\s+and\s+|[?,;]|$)`)
	mathRequest    = regexp.MustCompile(`(?i)\b(?:calculate|compute|evaluate)\s+(.+?)(?:\s+and\s+|[?;]|$)`)
	arithmetic     = regexp.MustCompile(`^[0-9 .()+*/%\-]{1,160}$`)
)

func evaluate(expr ast.Expr) (float64, error) {
	switch node := expr.(type) {
	case *ast.BasicLit:
		return strconv.ParseFloat(node.Value, 64)
	case *ast.ParenExpr:
		return evaluate(node.X)
	case *ast.UnaryExpr:
		value, err := evaluate(node.X)
		switch node.Op {
		case token.ADD:
			return value, err
		case token.SUB:
			return -value, err
		}
	case *ast.BinaryExpr:
		left, err := evaluate(node.X)
		if err != nil {
			return 0, err
		}
		right, err := evaluate(node.Y)
		if err != nil {
			return 0, err
		}
		switch node.Op {
		case token.ADD:
			return left + right, nil
		case token.SUB:
			return left - right, nil
		case token.MUL:
			return left * right, nil
		case token.QUO, token.REM:
			if right == 0 {
				return 0, errors.New("division or remainder by zero")
			}
			if node.Op == token.QUO {
				return left / right, nil
			}
			return math.Mod(left, right), nil
		}
	}
	return 0, errors.New("unsupported arithmetic expression")
}

func calculate(input string) (float64, error) {
	if !arithmetic.MatchString(input) || (strings.Contains(input, "/*") || strings.Contains(input, "//")) {
		return 0, errors.New("enter numbers, parentheses, and + - * / %")
	}
	expr, err := parser.ParseExpr(input)
	if err != nil {
		return 0, errors.New("invalid arithmetic expression")
	}
	value, err := evaluate(expr)
	if err != nil {
		return 0, err
	}
	if math.IsInf(value, 0) || math.IsNaN(value) {
		return 0, errors.New("result is undefined or out of range")
	}
	return value, nil
}

type session struct {
	lastCalculation string
}

func (s *session) answer(prompt string, trace bool) {
	traceTool := func(name string, start time.Time) {
		if trace {
			fmt.Fprintf(os.Stderr, "%s (%s)\n", name, time.Since(start))
		}
	}
	city := weatherRequest.FindStringSubmatch(prompt)
	expression := mathRequest.FindStringSubmatch(prompt)
	if city != nil {
		start := time.Now()
		name := strings.TrimSpace(city[1])
		temperatures := map[string]int{"san jose": 19, "seattle": 12, "paris": 16}
		if temperature, ok := temperatures[strings.ToLower(name)]; ok {
			fmt.Printf("%s: %d °C (fixture)\n", name, temperature)
		} else {
			fmt.Printf("weather error: no fixture weather for %s\n", name)
		}
		traceTool("weather", start)
	}
	if expression != nil {
		start := time.Now()
		input := strings.TrimSpace(expression[1])
		value, err := calculate(input)
		if err != nil {
			fmt.Printf("calculate error: %s\n", err)
		} else {
			s.lastCalculation = fmt.Sprintf("%s = %g", input, value)
			fmt.Println(s.lastCalculation)
		}
		traceTool("calculate", start)
	}
	if city == nil && expression == nil {
		switch {
		case !strings.Contains(strings.ToLower(prompt), "what did you calculate"):
			fmt.Println("Ask for weather in a city, or try 'calculate (12 + 8) / 4'.")
		case s.lastCalculation == "":
			fmt.Println("No calculation in this session yet.")
		default:
			fmt.Println(s.lastCalculation)
		}
	}
}

func main() {
	prompt := flag.String("prompt", "", "answer a single prompt")
	trace := flag.Bool("trace", false, "show tool timings")
	flag.Parse()
	var conversation session
	if *prompt != "" {
		conversation.answer(*prompt, *trace)
		return
	}
	fmt.Println("Weather and math assistant. /reset clears the session, /quit exits.")
	scanner := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("you> ")
		if !scanner.Scan() {
			if err := scanner.Err(); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
			return
		}
		switch prompt := strings.TrimSpace(scanner.Text()); prompt {
		case "/quit":
			return
		case "/reset":
			conversation = session{}
		case "":
		default:
			conversation.answer(prompt, *trace)
		}
	}
}
