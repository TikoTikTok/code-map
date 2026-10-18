package main

import (
	"fmt"
	"os"
)

// Server holds the listener state.
type Server struct {
	Addr string
}

// Handler is anything that can serve.
type Handler interface {
	Serve() error
}

const Version = "1.0"

var debug = false

// Start runs the server.
func (s *Server) Start() error {
	fmt.Println(s.Addr, helper(1))
	return nil
}

func helper(n int) int {
	return n + 1
}

func main() {
	s := &Server{Addr: os.Args[0]}
	_ = s.Start()
}
