#!/usr/bin/env python3
"""Send one command to the proxy's loopback-only admin endpoint."""
import argparse
import socket


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", nargs="+", help="status, or an explicitly requested experiment")
    args = ap.parse_args()
    with socket.create_connection(("127.0.0.1", 9999), 5) as connection:
        connection.settimeout(15)
        connection.sendall(" ".join(args.command).encode() + b"\n")
        print(connection.recv(4096).decode().strip())


if __name__ == "__main__":
    main()
